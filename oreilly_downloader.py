# /// script
# dependencies = [
#   "aiohttp",
#   "lxml",
#   "yarl",
# ]
# ///
"""Download a book you have access to on O'Reilly learning (learning.oreilly.com)
as a standalone .epub file.

QUICK START
    1. Log into learning.oreilly.com in your browser.
    2. Export your cookies for the site to a JSON file. Any browser
       extension that does this works (e.g. "Cookie-Editor" or
       "EditThisCookie" for Chrome/Firefox) - export "all cookies for this
       site" to cookies.json. You do not need to hand-pick which cookies;
       anything not scoped to oreilly.com is ignored automatically.
    3. Find the book's ID: it's the digits in the book's learning.oreilly.com
       URL, e.g. for https://learning.oreilly.com/library/view/some-book/9781633437777/
       the id is 9781633437777.
    4. Run:
           python3 oreilly_downloader.py 9781633437777 --cookies cookies.json
       This writes 9781633437777.epub in the current directory.

    Re-run the exact same command whenever you need another book (with a
    different id) - --cookies keeps itself up to date (see below), so you
    normally never need to re-export from the browser more than once in a
    while.

WHY --cookies INSTEAD OF JUST A JWT
    O'Reilly authenticates API requests with a short-lived JWT (the
    "orm-jwt" cookie), which on its own typically expires within a day.
    Passing the full cookie set - not just that one cookie - matters for
    two reasons:
      - Some of those other cookies (e.g. "orm-rt", a refresh token) may be
        what lets O'Reilly's own backend transparently hand back a fresh
        JWT on an ordinary request once the old one has expired, the same
        way staying logged in in a browser tab works.
      - Whether or not that happens depends on O'Reilly's server-side
        behaviour, not on this script - it isn't something this script can
        force. When it works, requests just keep succeeding with the jwt
        quietly refreshed underneath; when it doesn't, requests fail with
        403s and get reported the same way any other download failure is.
    After the run, the current cookies (including anything the API rotated
    in along the way) are written back to the same file you passed via
    --cookies, so the next run can just reuse it.

    A single --jwt value still works for a quick one-off run, but it will
    only stay valid for as long as that token's own expiry allows, with no
    chance to refresh - fine for one small book, not for a big one that
    might outlive the token, and not something worth reusing across runs.

IF AUTHENTICATION FAILS
    "No cookies/JWT provided" means neither --cookies nor --jwt was given -
    the download will still be attempted, but will only get content
    O'Reilly serves without a login (usually nothing, for a real book).

    "Authentication check failed" after providing --cookies or --jwt does
    NOT necessarily mean the login itself expired. This site sits behind
    Akamai Bot Manager (recognisable by cookies named bm_s/bm_sz/bm_so/
    bm_lso/_abck), which can reject a request that merely looks automated
    - wrong headers, an unconvincing browser fingerprint - independently
    of whether the actual session/JWT is completely valid. The script
    decodes orm-jwt's own expiry claim locally (no network needed) and
    prints it alongside any failure specifically so you can tell "this
    token is genuinely dead" apart from "something is blocking this
    request regardless of the token" before concluding you need to
    re-export anything.

    --webview is the fix for the second case, and for getting a session in
    the first place: it opens a real, native browser window (via the
    separately-installed 'pywebview' package) to learning.oreilly.com, so
    you can log in exactly as you would normally, and Akamai's own JS
    sensor - which only runs in a genuine browser page, never in a plain
    HTTP client - gets to do its thing. Whatever cookies that produces,
    including Akamai's, are picked up automatically. Needs an actual
    display (X11/Wayland/macOS/Windows desktop); it will not work over a
    plain SSH terminal or inside a headless container. Combine with
    --cookies so the result is saved for next time rather than used once:
        python3 oreilly_downloader.py 9781633437777 --cookies cookies.json --webview
    --webview-profile controls where pywebview keeps its own persistent
    browser profile between runs (default: a folder next to --cookies), so
    in practice logging in is a one-time thing, not a per-run one.

    A handful of "FAILED to download ..." lines at the end, listing a few
    files, is usually transient rate-limiting - just run the exact same
    command again. Raw file bytes are cached under .oreilly_cache/<book_id>/
    so a re-run only re-fetches what is still missing (or pass --force to
    ignore the cache and re-download everything). The .epub itself is
    always rebuilt from the cache so path rewriting stays consistent.

OUTPUT
    A single <book_id>.epub file in the current directory, containing
    every file the O'Reilly API lists for that book, converted to valid
    XHTML content documents with all internal links, stylesheets, and
    images rewritten to work as a normal, self-contained EPUB. When the
    source only ships an EPUB2 NCX, an EPUB3 nav.xhtml is synthesised so
    modern readers get a proper table of contents too. Calibre production
    metadata (if present in the upstream package) is stripped.
"""

import argparse
import asyncio
import base64
import json
import logging
import os
import posixpath
import random
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import unquote

# ---------------------------------------------------------------------------
# Bootstrap: Python version + third-party deps (friendly errors, no traceback)
# ---------------------------------------------------------------------------
# Absolute floor — below this the language/stdlib is too old to bother.
ABSOLUTE_MIN_PYTHON = (3, 7)
# Recommended minimum this project targets. Below this: warning + confirm.
RECOMMENDED_MIN_PYTHON = (3, 9)
# Highest Python minor we regularly exercise. Newer: warning + confirm.
MAX_TESTED_PYTHON = (3, 13)

SUPPORTED_OS = ('Linux', 'Windows')
# Official creator — please open a GitHub Issue here when reporting bugs:
REPORT_URL = 'https://github.com/official-kandoamoa'
CALIBRE_URL = 'https://calibre-ebook.com/'
SCRIPT_VERSION = '1.2.0'


def _die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def report_hint():
    """Short footer telling users how to report bugs."""
    return (
        f'  If you believe this is a bug, please open an Issue on GitHub:\n'
        f'    {REPORT_URL}'
    )


def _redact_secrets(text):
    """Mask JWT-like and long token strings before writing logs."""
    if not text:
        return text
    # JWT: header.payload.sig
    text = re.sub(
        r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+',
        '[REDACTED_JWT]',
        text,
    )
    # Long opaque tokens (32+ url-safe chars)
    text = re.sub(r'\b[A-Za-z0-9_-]{40,}\b', '[REDACTED_TOKEN]', text)
    return text


def _safe_args_for_log(args):
    """argparse namespace → dict with secrets removed."""
    d = dict(vars(args))
    if d.get('jwt'):
        d['jwt'] = '[REDACTED]'
    return d


def _assert_url_allowed(url):
    """Reject requests to hosts outside CONFIG allowed_hosts."""
    try:
        host = (yarl.URL(url).host or '').lower()
    except Exception:
        raise RuntimeError(f'refusing request: unparseable URL {url!r}') from None
    allowed = tuple(CONFIG.get('allowed_hosts') or ('oreilly.com',))
    if not host or not any(host == h or host.endswith('.' + h) for h in allowed):
        raise RuntimeError(
            f'refusing request to unexpected host {host!r} (url={url!r}). '
            f'Allowed suffixes: {allowed}'
        )


def _chmod_private(path, mode=None):
    mode = mode if mode is not None else CONFIG.get('cookies_file_mode', 0o600)
    try:
        if mode and os.name != 'nt' and os.path.isfile(path):
            os.chmod(path, mode)
    except OSError:
        pass


def ask_continue(prompt, *, assume_yes=False):
    """Ask the user to confirm. Returns True to continue, False to abort.

    When assume_yes is True (e.g. --yes / non-interactive), skips the prompt
    and continues. When stdin is not a TTY, refuses to continue unless
    assume_yes is set.
    """
    if assume_yes:
        print(f'{prompt} [auto-yes]')
        return True
    if not sys.stdin.isatty():
        print(
            f'{prompt}\n'
            f'  error: non-interactive session — pass --yes to continue anyway, '
            f'or run in a terminal.',
            file=sys.stderr,
        )
        return False
    try:
        answer = input(f'{prompt} [y/N] ').strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ('y', 'yes')


def check_python_version(*, assume_yes=False):
    have = '.'.join(str(x) for x in sys.version_info[:3])
    abs_need = '.'.join(str(x) for x in ABSOLUTE_MIN_PYTHON)
    rec_need = '.'.join(str(x) for x in RECOMMENDED_MIN_PYTHON)

    if sys.version_info < ABSOLUTE_MIN_PYTHON:
        _die(
            f'error: Python {abs_need}+ is required (you have {have}).\n'
            f'  This version is too old to run the script at all.\n'
            f'  Install a newer Python, or:  uv run --python {rec_need} '
            f'oreilly_downloader.py ...\n'
            + report_hint()
        )

    if sys.version_info < RECOMMENDED_MIN_PYTHON:
        print(
            f'warning: Python {have} is below the recommended minimum '
            f'({rec_need}+).\n'
            f'  The script may fail or behave incorrectly. '
            f'Upgrading is strongly advised.',
            file=sys.stderr,
        )
        if not ask_continue(
                'Continue with this older Python version anyway?',
                assume_yes=assume_yes):
            _die(
                f'Aborted. Install Python {rec_need}+ (recommended) or pass --yes.\n'
                + report_hint()
            )

    # Too new / untested
    if sys.version_info[:2] > MAX_TESTED_PYTHON[:2]:
        tested = '.'.join(str(x) for x in MAX_TESTED_PYTHON)
        print(
            f'warning: Python {have} is newer than the highest version this '
            f'script was tested with ({tested}.x).\n'
            f'  It may still work, but unexpected breakage is possible.',
            file=sys.stderr,
        )
        if not ask_continue('Continue with this untested Python version?',
                            assume_yes=assume_yes):
            _die(
                'Aborted. Install a tested Python version or pass --yes.\n'
                + report_hint()
            )


def check_operating_system(*, assume_yes=False):
    import platform
    system = platform.system() or 'Unknown'
    if system in SUPPORTED_OS:
        return system
    # macOS and others
    print(
        f'warning: operating system {system!r} is not in the supported set '
        f'{SUPPORTED_OS}.\n'
        f'  This script is optimized for Linux and Windows. Other systems '
        f'are untested and may fail (paths, permissions, Calibre, webview).',
        file=sys.stderr,
    )
    if not ask_continue(
            f'Continue anyway on {system}?',
            assume_yes=assume_yes):
        _die(
            'Aborted. Use Linux or Windows, or pass --yes to override.\n'
            + report_hint()
        )
    return system


def check_dependencies():
    missing = []
    for name in ('aiohttp', 'yarl', 'lxml'):
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    if not missing:
        return
    pkgs = ' '.join(missing)
    _die(
        'error: missing required Python package(s): '
        + ', '.join(missing)
        + '\n\n'
        '  Install with one of:\n'
        f'    pip install {pkgs}\n'
        f'    pip install {pkgs} --break-system-packages   # if pip complains\n'
        f'    uv run oreilly_downloader.py ...             # auto-installs from\n'
        '                                                 # the script metadata\n'
    )


# Absolute floor only at import time; recommended/too-new prompts run after
# argparse so --yes can skip them.
if sys.version_info < ABSOLUTE_MIN_PYTHON:
    check_python_version(assume_yes=False)
check_dependencies()

import aiohttp
import yarl
from http.cookies import SimpleCookie
from lxml import etree
from lxml import html as lhtml

log = logging.getLogger('oreilly_downloader')

# =============================================================================
# USER CONFIG — edit these defaults freely (CLI flags still override them)
# =============================================================================
# Paths are relative to the current working directory unless absolute.
CONFIG = {
    # Default cookies file when --cookies is omitted (None = require CLI)
    'cookies_path': 'cookies.json',

    # Default cache root (per-book subdirs are created under this)
    'cache_dir': '.oreilly_cache',

    # Parallel file downloads
    'concurrency': 8,

    # Default books list for batch-style runs (used by batch_download.sh;
    # also accepted by this script via --books FILE)
    'books_file': 'books.txt',

    # Directory for crash / unknown-error logs
    'error_log_dir': 'error_logs',

    # When True, synthesise EPUB3 nav.xhtml from NCX if missing
    'make_nav': True,

    # When True, run Calibre EPUB→EPUB polish after each successful build
    'calibre_polish': False,

    # When True, package API bytes with no rewriting (archival)
    'raw': False,
    # Inject Kindle overflow CSS (table/pre) when True
    'kindle_fix': False,

    # Append this suffix to polished/raw outputs only when non-empty
    # (leave '' for default naming: <id>.epub / <id>-raw.epub)
    'output_suffix': '',

    # Extra HTTP headers (merged over script defaults; rarely needed)
    'extra_headers': {
        # 'User-Agent': 'Mozilla/5.0 ...',
    },

    # --- security / robustness ---
    # Total / connect / sock-read timeouts for HTTP (seconds)
    'http_timeout_total': 120,
    'http_timeout_connect': 30,
    'http_timeout_sock_read': 90,
    # Refuse a single response larger than this (bytes); 0 = no limit
    'max_response_bytes': 80 * 1024 * 1024,  # 80 MiB per file
    # Only allow downloads whose URL host ends with one of these
    'allowed_hosts': ('oreilly.com', 'learning.oreilly.com'),
    # chmod cookies file to owner-only when saving (Unix; no-op on some OS)
    'cookies_file_mode': 0o600,
    # Expected O'Reilly Learning API major version (path /api/vN/...)
    'api_version': 2,
    # Set False to skip the online/API probe (not recommended)
    'check_connectivity': True,

    # Persist CLI defaults here (cookies path, concurrency, etc.)
    # Created/updated with --save-options
    'options_file': '.oreilly_options.json',
    # If True, write options_file after every successful run
    'auto_save_options': False,
}
# =============================================================================

BASE_URL = 'https://learning.oreilly.com'

# Extensions the API serves as (X)HTML content documents. Everything else
# (css, images, fonts, ncx, opf, etc.) is written through unmodified, except
# for .css which gets its own url()/@import rewrite below.
# .htm is common in older Manning / O'Reilly titles (this book's chapters
# are all *.htm); without it those files are written through untouched and
# every image/CSS reference stays as an absolute /api/v2/... path.
HTML_EXTENSIONS = ('.html', '.xhtml', '.htm')

# Statuses worth retrying: O'Reilly appears to answer with a plain 403
# (rather than 429) when it wants a client to back off, alongside the usual
# transient server errors.
RETRYABLE_STATUSES = {403, 429, 500, 502, 503, 504}

DEFAULT_CONCURRENCY = int(CONFIG.get('concurrency', 8))

# DNS / connection drops are common on Termux and mobile networks.
_NETWORK_ERRORS = (
    aiohttp.ClientConnectorError,
    aiohttp.ClientOSError,
    aiohttp.ServerTimeoutError,
    asyncio.TimeoutError,
    ConnectionError,
    OSError,
)

_XML_DECL_RE = re.compile(r'^\s*<\?xml[^>]*\?>\s*', re.IGNORECASE)

# Matches a <style>...</style> or <script>...</script> element, capturing
# its opening tag, text content, and closing tag separately.
_RAW_TEXT_ELEMENT_RE = re.compile(
    rb'(<(?:style|script)\b[^>]*>)(.*?)(</(?:style|script)\s*>)',
    re.IGNORECASE | re.DOTALL,
)


