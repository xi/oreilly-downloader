#!/usr/bin/env bash
# Batch-download O'Reilly books using oreilly_downloader.py
#
# Usage:
#   ./batch_download.sh -f sources.txt [--cookies cookies.json] [options...]
#   ./batch_download.sh BOOK_ID [BOOK_ID ...] [options...]
#
# sources.txt format (one entry per line):
#   9781617295355 # "Math for Programmers"
#   9781633437777 # "Some Other Book"
#   9781492052203
#
# Rules:
#   - book_id must be digits only (ISBN-style)
#   - optional title after a single " # " separator
#   - at most one # separator; extra # → line rejected
#   - non-numeric id (e.g. jfjd # ejsk) → line rejected
#
# When a title is given, the finished EPUB is renamed to
#   <sanitised-title>.epub
# (or <sanitised-title>-raw.epub with --raw). Illegal filename characters
# for Windows, Linux, and Android are removed before renaming.
#
# Extra arguments are passed through to oreilly_downloader.py
# (--cookies, --force, --raw, --calibre, --no-nav, ...).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOWNLOADER="${SCRIPT_DIR}/oreilly_downloader.py"
PYTHON="${PYTHON:-}"

usage() {
  cat <<'EOF'
Batch-download O'Reilly books via oreilly_downloader.py

Usage:
  batch_download.sh -f sources.txt [downloader options...]
  batch_download.sh BOOK_ID [BOOK_ID ...] [downloader options...]

sources.txt format (one per line):
  9781617295355 # "Math for Programmers"
  9781633437777 # 'Another Title'
  9781492052203 # Plain title without quotes
  9781098104030

  Full-line comments start with # (no book id).
  Separator between id and title is a single #.
  book_id must be digits only. Lines like "jfjd # ejsk" or
  "jeleo # 83hd # heoo" are invalid and skipped with an error.

  When a title is provided, the EPUB is renamed to <sanitised-title>.epub
  (or <sanitised-title>-raw.epub with --raw). Titles are sanitised for
  Windows / Linux / Android: control chars and  \ / : * ? " < > |  are
  removed; leading/trailing spaces and dots are stripped; Windows reserved
  device names (CON, PRN, ...) are adjusted.

Options (this script):
  -f, --file FILE   Read book ids (and optional titles) from FILE
  --no-pdf          Skip optional PDF conversion even if ebook-convert exists
  -h, --help        Show this help

Downloader options are passed through, for example:
  --cookies cookies.json
  --force
  --raw
  --calibre
  --concurrency 4
  --no-nav
  --cache-dir /path/to/cache
  --webview

Runner order:
  1. $PYTHON if set
  2. uv run (if uv is on PATH)
  3. python3 / python

Examples:
  ./batch_download.sh -f sources.txt --cookies cookies.json
  ./batch_download.sh -f sources.txt --cookies cookies.json --raw --calibre
  ./batch_download.sh 9781617295355 --cookies cookies.json --calibre

Exit code:
  0 if every book succeeded, 1 if any failed (others still attempted).
  Missing ebook-convert is not a failure.
EOF
}

