**AI development disclosure:** This project was developed with assistance from the free versions of ChatGPT, Grok, and Claude (LLMs), with the user providing ideas and testing while the AIs and user collaboratively suggested, generated, reviewed, and refined code and solutions.

---

# O'Reilly EPUB downloader

**Version:** 1.2.0

O'Reilly Learning (formerly Safari Books Online) serves its library through a web reader. This project downloads the individual files that make up a book **you already have access to** and rebuilds them into a normal, standalone `.epub` you can open in any reader — desktop, mobile, e-ink, or accessibility tools the built-in reader may not support well.

| | |
| --- | --- |
| **Main script** | `oreilly_downloader.py` |
| **Batch helper** | `batch_download.sh` |
| **Book lists** | `books.txt` / `sources.txt` |
| **Report bugs** | [https://github.com/official-kandoamoa](https://github.com/official-kandoamoa) (open a GitHub Issue) |

Before any use, please read the [O'Reilly Terms of Service](https://learning.oreilly.com/terms/). This tool is intended for **personal, offline access** to titles covered by your legitimate subscription — **not** for redistribution or sharing copyrighted files.

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
13. [Security defaults](#security-defaults)
14. [Safety checks and prompts](#safety-checks-and-prompts)
15. [Logging and error reports](#logging-and-error-reports)
16. [Calibre polish](#calibre-polish)
17. [How it works](#how-it-works)
18. [Troubleshooting](#troubleshooting)
19. [Limitations](#limitations)
20. [Project files](#project-files)
21. [Similar projects](#similar-projects)
22. [Contributing](#contributing)

---

## Features

### Core download and EPUB build

- Lists every file the Learning API exposes for a book and downloads them concurrently (bounded by `--concurrency`).
- Rewrites HTML / XHTML / **HTM**, CSS, OPF, and NCX so links no longer point at live `/api/v2/...` URLs.
- Produces a standards-shaped EPUB container: `mimetype`, `META-INF/container.xml`, package under `EPUB/`.
- Renames the package document to `content.opf` in normal mode; keeps the original name in `--raw` mode.
- Synthesises an **EPUB3 `nav.xhtml`** from the NCX when the package only has an EPUB2 toc (disable with `--no-nav`).
- Ensures `dcterms:modified` on the package document for EPUB 3 validity.
- Strips residual Calibre production metadata when present in upstream packages.
- Progress reporting (`N/M files`, including cache hits).
- Atomic write via a `.partial` file, then rename — interrupted runs do not leave a half-written EPUB.

### Authentication and session

- Loads a full browser cookie export (domain, path, secure, httpOnly, expiry respected).
- Writes cookies back after each run so sessions can outlive a single short-lived `orm-jwt`.
- Optional `--jwt` for one-off use; optional `--webview` for interactive login (Akamai sensor cookies included).
- Explains 401/403 failures in light of local JWT expiry claims vs Akamai Bot Manager.

### Robustness

- Retries rate limits and transient errors (403/429/5xx, network blips).
- Empty HTTP 200 bodies are **not** cached (avoids poisoning the cache).
- Processing failures delete that file’s cache entry so the next run can recover.
- Preflight: site online check + **API v2** availability probe.
- Host allowlist, HTTP timeouts, per-response size cap.
- Path-traversal-safe cache keys.

### Convenience

- `--books` / `books.txt` multi-title lists with optional display titles.
- `--save-options` / `.oreilly_options.json` to remember `--cookies`, `--calibre`, etc.
- `--raw` archival dump; `--calibre` EPUB→EPUB polish via Calibre.
- No-argument invocation prints a quick reference; `--help` prints a detailed guide.
- Editable `CONFIG` dictionary at the top of the script for defaults.

### Batch companion

- `batch_download.sh` reads a list file, runs the downloader, renames to sanitised titles, optionally converts to PDF with Calibre while **keeping** the EPUB.

---

## Requirements

| Component | Notes |
| --- | --- |
| **Python** | **3.9+ recommended.** 3.7–3.8 allowed with a warning and confirmation. Below 3.7 is rejected. Newer than 3.13.x warns (untested). |
| **OS** | **Linux and Windows** are the supported targets. Other systems (e.g. macOS) show a warning and ask before continuing. |
| **Packages** | `aiohttp`, `lxml`, `yarl` (declared for `uv`; install with pip if needed). |
| **Optional: Calibre** | Provides `ebook-convert` for `--calibre` and batch PDF conversion. [https://calibre-ebook.com/](https://calibre-ebook.com/) |
| **Optional: pywebview** | Only for `--webview` interactive login (needs a real display). |

---

## Installation

### With `uv` (recommended)

The script carries inline dependency metadata. `uv run` installs what it needs on first use:

```bash
uv run oreilly_downloader.py 9781098148706 --cookies cookies.json
```

### With pip + python3

```bash
pip install aiohttp lxml yarl
# if your environment blocks system installs:
# pip install aiohttp lxml yarl --break-system-packages

python3 oreilly_downloader.py 9781098148706 --cookies cookies.json
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

   `https://learning.oreilly.com/library/view/some-book/9781098148706/`  
   → id is `9781098148706`.

4. **Download:**

```bash
python3 oreilly_downloader.py 9781098148706 --cookies cookies.json
```

You should see authentication status, a file listing, download progress, and finally:

```text
created 9781098148706.epub
```

5. **Optional — remember flags and polish with Calibre:**

```bash
python3 oreilly_downloader.py 9781098148706 --cookies cookies.json --calibre --save-options
```

Later:

```bash
python3 oreilly_downloader.py 9781098148706
# picks up saved --cookies and --calibre from .oreilly_options.json
```

---

## Authentication

### Why full `--cookies` beats a bare JWT

O'Reilly authenticates API calls primarily with the short-lived `orm-jwt` cookie (often under an hour). A full export also carries other cookies (for example refresh-related values and Akamai bot-manager cookies). The script:

- Sends each cookie only according to its domain/path/secure/expiry metadata.
- Skips cookies that are already expired at load time (with a warning).
- Writes the **current** jar back to the same file after the run, including any values the server rotated mid-download.

You can still pass `--jwt VALUE` alone or combined with `--cookies` (JWT overrides that one name).

### Cookie file formats

Accepted:

- Browser extension export: JSON **array** of objects with `name`, `value`, `domain`, `path`, etc.
- Simple object: `{ "orm-jwt": "...", "orm-rt": "..." }`

The file written back is the full list shape (extension-friendly).

### `--webview`

Use when exports keep failing or Akamai blocks scripted requests:

```bash
python3 oreilly_downloader.py 9781098148706 --cookies cookies.json --webview
```

Opens a native browser window, lets you log in normally, then captures cookies (including sensor cookies) and saves them to `--cookies` when provided. Requires a graphical session (not plain SSH without display forwarding).

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
python3 oreilly_downloader.py [BOOK_ID] [options]
python3 oreilly_downloader.py --books FILE [options]
```

| Argument | Description |
| --- | --- |
| `book_id` | Numeric id from the Learning URL. Optional when `--books` is set or a `CONFIG` books file exists. Must be digits only. |
| `--cookies PATH` | Path to cookie JSON. Preferred auth method. |
| `--jwt VALUE` | `orm-jwt` string only; short-lived. **Never** written to the saved-options file. |
| `--books FILE` | Download every valid line in FILE (see [Batch download](#batch-download)). |
| `--output-dir DIR` | Directory for finished EPUBs (default: current directory). |
| `--concurrency N` | Parallel file downloads (default: 8, maximum accepted: 64). |
| `--cache-dir PATH` | Cache root directory (default: `.oreilly_cache`). |
| `--force` | Ignore on-disk cache; re-fetch every file. |
| `--raw` | No content rewriting; output `<id>-raw.epub`. |
| `--calibre` | After a successful build, run Calibre `ebook-convert` (EPUB→EPUB). Exits early if `ebook-convert` is missing. |
| `--no-nav` | Do not synthesise EPUB3 `nav.xhtml` (ignored in `--raw` mode). |
| `--webview` | Interactive browser login when needed. |
| `--webview-profile PATH` | Directory for the embedded browser profile. |
| `--log PATH` | Write a DEBUG log to PATH (secrets redacted). |
| `-v`, `--verbose` | Print INFO-level messages to stderr. |
| `--save-options` | After success (or alone with flags), save common options to the options file. |
| `--print-options` | Print the saved options file and exit. |
| `--skip-connectivity` | Skip site / API v2 probes. |
| `-y`, `--yes` | Auto-confirm safety prompts (old/new Python, OS, missing cookies, weak API probe). |
| `--version` | Print version and exit. |
| `-h`, `--help` | Full help text including the detailed guide. |

Invalid options or bad value formats exit with code `2`, a short explanation, and a pointer to quick reference / full help / GitHub Issues.

---

## Output modes

### Default (processed) EPUB

- HTML/CSS/OPF/NCX rewritten for offline relative paths.
- Package document exposed as `EPUB/content.opf`.
- Optional nav synthesis and `dcterms:modified`.
- Output name: `<book_id>.epub` (or title-based name when using a list with titles).

### `--raw`

- File bytes stored exactly as returned by the API.
- Original paths preserved; `container.xml` points at the real `.opf`.
- Output: `<book_id>-raw.epub`.
- Absolute `/api/v2/...` links remain inside chapters unless you also run `--calibre` or convert manually.

### `--calibre`

Runs:

```bash
ebook-convert input.epub input.calibre.epub
# then replaces input.epub with the polished file
```

As root, sets `QTWEBENGINE_DISABLE_SANDBOX=1`. If Calibre is not installed, the script **exits before downloading** when `--calibre` was requested, and points you to [https://calibre-ebook.com/](https://calibre-ebook.com/).

---

## Batch download

### Using the Python script

`books.txt` (default name also in `CONFIG['books_file']`):

```text
# comments and blank lines ignored
9781098148706 # "Math for Programmers"
9781098148706 # 'Grokking Deep Learning'
9781098104030
```

Rules:

| Rule | Detail |
| --- | --- |
| Separator | A single `#` between id and optional title |
| Book id | Digits only |
| Multiple `#` | Line rejected (`test # test # test`) |
| Non-numeric id | Line rejected (`test # test`) |
| Titles | Optional quotes; sanitised for Windows / Linux / Android file names |

```bash
python3 oreilly_downloader.py --books books.txt --cookies cookies.json
python3 oreilly_downloader.py --books books.txt --cookies cookies.json --calibre
```

### Using `batch_download.sh`

```bash
chmod +x batch_download.sh
./batch_download.sh -f books.txt --cookies cookies.json
./batch_download.sh -f books.txt --cookies cookies.json --raw --calibre
./batch_download.sh -f books.txt --cookies cookies.json --no-pdf
```

Behaviour:

- Passes unknown flags through to `oreilly_downloader.py`.
- Renames `<id>.epub` → `<Sanitised Title>.epub` when a title was given.
- Optionally runs `ebook-convert` to PDF after each success; **keeps the EPUB**.
- Prefers `uv run` when `uv` is on `PATH`, else `python3` / `python`.
- Exit code `1` if any book failed; others still attempted.

Title sanitisation removes `\ / : * ? " < > |` and control characters, trims leading/trailing spaces and dots, adjusts Windows reserved names (`CON`, `PRN`, …), and caps length.

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
python3 oreilly_downloader.py 9781098148706   # uses saved defaults
```

- **CLI always wins** over the file.
- **`--jwt` is never saved.**
- Set `CONFIG['auto_save_options'] = True` to refresh the file after every successful run.
- You can save without downloading:  
  `python3 oreilly_downloader.py --cookies cookies.json --save-options`

---

## Configuration (`CONFIG`)

Near the top of `oreilly_downloader.py` is an editable dictionary. Change defaults without touching the rest of the code. **CLI flags override CONFIG.**

| Key | Role |
| --- | --- |
| `cookies_path` | Used when `--cookies` is omitted and the file exists |
| `cache_dir` | Download cache root |
| `concurrency` | Default parallel downloads |
| `books_file` | Default list file name |
| `error_log_dir` | Directory for crash logs |
| `make_nav` | Synthesise EPUB3 nav when possible |
| `calibre_polish` | Act as if `--calibre` were always passed |
| `raw` | Act as if `--raw` were always passed |
| `output_suffix` | Optional extra suffix before `.epub` |
| `extra_headers` | Extra HTTP headers (advanced) |
| `options_file` | Path for `--save-options` |
| `auto_save_options` | Save options after every success |
| `api_version` | Expected API major version (default `2`) |
| `check_connectivity` | Run site/API probes |
| `http_timeout_total` / `_connect` / `_sock_read` | aiohttp timeouts (seconds) |
| `max_response_bytes` | Per-file size cap (default 80 MiB; `0` = unlimited) |
| `allowed_hosts` | URL host allowlist suffixes |
| `cookies_file_mode` | Unix mode for saved cookies (default `0o600`) |

---

## Cache and re-runs

Raw API payloads are stored under:

```text
.oreilly_cache/<book_id>/...
```

- Non-empty cache files are reused unless `--force` is set.
- Empty 200 responses are never written.
- Failed processing removes that entry so a later run can refetch.
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

This script is built for **API version 2** (`/api/v2/epubs/urn:orm:book:…/files/`). If O'Reilly retires or renames the API, downloads will fail; adjust `CONFIG['api_version']` only if the project is updated to match, and [open an Issue](https://github.com/official-kandoamoa) if the site works in a browser but probes keep failing.

---

## Security defaults

| Control | Behaviour |
| --- | --- |
| TLS | Certificate verification remains enabled |
| Hosts | Only hosts under `allowed_hosts` (default `*.oreilly.com`) |
| Timeouts | Connect / read / total limits from CONFIG |
| Size | Default max 80 MiB per response |
| Cookies file | `chmod` to owner-only (`0600`) on Unix when saving |
| Logs | JWTs and long tokens redacted in `--log` and `error_logs/*` |
| CLI log | `--jwt` value not written to debug logs as plaintext |
| Subprocess | Calibre invoked as an argument list (no shell) |

---

## Safety checks and prompts

| Situation | Behaviour |
| --- | --- |
| Python &lt; 3.7 | Exit |
| Python 3.7–3.8 | Warning + confirm (recommended 3.9+) |
| Python newer than tested (3.13.x) | Warning + confirm |
| OS not Linux/Windows | Warning + confirm |
| Missing `aiohttp` / `lxml` / `yarl` | Exit with install commands |
| Unwritable output, cache, or cookies path | Exit with permission hint (storage permission, read-only FS, …) |
| `--calibre` but no `ebook-convert` | Exit before download + link to Calibre’s site |
| No cookies / no orm-jwt | Confirm before download |
| API probe failed | Explain + confirm |

Use `-y` / `--yes` to auto-accept prompts in CI or wrappers (use carefully).

---

## Logging and error reports

```bash
python3 oreilly_downloader.py 978… --cookies cookies.json --log debug.log --verbose
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

### Calibre EPUB conversion
**Important**: To ensure best quality of the output, I suggest you to always convert the `EPUB` obtained by the script to standard-`EPUB` with [Calibre](https://calibre-ebook.com/).

Even a carefully rewritten EPUB can benefit from Calibre’s normaliser (structure, media types, TOC quirks).

```bash
python3 oreilly_downloader.py 9781491958698 --cookies cookies.json --calibre
```

You can also use the command-line version of Calibre with `ebook-convert`, e.g.:

```bash
ebook-convert "9781491958698.epub" "9781491958698_CLEAR.epub"
```

After the execution, you can read the `9781491958698_CLEAR.epub` in every E-Reader and delete all other files.

The program offers also an option to ensure best compatibilities for who wants to export the `EPUB` to E-Readers like Amazon Kindle: `--kindle`, it blocks overflow on `table` and `pre` elements (see [example](#use-or-not-the---kindle-option)).  
In this case, I suggest you to convert the `EPUB` to `AZW3` with Calibre or to `MOBI`, remember in this case to select `Ignore margins` in the conversion options:  
  
![Calibre IgnoreMargins](https://github.com/lorenzodifuccia/cloudflare/raw/master/Images/safaribooks/safaribooks_calibre_IgnoreMargins.png "Select Ignore margins")  

Install from the official site: [https://calibre-ebook.com/](https://calibre-ebook.com/).  
Credit for documenting the polish workflow in similar tools: [lorenzodifuccia/safaribooks](https://github.com/lorenzodifuccia/safaribooks).

---

## How it works

1. **Bootstrap** — Python version floor, dependency imports, optional soft prompts after CLI parse.  
2. **Resolve jobs** — Single `book_id` and/or `--books` / CONFIG list; validate id format.  
3. **Connectivity** — Site + API v2 probes.  
4. **Authenticate** — Load cookies/JWT; optional webview; consent if unauthenticated.  
5. **List files** — Paginate `GET /api/v2/epubs/urn:orm:book:<id>/files/`.  
6. **Download** — Concurrent GETs with retries, allowlist, size cap, disk cache.  
7. **Transform** (unless `--raw`) — `to_xhtml`, CSS/OPF/NCX rewrite, nav, metadata cleanup.  
8. **Package** — Zip EPUB layout; atomic replace into the final path.  
9. **Optional** — Title rename, Calibre polish, save options, error isolation per book in multi mode.

---

## Troubleshooting

| Symptom | What to try |
| --- | --- |
| `missing required Python package(s)` | `pip install aiohttp lxml yarl` or use `uv run` |
| `cannot reach learning.oreilly.com` | Check network/VPN/DNS; try a browser; disable `--skip-connectivity` only for debugging |
| `API v2` unavailable / 404 | Site may have changed API; open an Issue with details |
| Auth failed, JWT claim still valid | Likely Akamai; retry later or `--webview` |
| Auth failed, JWT expired | Re-export cookies from a logged-in tab |
| Partial EPUB / missing images | Re-run same command (cache fills gaps); try `--force` if content is stale |
| `--calibre` errors at start | Install Calibre and ensure `ebook-convert` is on `PATH` |
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
| `oreilly_downloader.py` | Main downloader (CLI, CONFIG, cache, rewrite, safety) |
| `batch_download.sh` | Shell batch runner + title rename + optional PDF |
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
- Whether you used `--cookies`, `--webview`, `--raw`, `--calibre`
- A **redacted** log from `--log` (secrets are stripped, but review before uploading)
- The exact command line (omit JWT values)

---

*This software is provided as-is, for interoperability with content you are already licensed to access. Respect publishers’ rights and O'Reilly’s terms.*