def _unescape_raw_text_elements(xhtml_bytes):
    """Undo XML text-node escaping of '>' specifically inside <style> and
    <script> element content.

    etree.tostring() (strict XML mode, required for EPUB validity) escapes
    every '<', '>' and '&' in ordinary text nodes - correct and necessary
    for the document to be well-formed XML. But HTML5 - and every
    real-world HTML5-based consumer that actually renders these files,
    WeasyPrint included - parses <style> and <script> as "raw text"
    elements and does NOT decode entities inside them. The escaped text
    (literally "&gt;") is what a CSS/JS parser sees, not the character it
    represents. A CSS child-combinator selector like ".content > p" -
    extremely common, including in O'Reilly's own class-based stylesheets -
    becomes ".content &gt; p" and silently never matches anything, with no
    error anywhere.

    Only '>' is reversed, deliberately not '<' or '&'. Per the XML spec, a
    lone '>' in text is always well-formed on its own; only '<' and a bare
    '&' are hard well-formedness violations (they always start a markup
    token or entity reference). Restoring '>' therefore fixes the common
    real case - combinators - without ever producing invalid XML. A '<' or
    '&' that originated as a literal character in the source CSS (e.g. a
    `content: "<tag>"` or `content: "AT&T"` value, both rare in practice)
    stays escaped: that one value's raw-text rendering is imperfect, but
    the document stays valid XML rather than risking rejection outright by
    a strict XML-based reader.
    """
    def unescape(m):
        open_tag, content, close_tag = m.groups()
        content = content.replace(b'&gt;', b'>')
        # The one other text-content restriction in XML: a literal "]]>"
        # is reserved for ending CDATA sections and is never allowed as
        # plain text, even outside CDATA. Unescaping "&gt;" could in
        # principle produce it if it happens to follow "]]" - vanishingly
        # unlikely in real CSS/JS, but cheap to guard against exactly by
        # re-escaping only that specific sequence back.
        content = content.replace(b']]>', b']]&gt;')
        return open_tag + content + close_tag

    return _RAW_TEXT_ELEMENT_RE.sub(unescape, xhtml_bytes)


async def get_with_retry(session, url, *, max_attempts=30, base_delay=3.0,
                          max_delay=60.0, consecutive_403_limit=10):
    """GET url and return its raw bytes, retrying with exponential backoff
    on rate-limit/anti-bot/transient server responses and network blips.

    A single flaky or rate-limited request shouldn't take down an entire
    multi-hundred-file book download - previously any non-2xx response
    raised straight through asyncio.gather and aborted the whole run,
    discarding every file that hadn't been written to the zip yet.

    Consecutive 403s abort early so an expired session does not hang for
    many minutes under max_attempts.

    Empty 200 bodies are treated as transient failures (not success): some
    edge/CDN glitches return HTTP 200 with zero bytes, and caching that
    would poison every later re-run until --force.
    """
    consecutive_403 = 0
    for attempt in range(1, max_attempts + 1):
        try:
            _assert_url_allowed(url)
            async with session.get(url) as r:
                r.raise_for_status()
                consecutive_403 = 0
                # Stream with size cap when configured
                max_bytes = int(CONFIG.get('max_response_bytes') or 0)
                if max_bytes > 0:
                    chunks = []
                    total = 0
                    async for chunk in r.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            raise RuntimeError(
                                f'Response for {url.split("/files/")[-1]} '
                                f'exceeds max_response_bytes ({max_bytes}); '
                                f'aborting to protect disk.'
                            )
                        chunks.append(chunk)
                    data = b''.join(chunks)
                else:
                    data = await r.read()
                if not data:
                    short = url.split('/files/')[-1] if '/files/' in url else url
                    if attempt == max_attempts:
                        raise RuntimeError(
                            f'Empty 200 body for {short} after {max_attempts} '
                            f'attempts — not caching.'
                        )
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay) \
                        + random.uniform(0, 0.5)
                    print(f'  got empty 200 body fetching {short}, retrying in '
                          f'{delay:.1f}s (attempt {attempt}/{max_attempts})')
                    await asyncio.sleep(delay)
                    continue
                return data
        except aiohttp.ClientResponseError as exc:
            if exc.status == 403:
                consecutive_403 += 1
                if consecutive_403 >= consecutive_403_limit:
                    raise RuntimeError(
                        f'Persistent 403 after {consecutive_403} attempts for '
                        f'{url}. Session may be expired – re-export cookies, '
                        f'or wait a minute if this is rate-limiting.'
                    ) from exc
            else:
                consecutive_403 = 0
            if exc.status not in RETRYABLE_STATUSES or attempt == max_attempts:
                raise
            # Capped, not just exponential: uncapped, attempt 20 alone is
            # an 18-day sleep and attempt 25 is over a year, so a
            # persistently failing file (an expired JWT causing every
            # request to 403, say) would never practically reach
            # max_attempts - it would just hang that task, and therefore
            # the whole asyncio.gather, indefinitely instead of failing
            # within a reasonable time and surfacing the "expired JWT?"
            # hint below.
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay) \
                + random.uniform(0, 0.5)
            short = url.split('/files/')[-1] if '/files/' in url else url
            print(f'  got {exc.status} fetching {short}, retrying in '
                  f'{delay:.1f}s (attempt {attempt}/{max_attempts})')
            await asyncio.sleep(delay)
        except _NETWORK_ERRORS as exc:
            if attempt == max_attempts:
                raise
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay) \
                + random.uniform(0, 0.5)
            print(f'  network error ({type(exc).__name__}), '
                  f'retrying in {delay:.1f}s '
                  f'(attempt {attempt}/{max_attempts})')
            await asyncio.sleep(delay)


def _container_xml(opf_zip_path='EPUB/content.opf'):
    """Build META-INF/container.xml pointing at the package document.

    opf_zip_path is the path *inside the zip* (e.g. EPUB/content.opf or
    EPUB/OEBPS/package.opf). Used by normal mode (always content.opf) and
    --raw mode (whatever full_path the API listed for the .opf).
    """
    # Keep it minimal and deterministic — no pretty-print variance.
    return (
        b'<?xml version="1.0"?>\n'
        b'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
        b'version="1.0">\n'
        b'    <rootfiles>\n'
        b'        <rootfile full-path="'
        + opf_zip_path.encode('utf-8')
        + b'" media-type="application/oebps-package+xml"/>\n'
        b'    </rootfiles>\n'
        b'</container>\n'
    )


# Default container for the processed (non-raw) build.
CONTAINER = _container_xml('EPUB/content.opf')

_CSS_URL_RE = re.compile(r'''url\(\s*(?P<q>['"]?)(?P<url>[^'")]+)(?P=q)\s*\)''')
_CSS_IMPORT_RE = re.compile(r'''@import\s+(?P<q>['"])(?P<url>[^'"]+)(?P=q)''')


def _relpath(target, current_dir):
    """target and current_dir are both paths relative to the EPUB root;
    return target expressed relative to current_dir instead."""
    return posixpath.relpath(target, current_dir) if current_dir else target


def _resolve_ref(value, root_path, current_dir):
    """Turn an API root_path-prefixed reference into a path relative to
    current_dir (the folder of the file that's doing the referencing).

    The O'Reilly API hands out references to other EPUB files as an absolute
    path under root_path (e.g. '/api/.../files/styles/base.css'). That
    corresponds 1:1 to the file's full_path relative to the EPUB root (e.g.
    'styles/base.css'). Simply stripping the prefix - which the original code
    did - leaves that EPUB-root-relative path in place, but an (X)HTML/CSS
    reference is resolved relative to the *referencing* file's own folder,
    not the EPUB root. So a chapter at text/ch01.html linking to
    'styles/base.css' would actually resolve to the non-existent
    text/styles/base.css, silently failing to load - taking every bit of
    CSS-driven styling (colors, syntax highlighting, callouts, ...) with it.

    Also normalises percent-encoding and backslashes (real-world OPF/NCX/HTML
    sometimes carries either), matching how a careful EPUB reader resolves
    hrefs. Values that don't start with root_path (external URLs, fragment-
    only anchors, data: URIs, etc.) are left untouched aside from that
    light normalisation when they *do* start with root_path.
    """
    # Split off any #fragment so we never try to resolve it as a path.
    path_part, sep, frag = value.partition('#')
    path_part = unquote(path_part).replace('\\', '/')
    if not path_part.startswith(root_path):
        # Preserve original fragment attachment for non-API refs.
        return value
    resolved = _relpath(path_part.removeprefix(root_path), current_dir)
    return f'{resolved}#{frag}' if sep else resolved


def _rewrite_srcset(value, root_path, current_dir):
    candidates = []
    for candidate in value.split(','):
        candidate = candidate.strip()
        if not candidate:
            continue
        url, _, descriptor = candidate.partition(' ')
        url = _resolve_ref(url, root_path, current_dir)
        candidates.append(f'{url} {descriptor}'.strip())
    return ', '.join(candidates)


def rewrite_css_text(text, root_path, current_dir):
    """Rewrite url(...) and @import references inside CSS source text.

    Only .html/.xhtml files went through any rewriting before; .css files
    (and inline <style> blocks) were written out completely untouched. Any
    @font-face src, background-image, or @import that the API expressed as a
    root_path-prefixed reference was therefore left pointing at an absolute
    API path that doesn't exist inside the EPUB, breaking that stylesheet
    (or silently dropping the font/background it referenced).
    """
    text = _CSS_URL_RE.sub(
        lambda m: 'url({q}{url}{q})'.format(
            q=m.group('q'),
            url=_resolve_ref(m.group('url'), root_path, current_dir),
        ),
        text,
    )
    text = _CSS_IMPORT_RE.sub(
        lambda m: '@import {q}{url}{q}'.format(
            q=m.group('q'),
            url=_resolve_ref(m.group('url'), root_path, current_dir),
        ),
        text,
    )
    return text


def _decode_css(raw):
    """Decode CSS bytes, honouring a declared @charset and falling back
    through common encodings instead of assuming UTF-8. A hardcoded
    '.decode(\'utf-8\')' raises UnicodeDecodeError on the first legacy
    stylesheet that isn't UTF-8, and since that happens outside the
    per-file error handling in download(), it would crash the entire
    multi-hundred-file run rather than just that one file.
    """
    m = re.match(rb'@charset\s+["\']([\w-]+)["\']', raw)
    declared = m.group(1).decode('ascii', errors='ignore') if m else None
    for enc in filter(None, [declared, 'utf-8']):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode('latin-1')  # last resort, never fails


def rewrite_css(content, root_path, full_path):
    current_dir = posixpath.dirname(full_path)
    text = _decode_css(content)
    return rewrite_css_text(text, root_path, current_dir).encode('utf-8')


# Extensions that must be application/xhtml+xml in a valid EPUB manifest,
# regardless of what the API (or an upstream Calibre pass) labelled them.
_XHTML_MEDIA_TYPE = 'application/xhtml+xml'
_XHTML_EXTS = ('.html', '.xhtml', '.htm')

# Namespaces used when synthesising an EPUB3 nav document.
_XHTML_NS = 'http://www.w3.org/1999/xhtml'
_EPUB_NS = 'http://www.idpf.org/2007/ops'
_NCX_NS = 'http://www.daisy.org/z3986/2005/ncx/'
_OPF_NS = 'http://www.idpf.org/2007/opf'
_DC_NS = 'http://purl.org/dc/elements/1.1/'

# Default on-disk cache for raw API payloads (resume support).
DEFAULT_CACHE_DIR = CONFIG.get('cache_dir', '.oreilly_cache')

# Fixed path of the synthesised EPUB3 nav document inside the EPUB.
NAV_PATH = 'nav.xhtml'
KINDLE_CSS_PATH = 'Styles/kindle-fix.css'
KINDLE_CSS = b"""/* Injected by oreilly_downloader --kindle
 * Adapted from safaribooks-style rules for narrow E-Ink / Kindle screens.
 */
* {
  word-wrap: break-word !important;
  word-break: break-word !important;
}
table, pre {
  overflow-x: unset !important;
  overflow-y: unset !important;
  overflow: unset !important;
  white-space: pre-wrap !important;
  max-width: 100% !important;
}
pre, pre code, code {
  white-space: pre-wrap !important;
  word-wrap: break-word !important;
  max-width: 100% !important;
}
img, svg {
  max-width: 100% !important;
  height: auto !important;
}
"""


def _local_tag(el):
    """Return the element tag without any {namespace} prefix."""
    tag = el.tag
    if isinstance(tag, str) and tag.startswith('{'):
        return tag.rsplit('}', 1)[-1]
    return tag


def _strip_calibre_metadata(tree):
    """Remove Calibre production fingerprints from the package document.

    Many Manning/O'Reilly packages were run through Calibre at some point
    and carry calibre:* meta, a calibre xmlns on <metadata>, and a 'bkp'
    contributor advertising the Calibre version. Harmless but noisy;
    strip them so the finished EPUB looks like a clean publisher package.
    """
    for el in list(tree.iter()):
        # Drop xmlns:calibre (and any other calibre-related) attribute on
        # every element — most commonly the <metadata> wrapper.
        for attr in list(el.attrib):
            val = el.attrib.get(attr) or ''
            if (attr.startswith('xmlns') and 'calibre' in val.lower()) or \
               (attr.startswith('{') and 'calibre' in attr.lower()) or \
               attr.lower().startswith('calibre') or \
               'calibre.kovidgoyal' in val.lower():
                del el.attrib[attr]

        tag = _local_tag(el)
        # calibre namespaced elements
        if isinstance(el.tag, str) and 'calibre' in el.tag:
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
            continue
        # <meta name="calibre:...">
        if tag == 'meta':
            name = (el.get('name') or '')
            prop = (el.get('property') or '')
            if name.startswith('calibre:') or prop.startswith('calibre:'):
                parent = el.getparent()
                if parent is not None:
                    parent.remove(el)
                continue
        # <dc:contributor>calibre (...)</dc:contributor>
        if tag == 'contributor':
            text = (el.text or '').strip().lower()
            if 'calibre' in text:
                parent = el.getparent()
                if parent is not None:
                    parent.remove(el)
                continue
        # <dc:identifier opf:scheme="calibre">...</dc:identifier>
        if tag == 'identifier':
            scheme = (el.get(f'{{{_OPF_NS}}}scheme') or el.get('scheme') or '').lower()
            if scheme == 'calibre':
                parent = el.getparent()
                if parent is not None:
                    parent.remove(el)


def rewrite_opf(content, root_path, full_path):
    """Rewrite href attributes inside the OPF package document so they
    become correct relative paths after the root_path prefix is removed.
    Also normalises the package so it remains valid EPUB after renaming
    the file itself to content.opf at the EPUB root.

    Always resolve hrefs against the EPUB root (current_dir=''), not
    against the OPF's original folder: fetch_book always writes the
    package document as EPUB/content.opf, so any relative path computed
    against e.g. OEBPS/ would be wrong once the file has been moved.
    """
    if isinstance(content, str):
        content = content.encode('utf-8')
    try:
        tree = etree.fromstring(content)
    except etree.XMLSyntaxError:
        # Fall back to leaving the OPF untouched rather than dropping it.
        return content
    # OPF always ends up at EPUB/content.opf — resolve against root.
    current_dir = ''
    for el in tree.iter():
        href = el.get('href')
        if href:
            new_href = _resolve_ref(href, root_path, current_dir)
            el.set('href', new_href)
            # Force correct media-type for content documents. Some API
            # manifests (and Calibre-touched upstream packages) still
            # advertise text/html for .htm/.html files; readers that
            # require application/xhtml+xml then refuse to render them.
            if _local_tag(el) == 'item' and new_href:
                path_only = new_href.split('#', 1)[0].lower()
                if path_only.endswith(_XHTML_EXTS):
                    el.set('media-type', _XHTML_MEDIA_TYPE)
    _strip_calibre_metadata(tree)
    out = etree.tostring(tree, xml_declaration=True, encoding='utf-8',
                         pretty_print=True)
    # lxml keeps original xmlns:calibre declarations in its internal nsmap
    # (read-only), so they survive attribute deletion above. Strip any
    # remaining calibre namespace decls from the serialised bytes.
    out = re.sub(
        br'\s+xmlns:calibre="[^"]*"',
        b'',
        out,
    )
    return out