# Sanitize a book title into a safe filename stem (no extension).
# Safe on Windows, Linux, macOS, and Android filesystems.
sanitize_title() {
  # Safe filename stem for Windows / Linux / Android.
  # Includes safaribooks-style "Title: Subtitle" truncation and a broader
  # set of characters replaced with underscore.
  local s="$1"
  s="$(printf '%s' "$s" | tr -d '\000-\037\177')"

  if [[ "$s" == *:* ]]; then
    local before="${s%%:*}"
    if [[ ${#before} -gt 15 ]]; then
      s="$before"
    else
      case "$(uname -s 2>/dev/null)" in
        MINGW*|MSYS*|CYGWIN*) s="${s//:/,}" ;;
        *) s="${s//:/_}" ;;
      esac
    fi
  fi

  s="$(printf '%s' "$s" | sed 's#[~#%&*{}\\<>?/`'"'"'"|+;:]#_#g')"
  s="$(printf '%s' "$s" | sed 's/[[:space:]]\+/ /g')"
  s="$(printf '%s' "$s" | sed 's/^[[:space:].]*//;s/[[:space:].]*$//')"
  case "$(printf '%s' "$s" | tr '[:lower:]' '[:upper:]')" in
    CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9]) s="${s}_book" ;;
  esac
  if [[ -z "$s" ]]; then
    s="book"
  fi
  if [[ ${#s} -gt 180 ]]; then
    s="${s:0:180}"
    s="$(printf '%s' "$s" | sed 's/[[:space:].]*$//')"
  fi
  printf '%s' "$s"
}


parse_source_line() {
  # Parse "BOOK_ID" or "BOOK_ID # title". Sets PARSE_ID, PARSE_TITLE.
  # Returns 0 on success, 1 on empty, 2 on invalid format.
  local line="$1"
  PARSE_ID=""
  PARSE_TITLE=""

  line="${line#"${line%%[![:space:]]*}"}"
  line="${line%"${line##*[![:space:]]}"}"
  [[ -z "$line" ]] && return 1

  # Full-line comment: starts with #
  if [[ "$line" == \#* ]]; then
    return 1
  fi

  # Reject more than one # (invalid: "jeleo # 83hd # heoo")
  local hashes
  hashes="$(printf '%s' "$line" | tr -cd '#' | wc -c)"
  hashes="$(printf '%s' "$hashes" | tr -d '[:space:]')"
  if [[ "$hashes" -gt 1 ]]; then
    echo "error: invalid sources line (only one # separator allowed): $line" >&2
    return 2
  fi

  if [[ "$line" == *"#"* ]]; then
    PARSE_ID="${line%%#*}"
    PARSE_TITLE="${line#*#}"
    PARSE_ID="${PARSE_ID%"${PARSE_ID##*[![:space:]]}"}"
    PARSE_ID="${PARSE_ID#"${PARSE_ID%%[![:space:]]*}"}"
    PARSE_TITLE="${PARSE_TITLE#"${PARSE_TITLE%%[![:space:]]*}"}"
    PARSE_TITLE="${PARSE_TITLE%"${PARSE_TITLE##*[![:space:]]}"}"
    # strip surrounding quotes if present
    if [[ "$PARSE_TITLE" =~ ^\".*\"$ ]]; then
      PARSE_TITLE="${PARSE_TITLE:1:${#PARSE_TITLE}-2}"
    elif [[ "$PARSE_TITLE" =~ ^\'.*\'$ ]]; then
      PARSE_TITLE="${PARSE_TITLE:1:${#PARSE_TITLE}-2}"
    fi
    PARSE_TITLE="${PARSE_TITLE#"${PARSE_TITLE%%[![:space:]]*}"}"
    PARSE_TITLE="${PARSE_TITLE%"${PARSE_TITLE##*[![:space:]]}"}"
  else
    PARSE_ID="$line"
  fi

  # book_id must be all digits (ISBN-style)
  if [[ ! "$PARSE_ID" =~ ^[0-9]+$ ]]; then
    echo "error: invalid sources line (book id must be digits only): $line" >&2
    return 2
  fi
  return 0
}


resolve_runner() {
  if [[ -n "${PYTHON}" ]]; then
    RUNNER=("$PYTHON")
    return
  fi
  if command -v uv >/dev/null 2>&1; then
    RUNNER=(uv run --python 3)
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    RUNNER=(python3)
    return
  fi
  if command -v python >/dev/null 2>&1; then
    RUNNER=(python)
    return
  fi
  echo "error: no Python interpreter found (install python3 or uv)" >&2
  exit 2
}

# Optional PDF conversion; never fails the batch. Keeps the EPUB.
convert_to_pdf() {
  local epub_path="$1"
  local pdf_path="${epub_path%.epub}.pdf"

  if [[ "$DO_PDF" -eq 0 ]]; then
    return 0
  fi
  if ! command -v ebook-convert >/dev/null 2>&1; then
    if [[ "$PDF_WARNED" -eq 0 ]]; then
      echo "  note: ebook-convert (Calibre) not found — skipping PDF conversion"
      PDF_WARNED=1
    fi
    return 0
  fi
  if [[ ! -f "$epub_path" ]]; then
    echo "  warning: cannot convert — $epub_path not found"
    return 0
  fi

  echo "  converting to PDF → ${pdf_path}"
  local -a env_prefix=()
  if [[ "$(id -u)" -eq 0 ]]; then
    env_prefix=(env QTWEBENGINE_DISABLE_SANDBOX=1)
  fi

  set +e
  "${env_prefix[@]}" ebook-convert "$epub_path" "$pdf_path" --pretty-print \
    2>&1 | sed 's/^/    /'
  local rc=${PIPESTATUS[0]}
  set -e

  if [[ $rc -eq 0 && -f "$pdf_path" ]]; then
    echo "  PDF ok: ${pdf_path}"
  else
    echo "  warning: PDF conversion failed (exit ${rc}) — EPUB kept"
  fi
  return 0
}

ids=()
titles=()
passthrough=()
id_file=""
DO_PDF=1
PDF_WARNED=0
USE_RAW=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    -f|--file)
      if [[ $# -lt 2 ]]; then
        echo "error: $1 requires a file path" >&2
        exit 2
      fi
      id_file="$2"
      shift 2
      ;;
    -f=*|--file=*)
      id_file="${1#*=}"
      shift
      ;;
    --no-pdf)
      DO_PDF=0
      shift
      ;;
    --cookies|--jwt|--concurrency|--cache-dir|--webview-profile|--log)
      if [[ $# -lt 2 ]]; then
        echo "error: $1 requires a value" >&2
        exit 2
      fi
      passthrough+=("$1" "$2")
      shift 2
      ;;
    --cookies=*|--jwt=*|--concurrency=*|--cache-dir=*|--webview-profile=*|--log=*)
      passthrough+=("$1")
      shift
      ;;
    --force|--no-nav|--webview|--raw|--calibre|--kindle|--verbose|-v)
      if [[ "$1" == "--raw" ]]; then
        USE_RAW=1
      fi
      passthrough+=("$1")
      shift
      ;;
    -*)
      passthrough+=("$1")
      shift
      ;;
    *)
      ids+=("$1")
      titles+=("")
      shift
      ;;
  esac
done

if [[ -n "$id_file" ]]; then
  if [[ ! -f "$id_file" ]]; then
    echo "error: file not found: $id_file" >&2
    exit 2
  fi
  while IFS= read -r line || [[ -n "$line" ]]; do
    trimmed="${line#"${line%%[![:space:]]*}"}"
    [[ -z "$trimmed" ]] && continue
    # Full-line comments: optional whitespace then #
    if [[ "$trimmed" == \#* ]]; then
      continue
    fi
    parse_source_line "$line"
    rc=$?
    if [[ $rc -eq 0 ]]; then
      ids+=("$PARSE_ID")
      titles+=("$PARSE_TITLE")
    elif [[ $rc -eq 2 ]]; then
      # invalid format already reported to stderr
      :
    fi
  done < "$id_file"
fi

if [[ ${#ids[@]} -eq 0 ]]; then
  echo "error: no book ids given (pass ids as arguments or use -f sources.txt)" >&2
  usage >&2
  exit 2
fi

if [[ ! -f "$DOWNLOADER" ]]; then
  echo "error: oreilly_downloader.py not found next to this script:" >&2
  echo "  expected: $DOWNLOADER" >&2
  exit 2
fi

resolve_runner

has_cookies=0
for arg in "${passthrough[@]+"${passthrough[@]}"}"; do
  case "$arg" in
    --cookies|--cookies=*) has_cookies=1 ;;
  esac
done
if [[ $has_cookies -eq 0 && -f "${SCRIPT_DIR}/cookies.json" ]]; then
  passthrough+=(--cookies "${SCRIPT_DIR}/cookies.json")
elif [[ $has_cookies -eq 0 && -f "./cookies.json" ]]; then
  passthrough+=(--cookies "./cookies.json")
fi

total=${#ids[@]}
ok=0
fail=0
failed_ids=()
start_all=$(date +%s)

echo "Batch download: ${total} book(s)"
echo "Downloader:     $DOWNLOADER"
echo "Runner:         ${RUNNER[*]}"
if [[ "$USE_RAW" -eq 1 ]]; then
  echo "Mode:           raw (+ title rename when provided)"
fi
if [[ "$DO_PDF" -eq 1 ]]; then
  if command -v ebook-convert >/dev/null 2>&1; then
    echo "PDF convert:    ebook-convert (Calibre)"
  else
    echo "PDF convert:    skipped (ebook-convert not found)"
  fi
else
  echo "PDF convert:    disabled (--no-pdf)"
fi
if [[ ${#passthrough[@]} -gt 0 ]]; then
  echo "Options:        ${passthrough[*]}"
fi
echo

i=0
for idx in "${!ids[@]}"; do
  book_id="${ids[$idx]}"
  title="${titles[$idx]}"
  i=$((i + 1))

  # What the Python script will write
  if [[ "$USE_RAW" -eq 1 ]]; then
    produced="${book_id}-raw.epub"
  else
    produced="${book_id}.epub"
  fi

  # Desired final name from title (sanitised) or fall back to produced name
  final_name="$produced"
  display="$book_id → $produced"
  if [[ -n "$title" ]]; then
    safe="$(sanitize_title "$title")"
    if [[ "$USE_RAW" -eq 1 ]]; then
      final_name="${safe}-raw.epub"
    else
      final_name="${safe}.epub"
    fi
    display="$book_id → $final_name"
  fi

  echo "════════════════════════════════════════════════════════════"
  echo "[${i}/${total}] ${display}"
  echo "════════════════════════════════════════════════════════════"
  start=$(date +%s)

  set +e
  "${RUNNER[@]}" "$DOWNLOADER" "$book_id" ${passthrough[@]+"${passthrough[@]}"}
  rc=$?
  set -e

  elapsed=$(( $(date +%s) - start ))

  if [[ $rc -ne 0 ]]; then
    fail=$((fail + 1))
    failed_ids+=("$book_id")
    echo "→ FAIL  ${book_id}  (exit ${rc}, ${elapsed}s)"
    echo
    continue
  fi

  # Rename numbered output → title-based name when different
  if [[ -f "$produced" ]]; then
    if [[ "$final_name" != "$produced" ]]; then
      if [[ -f "$final_name" ]]; then
        echo "  warning: ${final_name} already exists — keeping ${produced}"
        final_name="$produced"
      else
        mv -- "$produced" "$final_name"
        echo "  renamed ${produced} → ${final_name}"
      fi
    fi
  else
    echo "  warning: expected ${produced} not found after download"
    # Best-effort: if title rename already matches something on disk
    if [[ ! -f "$final_name" ]]; then
      fail=$((fail + 1))
      failed_ids+=("$book_id")
      echo "→ FAIL  ${book_id}  (output missing, ${elapsed}s)"
      echo
      continue
    fi
  fi

  convert_to_pdf "$final_name"

  ok=$((ok + 1))
  echo "→ OK  ${final_name}  (${elapsed}s)"
  echo
done

total_elapsed=$(( $(date +%s) - start_all ))
echo "════════════════════════════════════════════════════════════"
echo "Done in ${total_elapsed}s — ${ok} succeeded, ${fail} failed (of ${total})"
if [[ $fail -gt 0 ]]; then
  echo "Failed ids:"
  for id in "${failed_ids[@]}"; do
    echo "  $id"
  done
  exit 1
fi
exit 0
