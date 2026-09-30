**AI development disclosure:** This project was developed with assistance from the free versions of ChatGPT, Grok, and Claude (LLMs), with the user providing ideas and testing while the AIs and user collaboratively suggested, generated, reviewed, and refined code and solutions.

---

# O'Reilly EPUB downloader

**Version:** 1.2.0

O'Reilly Learning (formerly Safari Books Online) serves its library through a web reader. This project downloads the individual files that make up a book **you already have access to** and rebuilds them into a normal, standalone `.epub` you can open in any reader — desktop, mobile, e-ink, or accessibility tools the built-in reader may not support well.

| | |
| --- | --- |
| **Main script** | `oreilly_downloader.py` (single-book **and** batch) |
| **Optional wrapper** | `batch_download.sh` (`-f FILE` → `--books FILE`) |
| **Book lists** | `books.txt` / `sources.txt` |
| **Report bugs** | [https://github.com/official-kandoamoa](https://github.com/official-kandoamoa) (open a GitHub Issue) |

Before any use, please read the [O'Reilly Terms of Service](https://learning.oreilly.com/terms/) (open in a normal browser if the link is blocked on a restricted network). This tool is intended for **personal, offline access** to titles covered by your legitimate subscription — **not** for redistribution or sharing copyrighted files.

```bash
python3 oreilly_downloader.py              # quick reference card
python3 oreilly_downloader.py --help       # full detailed help
python3 oreilly_downloader.py --version
```

---

## Table of contents

1. [Features](#features)
2. [Requirements](#requirements)
3. [Installation](#installation)
4. [Quick start](#quick-start)
5. [Authentication](#authentication)
6. [Command-line reference](#command-line-reference)
7. [Output modes](#output-modes)
8. [Batch download](#batch-download)
9. [Saved options](#saved-options)
10. [Configuration (`CONFIG`)](#configuration-config)
11. [Cache and re-runs](#cache-and-re-runs)
12. [Connectivity and API checks](#connectivity-and-api-checks)
13. [Failure handling](#failure-handling)
14. [Security defaults](#security-defaults)
15. [Safety checks and prompts](#safety-checks-and-prompts)
16. [Logging and error reports](#logging-and-error-reports)
17. [Calibre EPUB conversion](#calibre-epub-conversion)
18. [How it works](#how-it-works)
19. [Troubleshooting](#troubleshooting)
20. [Limitations](#limitations)
21. [Project files](#project-files)
22. [Similar projects](#similar-projects)
23. [Contributing](#contributing)

---

## Features

### Core download and EPUB build

- Lists every file the Learning **API v2** exposes for a book and downloads them concurrently (bounded by `--concurrency`, default 8, max 64).
- Rewrites HTML / XHTML / **HTM**, CSS, OPF, and NCX so links no longer point at live `/api/v2/...` URLs.
- Strips Akamai-injected `<script>`, site chrome `<link href="/…">`, and `#sec-overlay` from HTML responses.
- Fetches book metadata from `/api/v2/epubs/urn:orm:book:<id>/` to print title/authors and correct OPF title/language when the package file is wrong.
- Produces a standards-shaped EPUB container: `mimetype` (first, uncompressed), `META-INF/container.xml`, package under `EPUB/`.
- Renames the package document to `content.opf` in normal mode; keeps the original name in `--raw` mode.
- Synthesises an **EPUB3 `nav.xhtml`** from the NCX when the package only has an EPUB2 toc (disable with `--no-nav`).
- Ensures `dcterms:modified` on the package document for EPUB 3 validity.
- Strips residual Calibre production metadata when present in upstream packages.
- Progress reporting (`N/M files`, including cache hits).
- Atomic write via a `.partial` file, then rename — interrupted runs do not leave a half-written EPUB as the final name.

### Authentication and session

- Loads a full browser cookie export (domain, path, secure, httpOnly, expiry respected).
- Writes cookies back **atomically** (temp file + `os.replace`) after each run so sessions can outlive a single short-lived `orm-jwt`.
- For `orm-jwt`, saves `expirationDate` from the JWT’s own `exp` claim (avoids dropping a freshly rotated token as “already expired”).
- Optional `--jwt` for one-off use; optional `--webview` for interactive login (Akamai sensor cookies included).
- Explains 401/403 failures in light of local JWT expiry claims vs Akamai Bot Manager.

### Robustness

- Retries rate limits and transient errors (403/429/5xx) and **`aiohttp.ClientError`** (disconnects, truncated bodies, DNS, etc.).
- Honours **`Retry-After`** when the server sends it.
- **Shared session 403 counter** aborts further downloads for that book after persistent 403s (does not burn 10 retries × every file).
- Empty HTTP 200 bodies are **not** cached (avoids poisoning the cache).
- Processing failures delete that file’s cache entry so the next run can recover.
- Preflight: site online check + **API v2** availability probe.
- Host allowlist on every request **and** every redirect hop; HTTP timeouts; per-response size cap.
- Path-traversal-safe cache keys.
- If any listed file is still missing after retries, the book is a **failure** (not exit 0 with a partial EPUB).

### Convenience

- Zero or more book ids on the command line: `978A 978B`.
- `--books` / `books.txt` multi-title lists with optional display titles and automatic rename to sanitised titles.
- `--save-options` / `.oreilly_options.json` to remember `--cookies`, `--calibre`, etc. (**not** `--jwt`).
- `--raw` archival dump; `--calibre` EPUB→EPUB polish; `--kindle` overflow CSS; **`--pdf` opt-in** PDF via Calibre (EPUB always kept).
- No-argument invocation prints a quick reference; `--help` prints a detailed guide.
- Editable `CONFIG` dictionary at the top of the script for defaults.

### Batch companion

- Batch mode is **inside** `oreilly_downloader.py` (see [Batch download](#batch-download)).
- `batch_download.sh` is only a thin wrapper that maps `-f FILE` → `--books FILE` and prefers `uv run` when available.

---

## Requirements

| Component | Notes |
| --- | --- |
| **Python** | **3.9+ recommended.** 3.7–3.8: warning + confirmation (or `--yes`). Below 3.7: rejected. Newer than 3.13.x: **warning only** (continues). |
| **OS** | **Linux and Windows** are the primary targets. Other systems (e.g. macOS): **warning only** (continues). |
| **Packages** | `aiohttp`, `lxml`, `yarl` (declared for `uv`; install with pip if needed). |
| **Optional: Calibre** | Provides `ebook-convert` for `--calibre` and `--pdf`. [https://calibre-ebook.com/](https://calibre-ebook.com/) |
| **Optional: pywebview** | Only for `--webview` interactive login (needs a real display). [https://pywebview.flowrl.com/](https://pywebview.flowrl.com/) |

---

## Installation

### With `uv` (recommended)

The script carries inline dependency metadata. `uv run` installs what it needs on first use:

```bash
uv run oreilly_downloader.py 9780000000003 --cookies cookies.json
```

### With pip + python3

```bash
pip install aiohttp lxml yarl
# if your environment blocks system installs:
# pip install aiohttp lxml yarl --break-system-packages

python3 oreilly_downloader.py 9780000000003 --cookies cookies.json
```

### Optional tools

```bash
# Calibre: use your OS installer or https://calibre-ebook.com/
# Confirm:
ebook-convert --version

# Webview login (GUI required):
pip install pywebview
# Linux may also need WebKitGTK, e.g.:
# sudo apt install python3-gi gir1.2-webkit2-4.0
```

---

## Quick start

1. **Log in** at [https://learning.oreilly.com](https://learning.oreilly.com) in a normal browser.
2. **Export cookies** for `learning.oreilly.com` to a file named `cookies.json`  
   (extensions such as [Cookie-Editor](https://cookie-editor.com/) or EditThisCookie work). Export **all** cookies for the site, not only `orm-jwt`.
3. **Find the book id** — the digit string in the book URL:

   `https://learning.oreilly.com/library/view/example-book/9780000000003/`  
   → id is `9780000000003`.

4. **Download:**

```bash
python3 oreilly_downloader.py 9780000000003 --cookies cookies.json
```

You should see authentication status, a file listing, download progress, and finally a created `.epub` in the current directory (or `--output-dir`).

5. **Optional — remember flags and polish with Calibre:**

```bash
python3 oreilly_downloader.py 9780000000003 --cookies cookies.json --calibre --save-options
```

Later:

```bash
python3 oreilly_downloader.py 9780000000003
# picks up saved --cookies and --calibre from .oreilly_options.json
```

---

## Authentication

### Why full `--cookies` beats a bare JWT

O'Reilly authenticates API calls primarily with the short-lived `orm-jwt` cookie (often under an hour). A full export also carries other cookies (for example refresh-related values and Akamai bot-manager cookies). The script:

- Sends each cookie only according to its domain/path/secure/expiry metadata.
- Skips cookies that are already expired at load time (with a warning).
- Writes the **current** jar back to the same file after the run, including any values the server rotated mid-download, using an **atomic** replace so a crash mid-write cannot truncate your only refresh token.
- For `orm-jwt`, stores `expirationDate` from the token’s own `exp` claim.

You can still pass `--jwt VALUE` alone or combined with `--cookies` (JWT overrides that one name).

### Cookie file formats

Accepted:

- Browser extension export: JSON **array** of objects with `name`, `value`, `domain`, `path`, etc.
- Simple object: `{ "orm-jwt": "...", "orm-rt": "..." }`

The file written back is the full list shape (extension-friendly), mode `0600` on Unix (`CONFIG['cookies_file_mode']`).

### `--webview`

Use when exports keep failing or Akamai blocks scripted requests:

```bash
python3 oreilly_downloader.py 9780000000003 --cookies cookies.json --webview
```

Opens a native browser window, lets you log in normally, then captures cookies (including sensor cookies) and saves them to `--cookies` when provided. Requires a graphical session (not plain SSH without display forwarding). Install with `pip install pywebview`; on Linux you may need WebKitGTK (see [pywebview installation notes](https://pywebview.flowrl.com/)). Profile data is stored under `--webview-profile` or a default folder next to your cookies file.

### Missing or empty credentials

If there is no usable cookie jar / `orm-jwt`, the script **does not** start downloading until you confirm:

```text
Are you sure you want to continue without valid cookies or orm-jwt?
This may generate a partial EPUB. [y/N]
```

Use `--webview` to supply a session, or `--yes` to skip prompts in automation (not recommended unless you know the risk).

---

## Command-line reference

```text
python3 oreilly_downloader.py [BOOK_ID ...] [options]
python3 oreilly_downloader.py --books FILE [options]
```

| Argument | Description |
| --- | --- |
| `BOOK_ID …` | Zero or more numeric ids from the Learning URL (digits only). Example: `9780000000001 9780000000002`. Optional when `--books` is set or a `CONFIG` books file exists. |
| `--cookies PATH` | Path to cookie JSON. Preferred auth method. |
| `--jwt VALUE` | `orm-jwt` string only; short-lived. **Never** written to the saved-options file. |
| `--books FILE` | Download every valid line in FILE (see [Batch download](#batch-download)). |
| `--output-dir DIR` | Directory for finished EPUBs/PDFs (default: current directory). |
| `--concurrency N` | Parallel file downloads (default: 8, maximum accepted: 64). |
| `--cache-dir PATH` | Cache root directory (default: `.oreilly_cache`). |
| `--force` | Ignore on-disk cache; re-fetch every file. |
| `--raw` | No content rewriting; output `<id>-raw.epub`. |
| `--calibre` | After a successful build, run Calibre `ebook-convert` (EPUB→EPUB). **Exits before download** if `ebook-convert` is missing. |
| `--kindle` | Inject CSS so `table` / `pre` wrap and do not overflow on narrow Kindle / E-Ink screens. Ignored with `--raw`. |
| `--pdf` | **Opt-in.** After each successful EPUB, also convert to PDF with Calibre. **Off by default.** EPUB is always kept. Missing Calibre → skip with a note (not a hard failure). |
| `--no-nav` | Do not synthesise EPUB3 `nav.xhtml` (ignored in `--raw` mode). |
| `--webview` | Interactive browser login when needed. |
| `--webview-profile PATH` | Directory for the embedded browser profile. |
| `--log PATH` | Write a DEBUG log to PATH (secrets redacted). |
| `-v`, `--verbose` | Print INFO-level messages to stderr. |
| `--save-options` | After success (or alone with flags), save common options to the options file. |
| `--print-options` | Print the saved options file and exit. |
| `--skip-connectivity` | Skip site / API v2 probes. |
| `-y`, `--yes` | Auto-confirm safety prompts (older Python, missing cookies, weak API probe). |
| `--version` | Print version and exit. |
| `-h`, `--help` | Full help text including the detailed guide. |

Invalid options or bad value formats exit with code `2`, a short explanation, and a pointer to quick reference / full help / GitHub Issues.

---

## Output modes

### Default (processed) EPUB

- HTML/CSS/OPF/NCX rewritten for offline relative paths.
- Akamai injection stripped; OPF title/language aligned with API metadata when available.
- Package document exposed as `EPUB/content.opf`.
- Optional nav synthesis and `dcterms:modified`.
- Output name: `<book_id>.epub` (or title-based name when using a list with titles).

### `--raw`

- File bytes stored exactly as returned by the API.
- Original paths preserved; `container.xml` points at the real `.opf`.
- Output: `<book_id>-raw.epub` (or `<title>-raw.epub`).
- Absolute `/api/v2/...` links remain inside chapters unless you also run `--calibre` or convert manually.
- `--kindle` has no effect in raw mode.

### `--calibre`

Runs:

```bash
ebook-convert input.epub input.calibre.epub
# then replaces input.epub with the polished file
```

As root, sets `QTWEBENGINE_DISABLE_SANDBOX=1`. If Calibre is not installed, the script **exits before downloading** when `--calibre` was requested, and points you to [https://calibre-ebook.com/](https://calibre-ebook.com/).

### `--kindle`

Injects `Styles/kindle-fix.css` and links it from every chapter so wide **tables** and **`<pre>`** blocks wrap on narrow screens (Kindle, other E-Ink). Also caps image width. Has no effect with `--raw`. See [Calibre EPUB conversion](#calibre-epub-conversion) for AZW3 tips.

### `--pdf` (opt-in)

```bash
python3 oreilly_downloader.py 9780000000003 --cookies cookies.json --pdf
```

After a successful EPUB (and optional title rename / `--calibre`), runs `ebook-convert` to `<same-stem>.pdf` with `--pretty-print`. The EPUB is **not** deleted. Default is **off**; enable with `--pdf` or `CONFIG['convert_pdf'] = True`.

---

## Batch download

Batch mode is **built into** `oreilly_downloader.py`. You do not need the shell script.

### Using the Python script

`books.txt` (default name also in `CONFIG['books_file']`):

```text
# comments and blank lines ignored
9780000000001 # "Example Book Title"
9780000000002 # 'Sample Training Guide'
9780000000004
```

Rules:

| Rule | Detail |
| --- | --- |
| Separator | A single `#` between id and optional title |
| Book id | Digits only |
| Multiple `#` | Line rejected (`jeleo # 83hd # heoo`) |
| Non-numeric id | Line rejected (`jfjd # ejsk`) |
| Titles | Optional quotes; sanitised for Windows / Linux / Android file names |

```bash
python3 oreilly_downloader.py --books books.txt --cookies cookies.json
python3 oreilly_downloader.py 9780000000001 9780000000002 --cookies cookies.json
python3 oreilly_downloader.py --books books.txt --cookies cookies.json --calibre --pdf
```

If one book fails, the script writes an error log, continues with the next book, and exits with code **1** if any book failed.

### Using `batch_download.sh`

Thin wrapper only (`-f` / `--file` → `--books`):

```bash
chmod +x batch_download.sh
./batch_download.sh -f books.txt --cookies cookies.json
./batch_download.sh -f books.txt --cookies cookies.json --raw --calibre --pdf
```

Prefers `uv run` when `uv` is on `PATH`, else `python3` / `python`.

### Title sanitisation

When a title is provided, the finished EPUB is renamed to `<sanitised-title>.epub` (or `<sanitised-title>-raw.epub` with `--raw`):

- Long `Title: Subtitle …` strings keep only the part before the first colon when that colon is after position 15 (safaribooks-style).
- Unsafe characters (`~ # % & * { } \ < > ? / ` ' " | + ; :` and path separators) become underscores.
- Control characters removed; whitespace collapsed; leading/trailing spaces and dots stripped.
- Windows reserved names (`CON`, `PRN`, `AUX`, `NUL`, `COM1`–`9`, `LPT1`–`9`) get a `_book` suffix.
- Length capped at 180 characters.

---

## Saved options

Avoid retyping common flags:

```bash
python3 oreilly_downloader.py --cookies cookies.json --calibre --concurrency 4 --save-options
```

Creates or updates `.oreilly_options.json` (path overridable via `CONFIG['options_file']`), for example:

```json
{
  "calibre": true,
  "concurrency": 4,
  "cookies": "cookies.json",
  "cache_dir": ".oreilly_cache",
  "output_dir": "."
}
```

```bash
python3 oreilly_downloader.py --print-options
python3 oreilly_downloader.py 9780000000003   # uses saved defaults
```

- **CLI always wins** over the file.
- **`--jwt` is never saved.**
- Set `CONFIG['auto_save_options'] = True` to refresh the file after every successful run.
- You can save without downloading:  
  `python3 oreilly_downloader.py --cookies cookies.json --save-options`

---

## Configuration (`CONFIG`)

Near the top of `oreilly_downloader.py` (search for `CONFIG = {`) is a plain Python dictionary. **Edit these values** to change defaults without rewriting CLI flags every time. Anything you pass on the command line still **overrides** `CONFIG`.

```python
CONFIG = {
    'cookies_path': 'cookies.json',
    'cache_dir': '.oreilly_cache',
    'concurrency': 8,
    'books_file': 'books.txt',
    'error_log_dir': 'error_logs',
    'make_nav': True,
    'calibre_polish': False,
    'raw': False,
    'kindle_fix': False,
    'convert_pdf': False,          # --pdf is opt-in; set True to always convert
    'output_suffix': '',
    'extra_headers': {},
    'http_timeout_total': 120,
    'http_timeout_connect': 30,
    'http_timeout_sock_read': 90,
    'max_response_bytes': 80 * 1024 * 1024,
    'allowed_hosts': ('oreilly.com', 'learning.oreilly.com'),
    'cookies_file_mode': 0o600,
    'api_version': 2,
    'check_connectivity': True,
    'options_file': '.oreilly_options.json',
    'auto_save_options': False,
}
```

### Paths and files

| Key | Type | Default | Detail |
| --- | --- | --- | --- |
| `cookies_path` | `str` | `'cookies.json'` | If you omit `--cookies` and this file **exists** in the working directory, it is loaded automatically. Does **not** create the file for you — export cookies from a browser first. |
| `cache_dir` | `str` | `'.oreilly_cache'` | Root directory for per-book raw API caches. Actual files live under `<cache_dir>/<book_id>/…`. Override with `--cache-dir`. Safe to delete anytime; the next run re-downloads. |
| `books_file` | `str` | `'books.txt'` | Default multi-book list. If you run with **no** book ids and **no** `--books`, and this file exists, it is used automatically. Format: `BOOK_ID` or `BOOK_ID # "Title"` per line. |
| `error_log_dir` | `str` | `'error_logs'` | Directory for crash logs named `error_log_YYYY_MM_DD_HHMMSS.log`. Created on demand. Logs redact JWTs and long tokens. |
| `options_file` | `str` | `'.oreilly_options.json'` | JSON file written by `--save-options` / read on startup as CLI defaults. |

### Download behaviour

| Key | Type | Default | Detail |
| --- | --- | --- | --- |
| `concurrency` | `int` | `8` | Parallel chapter/asset downloads. Lower (e.g. `3`–`4`) if you see many `403` responses. CLI: `--concurrency N` (max 64). |
| `raw` | `bool` | `False` | When `True`, every run behaves like `--raw`. Prefer CLI `--raw` for one-offs. |
| `make_nav` | `bool` | `True` | When `True` (and not raw), synthesise `nav.xhtml` from NCX if missing. `False` ≡ always `--no-nav`. |
| `calibre_polish` | `bool` | `False` | When `True`, always run Calibre EPUB→EPUB after a successful build (≡ always `--calibre`). Requires `ebook-convert` on `PATH` or the run aborts before download. |
| `kindle_fix` | `bool` | `False` | When `True`, always inject Kindle overflow CSS (≡ always `--kindle`). Ignored when raw. |
| `convert_pdf` | `bool` | **`False`** | When `True`, always convert each successful EPUB to PDF (≡ always `--pdf`). EPUB is kept. Missing Calibre → skip with a note. |
| `output_suffix` | `str` | `''` | Optional string inserted before `.epub` in **numeric** filenames only (e.g. `'_v2'` → `978…_v2.epub`). Title-based names are unaffected. |

### Network and security

| Key | Type | Default | Detail |
| --- | --- | --- | --- |
| `http_timeout_total` | `float` (seconds) | `120` | Overall timeout for a single HTTP request (`ClientTimeout.total`). |
| `http_timeout_connect` | `float` | `30` | Max time to establish the TCP/TLS connection. |
| `http_timeout_sock_read` | `float` | `90` | Max idle time while reading the response body. |
| `max_response_bytes` | `int` | `80 * 1024 * 1024` (80 MiB) | Maximum size of one downloaded file. Larger responses are aborted. Set `0` to disable the cap (not recommended on small disks). |
| `allowed_hosts` | `tuple` of `str` | `('oreilly.com', 'learning.oreilly.com')` | Request host must equal one of these or be a subdomain. Redirects are checked on each hop. |
| `cookies_file_mode` | `int` (octal) | `0o600` | Unix mode applied after writing cookies. Ignored on Windows. |
| `api_version` | `int` | `2` | Major version in API paths (`/api/v2/...`). Used by the connectivity probe. |
| `check_connectivity` | `bool` | `True` | When `True`, probe the site and API before downloading. `False` ≡ `--skip-connectivity`. |

### HTTP headers (advanced)

| Key | Type | Default | Detail |
| --- | --- | --- | --- |
| `extra_headers` | `dict` | `{}` | Merged on top of the script’s built-in `User-Agent` / `Referer` / etc. Leave empty unless diagnosing header-related blocks. |

### Saved-options automation

| Key | Type | Default | Detail |
| --- | --- | --- | --- |
| `auto_save_options` | `bool` | `False` | When `True`, after every **successful** multi-book run the script writes current common flags to `options_file`. **Never** stores `--jwt`. |

### Precedence (what wins?)

From lowest to highest priority:

1. Built-in script defaults  
2. Values in `CONFIG`  
3. Values loaded from `options_file` (`.oreilly_options.json`)  
4. Explicit command-line flags  

Example: `CONFIG['concurrency'] = 4`, options file has `"concurrency": 6`, CLI has `--concurrency 10` → **10** is used.

### Practical recipes

```python
# Always polish with Calibre + Kindle CSS; cookies next to the script
CONFIG['cookies_path'] = 'cookies.json'
CONFIG['calibre_polish'] = True
CONFIG['kindle_fix'] = True

# Also always emit PDF (only if you want this for every run)
CONFIG['convert_pdf'] = True

# Slow or flaky network
CONFIG['concurrency'] = 3
CONFIG['http_timeout_total'] = 300
CONFIG['http_timeout_sock_read'] = 180

# Remember last flags without typing --save-options
CONFIG['auto_save_options'] = True
```

---

## Cache and re-runs

Raw API payloads are stored under:

```text
.oreilly_cache/<book_id>/...
```

- Non-empty cache files are reused unless `--force` is set.
- Empty 200 responses are never written.
- Failed processing removes that entry so a later run can refetch.
- Path components that escape the cache root (`..`, absolute paths) are rejected.
- The EPUB is always rebuilt from cached (or fresh) bytes so rewriting stays consistent.
- Delete `.oreilly_cache/` or a single book subdirectory to reclaim disk space.

---

## Connectivity and API checks

Before downloading, unless `--skip-connectivity` or `CONFIG['check_connectivity'] = False`:

1. **Site probe** — `GET https://learning.oreilly.com/`  
   - Connection errors → treat as offline and exit with guidance (network, DNS, VPN, firewall).

2. **API v2 probe** — `GET /api/v2/` and `/api/v2/epubs/`  
   - **401 / 403 / 200** → API route still exists (auth may be required).  
   - **404** → path may have changed; warn and ask whether to continue.  
   - Timeouts / connection errors → unavailable.

This script is built for **API version 2** (`/api/v2/epubs/urn:orm:book:…/files/?limit=200`). If O'Reilly retires or renames the API, downloads will fail; adjust `CONFIG['api_version']` only when the project is updated to match, and [open an Issue](https://github.com/official-kandoamoa) if the site works in a browser but probes keep failing.

---

## Failure handling

| Situation | Behaviour |
| --- | --- |
| HTTP 403 / 429 / 5xx | Retry with backoff; use `Retry-After` when present |
| Network disconnect / payload errors | Retried as `aiohttp.ClientError` |
| Many 403s across the **whole book** | Shared session guard stops further requests for that book |
| File still missing after retries | Book fails; path listed on stderr |
| Any missing file after the download phase | **Not** treated as success — error raised for that book |
| Multi-book: one book fails | Error log written; remaining books still run; process exit code **1** if any failed |
| Redirect to non-allowlisted host | Request refused |

---

## Security defaults

These are always on unless you deliberately change `CONFIG` (there is **no** `--insecure` flag to disable TLS).

| Control | Behaviour |
| --- | --- |
| TLS | Certificate verification stays enabled (aiohttp default). The script never sets `ssl=False`. |
| Hosts | Only hosts under `CONFIG['allowed_hosts']` (default `oreilly.com` and `learning.oreilly.com`, including subdomains). Redirect hops are checked the same way. |
| Timeouts | `http_timeout_connect` / `http_timeout_sock_read` / `http_timeout_total` apply to every API request. |
| Size | Each response is capped by `max_response_bytes` (default 80 MiB). |
| Cookies file | Atomic write + Unix mode `cookies_file_mode` (default `0o600`). |
| Logs | JWTs (`eyJ…`) and long opaque tokens are replaced with `[REDACTED_…]` in `--log` files and `error_logs/*`. |
| CLI log | If you pass `--jwt`, the value is stored as `[REDACTED]` in structured debug logs. |
| Subprocess | `ebook-convert` is started with an argument list only (`shell=False`). |

---

## Safety checks and prompts

| Situation | Behaviour |
| --- | --- |
| Python &lt; 3.7 | Exit |
| Python 3.7–3.8 | Warning + confirm (recommended 3.9+); `--yes` skips confirm |
| Python newer than tested (3.13.x) | **Warning only** — continues |
| OS not Linux/Windows | **Warning only** — continues |
| Missing `aiohttp` / `lxml` / `yarl` | Exit with install commands |
| Unwritable output, cache, or cookies path | Exit with permission hint |
| `--calibre` but no `ebook-convert` | Exit before download + link to Calibre’s site |
| No cookies / no orm-jwt | Confirm before download |
| API probe failed | Explain + confirm |

Use `-y` / `--yes` to auto-accept interactive confirms in CI or wrappers (use carefully).

---

## Logging and error reports

```bash
python3 oreilly_downloader.py 9780000000003 --cookies cookies.json --log debug.log --verbose
```

- `--log` — DEBUG detail to a file (redacted).  
- `--verbose` — INFO on stderr.  

Unexpected exceptions create:

```text
error_logs/error_log_YYYY_MM_DD_HHMMSS.log
```

Contents include timestamp, Python version, script version, redacted argv, and traceback. The console message asks you to **open a GitHub Issue** at:

**https://github.com/official-kandoamoa**

---

## Calibre EPUB conversion

**Important:** For the best reader compatibility, polish the EPUB with [Calibre](https://calibre-ebook.com/) after download (structure, media types, TOC).

### Built-in `--calibre`

```bash
python3 oreilly_downloader.py 9780000000005 --cookies cookies.json --calibre
```

If `ebook-convert` is missing, the script exits **before** downloading and points you to the official installer.

### Built-in `--pdf` (opt-in)

```bash
python3 oreilly_downloader.py 9780000000005 --cookies cookies.json --pdf
```

Creates `9780000000005.pdf` beside the EPUB. Does not delete the EPUB. Off unless you pass `--pdf` or set `CONFIG['convert_pdf'] = True`.

### Manual conversion

```bash
ebook-convert "9780000000005.epub" "example_CLEAR.epub"
ebook-convert "9780000000005.epub" "9780000000005.pdf" --pretty-print
```

### `--kindle` (table / pre overflow)

For Amazon Kindle and other narrow E-Ink screens, wide tables and preformatted code often overflow or clip. **`--kindle`** injects `Styles/kindle-fix.css` that:

- Applies word-wrap / word-break broadly
- Sets `table` / `pre` overflow to unset and `white-space: pre-wrap`
- Caps `img` / `svg` to `max-width: 100%`

```bash
python3 oreilly_downloader.py 9780000000005 --cookies cookies.json --kindle
python3 oreilly_downloader.py 9780000000005 --cookies cookies.json --kindle --calibre
```

Ignored with `--raw`. You can also set `CONFIG['kindle_fix'] = True`.

When targeting Kindle specifically, convert the result to **AZW3** (or MOBI) in Calibre and enable **Ignore margins** in the conversion options:

![Calibre IgnoreMargins](https://github.com/lorenzodifuccia/cloudflare/raw/master/Images/safaribooks/safaribooks_calibre_IgnoreMargins.png "Select Ignore margins")

Install Calibre from [https://calibre-ebook.com/](https://calibre-ebook.com/).  
Workflow notes adapted from [lorenzodifuccia/safaribooks](https://github.com/lorenzodifuccia/safaribooks).

---

## How it works

1. **Bootstrap** — Python version floor, dependency imports, optional soft prompts after CLI parse.  
2. **Resolve jobs** — Positional book ids and/or `--books` / CONFIG list; validate id format.  
3. **Connectivity** — Site + API v2 probes.  
4. **Authenticate** — Load cookies/JWT; optional webview; consent if unauthenticated.  
5. **Per book** — Reset 403 guard; fetch metadata; list files; concurrent download with cache; rewrite (unless `--raw`); nav / OPF / Kindle CSS; zip; atomic rename; optional title rename; optional `--calibre`; optional `--pdf`.  
6. **Exit** — 0 only if every book succeeded; otherwise 1.

---

## Troubleshooting

| Symptom | What to try |
| --- | --- |
| `missing required Python package(s)` | `pip install aiohttp lxml yarl` or use `uv run` |
| `cannot reach learning.oreilly.com` | Check network/VPN/DNS; try a browser; use `--skip-connectivity` only for debugging |
| `API v2` unavailable / 404 | Site may have changed API; open an Issue with details |
| Auth failed, JWT claim still valid | Likely Akamai; retry later or `--webview` |
| Auth failed, JWT expired | Re-export cookies from a logged-in tab |
| Partial EPUB / “refusing to treat … as success” | Re-run same command (cache fills gaps); try `--force` if content is stale |
| Persistent 403 / session aborted | Re-export cookies; lower `--concurrency`; wait and retry |
| `--calibre` errors at start | Install Calibre and ensure `ebook-convert` is on `PATH` |
| `--pdf` does nothing | Flag is **opt-in** — pass `--pdf`; install Calibre |
| Permission denied writing files | Fix directory permissions; on Android/Termux grant storage access |
| Invalid sources line | Digits-only id; single `#`; see books.txt rules |
| Need support | Attach a redacted `--log` file and open an Issue at the GitHub link above |

---

## Limitations

- Does not bypass Akamai or other bot management; `--webview` uses a real browser instead of spoofing it.
- Only content your account is allowed to retrieve is available.
- Cookie lifetime follows the real login session; when the session is fully dead, export again or use `--webview`.
- Cache growth is unbounded until you delete `.oreilly_cache/`.
- `--raw` packages are archival; they may not open cleanly in all readers without Calibre or the default rewrite mode.
- This is not an official O'Reilly product.

---

## Project files

| File | Purpose |
| --- | --- |
| `oreilly_downloader.py` | Full downloader: CLI, batch, cache, rewrite, PDF, safety |
| `batch_download.sh` | Optional thin wrapper (`-f` → `--books`) |
| `books.txt` / `sources.txt` | Example book lists (`id # "title"`) |
| `cookies.json` | Your export (not shipped; create locally) |
| `.oreilly_options.json` | Created by `--save-options` |
| `.oreilly_cache/` | Per-book raw API cache |
| `error_logs/` | Timestamped crash reports |

---

## Similar projects

- [lorenzodifuccia/safaribooks](https://github.com/lorenzodifuccia/safaribooks) (Python)
- [hurlenko/orly](https://github.com/hurlenko/orly) (Rust)
- [jenni/obooks](https://github.com/jenni/obooks) (JavaScript)
- [rahulvramesh/oreilly-books-grabber](https://github.com/rahulvramesh/oreilly-books-grabber) (Go)

---

## Contributing

Fixes and carefully scoped features are welcome. Please open an Issue first for larger changes:

**https://github.com/official-kandoamoa**

When reporting a bug, include:

- Script version (`--version`)
- OS and Python version
- Whether you used `--cookies`, `--webview`, `--raw`, `--calibre`, `--pdf`, `--kindle`
- A **redacted** log from `--log` (secrets are stripped, but review before uploading)
- The exact command line (omit JWT values)

---

*This software is provided as-is, for interoperability with content you are already licensed to access. Respect publishers’ rights and O'Reilly’s terms.*