def rewrite_ncx(content, root_path, full_path):
    """Rewrite src/href attributes inside an NCX navigation document.

    NCX is not HTML, so it never went through to_xhtml; without this,
    any API-prefixed content src= paths stay absolute and the TOC in
    EPUB2 readers points at non-existent URLs. Always resolve against
    the NCX file's own directory (usually the EPUB root). Also clears
    Calibre generator metadata when present.
    """
    if isinstance(content, str):
        content = content.encode('utf-8')
    try:
        tree = etree.fromstring(content)
    except etree.XMLSyntaxError:
        return content
    current_dir = posixpath.dirname(full_path)
    for el in tree.iter():
        for attr in ('src', 'href'):
            value = el.get(attr)
            if value:
                el.set(attr, _resolve_ref(value, root_path, current_dir))
        # <meta name="dtb:generator" content="calibre (...)"/>
        if _local_tag(el) == 'meta':
            name = (el.get('name') or '').lower()
            content_val = (el.get('content') or '').lower()
            if name == 'dtb:generator' and 'calibre' in content_val:
                parent = el.getparent()
                if parent is not None:
                    parent.remove(el)
    return etree.tostring(tree, xml_declaration=True, encoding='utf-8',
                          pretty_print=True)


def _ncx_navpoints_to_ol(parent, ncx_dir, nav_dir):
    """Recursively convert NCX navPoint children into an XHTML <ol> tree.

    Paths in the NCX are relative to the NCX file; the nav document may
    live in a different folder, so each href is re-based onto nav_dir.
    """
    ol = None
    for np in parent:
        if _local_tag(np) != 'navPoint':
            continue
        label = ''
        href = ''
        for child in np:
            ctag = _local_tag(child)
            if ctag == 'navLabel':
                for t in child:
                    if _local_tag(t) == 'text' and t.text:
                        label = ' '.join(t.text.split())
                        break
            elif ctag == 'content':
                src = child.get('src') or ''
                if src:
                    # Resolve against NCX dir → EPUB-root path, then
                    # express relative to the nav document's directory.
                    root_path = posixpath.normpath(
                        posixpath.join(ncx_dir, unquote(src).replace('\\', '/'))
                        if ncx_dir else unquote(src).replace('\\', '/')
                    )
                    href = _relpath(root_path, nav_dir)
        child_ol = _ncx_navpoints_to_ol(np, ncx_dir, nav_dir)
        if not label and not child_ol:
            continue
        if ol is None:
            ol = etree.Element(f'{{{_XHTML_NS}}}ol')
        li = etree.SubElement(ol, f'{{{_XHTML_NS}}}li')
        if href:
            a = etree.SubElement(li, f'{{{_XHTML_NS}}}a', href=href)
            a.text = label or href
        else:
            span = etree.SubElement(li, f'{{{_XHTML_NS}}}span')
            span.text = label or 'Untitled'
        if child_ol is not None:
            li.append(child_ol)
    return ol


def generate_nav_from_ncx(ncx_content, ncx_path, nav_path=NAV_PATH):
    """Build a minimal EPUB3 nav.xhtml document from an NCX byte string.

    Returns None if the NCX cannot be parsed or has no usable navPoints.
    """
    if isinstance(ncx_content, str):
        ncx_content = ncx_content.encode('utf-8')
    try:
        ncx = etree.fromstring(ncx_content)
    except etree.XMLSyntaxError:
        return None

    ncx_dir = posixpath.dirname(ncx_path)
    nav_dir = posixpath.dirname(nav_path)

    # Prefer <navMap>; fall back to scanning for navPoint anywhere.
    nav_map = None
    for el in ncx.iter():
        if _local_tag(el) == 'navMap':
            nav_map = el
            break
    if nav_map is None:
        return None

    ol = _ncx_navpoints_to_ol(nav_map, ncx_dir, nav_dir)
    if ol is None:
        return None

    # Pull a title from NCX docTitle if present.
    title_text = 'Table of Contents'
    for el in ncx.iter():
        if _local_tag(el) == 'docTitle':
            for t in el:
                if _local_tag(t) == 'text' and t.text and t.text.strip():
                    title_text = ' '.join(t.text.split())
                    break
            break

    html_el = etree.Element(f'{{{_XHTML_NS}}}html', nsmap={
        None: _XHTML_NS,
        'epub': _EPUB_NS,
    })
    head = etree.SubElement(html_el, f'{{{_XHTML_NS}}}head')
    title_el = etree.SubElement(head, f'{{{_XHTML_NS}}}title')
    title_el.text = title_text
    body = etree.SubElement(html_el, f'{{{_XHTML_NS}}}body')
    nav = etree.SubElement(body, f'{{{_XHTML_NS}}}nav')
    nav.set(f'{{{_EPUB_NS}}}type', 'toc')
    nav.set('id', 'toc')
    h1 = etree.SubElement(nav, f'{{{_XHTML_NS}}}h1')
    h1.text = 'Table of Contents'
    nav.append(ol)

    return etree.tostring(
        html_el,
        xml_declaration=True,
        encoding='utf-8',
        pretty_print=True,
        doctype='<!DOCTYPE html>',
    )


def _opf_has_nav(tree):
    """True if the package already declares an EPUB3 nav document."""
    for el in tree.iter():
        if _local_tag(el) != 'item':
            continue
        props = (el.get('properties') or '').split()
        if 'nav' in props:
            return True
        href = (el.get('href') or '').lower()
        if href.endswith('nav.xhtml') or href.endswith('nav.html'):
            return True
    return False


def _ensure_dcterms_modified(tree):
    """EPUB 3 requires <meta property="dcterms:modified">YYYY-MM-DDThh:mm:ssZ</meta>.

    Insert or refresh it under <metadata>. Harmless on EPUB 2 packages and
    required once we bump version to 3.0 for the synthesised nav.
    """
    metadata = None
    for el in tree.iter():
        if _local_tag(el) == 'metadata':
            metadata = el
            break
    if metadata is None:
        return

    modified = None
    for el in metadata:
        if _local_tag(el) == 'meta' and (el.get('property') or '') == 'dcterms:modified':
            modified = el
            break

    # UTC timestamp without fractional seconds, with Z suffix (EPUB 3 form).
    stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    if modified is not None:
        modified.text = stamp
        return

    # Prefer namespaced children when the metadata block already uses them.
    use_ns = any(isinstance(c.tag, str) and c.tag.startswith('{')
                 for c in metadata)
    tag = f'{{{_OPF_NS}}}meta' if use_ns else 'meta'
    meta = etree.SubElement(metadata, tag)
    meta.set('property', 'dcterms:modified')
    meta.text = stamp



def apply_kindle_fixes(members):
    """Inject CSS that constrains overflow on table/pre (and wide media).

    Adds Styles/kindle-fix.css, registers it in content.opf, and links it
    from every HTML/XHTML content document so Kindle / narrow E-Ink readers
    wrap code blocks and tables instead of clipping or forcing huge widths.
    Mutates members in place. Returns True if applied.
    """
    if 'content.opf' not in members:
        return False

    css_path = KINDLE_CSS_PATH
    members[css_path] = KINDLE_CSS

    # --- OPF manifest item ---
    opf = members['content.opf']
    if isinstance(opf, str):
        opf = opf.encode('utf-8')
    try:
        tree = etree.fromstring(opf)
    except etree.XMLSyntaxError:
        return False

    manifest = None
    for el in tree.iter():
        if _local_tag(el) == 'manifest':
            manifest = el
            break
    if manifest is None:
        return False

    # Skip if already registered
    already = False
    for el in manifest:
        if _local_tag(el) == 'item' and el.get('href') == css_path:
            already = True
            break
    if not already:
        existing_ids = {
            el.get('id') for el in manifest
            if _local_tag(el) == 'item' and el.get('id')
        }
        kid = 'kindle-fix'
        n = 1
        while kid in existing_ids:
            n += 1
            kid = f'kindle-fix-{n}'
        namespaced = any(
            isinstance(c.tag, str) and c.tag.startswith('{')
            for c in manifest
        )
        tag = f'{{{_OPF_NS}}}item' if namespaced else 'item'
        item = etree.SubElement(manifest, tag)
        item.set('id', kid)
        item.set('href', css_path)
        item.set('media-type', 'text/css')

    members['content.opf'] = etree.tostring(
        tree, xml_declaration=True, encoding='utf-8', pretty_print=True
    )

    # --- link from each HTML document ---
    for path, content in list(members.items()):
        if not path.endswith(HTML_EXTENSIONS):
            continue
        if isinstance(content, str):
            content = content.encode('utf-8')
        try:
            # Prefer XML parse for XHTML; fall back to HTML
            try:
                doc = etree.fromstring(content)
            except etree.XMLSyntaxError:
                doc = lhtml.fromstring(content)
        except Exception:
            continue

        # Find head
        head = None
        for el in doc.iter():
            if _local_tag(el) == 'head':
                head = el
                break
        if head is None:
            # create head under html root
            root = doc
            if _local_tag(root) != 'html':
                continue
            head = etree.Element('head')
            root.insert(0, head)

        # Relative href from this file to css_path
        href = _relpath(css_path, posixpath.dirname(path) or '.')
        # Avoid duplicate links
        dup = False
        for link in head.iter():
            if _local_tag(link) == 'link' and link.get('href') == href:
                dup = True
                break
        if not dup:
            link = etree.SubElement(head, 'link')
            link.set('rel', 'stylesheet')
            link.set('type', 'text/css')
            link.set('href', href)

        try:
            out = etree.tostring(
                doc,
                xml_declaration=True,
                doctype='<!DOCTYPE html>',
                pretty_print=True,
                encoding='utf-8',
            )
        except Exception:
            out = etree.tostring(doc, encoding='utf-8')
        members[path] = out

    return True


def finalize_opf(opf_content, *, nav_path=None, bump_to_epub3=False):

    """Post-process the rewritten OPF: optionally register a synthesised
    nav document, ensure dcterms:modified, and bump package version to 3.0
    when dual NCX+nav is in use.
    """
    if isinstance(opf_content, str):
        opf_content = opf_content.encode('utf-8')
    try:
        tree = etree.fromstring(opf_content)
    except etree.XMLSyntaxError:
        return opf_content

    if nav_path and not _opf_has_nav(tree):
        # Find the <manifest> element.
        manifest = None
        for el in tree.iter():
            if _local_tag(el) == 'manifest':
                manifest = el
                break
        if manifest is not None:
            # Avoid id collisions.
            existing_ids = {
                el.get('id') for el in manifest
                if _local_tag(el) == 'item' and el.get('id')
            }
            nav_id = 'nav'
            n = 1
            while nav_id in existing_ids:
                n += 1
                nav_id = f'nav{n}'
            item = etree.SubElement(manifest, f'{{{_OPF_NS}}}item')
            # If the package isn't namespaced on children, use a bare tag.
            if not any(isinstance(c.tag, str) and c.tag.startswith('{')
                       for c in manifest):
                manifest.remove(item)
                item = etree.SubElement(manifest, 'item')
            item.set('id', nav_id)
            item.set('href', nav_path)
            item.set('media-type', _XHTML_MEDIA_TYPE)
            item.set('properties', 'nav')

    if bump_to_epub3:
        ver = tree.get('version') or ''
        if not ver.startswith('3'):
            tree.set('version', '3.0')

    # Always keep dcterms:modified current (required for EPUB 3 packages).
    _ensure_dcterms_modified(tree)

    return etree.tostring(tree, xml_declaration=True, encoding='utf-8',
                          pretty_print=True)


# lxml's HTML parser lowercases every attribute name, including inside
# embedded SVG. SVG attribute names are case-sensitive, so "viewBox"
# surviving as "viewbox" is silently ignored by real SVG renderers. Cover
# pages are the case this bites hardest: they're commonly a single
# <svg viewBox="..."><image .../></svg> so the image scales to the page,
# and losing viewBox/preserveAspectRatio can leave that cover scaled wrong,
# cropped, or blank in any reader that renders the EPUB directly (rather
# than through a tool that repairs this on the way to some other format).
_SVG_ATTR_CASE_MAP = {
    'viewbox': 'viewBox',
    'preserveaspectratio': 'preserveAspectRatio',
    'gradienttransform': 'gradientTransform',
    'gradientunits': 'gradientUnits',
    'patterntransform': 'patternTransform',
    'patternunits': 'patternUnits',
    'patterncontentunits': 'patternContentUnits',
    'spreadmethod': 'spreadMethod',
    'clippathunits': 'clipPathUnits',
    'markerwidth': 'markerWidth',
    'markerheight': 'markerHeight',
    'markerunits': 'markerUnits',
    'refx': 'refX',
    'refy': 'refY',
    'textlength': 'textLength',
    'lengthadjust': 'lengthAdjust',
    'baseprofile': 'baseProfile',
}


def _fix_svg_attribute_case(svg_root):
    for el in svg_root.iter():
        attrib = el.attrib
        for key in list(attrib.keys()):
            fixed = _SVG_ATTR_CASE_MAP.get(key)
            if fixed and fixed not in attrib:
                attrib[fixed] = attrib.pop(key)



def _sniff_html_encoding(raw):
    """Decode (X)HTML bytes honouring any declared encoding, falling back
    through common encodings. Avoids silent U+FFFD replacement or crashes
    on non-UTF-8 chapters.
    """
    if isinstance(raw, str):
        return raw
    for bom, enc in ((b'\xef\xbb\xbf', 'utf-8-sig'),
                     (b'\xff\xfe', 'utf-16-le'),
                     (b'\xfe\xff', 'utf-16-be')):
        if raw.startswith(bom):
            return raw.decode(enc, errors='replace')
    head = raw[:1024].decode('ascii', errors='ignore')
    m = (re.search(r'encoding=["\']([\w-]+)["\']', head, re.IGNORECASE) or
         re.search(r'charset=["\']?([\w-]+)', head, re.IGNORECASE))
    declared = m.group(1) if m else None
    for enc in filter(None, [declared, 'utf-8']):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode('latin-1')


def _pre_rewrite_style_urls(html_text, root_path, current_dir):
    """Rewrite url()/@import inside <style> blocks on the raw HTML string
    *before* the HTML parser runs.

    The HTML parser treats the first literal '</style>' as the end of a
    style element, even when it appears inside a CSS string such as
    content: "</style>". That truncates the block and leaves any later
    url(...) references as ordinary body text that the tree walk never
    rewrites. A quote-aware scan finds the real closer, rewrites CSS
    URLs, and escapes embedded '</style>' sequences so the subsequent
    HTML parse cannot split the block.
    """
    result = []
    pos = 0
    lower = html_text.lower()
    while True:
        start = lower.find('<style', pos)
        if start < 0:
            result.append(html_text[pos:])
            break
        gt = html_text.find('>', start)
        if gt < 0:
            result.append(html_text[pos:])
            break
        result.append(html_text[pos:gt + 1])
        body_start = gt + 1
        i = body_start
        in_single = in_double = False
        found_close = False
        while i < len(html_text):
            c = html_text[i]
            if c == '"' and not in_single and (i == 0 or html_text[i - 1] != '\\'):
                in_double = not in_double
            elif c == "'" and not in_double and (i == 0 or html_text[i - 1] != '\\'):
                in_single = not in_single
            elif not in_single and not in_double and lower.startswith('</style', i):
                body = html_text[body_start:i]
                body = rewrite_css_text(body, root_path, current_dir)
                # Neutralise any remaining '</style>' so the HTML parser
                # keeps the whole block intact.
                body = re.sub(r'</style>', r'<\\/style>', body, flags=re.IGNORECASE)
                result.append(body)
                end = html_text.find('>', i)
                if end < 0:
                    result.append(html_text[i:])
                    return ''.join(result)
                result.append(html_text[i:end + 1])
                pos = end + 1
                found_close = True
                break
            i += 1
        if not found_close:
            result.append(html_text[body_start:])
            break
    return ''.join(result)



