#!/usr/bin/env bash
# Thin wrapper — batch download is built into oreilly_downloader.py.
# Prefer:
#   python3 oreilly_downloader.py --books books.txt --cookies cookies.json
#   python3 oreilly_downloader.py 978… 978… --cookies cookies.json --pdf
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOWNLOADER="${SCRIPT_DIR}/oreilly_downloader.py"

if [[ ! -f "$DOWNLOADER" ]]; then
  echo "error: oreilly_downloader.py not found next to this script" >&2
  exit 1
fi

# Map -f FILE → --books FILE for convenience
args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -f|--file)
      shift
      [[ $# -gt 0 ]] || { echo "error: -f requires a file" >&2; exit 2; }
      args+=(--books "$1")
      shift
      ;;
    -h|--help)
      echo "Wrapper for oreilly_downloader.py batch mode."
      echo "  $0 -f books.txt [options…]"
      echo "  $0 BOOK_ID [BOOK_ID …] [options…]"
      echo "All options pass through (including --no-pdf, --calibre, --cookies)."
      python3 "$DOWNLOADER" --help
      exit 0
      ;;
    *)
      args+=("$1")
      shift
      ;;
  esac
done

if command -v uv >/dev/null 2>&1; then
  exec uv run "$DOWNLOADER" "${args[@]}"
elif command -v python3 >/dev/null 2>&1; then
  exec python3 "$DOWNLOADER" "${args[@]}"
else
  exec python "$DOWNLOADER" "${args[@]}"
fi
