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
    command again; already-downloaded files are re-fetched too (the script
    always rebuilds the .epub from scratch) but that's normally quick.

OUTPUT
    A single <book_id>.epub file in the current directory, containing
    every file the O'Reilly API lists for that book, converted to valid
    XHTML content documents with all internal links, stylesheets, and
    images rewritten to work as a normal, self-contained EPUB.
"""

import argparse
import asyncio
import base64
import json
import os
import posixpath
import random
import re
import sys
import time
import zipfile

import aiohttp
import yarl
from http.cookies import SimpleCookie
from lxml import etree
from lxml import html as lhtml

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

DEFAULT_CONCURRENCY = 8

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
    """
    consecutive_403 = 0
    for attempt in range(1, max_attempts + 1):
        try:
            async with session.get(url) as r:
                r.raise_for_status()
                consecutive_403 = 0
                return await r.read()
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


CONTAINER = b"""<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
    <rootfiles>
        <rootfile full-path="EPUB/content.opf" media-type="application/oebps-package+xml"/>
    </rootfiles>
</container>
"""  # noqa

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

    Values that don't start with root_path (external URLs, fragment-only
    anchors, data: URIs, etc.) are left untouched.
    """
    if not value.startswith(root_path):
        return value
    return _relpath(value.removeprefix(root_path), current_dir)


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


def rewrite_opf(content, root_path, full_path):
    """Rewrite href attributes inside the OPF package document so they
    become correct relative paths after the root_path prefix is removed.
    Also normalises the package so it remains valid EPUB after renaming
    the file itself to content.opf.
    """
    if isinstance(content, str):
        content = content.encode('utf-8')
    try:
        tree = etree.fromstring(content)
    except etree.XMLSyntaxError:
        # Fall back to leaving the OPF untouched rather than dropping it.
        return content
    current_dir = posixpath.dirname(full_path)
    for el in tree.iter():
        href = el.get('href')
        if href:
            el.set('href', _resolve_ref(href, root_path, current_dir))
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
    tree = lhtml.fromstring(s, parser=lhtml.HTMLParser(encoding=None))

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


async def fetch_book(book_id, zfh, session, concurrency=DEFAULT_CONCURRENCY):
    root_path = f'/api/v2/epubs/urn:orm:book:{book_id}/files/'

    zfh.writestr('mimetype', b'application/epub+zip', compress_type=zipfile.ZIP_STORED)
    zfh.writestr('META-INF/container.xml', CONTAINER)

    # Collect the *complete* file listing across every paginated results page
    # up front, before downloading anything. We need the full set of
    # stylesheets in the book (see to_xhtml's stylesheet backfill above)
    # before we can correctly process any single HTML page.
    results = []
    url = BASE_URL + root_path
    while url:
        print(f'listing {url}')
        data = json.loads(await get_with_retry(session, url))
        results.extend(data.get('results', []))
        url = data.get('next')

    css_paths = [r['full_path'] for r in results if r['full_path'].endswith('.css')]

    failed = []

    async def download(result, sem):
        full_path = result['full_path']
        async with sem:
            # A little jitter between requests avoids bursty, all-at-once
            # request patterns that are more likely to trip rate limiting.
            await asyncio.sleep(random.uniform(0, 0.2))
            try:
                content = await get_with_retry(session, result['url'])
            except (aiohttp.ClientResponseError, RuntimeError) as exc:
                print(f'FAILED to download {full_path}: {exc}')
                failed.append(full_path)
                return

        # A network failure is handled above without derailing the rest of
        # the book (that's the whole point of get_with_retry), but a
        # processing failure - a chapter with markup lxml can't parse, a
        # stylesheet in an encoding other than UTF-8 - previously wasn't:
        # it propagated out of this coroutine and through the unguarded
        # asyncio.gather below, aborting every other in-flight download
        # too. One bad file should be reported and skipped like any other
        # failure, not take the whole run down with it.
        try:
            if full_path.endswith(HTML_EXTENSIONS):
                content = to_xhtml(content, root_path, full_path, css_paths)
            elif full_path.endswith('.css'):
                content = rewrite_css(content, root_path, full_path)
            elif full_path.endswith('.opf'):
                content = rewrite_opf(content, root_path, full_path)
        except Exception as exc:  # noqa: BLE001 - skip just this file
            print(f'FAILED to process {full_path}: {exc!r}')
            failed.append(full_path)
            return

        # The API doesn't always name the package document "content.opf"
        # (e.g. it may be "9780138308667.opf") but container.xml above
        # always points at "EPUB/content.opf" - write it under that fixed
        # name so the two agree regardless of what the API calls it.
        if full_path.endswith('.opf'):
            full_path = 'content.opf'

        zfh.writestr(f'EPUB/{full_path}', content)

    # Unbounded concurrency across a whole book can trip the API's rate
    # limiting, which shows up as random missing/truncated pages rather than
    # a clean error. Cap how many requests are in flight at once.
    sem = asyncio.Semaphore(max(1, concurrency))

    print(f'downloading {len(results)} files (concurrency={concurrency})')
    await asyncio.gather(*[download(result, sem) for result in results])

    if failed:
        print()
        print(f'WARNING: {len(failed)} of {len(results)} files failed to '
              f'download and are missing from the epub:')
        for full_path in failed:
            print(f'  {full_path}')
        print('This is usually transient rate-limiting or an expired JWT - '
              'try running again (see --cookies for something more durable '
              'than passing --jwt by hand each time).')


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


async def amain():
    parser = argparse.ArgumentParser(
        prog='oreilly_downloader.py',
        description=(
            "Download a book from O'Reilly learning as a standalone .epub "
            "file. See the top of this script (or run pydoc on it) for the "
            "full walkthrough of getting cookies.json set up."
        ),
        epilog=(
            "example:\n"
            "  python3 oreilly_downloader.py 9781633437777 --cookies cookies.json\n"
            "\n"
            "book_id is the digits in the book's learning.oreilly.com URL,\n"
            "e.g. .../library/view/some-book/9781633437777/ -> 9781633437777.\n"
            "\n"
            "cookies.json: export all cookies for learning.oreilly.com with a\n"
            "browser extension (Cookie-Editor, EditThisCookie, ...) while\n"
            "logged in. This script reads that export directly and rewrites\n"
            "the same file afterwards, so normally you only need to re-export\n"
            "again once the whole login session has actually expired, not\n"
            "before every single run."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('book_id', help=(
        "the numeric book id from the book's learning.oreilly.com URL"
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
    args = parser.parse_args()

    records = {}
    if args.cookies:
        records.update(load_cookies(args.cookies))
    if args.jwt:
        records['orm-jwt'] = _make_record('orm-jwt', args.jwt, secure=True)

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

    filename = f'{args.book_id}.epub'
    # Write to a temporary sibling first, then atomically rename. This avoids
    # leaving a truncated/corrupt .epub if the process is interrupted while
    # the ZipFile context manager is still open.
    tmp_filename = f'{args.book_id}.epub.partial'

    try:
        with zipfile.ZipFile(tmp_filename, 'w') as zfh:
            async with aiohttp.ClientSession(
                raise_for_status=True,
                headers=DEFAULT_HEADERS,
            ) as session:
                apply_cookies_to_session(session, records)
                if not records:
                    print('No cookies/JWT provided. Continuing without…')
                else:
                    # Decoded locally, zero requests made - this can never
                    # be confused with rate-limiting, an Akamai challenge,
                    # or any other transient network condition, unlike
                    # everything below it. Print it unconditionally, before
                    # the network even gets a chance to muddy the picture.
                    rem = _jwt_remaining(records.get('orm-jwt', {}).get('value', ''))
                    jwt_definitely_expired = rem is not None and rem <= 0
                    if rem is not None:
                        print(f'  orm-jwt\'s own expiry claim: '
                              + (f'valid for ~{rem // 60}m more' if rem > 0
                                 else f'expired {-rem}s ago'))

                    ok, status, detail = await check_auth(session)
                    used_webview = False
                    if not ok and args.webview:
                        print(f'Authentication check failed: '
                              f'HTTP {status if status is not None else "no response"} '
                              f'- {detail}')
                        if _run_webview_login():
                            # The session's jar was already seeded from the
                            # old records; re-apply now that records has
                            # been updated in place with the fresh ones,
                            # rather than opening a second session.
                            apply_cookies_to_session(session, records)
                            rem = _jwt_remaining(records.get('orm-jwt', {}).get('value', ''))
                            jwt_definitely_expired = rem is not None and rem <= 0
                            ok, status, detail = await check_auth(session)
                            used_webview = True

                    if ok:
                        print('Authentication successful.'
                              + (' (via webview login)' if used_webview else ''))
                        rem = _jwt_remaining(
                            _best_jwt_from_jar(session)
                            or records.get('orm-jwt', {}).get('value', '')
                        )
                        if rem is not None:
                            if rem <= 0:
                                print(f'  JWT expired {abs(rem)}s ago '
                                      f'(server may still refresh via orm-rt)')
                            else:
                                print(f'  JWT valid for ~{rem // 60}m')
                    else:
                        print(f'Authentication check failed: '
                              f'HTTP {status if status is not None else "no response"} '
                              f'- {detail}')
                        if jwt_definitely_expired:
                            print(
                                '  The expiry claim above confirms this token '
                                'has genuinely expired - re-export cookies '
                                'from an active, logged-in browser tab' + (
                                    ', or try --webview to log in directly.'
                                    if not args.webview else '.'
                                )
                            )
                        elif status in (401, 403) and _uses_akamai_bot_manager(records):
                            print(
                                '  This site is protected by Akamai Bot Manager '
                                '(the bm_*/_abck cookies). A 401/403 here is not '
                                'reliable evidence the login itself expired - '
                                'Akamai can reject a request that merely looks '
                                'automated even with a perfectly valid session, '
                                'and let an identical request through moments '
                                'later. Since the expiry claim above still '
                                'shows time remaining (or was unreadable), the '
                                'cookies may well still be good - try again '
                                'shortly' + (
                                    ', or pass --webview to log in directly '
                                    'in a real browser window.'
                                    if not args.webview else
                                    ' (a --webview login attempt just failed '
                                    'to produce a live session too).'
                                )
                            )
                        elif args.cookies:
                            print(
                                '  Re-export cookies from an active, '
                                'logged-in browser tab and try again' + (
                                    ', or pass --webview to log in directly.'
                                    if not args.webview else '.'
                                )
                            )

                try:
                    await fetch_book(
                        args.book_id, zfh, session,
                        concurrency=args.concurrency,
                    )
                finally:
                    # Save even on failure/Ctrl-C part-way through: whatever
                    # cookie state we ended up with is still worth keeping for
                    # next time.
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
        sys.exit(130)
    except BaseException:
        try:
            if os.path.exists(tmp_filename):
                os.unlink(tmp_filename)
        except OSError:
            pass
        raise


if __name__ == '__main__':
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        sys.exit(130)