def strip_akamai_injected(tree):
    """Remove anti-bot markup Akamai sometimes injects into HTML responses.

    From the community download_v2 approach: drop <script>, absolute-path
    <link href="/..."> (site chrome, not book CSS), and #sec-overlay.
    Mutates the lxml tree in place.
    """
    to_remove = []
    for el in tree.iter():
        tag = _local_tag(el)
        if tag == 'script':
            to_remove.append(el)
            continue
        if tag == 'link':
            href = el.get('href') or ''
            # Absolute site paths only — keep relative book stylesheets
            if href.startswith('/') and not href.startswith('//'):
                # Keep if it looks like an API book asset path
                if '/api/v2/epubs/' not in href and '/files/' not in href:
                    to_remove.append(el)
            continue
        if tag == 'div' and (el.get('id') or '') == 'sec-overlay':
            to_remove.append(el)
    for el in to_remove:
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)


def strip_akamai_from_text(text):
    """Regex fallback for full HTML documents (e.g. nav) before parse."""
    if isinstance(text, bytes):
        try:
            text = text.decode('utf-8')
        except UnicodeDecodeError:
            text = text.decode('utf-8', errors='replace')
    text = re.sub(
        r'<script\b[^>]*>.*?</script>',
        '',
        text,
        flags=re.I | re.S,
    )
    text = re.sub(
        r'<link\b[^>]*href="/[^"]*"[^>]*/?>',
        '',
        text,
        flags=re.I | re.S,
    )
    text = re.sub(
        r'<div\b[^>]*\bid=["\']sec-overlay["\'][^>]*>.*?</div>',
        '',
        text,
        flags=re.I | re.S,
    )
    return text


async def fetch_book_info(session, book_id):
    """GET /api/v2/epubs/urn:orm:book:<id>/ metadata (title, language, …).

    Returns a dict (may be empty on failure). Used to print the title and
    to correct OPF metadata when the package still carries another edition.
    """
    url = f'{BASE_URL}/api/v2/epubs/urn:orm:book:{book_id}/'
    try:
        _assert_url_allowed(url)
        data = json.loads(await get_with_retry(session, url, max_attempts=5))
        if isinstance(data, dict):
            return data
    except Exception as exc:  # noqa: BLE001
        log.debug('book info fetch failed for %s: %s', book_id, exc)
        print(f'  note: could not fetch book metadata ({type(exc).__name__})')
    return {}


def patch_opf_from_book_info(opf_content, info):
    """Align OPF title/language with the edition metadata from the API.

    download_v2 notes that the OPF may keep another edition's title/language
    (e.g. English package on a translated listing). Prefer the API values.
    """
    if not info:
        return opf_content
    if isinstance(opf_content, str):
        opf_content = opf_content.encode('utf-8')
    try:
        tree = etree.fromstring(opf_content)
    except etree.XMLSyntaxError:
        return opf_content

    title = (info.get('title') or '').strip()
    language = (info.get('language') or info.get('lang') or '').strip()
    if not title and not language:
        return opf_content

    def _set_text_for_local(local_name, value):
        if not value:
            return
        for el in tree.iter():
            if _local_tag(el) == local_name:
                el.text = value
        # Also dcterms-style meta property=
        for el in tree.iter():
            if _local_tag(el) == 'meta':
                prop = (el.get('property') or '').lower()
                if prop in (f'dcterms:{local_name}', local_name):
                    el.text = value

    _set_text_for_local('title', title)
    _set_text_for_local('language', language)

    return etree.tostring(tree, xml_declaration=True, encoding='utf-8',
                          pretty_print=True)


def to_xhtml(s, root_path, full_path, css_paths=()):
    if isinstance(s, bytes):
        s = _sniff_html_encoding(s)
    # lxml rejects Unicode strings that still carry an XML encoding
    # declaration (e.g. <?xml version="1.0" encoding="UTF-8"?>). Nav
    # documents and many spine chapters include one; strip it after we
    # have already used the declaration to choose a decode encoding.
    s = _XML_DECL_RE.sub('', s)
    current_dir = posixpath.dirname(full_path)
    # Rewrite style-block URLs on the raw string so a CSS string that
    # contains the sequence '</style>' cannot truncate the block and
    # leave API paths unrewritten.
    s = _pre_rewrite_style_urls(s, root_path, current_dir)
    s = strip_akamai_from_text(s)
    tree = lhtml.fromstring(s, parser=lhtml.HTMLParser(encoding=None))
    strip_akamai_injected(tree)

    for svg_el in tree.iter('svg'):
        _fix_svg_attribute_case(svg_el)

    for el in list(tree.iter()):
        # "xlink:href" covers <image xlink:href="..."> inside an inline
        # <svg> - the common way an EPUB cover page scales a raster image
        # to the page. lxml's HTML parser isn't namespace-aware, so this
        # survives as the literal attribute name "xlink:href" rather than
        # being resolved to a real namespace; skipping it here left the
        # cover (and any other svg-wrapped image) pointing at the raw API
        # path forever, which is neither a valid relative path nor a
        # working URL once written into the .epub.
        for attr in ('href', 'src', 'xlink:href'):
            value = el.get(attr)
            if value:
                el.set(attr, _resolve_ref(value, root_path, current_dir))

        srcset = el.get('srcset')
        if srcset:
            el.set('srcset', _rewrite_srcset(srcset, root_path, current_dir))

        # Inline <style> blocks have the same url()/@import problem as
        # standalone .css files - rewrite those too.
        if el.tag == 'style' and el.text:
            el.text = rewrite_css_text(el.text, root_path, current_dir)

    already_namespaced = False

    if tree.tag != 'html':
        # Some API results (e.g. generated landing/cover pages) come back as
        # a bare content fragment with no <html>/<head> at all. Wrap it in a
        # minimal document instead of leaving it as an invalid, unstyled
        # fragment.
        wrapper = etree.Element('html', nsmap={
            None: 'http://www.w3.org/1999/xhtml',
            'epub': 'http://www.idpf.org/2007/ops',
        })
        head = etree.SubElement(wrapper, 'head')

        h1 = tree.find('.//h1')
        if h1 is not None:
            title = etree.SubElement(head, 'title')
            title.text = ''.join(h1.itertext()).strip()

        body = etree.SubElement(wrapper, 'body')
        body.append(tree)
        tree = wrapper
        already_namespaced = True
    else:
        head = tree.find('head')
        if head is None:
            head = etree.Element('head')
            tree.insert(0, head)

    # If this page ends up with no stylesheet reference at all - either
    # because it was a bare fragment above, or its own <head> just didn't
    # have one - link every stylesheet in the book so it still renders with
    # the book's normal styling instead of completely unstyled text. Pages
    # that already reference their own stylesheet(s) are left alone.
    has_stylesheet = any(
        el.get('rel') == 'stylesheet' for el in head.iter('link')
    )
    if not has_stylesheet:
        for css_path in css_paths:
            link = etree.SubElement(head, 'link')
            link.set('rel', 'stylesheet')
            link.set('type', 'text/css')
            link.set('href', _relpath(css_path, current_dir))

    out = etree.tostring(
        tree,
        xml_declaration=True,
        doctype='<!DOCTYPE html>',
        pretty_print=True,
        encoding='utf-8',
    )

    # lxml's HTML parser doesn't namespace the tree it builds, so a document
    # that already had its own <html> root (the "not wrapped" branch above)
    # would otherwise be serialized without the XHTML namespace declaration
    # EPUB requires - which some reading systems need in order to treat the
    # file as XHTML at all (and thus apply its CSS) rather than falling back
    # to unstyled plain text.
    if not already_namespaced:
        # lxml.html's parser isn't namespace-aware, so if the source page's
        # <html> tag already had an xmlns (or xmlns:epub) attribute, it
        # round-tripped as an ordinary attribute rather than a real
        # namespace declaration and is still sitting on `tree` here.
        # Blindly injecting our own would duplicate it - a fatal XML
        # well-formedness error ("Attribute xmlns redefined") that breaks
        # this content document for every EPUB reader, since content
        # documents must parse as strict XML. Only add what isn't already
        # there.
        extra = b''
        if tree.get('xmlns') is None:
            extra += b' xmlns="http://www.w3.org/1999/xhtml"'
        if tree.get('xmlns:epub') is None:
            extra += b' xmlns:epub="http://www.idpf.org/2007/ops"'
        if extra:
            out = out.replace(b'<html', b'<html' + extra, 1)

    out = _unescape_raw_text_elements(out)

    return out


_AKAMAI_COOKIE_NAMES = {'_abck', 'bm_sz', 'bm_so', 'bm_s', 'bm_lso', 'ak_bmsc'}


def _uses_akamai_bot_manager(cookies):
    """Whether the cookie set includes Akamai Bot Manager's own cookies
    (bm_s, bm_sz, bm_so, bm_lso, _abck, ak_bmsc - all Akamai's, not
    O'Reilly's). Their presence means a 401/403 here is not reliable
    evidence that the login itself expired: Akamai can reject a request
    that merely *looks* automated - wrong User-Agent, missing browser
    headers, or its own opaque bot-score heuristics - independently of
    whether the actual O'Reilly session and JWT are completely valid.
    """
    return any(name in cookies for name in _AKAMAI_COOKIE_NAMES)


# aiohttp's default User-Agent ("Python/3.x aiohttp/3.x") is an immediate,
# unambiguous bot signal to Akamai Bot Manager. These headers don't defeat
# Akamai's heavier JS-based sensor checks - nothing short of an actual
# browser can - but they remove the single most obvious tell, and cost
# nothing to send regardless.
DEFAULT_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': BASE_URL + '/',
}


def _jwt_remaining(jwt_value):
    """Seconds until orm-jwt expiry, or None if unreadable."""
    if not jwt_value or jwt_value.count('.') < 2:
        return None
    try:
        payload = jwt_value.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get('exp')
        if not exp:
            return None
        return int(exp - time.time())
    except Exception:
        return None


def _best_jwt_from_jar(session):
    """Return the orm-jwt value with the latest expiry from the cookie jar."""
    best_val, best_rem = None, None
    for cookie in session.cookie_jar:
        if cookie.key != 'orm-jwt':
            continue
        rem = _jwt_remaining(cookie.value)
        if rem is None:
            continue
        if best_rem is None or rem > best_rem:
            best_val, best_rem = cookie.value, rem
    return best_val



async def check_site_online(session):
    """Return (online: bool, detail: str) for learning.oreilly.com reachability."""
    url = BASE_URL + '/'
    try:
        _assert_url_allowed(url)
        async with session.get(
            url,
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=20, connect=10),
        ) as r:
            # Any HTTP response means the host is reachable (even 403/503).
            if r.status >= 500:
                return False, f'site returned HTTP {r.status} (server error)'
            return True, f'site reachable (HTTP {r.status})'
    except aiohttp.ClientConnectorError as exc:
        return False, f'cannot connect — offline or DNS failure ({exc})'
    except aiohttp.ServerTimeoutError:
        return False, 'connection timed out — network slow or site down'
    except asyncio.TimeoutError:
        return False, 'connection timed out — network slow or site down'
    except OSError as exc:
        return False, f'network error ({type(exc).__name__}: {exc})'
    except Exception as exc:  # noqa: BLE001
        return False, f'unexpected connectivity error ({type(exc).__name__}: {exc})'


async def check_api_available(session, api_version=None):
    """Probe whether O'Reilly Learning API vN still exists.

    A 401/403 from the API is treated as *available* (auth required).
    A 404 on the version root suggests the API path changed.
    Connection failures mean offline / blocked.
    Returns (ok: bool, detail: str).
    """
    ver = int(api_version if api_version is not None
              else CONFIG.get('api_version', 2))
    # Prefer a lightweight authenticated-area path; without cookies this
    # typically returns 401/403, which still proves the route exists.
    probes = [
        f'{BASE_URL}/api/v{ver}/',
        f'{BASE_URL}/api/v{ver}/epubs/',
    ]
    last_detail = 'no probe attempted'
    for url in probes:
        try:
            _assert_url_allowed(url)
            async with session.get(
                url,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=20, connect=10),
            ) as r:
                status = r.status
                if status == 404:
                    last_detail = (
                        f'API v{ver} probe {url!r} returned 404 — the '
                        f'API path may have changed'
                    )
                    continue
                if status >= 500:
                    return False, (
                        f'API v{ver} returned HTTP {status} (server error) '
                        f'at {url}'
                    )
                # 200 / 301 / 401 / 403 / 405 etc. → route exists
                return True, (
                    f'API v{ver} reachable at {url} (HTTP {status})'
                )
        except aiohttp.ClientConnectorError as exc:
            return False, f'cannot reach API — offline or DNS failure ({exc})'
        except (aiohttp.ServerTimeoutError, asyncio.TimeoutError):
            return False, 'API probe timed out — network slow or site down'
        except OSError as exc:
            return False, f'API network error ({type(exc).__name__}: {exc})'
        except Exception as exc:  # noqa: BLE001
            last_detail = f'API probe error ({type(exc).__name__}: {exc})'
    return False, last_detail


async def run_connectivity_checks(session_headers, *, assume_yes=False):
    """Fail fast when offline or when API vN looks gone.

    Prints status lines. Returns None on success; calls _die / prompts on failure.
    """
    if not CONFIG.get('check_connectivity', True):
        print('note: connectivity checks disabled in CONFIG')
        return

    api_ver = int(CONFIG.get('api_version', 2))
    timeout = aiohttp.ClientTimeout(total=25, connect=10)
    async with aiohttp.ClientSession(
        headers=session_headers,
        timeout=timeout,
        raise_for_status=False,
    ) as session:
        print('Checking network / site …')
        online, site_detail = await check_site_online(session)
        if not online:
            print(f'  site: OFFLINE — {site_detail}', file=sys.stderr)
            _die(
                'error: cannot reach learning.oreilly.com.\\n'
                '  Check your internet connection, VPN, or firewall.\\n'
                '  If the site is up in a browser but not here, DNS or TLS\\n'
                '  interception may be blocking this script.\\n'
                + report_hint()
            )
        print(f'  site: online — {site_detail}')

        print(f'Checking O\'Reilly API v{api_ver} …')
        api_ok, api_detail = await check_api_available(session, api_ver)
        if not api_ok:
            print(f'  api: UNAVAILABLE — {api_detail}', file=sys.stderr)
            print(
                f'  This script expects API version {api_ver} '
                f'(/api/v{api_ver}/epubs/...).\\n'
                f'  O\'Reilly may have changed or removed this API.\\n'
                f'  Open an Issue if the site works in a browser but this '
                f'check keeps failing:\\n'
                f'    {REPORT_URL}',
                file=sys.stderr,
            )
            if not ask_continue(
                    'Continue anyway? Downloads will likely fail.',
                    assume_yes=assume_yes):
                _die('Aborted due to API availability check.\\n' + report_hint())
            print('  continuing despite API check failure …')
        else:
            print(f'  api: ok — {api_detail}')


async def check_auth(session, *, max_attempts=5):
    """Probe the preferences endpoint and report whether the session is
    authenticated.

    Retries both network errors *and* retryable HTTP statuses (403/429/5xx -
    see RETRYABLE_STATUSES) before concluding anything. A single 403 does
    not reliably mean "your login expired": besides ordinary rate-limiting,
    this site sits behind Akamai Bot Manager, which can reject one request
    as looking automated - independently of the actual session/JWT's
    validity - and then let the identical request through moments later.
    Treating that the same as "the JWT expired" would be actively
    misleading, so this gives the caller enough to actually tell the two
    apart instead of guessing from a single data point.

    Returns (ok, status, detail):
      ok      - True if the endpoint ultimately returned 2xx.
      status  - the last HTTP status seen, or None if every attempt was a
                network-level failure with no response at all.
      detail  - 'ok' on success; otherwise a short snippet of the response
                body (often diagnostic - Akamai's block pages usually say
                so directly), or the exception on a network failure.
    """
    url = BASE_URL + '/api/v1/user-preferences/'
    last_status = None
    for attempt in range(1, max_attempts + 1):
        try:
            async with session.get(url, raise_for_status=False) as r:
                last_status = r.status
                if r.ok:
                    return True, r.status, 'ok'
                if r.status not in RETRYABLE_STATUSES or attempt == max_attempts:
                    body = (await r.text())[:200].replace('\n', ' ').strip()
                    return False, r.status, body or '(empty response body)'
                delay = 2.0 * attempt + random.uniform(0, 0.5)
                print(f'  auth probe got HTTP {r.status}, retrying in '
                      f'{delay:.1f}s (attempt {attempt}/{max_attempts})')
                await asyncio.sleep(delay)
        except _NETWORK_ERRORS as exc:
            if attempt == max_attempts:
                return False, None, f'{type(exc).__name__}: {exc}'
            delay = 2.0 * attempt + random.uniform(0, 0.5)
            print(f'  auth probe network error, retrying in {delay:.1f}s '
                  f'(attempt {attempt}/{max_attempts})')
            await asyncio.sleep(delay)
    return False, last_status, 'gave up'


def _safe_cache_path(cache_root, full_path):
    """Map an API full_path to a file under cache_root, rejecting escapes.

    pathlib discards the left operand when the right is absolute
    (Path('/cache') / '/OEBPS/x' == Path('/OEBPS/x')), and '..'
    segments can climb out of the cache tree. Normalise and verify the
    result still lives under cache_root.
    """
    # Treat as a relative POSIX path regardless of OS separators.
    rel = unquote(str(full_path).replace('\\', '/')).lstrip('/')
    rel = posixpath.normpath(rel)
    if rel in ('', '.', '..') or rel.startswith('../') or posixpath.isabs(rel):
        raise ValueError(f'unsafe cache path: {full_path!r}')
    path = cache_root / rel
    # Path.resolve() follows symlinks; use absolute()+parts compare so a
    # not-yet-created file still validates.
    try:
        path.resolve().relative_to(cache_root.resolve())
    except ValueError as exc:
        raise ValueError(f'cache path escapes root: {full_path!r}') from exc
    return path


async def fetch_book(book_id, zfh, session, concurrency=DEFAULT_CONCURRENCY,
                     cache_dir=None, force=False, make_nav=True, raw=False,
                     kindle=False):
    """Download every file listed for the book and write them into zfh.

    Normal mode (raw=False):
      - rewrite HTML/CSS/OPF/NCX so internal links resolve inside the zip
      - rename the package document to content.opf
      - optionally synthesise EPUB3 nav.xhtml from NCX
      - ensure dcterms:modified on the OPF

    Raw mode (raw=True):
      - write every file's bytes *exactly* as the API returned them
      - keep original full_path names (no content.opf rename)
      - no link rewriting, no nav, no metadata injection
      - container.xml points at the OPF path the API actually used
      - still wraps files under EPUB/ with a standard mimetype entry so
        the zip is a valid EPUB package container (the files themselves
        are untouched)
    """
    root_path = f'/api/v2/epubs/urn:orm:book:{book_id}/files/'

    book_info = await fetch_book_info(session, book_id)
    if book_info.get('title'):
        print(f'  title: {book_info["title"]}')
        if book_info.get('isbn'):
            print(f'  isbn:  {book_info["isbn"]}')
        authors = book_info.get('authors') or []
        names = [a.get('name') for a in authors if isinstance(a, dict) and a.get('name')]
        if names:
            print(f'  authors: {", ".join(names)}')

    # mimetype must be first and uncompressed (EPUB spec). container.xml is
    # written later in raw mode once we know the real OPF path; in normal
    # mode it always points at EPUB/content.opf.
    zfh.writestr('mimetype', b'application/epub+zip', compress_type=zipfile.ZIP_STORED)
    if not raw:
        zfh.writestr('META-INF/container.xml', CONTAINER)

    # Collect the *complete* file listing across every paginated results page
    # up front, before downloading anything. We need the full set of
    # stylesheets in the book (see to_xhtml's stylesheet backfill above)
    # before we can correctly process any single HTML page.
    results = []
    url = BASE_URL + root_path
    if '?' not in url:
        url = url + '?limit=200'
    while url:
        print(f'listing {url}')
        data = json.loads(await get_with_retry(session, url))
        results.extend(data.get('results', []))
        url = data.get('next')

    css_paths = [r['full_path'] for r in results if r['full_path'].endswith('.css')]
    total = len(results)
    cache_root = Path(cache_dir or DEFAULT_CACHE_DIR) / str(book_id)
    cache_root.mkdir(parents=True, exist_ok=True)

    failed = []
    # Members keyed by path inside the EPUB/ folder (e.g. 'content.opf',
    # 'OEBPS/Text/01.htm'). Built fully before writing so we can synthesise
    # nav.xhtml and patch the OPF afterward (skipped entirely in --raw).
    # Mutations happen under progress_lock so concurrent tasks never interleave
    # dict/list updates even if this is later driven by multiple threads.
    members = {}
    done = 0
    cached_hits = 0
    progress_lock = asyncio.Lock()

    async def download(result, sem):
        nonlocal done, cached_hits
        full_path = result['full_path']
        try:
            cache_path = _safe_cache_path(cache_root, full_path)
        except ValueError as exc:
            print(f'FAILED to cache-map {full_path}: {exc}')
            async with progress_lock:
                failed.append(full_path)
                done += 1
            return

        content = None
        from_cache = False

        async with sem:
            if (not force and cache_path.is_file()
                    and cache_path.stat().st_size > 0):
                try:
                    content = cache_path.read_bytes()
                    from_cache = True
                except OSError:
                    content = None

            if content is None:
                # A little jitter between requests avoids bursty, all-at-once
                # request patterns that are more likely to trip rate limiting.
                await asyncio.sleep(random.uniform(0, 0.2))
                try:
                    content = await get_with_retry(session, result['url'])
                except (aiohttp.ClientResponseError, RuntimeError) as exc:
                    print(f'FAILED to download {full_path}: {exc}')
                    async with progress_lock:
                        failed.append(full_path)
                        done += 1
                    return
                # Never persist an empty payload — a bad 200 must not poison
                # the cache after the server recovers.
                if content:
                    try:
                        cache_path.parent.mkdir(parents=True, exist_ok=True)
                        cache_path.write_bytes(content)
                    except OSError as exc:
                        # Cache is best-effort; a write failure must not abort
                        # a successful download.
                        print(f'  warning: could not cache {full_path}: {exc}')

        # --raw: keep API bytes and original path, zero transformation.
        out_path = full_path
        if not raw:
            # Processing runs *outside* the download semaphore so CPU-bound
            # XHTML rewriting does not stall other network fetches.
            try:
                if full_path.endswith(HTML_EXTENSIONS):
                    content = to_xhtml(content, root_path, full_path, css_paths)
                elif full_path.endswith('.css'):
                    content = rewrite_css(content, root_path, full_path)
                elif full_path.endswith('.opf'):
                    content = rewrite_opf(content, root_path, full_path)
                    # The API doesn't always name the package document
                    # "content.opf" (e.g. it may be "9780138308667.opf") but
                    # container.xml always points at "EPUB/content.opf".
                    out_path = 'content.opf'
                elif full_path.endswith('.ncx'):
                    content = rewrite_ncx(content, root_path, full_path)
            except Exception as exc:  # noqa: BLE001 - skip just this file
                print(f'FAILED to process {full_path}: {exc!r}')
                # Drop the cache entry so a later re-run refetches after the
                # server (or our rewriter) recovers, instead of replaying poison.
                try:
                    if cache_path.is_file():
                        cache_path.unlink()
                        print(f'  removed bad cache entry for {full_path}')
                except OSError:
                    pass
                async with progress_lock:
                    failed.append(full_path)
                    done += 1
                return

        async with progress_lock:
            members[out_path] = content
            done += 1
            if from_cache:
                cached_hits += 1
            if done % 25 == 0 or done == total:
                print(f'  {done}/{total} files'
                      + (f' ({cached_hits} from cache)' if cached_hits else ''))

    # Unbounded concurrency across a whole book can trip the API's rate
    # limiting, which shows up as random missing/truncated pages rather than
    # a clean error. Cap how many requests are in flight at once.
    sem = asyncio.Semaphore(max(1, concurrency))

    mode = 'raw (no rewriting)' if raw else 'processed'
    print(f'downloading {total} files [{mode}] (concurrency={concurrency}'
          f', cache={cache_root}'
          + (', force re-fetch' if force else '')
          + ')')
    await asyncio.gather(*[download(result, sem) for result in results])

    if raw:
        # Locate the package document under its original API path.
        opf_members = [p for p in members if p.endswith('.opf')]
        if not opf_members:
            raise RuntimeError(
                'No .opf package document in the API listing — cannot build '
                'even a raw EPUB. Re-run with a live session.'
            )
        # Prefer a top-level *.opf; otherwise first match.
        opf_path = next((p for p in opf_members if '/' not in p), opf_members[0])
        zfh.writestr('META-INF/container.xml',
                     _container_xml(f'EPUB/{opf_path}'))
        print(f'  raw mode: container.xml → EPUB/{opf_path}')
    else:
        if 'content.opf' not in members:
            raise RuntimeError(
                'Package document (content.opf) missing after download — '
                'cannot build a valid EPUB. Re-run with a live session; if the '
                'OPF keeps failing, try --force to bypass a bad cache entry.'
            )

        # --- EPUB3 nav from NCX (when the package has no nav of its own) -----
        if make_nav:
            ncx_path = next((p for p in members if p.endswith('.ncx')), None)
            already_has_nav = any(
                p.lower().endswith(('nav.xhtml', 'nav.html')) for p in members
            )
            if ncx_path and not already_has_nav:
                nav_bytes = generate_nav_from_ncx(members[ncx_path], ncx_path)
                if nav_bytes:
                    members[NAV_PATH] = nav_bytes
                    members['content.opf'] = finalize_opf(
                        members['content.opf'],
                        nav_path=NAV_PATH,
                        bump_to_epub3=True,
                    )
                    print(f'  added EPUB3 nav.xhtml (from {ncx_path})')
            else:
                # No nav to inject (or one already present); still run finalize
                # for dcterms:modified / future hooks.
                members['content.opf'] = finalize_opf(members['content.opf'])

        # Align package metadata with the edition listed by the Learning API
        # (fixes wrong title/language on some translated/republished packages).
        if book_info:
            members['content.opf'] = patch_opf_from_book_info(
                members['content.opf'], book_info
            )

    # --- Kindle overflow fix (table / pre / wide media) ---------------------
    if kindle and not raw:
        if apply_kindle_fixes(members):
            print(f'  added Kindle overflow CSS ({KINDLE_CSS_PATH})')
        else:
            print('  warning: --kindle requested but could not inject CSS')
    elif kindle and raw:
        print('  note: --kindle is ignored in --raw mode '
              '(no HTML/OPF rewriting)')

    # Single-threaded write after gather — ZipFile is not concurrent-safe.
    for out_path, content in members.items():
        zfh.writestr(f'EPUB/{out_path}', content)

    if failed:
        print()
        print(f'WARNING: {len(failed)} of {total} files failed to '
              f'download and are missing from the epub:')
        for full_path in failed:
            print(f'  {full_path}')
        print('This is usually transient rate-limiting or an expired JWT - '
              'try running again; cached files will be reused automatically '
              '(pass --force to re-download everything).')
    elif cached_hits:
        print(f'reused {cached_hits}/{total} files from cache')


def _make_record(name, value, **overrides):
    """Build a cookie record for a bare name/value pair - the --jwt
    override, a webview-sourced cookie, or an entry from the legacy flat
    {"name": "value"} file format - with sensible default scoping matching
    how O'Reilly's own cookies actually look.
    """
    record = {'name': name, 'value': value, 'domain': '.oreilly.com', 'path': '/'}
    record.update(overrides)
    return record


def load_cookies(path):
    """Load cookies for learning.oreilly.com out of a JSON file, keeping
    each cookie's *complete* record - domain, path, secure, httpOnly,
    sameSite, expirationDate, and anything else the file carries - not
    just its value.

    Accepts either a plain {"name": "value", ...} object (values get the
    default scoping from _make_record), or the list-of-objects format
    browser extensions like Cookie-Editor/EditThisCookie export - the same
    format this script's own save_cookies writes back, so a round trip
    keeps everything intact.

    Preserving the full record instead of flattening to name/value matters
    for two reasons: aiohttp can actually enforce the real domain/path/
    expiry scoping a browser would (see apply_cookies_to_session), rather
    than sending every cookie to every request forever regardless of
    whether it's expired or scoped to a different domain; and the file
    this script writes back stays byte-compatible with re-importing
    straight into a browser extension, if you ever want to.

    Anything scoped to an unrelated domain, or already past its own
    expirationDate, is skipped - the latter printed as a warning, since a
    stale cookie silently sent forever regardless of its real expiry is
    exactly the kind of thing that made "is this actually expired?" hard
    to answer before.

    A --cookies path that doesn't exist yet isn't an error: it's the
    normal state of a first-ever --webview login, which is expected to
    create that file rather than read one that's already there.
    """
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}

    if isinstance(data, dict):
        return {str(k): _make_record(str(k), str(v)) for k, v in data.items()}

    records = {}
    skipped_expired = []
    now = time.time()
    for entry in data:
        domain = entry.get('domain', '')
        if domain and 'oreilly.com' not in domain:
            continue
        name, value = entry.get('name'), entry.get('value')
        if name is None or value is None:
            continue
        exp = entry.get('expirationDate')
        if exp is not None and not entry.get('session') and exp < now:
            skipped_expired.append(name)
            continue
        records[name] = dict(entry)  # keep every field, not just name/value
    if skipped_expired:
        print(f'  skipping {len(skipped_expired)} already-expired cookie(s) '
              f'from {path}: {", ".join(skipped_expired)}')
    return records


def _record_to_morsel(record):
    """Turn one cookie record into an http.cookies.Morsel carrying real
    domain/path/secure/httpOnly/expiry, so aiohttp's own cookie jar
    enforces that scoping - including auto-expiring it at the right time
    - instead of a bare value being sent to every request forever.
    """
    simple = SimpleCookie()
    simple[record['name']] = record['value']
    morsel = simple[record['name']]
    if record.get('domain'):
        morsel['domain'] = record['domain']
    morsel['path'] = record.get('path') or '/'
    if record.get('secure'):
        morsel['secure'] = True
    if record.get('httpOnly'):
        morsel['httponly'] = True
    same_site = record.get('sameSite')
    if same_site:
        morsel['samesite'] = same_site
    exp = record.get('expirationDate')
    if exp is not None:
        # Express even "already expired" explicitly as max-age=0, rather
        # than leaving max-age/expires unset: aiohttp only schedules
        # expiry when one of those fields is actually present, so leaving
        # both unset means "never expires" - the opposite of intended, and
        # exactly backwards for a cookie that's supposed to be dead.
        morsel['max-age'] = str(max(int(exp - time.time()), 0))
    return morsel


def apply_cookies_to_session(session, records):
    """Seed session's cookie jar from full-fidelity cookie records,
    preserving domain/path/secure/httpOnly/expiry so aiohttp enforces the
    same scoping and lifetime a real browser would, instead of the
    cookies= convenience kwarg's flat, scope-free, never-expires
    treatment.
    """
    simple = SimpleCookie()
    for record in records.values():
        simple[record['name']] = _record_to_morsel(record)
    session.cookie_jar.update_cookies(simple, response_url=yarl.URL(BASE_URL))


def save_cookies(path, records, session):
    """Write the current cookies for learning.oreilly.com back to path, as
    a full list of records (browser-export-compatible), not just a flat
    {"name": "value"} object - preserving domain/path/secure/httpOnly/
    sameSite/expirationDate and any other fields the original file
    carried.

    aiohttp's cookie jar already absorbs any Set-Cookie the API sends over
    the course of a run - if it rotates orm-jwt (or anything else) to a
    fresh value at any point, that's the value that ends up here, not
    whatever was loaded at the start. Point --cookies at the same file
    every time and each run picks up wherever the last one left off,
    turning "the JWT expires quickly" into a once-in-a-while chore (only
    when the whole session, not just the short-lived token, has actually
    died) instead of a once-per-run one.

    A cookie's *value* always comes from the live jar - it's the only
    place a mid-run rotation shows up. Domain/path/secure/httpOnly/
    sameSite prefer whatever the jar's Morsel actually specifies, falling
    back to the originally-loaded record when the Morsel is silent (most
    Set-Cookie responses don't restate every attribute on every rotation).
    expirationDate is a deliberate exception: it always keeps the
    originally-loaded value rather than trying to reinterpret the
    Morsel's own max-age/expires text as a fresh absolute time. That text
    reflects whenever update_cookies last ran for this cookie - which for
    an untouched cookie is when this run started, not "now" - so
    recomputing from it after the fact would quietly inflate the expiry by
    however long the run took. Keeping the original is honest about what
    is and isn't actually known.
    """
    best_jwt = _best_jwt_from_jar(session)
    merged = {}
    for morsel in session.cookie_jar:
        name = morsel.key
        original = records.get(name, {})
        value = best_jwt if name == 'orm-jwt' and best_jwt else morsel.value
        record = {
            'name': name,
            'value': value,
            'domain': morsel['domain'] or original.get('domain', ''),
            'path': morsel['path'] or original.get('path', '/'),
            'secure': bool(morsel['secure']) or bool(original.get('secure')),
            'httpOnly': bool(morsel['httponly']) or bool(original.get('httpOnly')),
        }
        same_site = morsel['samesite'] or original.get('sameSite')
        if same_site:
            record['sameSite'] = same_site
        if 'expirationDate' in original:
            record['expirationDate'] = original['expirationDate']
        if original.get('session'):
            record['session'] = True
        # Carry through anything else the original file had verbatim
        # (hostOnly, firstPartyDomain, partitionKey, storeId, ...) so the
        # output stays as close to the original export's shape as
        # possible - genuinely preserving structure, not just the fields
        # this script itself cares about.
        for key, val in original.items():
            record.setdefault(key, val)
        merged[name] = record

    with open(path, 'w') as f:
        json.dump(list(merged.values()), f, indent=2)
    # Restrict permissions so other users on the machine cannot read tokens
    _chmod_private(path)


def get_cookies_via_webview(profile_dir, start_url=None, timeout_minutes=10):
    """Open a real, native browser window (via pywebview) pointed at
    learning.oreilly.com so Akamai's own JS sensor can run and/or the user
    can log in normally, then harvest the resulting cookies straight out
    of that browser engine's cookie store.

    Nothing else here can substitute for this. Akamai's bot-detection
    cookies (_abck, bm_sz, ...) are populated by an obfuscated JS sensor
    that only runs inside a real page context, and orm-jwt/orm-rt are only
    ever issued after actually completing O'Reilly's login flow - a plain
    HTTP client can never produce either on its own, no matter how
    convincing its headers are. pywebview embeds the OS's own native
    browser engine (WebKit on Linux/macOS, WebView2 on Windows) rather
    than simulating one, so whatever a real Chrome or Safari tab would
    satisfy, this does too.

    profile_dir persists cookies and local storage across separate runs of
    this script, the same way a browser profile does - so in practice
    this is normally only a login step the first time, or again once the
    underlying session has genuinely died, not something to repeat before
    every single download.

    Requires an actual display (X11/Wayland, or a macOS/Windows desktop
    session) - this cannot run over a plain SSH terminal or inside a
    headless container. Not something this script can detect and fall
    back from automatically; if there's no display, pywebview itself will
    raise, and that error is left to surface as-is rather than guessed at.

    Returns the harvested cookies as a {name: value} dict once a live
    orm-jwt (per its own exp claim) appears in the window's cookie jar, or
    None if the window was closed, or timeout_minutes passed, first.
    """
    try:
        import webview
    except ImportError:
        print(
            "--webview requires the 'pywebview' package, which isn't "
            "installed. Install it with:\n"
            "  pip install pywebview --break-system-packages\n"
            "(pywebview also needs a native GUI toolkit present - e.g. "
            "GTK+WebKit2 on Linux - see pywebview's own docs if that "
            "install step fails.)"
        )
        return None

    result = {'cookies': None}

    def _watch(window):
        deadline = time.time() + timeout_minutes * 60
        consecutive_errors = 0
        while time.time() < deadline:
            time.sleep(1.5)
            try:
                raw_cookies = window.get_cookies()
                consecutive_errors = 0
            except Exception as exc:
                # Commonly just "page hasn't started loading yet" right at
                # the start - only give up if it never recovers.
                consecutive_errors += 1
                if consecutive_errors >= 10:
                    print(f'  webview: get_cookies() kept failing ({exc!r}); giving up')
                    break
                continue
            cookies = {name: morsel.value for c in raw_cookies for name, morsel in c.items()}
            rem = _jwt_remaining(cookies.get('orm-jwt', ''))
            if rem is not None and rem > 0:
                result['cookies'] = cookies
                break
        try:
            window.destroy()
        except Exception:
            pass

    window = webview.create_window(
        "Log in to O'Reilly - this window closes itself once you're in",
        start_url or (BASE_URL + '/'),
    )
    webview.start(_watch, window, private_mode=False, storage_path=profile_dir)
    return result['cookies']



def setup_logging(log_file=None, verbose=False):
    """Configure root logger for this script.

    --verbose → INFO on stderr; --log FILE → DEBUG to that file (and INFO
    on stderr if verbose, else WARNING on stderr only).
    """
    root = logging.getLogger()
    # Avoid duplicate handlers if called twice
    root.handlers.clear()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        '%(asctime)s %(levelname)-7s %(message)s',
        datefmt='%H:%M:%S',
    )
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    if verbose:
        console.setLevel(logging.INFO)
    else:
        console.setLevel(logging.WARNING)
    root.addHandler(console)

    if log_file:
        try:
            parent = os.path.dirname(os.path.abspath(log_file))
            if parent:
                os.makedirs(parent, exist_ok=True)
            fh = logging.FileHandler(log_file, encoding='utf-8')
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter(
                '%(asctime)s %(levelname)-7s [%(name)s] %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S',
            ))
            root.addHandler(fh)
            log.info('logging to %s', log_file)
        except OSError as exc:
            _die(
                f'error: cannot write log file {log_file!r}: {exc}\n'
                f'  Check the path exists and you have write permission '
                f'(storage permission on Android/Termux?).'
            )


def ensure_writable_dir(dir_path, label):
    """Create dir if needed and verify we can write a probe file. Exit on failure."""
    dir_path = os.path.abspath(dir_path)
    try:
        os.makedirs(dir_path, exist_ok=True)
    except OSError as exc:
        _die(
            f'error: cannot create {label} directory {dir_path!r}: {exc}\n'
            f'  Permission denied or path inaccessible — on Android/Termux, '
            f'grant storage permission or pick a path under $HOME.'
        )
    probe = os.path.join(dir_path, '.oreilly_write_test')
    try:
        with open(probe, 'w') as f:
            f.write('ok')
        os.unlink(probe)
    except OSError as exc:
        _die(
            f'error: cannot write to {label} directory {dir_path!r}: {exc}\n'
            f'  Permission denied or read-only filesystem — check storage '
            f'permissions or free space.'
        )
    log.debug('writable %s dir: %s', label, dir_path)
    return dir_path


def ensure_writable_file_parent(file_path, label):
    """Ensure the parent directory of a file path is writable."""
    parent = os.path.dirname(os.path.abspath(file_path)) or '.'
    return ensure_writable_dir(parent, label)


def polish_with_calibre(epub_path, *, required=False):

    """Run Calibre ebook-convert EPUB→EPUB to normalise the package.

    Produces a reader-friendly EPUB (fixed structure, media types, TOC, etc.).
    Returns the path of the polished file on success, or None if Calibre is
    missing / conversion failed. Never raises — missing Calibre is not fatal.

    When running as root, sets QTWEBENGINE_DISABLE_SANDBOX=1 (required by
    Qt WebEngine).
    """
    epub_path = os.path.abspath(epub_path)
    if not os.path.isfile(epub_path):
        print(f'  calibre: skip — {epub_path} not found')
        return None

    ebook_convert = shutil.which('ebook-convert')
    if not ebook_convert:
        msg = (
            'calibre: ebook-convert not found on PATH — cannot polish.\n'
            f'  Install Calibre from the official site: {CALIBRE_URL}\n'
            '  After installing, ensure "ebook-convert" is on your PATH, '
            'or omit --calibre.'
        )
        if required:
            _die('error: ' + msg)
        print('  ' + msg + '\n  Download kept without polish.')
        log.warning(msg)
        return None

    # Write to a sibling temp, then replace — ebook-convert refuses in==out.
    base, ext = os.path.splitext(epub_path)
    polished = f'{base}.calibre{ext or ".epub"}'
    env = os.environ.copy()
    if os.geteuid() == 0:
        env['QTWEBENGINE_DISABLE_SANDBOX'] = '1'

    print(f'  calibre: polishing {os.path.basename(epub_path)} → '
          f'{os.path.basename(polished)} …')
    try:
        proc = subprocess.run(
            [ebook_convert, epub_path, polished],
            env=env,
            capture_output=True,
            text=True,
            timeout=3600,
        )
    except FileNotFoundError:
        print('  calibre: ebook-convert disappeared from PATH — skip')
        return None
    except subprocess.TimeoutExpired:
        print('  calibre: ebook-convert timed out after 1h — skip')
        try:
            if os.path.isfile(polished):
                os.unlink(polished)
        except OSError:
            pass
        return None

    if proc.returncode != 0 or not os.path.isfile(polished):
        err = (proc.stderr or proc.stdout or '').strip().splitlines()
        tail = err[-5:] if err else ['(no output)']
        print(f'  calibre: conversion failed (exit {proc.returncode}):')
        for line in tail:
            print(f'    {line}')
        try:
            if os.path.isfile(polished):
                os.unlink(polished)
        except OSError:
            pass
        return None

    # Replace the original with the polished build; keep a .orig backup only
    # if replace fails mid-way (atomic replace when possible).
    try:
        os.replace(polished, epub_path)
    except OSError as exc:
        print(f'  calibre: could not replace {epub_path}: {exc}')
        print(f'  calibre: polished file left at {polished}')
        return polished

    print(f'  calibre: polished EPUB ready → {epub_path}')
    return epub_path



def write_error_log(exc, *, context=''):
    """Write an unknown/unexpected error to error_logs/error_log_YYYY_MM_DD_HHMMSS.log.

    Returns the path written, or None on failure.
    """
    import traceback
    from datetime import datetime

    log_dir = CONFIG.get('error_log_dir') or 'error_logs'
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError as e:
        print(f'warning: could not create error log dir {log_dir!r}: {e}',
              file=sys.stderr)
        log_dir = '.'

    stamp = datetime.now().strftime('%Y_%m_%d_%H%M%S')
    path = os.path.join(log_dir, f'error_log_{stamp}.log')
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f'time: {datetime.now().isoformat(timespec="seconds")}\n')
            f.write(f'python: {sys.version}\n')
            f.write(f'script_version: {SCRIPT_VERSION}\n')
            # Redact secrets from argv / exception text / traceback
            f.write(_redact_secrets(f'argv: {sys.argv!r}\n'))
            if context:
                f.write(_redact_secrets(f'context: {context}\n'))
            f.write(f'error_type: {type(exc).__name__}\n')
            f.write(_redact_secrets(f'error: {exc!r}\n\n'))
            f.write('traceback:\n')
            tb = ''.join(traceback.format_exception(
                type(exc), exc, exc.__traceback__))
            f.write(_redact_secrets(tb))
        _chmod_private(path)
        print(f'error: unexpected failure ({type(exc).__name__}: {exc})',
              file=sys.stderr)
        print(f'  details saved to {path}', file=sys.stderr)
        print(
            '  If this looks like a bug, please open an Issue on GitHub:\n'
            f'    {REPORT_URL}',
            file=sys.stderr,
        )
        log.exception('unexpected error (saved to %s)', path)
        return path
    except OSError as e:
        print(f'error: unexpected failure ({type(exc).__name__}: {exc})',
              file=sys.stderr)
        print(f'  and could not write error log: {e}', file=sys.stderr)
        print(
            '  Please open an Issue on GitHub:\n'
            f'    {REPORT_URL}',
            file=sys.stderr,
        )
        return None


def parse_books_file(path):
    """Parse books.txt / sources.txt lines: BOOK_ID or BOOK_ID # title.

    Yields (book_id, title_or_empty). Skips blank/full-line comments.
    Invalid lines are reported to stderr and skipped.
    """
    with open(path, encoding='utf-8') as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            if line.count('#') > 1:
                print(f'error: {path}:{lineno}: only one # separator allowed: {raw.rstrip()}',
                      file=sys.stderr)
                continue
            if '#' in line:
                book_id, title = line.split('#', 1)
                book_id, title = book_id.strip(), title.strip()
                if (title.startswith('"') and title.endswith('"')) or (
                        title.startswith("'") and title.endswith("'")):
                    title = title[1:-1].strip()
            else:
                book_id, title = line, ''
            if not book_id.isdigit():
                print(f'error: {path}:{lineno}: book id must be digits only: {raw.rstrip()}',
                      file=sys.stderr)
                continue
            yield book_id, title



def _validate_cli_args(args):
    """Reject invalid option values / formats before any network I/O."""
    errors = []

    if args.book_id is not None and not str(args.book_id).isdigit():
        errors.append(
            f'book id must be digits only (ISBN-style), got {args.book_id!r}'
        )

    if args.concurrency is not None:
        if not isinstance(args.concurrency, int) or args.concurrency < 1:
            errors.append(
                f'--concurrency must be an integer >= 1, got {args.concurrency!r}'
            )
        elif args.concurrency > 64:
            errors.append(
                f'--concurrency {args.concurrency} is too high (max 64); '
                f'large values often trigger rate limits'
            )

    if args.cookies is not None:
        p = args.cookies
        if p != '-' and os.path.isdir(p):
            errors.append(f'--cookies points to a directory, not a file: {p!r}')

    if args.books is not None and not os.path.isfile(args.books):
        # Allow CONFIG auto-detect path missing only when user did not pass --books
        # Here args.books is set explicitly or from CONFIG file existence.
        if not os.path.exists(args.books):
            errors.append(f'books file not found: {args.books!r}')
        elif not os.path.isfile(args.books):
            errors.append(f'books path is not a file: {args.books!r}')

    if args.cache_dir is not None and os.path.exists(args.cache_dir) and not os.path.isdir(args.cache_dir):
        errors.append(f'--cache-dir is not a directory: {args.cache_dir!r}')

    if args.output_dir is not None and os.path.exists(args.output_dir) and not os.path.isdir(args.output_dir):
        errors.append(f'--output-dir is not a directory: {args.output_dir!r}')

    if args.log is not None and os.path.isdir(args.log):
        errors.append(f'--log points to a directory, not a file: {args.log!r}')

    if args.raw and args.no_nav:
        # not fatal — no_nav is ignored in raw mode; just note
        print(
            'note: --no-nav is ignored when --raw is set '
            '(raw mode never synthesises nav).',
            file=sys.stderr,
        )

    if errors:
        for e in errors:
            print(f'error: {e}', file=sys.stderr)
        print('  Fix the option/format and try again.', file=sys.stderr)
        print(report_hint(), file=sys.stderr)
        sys.exit(2)




# Keys we persist in the options file (no secrets like jwt values).
_SAVED_OPTION_KEYS = (
    'cookies', 'concurrency', 'cache_dir', 'output_dir', 'books',
    'webview_profile', 'log', 'raw', 'calibre', 'kindle', 'no_nav', 'force',
    'verbose', 'skip_connectivity',
)


def load_saved_options(path=None):
    """Load persisted CLI defaults from JSON. Returns a dict (possibly empty)."""
    path = path or CONFIG.get('options_file') or '.oreilly_options.json'
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            print(f'warning: options file {path!r} is not a JSON object — ignored',
                  file=sys.stderr)
            return {}
        # Never load jwt from disk into defaults via this file
        data.pop('jwt', None)
        return data
    except (OSError, json.JSONDecodeError) as exc:
        print(f'warning: could not read options file {path!r}: {exc}',
              file=sys.stderr)
        return {}


def save_options(args, path=None):
    """Write selected CLI options to the options file (no secrets)."""
    path = path or CONFIG.get('options_file') or '.oreilly_options.json'
    data = {}
    for key in _SAVED_OPTION_KEYS:
        if not hasattr(args, key):
            continue
        val = getattr(args, key)
        # Skip empty / False for flags? Keep explicit False so user can persist off
        if val is None:
            continue
        if key in ('log',) and not val:
            continue
        data[key] = val
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write('\n')
        _chmod_private(path)
        print(f'saved options to {path}')
        log.info('saved options to %s: %s', path, data)
        return path
    except OSError as exc:
        print(f'warning: could not save options to {path!r}: {exc}',
              file=sys.stderr)
        return None


def print_quick_reference():
    """Short usage card when the script is run with no arguments."""
    prog = 'oreilly_downloader.py'
    lines = [
        f"O'Reilly EPUB downloader  v{SCRIPT_VERSION}",
        "Download books you have access to on learning.oreilly.com as standalone EPUBs.",
        "",
        "Quick start",
        "  1. Log in at https://learning.oreilly.com",
        "  2. Export cookies for the site to cookies.json  (Cookie-Editor, etc.)",
        "  3. Run:",
        f"       python3 {prog} BOOK_ID --cookies cookies.json",
        "",
        "Examples",
        f"  python3 {prog} 9781617295355 --cookies cookies.json",
        f"  python3 {prog} 9781617295355 --cookies cookies.json --calibre",
        f"  python3 {prog} --books books.txt --cookies cookies.json",
        f"  python3 {prog} 9781617295355 --cookies cookies.json --raw --calibre",
        f"  python3 {prog} --cookies cookies.json --webview",
        "",
        "Common options",
        "  BOOK_ID              Digits from the book URL (.../9781617295355/)",
        "  --cookies PATH       Browser cookie export (recommended)",
        "  --books FILE         List of ids:  978... # \"Title\"",
        "  --calibre            Polish EPUB with Calibre ebook-convert",
        "  --raw                Package API files with no rewriting",
        "  --webview            Log in via a real browser window",
        "  --force              Ignore download cache",
        "  --yes, -y            Skip safety prompts",
        "  --verbose, -v        More console output",
        "  --log PATH           Debug log file",
        "  --save-options       Remember current flags (e.g. --cookies)",
        "  --print-options      Show saved options file and exit",
        "  --version            Print version",
        "  -h, --help           Full detailed help",
        "",
        "books.txt format",
        '  9781617295355 # "Math for Programmers"',
        "  9781633437777",
        "",
        f"Need more detail?  Run:  python3 {prog} --help",
        f"Report bugs:  {REPORT_URL}",
    ]
    print("\n".join(lines))


async def amain():
    # No arguments at all → quick reference (not an error)
    if len(sys.argv) <= 1:
        print_quick_reference()
        sys.exit(0)

    class _ArgParser(argparse.ArgumentParser):
        def error(self, message):
            print(f'error: invalid option or argument: {message}', file=sys.stderr)
            print(f'  Quick reference:  python3 {self.prog}', file=sys.stderr)
            print(f'  Full help:        python3 {self.prog} --help', file=sys.stderr)
            print(report_hint(), file=sys.stderr)
            sys.exit(2)

    parser = _ArgParser(
        prog='oreilly_downloader.py',
        description=(
            "Download a book from O'Reilly learning as a standalone .epub "
            "file. See the top of this script (or run pydoc on it) for the "
            "full walkthrough of getting cookies.json set up."
        ),
        epilog=(
            "----------------------------------------------------------------\n"
            "DETAILED GUIDE\n"
            "----------------------------------------------------------------\n"
            "\n"
            "BOOK ID\n"
            "  From the book URL on learning.oreilly.com, e.g.\n"
            "    https://learning.oreilly.com/library/view/some-book/9781633437777/\n"
            "  -> book_id is 9781633437777 (digits only).\n"
            "\n"
            "AUTHENTICATION\n"
            "  --cookies PATH   Preferred. Export all cookies for\n"
            "                   learning.oreilly.com while logged in\n"
            "                   (Cookie-Editor / EditThisCookie). The file\n"
            "                   is updated after each run.\n"
            "  --jwt VALUE      Short-lived orm-jwt only; expires within ~1h.\n"
            "  --webview        Open a real browser window to log in (needs\n"
            "                   display + optional: pip install pywebview).\n"
            "  Without cookies the script asks before continuing (partial EPUB\n"
            "  risk). Use --yes to skip prompts in scripts.\n"
            "\n"
            "OUTPUT MODES\n"
            "  (default)        Rewrite links/CSS/OPF into a self-contained EPUB.\n"
            "  --raw            Store API bytes unchanged -> <id>-raw.epub.\n"
            "  --calibre        After build, run Calibre ebook-convert (EPUB->EPUB).\n"
            "                   Install from https://calibre-ebook.com/\n"
            "  --no-nav         Do not synthesise EPUB3 nav.xhtml from NCX.\n"
            "\n"
            "BATCH / LISTS\n"
            "  --books FILE     One book per line:\n"
            '                     9781617295355 # "Title Here"\n'
            "                     9781633437777\n"
            "                   book_id must be digits; only one # separator.\n"
            "  --output-dir DIR Write EPUBs into DIR.\n"
            "\n"
            "CACHE & NETWORK\n"
            "  --cache-dir PATH Raw API cache (default .oreilly_cache/<id>/).\n"
            "  --force          Re-download everything (ignore cache).\n"
            "  --concurrency N  Parallel downloads (default 8, max 64).\n"
            "  --skip-connectivity  Skip site/API v2 online checks.\n"
            "\n"
            "DEBUG & SAFETY\n"
            "  --log PATH       DEBUG log file (secrets redacted).\n"
            "  -v, --verbose    INFO messages on stderr.\n"
            "  -y, --yes        Auto-answer yes to safety prompts.\n"
            "  --version        Print version and exit.\n"
            "  --save-options   Remember flags like --cookies for next run.\n"
            "  --print-options  Show saved options file and exit.\n"
            "\n"
            "SAVED OPTIONS\n"
            "  Common flags can be stored in .oreilly_options.json (CONFIG\n"
            "  options_file). Example:\n"
            '    python3 oreilly_downloader.py --cookies cookies.json '
            '--save-options\n'
            "  Later runs pick those up as defaults; CLI still overrides.\n"
            "  --jwt is never saved.\n"
            "\n"
            "CONFIG\n"
            "  Edit the CONFIG dict near the top of this script for defaults\n"
            "  (cookies path, cache, calibre_polish, api_version, timeouts, ...).\n"
            "  CLI flags always override CONFIG.\n"
            "\n"
            "EXAMPLES\n"
            "  python3 oreilly_downloader.py 9781633437777 --cookies cookies.json\n"
            "  python3 oreilly_downloader.py 9781633437777 --cookies cookies.json --calibre\n"
            "  python3 oreilly_downloader.py --books books.txt --cookies cookies.json\n"
            "  python3 oreilly_downloader.py --cookies cookies.json --webview\n"
            "\n"
            "With no arguments this program prints a short quick reference.\n"
            "Report bugs: https://github.com/official-kandoamoa\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('book_id', nargs='?', default=None, help=(
        "the numeric book id from the book's learning.oreilly.com URL. "
        "Optional when --books FILE is set (or CONFIG books_file exists)."
    ))
    parser.add_argument('--jwt', help=(
        "Just the 'orm-jwt' cookie value. A quick one-off shortcut, but it "
        "expires quickly (well within an hour) and carries none of the "
        "other cookies that might keep it alive the way a browser tab "
        "does - prefer --cookies for anything longer than a single small "
        "book."
    ))
    parser.add_argument(
        '--concurrency', type=int, default=DEFAULT_CONCURRENCY,
        help=(
            f'parallel downloads (default: {DEFAULT_CONCURRENCY}). '
            f'Lower this if you see many 403 errors on large books.'
        ),
    )
    parser.add_argument('--cookies', metavar='PATH', help=(
        "Path to a JSON file of learning.oreilly.com's cookies - export "
        "all of them with a browser extension such as Cookie-Editor or "
        "EditThisCookie (their export format is read directly - domain, "
        "path, secure, httpOnly, sameSite, expirationDate and all), or "
        "just write a plain {\"name\": \"value\"} object yourself. Every "
        "field is preserved and actually used, not just the value: "
        "domain/path scope which requests a cookie is sent with and "
        "expirationDate makes aiohttp expire it at the right time, the "
        "same way a real browser would, rather than sending every cookie "
        "to every request forever. An already-expired cookie is skipped "
        "at load with a warning rather than sent anyway. After the run, "
        "the current cookies (including any the API rotated in along the "
        "way) are written back to this same file in that same full "
        "shape - re-importable into a browser extension too - so the "
        "next run can reuse it as-is instead of asking you to re-export "
        "from the browser every time. Combine with --jwt to override "
        "just that one cookie while keeping the rest from the file."
    ))
    parser.add_argument('--webview', action='store_true', help=(
        "If the session looks dead (no cookies given, or the auth check "
        "below fails outright), open a real native browser window (via "
        "the 'pywebview' package - installed separately) to "
        "learning.oreilly.com, log in there as normal, and pick up "
        "whatever cookies that produces - including Akamai's own bot-"
        "detection cookies (_abck, bm_sz, ...), which nothing else here "
        "can obtain since they come from an obfuscated JS sensor that "
        "only runs in a real browser page. Needs an actual display; "
        "won't work over a plain SSH terminal or in a headless container. "
        "Combine with --cookies so the result is saved for next time, not "
        "just used once."
    ))
    parser.add_argument('--webview-profile', metavar='PATH', default=None, help=(
        "Directory pywebview uses to persist its own browser profile "
        "(cookies, local storage) across runs, so --webview is normally "
        "only a login step the first time rather than every run. Defaults "
        "to .oreilly_webview_profile next to wherever --cookies points, "
        "or ./oreilly_webview_profile if --cookies wasn't given."
    ))
    parser.add_argument('--cache-dir', metavar='PATH', default=DEFAULT_CACHE_DIR,
                        help=(
                            "Directory for caching raw API file bytes so a "
                            "re-run only fetches what is still missing "
                            f"(default: {DEFAULT_CACHE_DIR}/<book_id>/)."
                        ))
    parser.add_argument('--force', action='store_true', help=(
        "Ignore the on-disk cache and re-download every file. The .epub is "
        "always rebuilt either way; this only forces fresh network fetches."
    ))
    parser.add_argument('--no-nav', action='store_true', help=(
        "Do not synthesise an EPUB3 nav.xhtml from the NCX. By default a "
        "nav document is generated when the package only has an EPUB2 "
        "NCX, so modern readers get a working table of contents. "
        "Ignored when --raw is set (raw mode never synthesises files)."
    ))
    parser.add_argument('--raw', action='store_true', help=(
        "Package the API files exactly as received — no HTML/CSS/OPF/NCX "
        "rewriting, no content.opf rename, no nav synthesis, no metadata "
        "injection. Paths stay as the API listed them; container.xml is "
        "pointed at the original .opf. Output is written to "
        "<book_id>-raw.epub so it does not overwrite a processed build. "
        "Useful when you want an archival dump of the source package. "
        "Combine with --calibre to let Calibre turn the raw package into a "
        "reader-friendly EPUB."
    ))
    parser.add_argument('--calibre', action='store_true', help=(
        "After the .epub is built, run Calibre's ebook-convert (EPUB→EPUB) "
        "to normalise structure, media types, and TOC so the file works in "
        "more readers. Requires ebook-convert on PATH. If Calibre is not "
        "installed, this is treated as an error (exit non-zero) so you notice "
        "missing tooling. As root, sets QTWEBENGINE_DISABLE_SANDBOX=1 "
        "automatically. Especially useful with --raw."
    ))
    parser.add_argument('--kindle', action='store_true', help=(
        "Inject CSS that constrains overflow on table and pre elements "
        "(and caps image width) for better display on Amazon Kindle and "
        "other narrow E-Ink readers. Ignored with --raw. Often combined "
        "with --calibre, then convert to AZW3/MOBI in Calibre with "
        "'Ignore margins' enabled."
    ))
    parser.add_argument('--log', metavar='PATH', default=None, help=(
        "Write a detailed debug log to PATH (created if missing). Useful for "
        "diagnosing auth, path rewriting, and download failures. Console "
        "stays quiet unless --verbose is also set."
    ))
    parser.add_argument('--verbose', '-v', action='store_true', help=(
        "Print progress and diagnostic messages to stderr (INFO level). "
        "Combine with --log for a full DEBUG transcript on disk."
    ))
    parser.add_argument('--books', metavar='FILE', default=None, help=(
        "Download every book listed in FILE (format: BOOK_ID or "
        "BOOK_ID # \"title\"). When set, book_id positional may be omitted. "
        f"Default list name in CONFIG: {CONFIG.get('books_file', 'books.txt')!r}."
    ))
    parser.add_argument('--output-dir', metavar='DIR', default='.', help=(
        "Directory to write finished .epub files into (default: current directory)."
    ))
    parser.add_argument('--yes', '-y', action='store_true', help=(
        "Assume yes for interactive safety prompts (untested Python, "
        "unsupported OS, missing cookies). Useful for scripts/CI; use with care."
    ))
    parser.add_argument('--save-options', action='store_true', help=(
        "After a successful run, save common options (cookies path, "
        "concurrency, calibre, raw, cache-dir, etc.) to the options file "
        f"({CONFIG.get('options_file', '.oreilly_options.json')}) so the next "
        "run can omit them. Does not store --jwt secrets."
    ))
    parser.add_argument('--print-options', action='store_true', help=(
        "Print the saved options file and exit."
    ))
    parser.add_argument('--version', action='version',
                        version=f'%(prog)s {SCRIPT_VERSION}')
    parser.add_argument('--skip-connectivity', action='store_true', help=(
        "Skip the online-site and API v2 availability checks. Use only if "
        "you know the network path is fine or are debugging offline."
    ))
    # book_id becomes optional when --books is used
    # Apply saved options as argparse defaults (CLI still wins)
    saved = load_saved_options()
    if saved:
        for key, val in saved.items():
            if key not in _SAVED_OPTION_KEYS:
                continue
            # Map file keys to dest names (same for our flags)
            try:
                parser.set_defaults(**{key: val})
            except TypeError:
                pass
        log.debug('loaded saved options: %s', saved)

    args = parser.parse_args()

    if getattr(args, 'print_options', False):
        opt_path = CONFIG.get('options_file') or '.oreilly_options.json'
        print(f'options file: {opt_path}')
        if os.path.isfile(opt_path):
            print(open(opt_path, encoding='utf-8').read())
        else:
            print('(no saved options yet — run with --save-options to create)')
        sys.exit(0)

    # Soft environment checks (may prompt unless --yes)
    check_python_version(assume_yes=args.yes)
    check_operating_system(assume_yes=args.yes)

    # Validate option values / formats early (clear errors, no download)
    _validate_cli_args(args)

    if getattr(args, 'skip_connectivity', False):
        CONFIG['check_connectivity'] = False

    # Apply CONFIG defaults when CLI left them at "unset" style
    if not args.cookies and CONFIG.get('cookies_path'):
        default_cookies = CONFIG['cookies_path']
        if os.path.isfile(default_cookies):
            args.cookies = default_cookies
            log.info('using CONFIG cookies_path: %s', default_cookies)
    if args.books is None and not getattr(args, 'book_id', None):
        # allow bare run with only CONFIG books_file if it exists
        bf = CONFIG.get('books_file')
        if bf and os.path.isfile(bf):
            args.books = bf

    setup_logging(log_file=args.log, verbose=args.verbose)
    log.info('Python %s', sys.version.replace('\n', ' '))
    log.info('args: %s', _safe_args_for_log(args))

    # CONFIG toggles (CLI flags already set win when user passed them;
    # these fill in when user relies on CONFIG only).
    if CONFIG.get('raw'):
        args.raw = True
    if CONFIG.get('calibre_polish'):
        args.calibre = True
    if CONFIG.get('kindle_fix'):
        args.kindle = True
    if CONFIG.get('make_nav') is False:
        args.no_nav = True

    records = {}
    if args.cookies:
        if not os.path.isfile(args.cookies):
            print(
                f'warning: cookies file not found: {args.cookies!r}',
                file=sys.stderr,
            )
        elif os.path.getsize(args.cookies) == 0:
            print(
                f'warning: cookies file is empty: {args.cookies!r}',
                file=sys.stderr,
            )
        else:
            records.update(load_cookies(args.cookies))
    if args.jwt:
        records['orm-jwt'] = _make_record('orm-jwt', args.jwt, secure=True)

    # Consent if there is no usable session material
    has_jwt = bool(records.get('orm-jwt', {}).get('value'))
    has_any_cookie = bool(records)
    if not has_jwt and not has_any_cookie:
        print(
            'warning: no valid cookies or orm-jwt found.\n'
            '  Without authentication the API usually returns little or no '
            'book content, and any EPUB produced may be empty or partial.',
            file=sys.stderr,
        )
        if not args.webview:
            if not ask_continue(
                'Are you sure you want to continue without valid cookies '
                'or orm-jwt? This may generate a partial EPUB.',
                assume_yes=args.yes,
            ):
                _die(
                    'Aborted. Export cookies from a logged-in browser '
                    '(Cookie-Editor → cookies.json) or pass --jwt / --webview.'
                )
        else:
            print(
                '  --webview is set; a login window can supply cookies next.',
                file=sys.stderr,
            )
    elif has_any_cookie and not has_jwt:
        print(
            'warning: cookies were loaded but orm-jwt is missing.\n'
            '  The session may be incomplete; download can still fail or '
            'be partial.',
            file=sys.stderr,
        )
        if not ask_continue(
            'Continue without orm-jwt?',
            assume_yes=args.yes,
        ):
            _die(
                'Aborted. Re-export cookies from an active logged-in tab '
                'so orm-jwt is included, or pass --jwt.'
            )

    webview_profile = args.webview_profile or (
        os.path.join(os.path.dirname(os.path.abspath(args.cookies)),
                      '.oreilly_webview_profile')
        if args.cookies else 'oreilly_webview_profile'
    )

    def _run_webview_login():
        print(
            "Opening a browser window to learning.oreilly.com - log in "
            "there as usual. The window will close itself once a valid "
            f'session is detected. Reusing/saving its profile at '
            f'"{webview_profile}".'
        )
        fresh = get_cookies_via_webview(webview_profile)
        if fresh:
            for name, value in fresh.items():
                records[name] = _make_record(name, value)
            if args.cookies:
                with open(args.cookies, 'w') as f:
                    json.dump(list(records.values()), f, indent=2)
                print(f'  got a live session; saved cookies to {args.cookies}')
            else:
                print('  got a live session (pass --cookies to save it for next time)')
            return True
        print('  webview closed (or timed out) without a live session appearing.')
        return False

    # No cookies at all and --webview was given: nothing to check yet, so
    # go straight to logging in rather than making a doomed anonymous
    # request first.
    if args.webview and not records:
        _run_webview_login()

    # ---- build job list (single book_id and/or --books file) ----
    jobs = []  # list of (book_id, title)
    if args.books:
        if not os.path.isfile(args.books):
            _die(f'error: books file not found: {args.books!r}')
        jobs.extend(list(parse_books_file(args.books)))
        log.info('loaded %d book(s) from %s', len(jobs), args.books)
    if args.book_id:
        if not str(args.book_id).isdigit():
            _die(
                f'error: book id must be digits only, got {args.book_id!r}\n'
                f'  Example: 9781617295355\n'
                + report_hint()
            )
        # Avoid duplicating if the same id is also in the books file
        if not any(j[0] == args.book_id for j in jobs):
            jobs.insert(0, (args.book_id, ''))
    if not jobs and getattr(args, 'save_options', False):
        # Allow saving defaults without downloading
        save_options(args)
        print('No books specified — options saved only.')
        return

    if not jobs:
        _die(
            'error: no books to download.\n'
            '  Pass a book id:  python3 oreilly_downloader.py 9781617295355 '
            '--cookies cookies.json\n'
            '  Or a list file:  python3 oreilly_downloader.py --books books.txt '
            '--cookies cookies.json\n'
            f'  Or create {CONFIG.get("books_file", "books.txt")!r} '
            f'(CONFIG books_file) with lines like:\n'
            '    9781617295355 # "Math for Programmers"'
        )

    out_dir = args.output_dir or '.'
    ensure_writable_dir(out_dir, 'output')
    if args.cookies:
        try:
            ensure_writable_file_parent(args.cookies, 'cookies')
        except SystemExit:
            raise
        if os.path.exists(args.cookies) and not os.access(args.cookies, os.R_OK):
            _die(
                f'error: cannot read cookies file {args.cookies!r}\n'
                f'  Permission denied — check file ownership and storage access.'
            )
    if args.calibre and not shutil.which('ebook-convert'):
        _die(
            'error: --calibre was set but ebook-convert was not found on PATH.\n'
            f'  Install Calibre from the official website: {CALIBRE_URL}\n'
            '  Then ensure ebook-convert is on your PATH, or omit --calibre.\n'
            '  Linux: install the "calibre" package, or use the official\n'
            '  binary tarball. Windows: use the official installer and\n'
            '  reopen the terminal so PATH updates apply.'
        )

    # Shared session headers (CONFIG may add extras)
    session_headers = dict(DEFAULT_HEADERS)
    session_headers.update(CONFIG.get('extra_headers') or {})

    # Network + API version probe (skip only if CONFIG check_connectivity False)
    await run_connectivity_checks(session_headers, assume_yes=args.yes)

    any_failed = False
    for job_index, (book_id, book_title) in enumerate(jobs, 1):
        args.book_id = book_id
        print(f'\n======== [{job_index}/{len(jobs)}] {book_id}'
              + (f'  ({book_title})' if book_title else '')
              + ' ========')
        log.info('starting book %s title=%r', book_id, book_title)

        suffix = CONFIG.get('output_suffix') or ''
        if args.raw:
            base_name = f'{book_id}-raw{suffix}.epub'
        else:
            base_name = f'{book_id}{suffix}.epub'
        filename = os.path.join(out_dir, base_name)
        tmp_filename = f'{filename}.partial'

        ensure_writable_dir(os.path.join(args.cache_dir, str(book_id)), 'cache')
        log.debug('preflight OK: output=%s cache=%s', filename, args.cache_dir)

        try:
            await _download_one_book(
                args, records, session_headers, filename, tmp_filename,
                webview_login=_run_webview_login,
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            any_failed = True
            write_error_log(exc, context=f'book_id={book_id}')
            print(f'→ FAIL  {book_id} (continuing with next book)')
            continue

        # Rename to sanitised title when provided
        if book_title:
            safe = _sanitize_filename(book_title)
            if args.raw:
                final = os.path.join(out_dir, f'{safe}-raw{suffix}.epub')
            else:
                final = os.path.join(out_dir, f'{safe}{suffix}.epub')
            if final != filename and os.path.isfile(filename):
                if os.path.exists(final):
                    print(f'  warning: {final} exists — keeping {filename}')
                else:
                    os.replace(filename, final)
                    print(f'  renamed → {final}')
                    filename = final

        if args.calibre:
            try:
                polish_with_calibre(filename, required=True)
            except SystemExit:
                raise
            except Exception as exc:  # noqa: BLE001
                any_failed = True
                write_error_log(exc, context=f'calibre book_id={book_id}')

    if any_failed:
        sys.exit(1)

    # Persist options when requested (or CONFIG auto_save_options)
    if getattr(args, 'save_options', False) or CONFIG.get('auto_save_options'):
        save_options(args)

    return


async def _download_one_book(args, records, session_headers, filename, tmp_filename,
                             webview_login=None):
    """Download a single book into filename (via tmp_filename)."""
    try:
        with zipfile.ZipFile(tmp_filename, 'w') as zfh:
            timeout = aiohttp.ClientTimeout(
                total=float(CONFIG.get('http_timeout_total', 120)),
                connect=float(CONFIG.get('http_timeout_connect', 30)),
                sock_read=float(CONFIG.get('http_timeout_sock_read', 90)),
            )
            async with aiohttp.ClientSession(
                raise_for_status=True,
                headers=session_headers,
                timeout=timeout,
                # SSL verification stays on (aiohttp default). Never disable
                # certificate checks in this script.
            ) as session:
                apply_cookies_to_session(session, records)
                if not records:
                    print('No cookies/JWT provided. Continuing without…')
                else:
                    rem = _jwt_remaining(records.get('orm-jwt', {}).get('value', ''))
                    jwt_definitely_expired = rem is not None and rem <= 0
                    if rem is not None:
                        print(
                            "  orm-jwt's own expiry claim: "
                            + (f'valid for ~{rem // 60}m more' if rem > 0
                               else f'expired {-rem}s ago')
                        )

                    ok, status, detail = await check_auth(session)
                    used_webview = False
                    if not ok and args.webview and webview_login:
                        print(
                            f'Authentication check failed: '
                            f'HTTP {status if status is not None else "no response"} '
                            f'- {detail}'
                        )
                        if webview_login():
                            apply_cookies_to_session(session, records)
                            rem = _jwt_remaining(
                                records.get('orm-jwt', {}).get('value', '')
                            )
                            jwt_definitely_expired = rem is not None and rem <= 0
                            ok, status, detail = await check_auth(session)
                            used_webview = True

                    if ok:
                        print(
                            'Authentication successful.'
                            + (' (via webview login)' if used_webview else '')
                        )
                        rem = _jwt_remaining(
                            _best_jwt_from_jar(session)
                            or records.get('orm-jwt', {}).get('value', '')
                        )
                        if rem is not None:
                            if rem <= 0:
                                print(
                                    f'  JWT expired {abs(rem)}s ago '
                                    f'(server may still refresh via orm-rt)'
                                )
                            else:
                                print(f'  JWT valid for ~{rem // 60}m')
                    else:
                        print(
                            f'Authentication check failed: '
                            f'HTTP {status if status is not None else "no response"} '
                            f'- {detail}'
                        )
                        if jwt_definitely_expired:
                            print(
                                '  The expiry claim above confirms this token '
                                'has genuinely expired - re-export cookies '
                                'from an active, logged-in browser tab'
                                + (
                                    ', or try --webview to log in directly.'
                                    if not args.webview else '.'
                                )
                            )
                        elif status in (401, 403) and _uses_akamai_bot_manager(records):
                            print(
                                '  This site is protected by Akamai Bot Manager. '
                                'A 401/403 here is not reliable evidence the login '
                                'itself expired. Try again shortly'
                                + (
                                    ', or pass --webview.'
                                    if not args.webview else '.'
                                )
                            )
                        elif args.cookies:
                            print(
                                '  Re-export cookies from an active, '
                                'logged-in browser tab and try again'
                                + (
                                    ', or pass --webview.'
                                    if not args.webview else '.'
                                )
                            )

                try:
                    await fetch_book(
                        args.book_id, zfh, session,
                        concurrency=args.concurrency,
                        cache_dir=args.cache_dir,
                        force=args.force,
                        make_nav=not args.no_nav,
                        raw=args.raw,
                        kindle=args.kindle,
                    )
                finally:
                    if args.cookies:
                        save_cookies(args.cookies, records, session)
                        print(f'saved current cookies to {args.cookies}')

        os.replace(tmp_filename, filename)
        print(f'created {filename}')
    except (KeyboardInterrupt, asyncio.CancelledError):
        print('\nInterrupted – partial EPUB discarded. Cookies were saved.')
        try:
            if os.path.exists(tmp_filename):
                os.unlink(tmp_filename)
        except OSError:
            pass
        raise
    except BaseException:
        try:
            if os.path.exists(tmp_filename):
                os.unlink(tmp_filename)
        except OSError:
            pass
        raise


def _sanitize_filename(title):
    """Make a title safe for Windows / Linux / Android filenames.

    Incorporates practical rules from the classic safaribooks downloader:
    - Long "Title: Subtitle …" strings keep only the part before the first
      colon when the colon appears after position 15 (avoids huge filenames).
    - On Windows, a short leading colon form is turned into a comma.
    - A broader set of unsafe characters is replaced with underscore.
    """
    import re as _re
    s = str(title or '')
    s = ''.join(ch for ch in s if ord(ch) >= 32 and ord(ch) != 127)

    if ':' in s:
        idx = s.index(':')
        if idx > 15:
            s = s.split(':', 1)[0]
        elif os.name == 'nt' or sys.platform.startswith('win'):
            s = s.replace(':', ',')
        else:
            s = s.replace(':', '_')

    for ch in '~#%&*{}\\<>?/`\'"|+;:':
        if ch in s:
            s = s.replace(ch, '_')

    s = _re.sub(r'\s+', ' ', s).strip(' .')
    if not s:
        s = 'book'
    upper = s.upper()
    if upper in {'CON', 'PRN', 'AUX', 'NUL'} or _re.match(
            r'^(COM|LPT)[0-9]$', upper):
        s = s + '_book'
    if len(s) > 180:
        s = s[:180].rstrip(' .')
    return s



def main():
    """Entry point with unknown-error catch-all → timestamped error log."""
    # Book titles are not always printable in the legacy Windows console
    # code page (download_v2 tip).
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print('\nInterrupted.', file=sys.stderr)
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — last-resort unknown errors
        write_error_log(exc, context='main')
        sys.exit(1)


if __name__ == '__main__':
    main()
