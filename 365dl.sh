#!/usr/bin/env bash
set -euo pipefail

red=""
cyan=""
reset=""
if [[ -t 2 && -z "${NO_COLOR:-}" ]]; then
  red=$'\033[31m'
  cyan=$'\033[36m'
  reset=$'\033[0m'
fi

error() {
  printf '%sError:%s %s\n' "$red" "$reset" "$*" >&2
}

info() {
  printf '%s[*]%s %s\n' "$cyan" "$reset" "$*" >&2
}

show_help() {
  cat <<'EOF'
Usage:
  ./365dl.sh -scan <scan_directory> [authentication] [options]

Authentication (choose one):
  -cfile FILE            Refresh-token credentials (default: ./creds.ini)
  -s SECTION             Credential section (default: DEFAULT)
  M365AT environment var   Existing Microsoft Graph access token

Options:
  -scan DIR           Scan output containing inventory.csv (default: newest scan-*)
  -out DIR            Download directory (default: ./dl)
  -max-mb N           Maximum size of one downloaded file (default: 500)
  -chunk-threshold N  Choose a subset before fzf above this row count (default: 100000)
  -chunk-mode MODE    site-library, site, extension, or none (default: site-library)
  -no-chunk           Disable chunking
  -id ID              Download one catalog ID and exit
  -view               With -id, extract and show supported text in fzf
  -help                Show help

fzf keys:
  ENTER       Download current file and view extractable text
  TAB         Select/deselect an entry
  CTRL-A      Toggle all entries
  CTRL-SPACE  Download selected entries
  CTRL-S      Site/library statistics, ascending
  ALT-S       Site/library statistics, descending
  CTRL-F      Choose extension and add it to the current search (ascending)
  ALT-F       Choose extension and add it to the current search (descending)

Chunk selector:
  [All files] Search the complete catalog instead of one chunk
EOF
}

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
WORKER_DIR="$SCRIPT_DIR/workers"
scan_dir=""
out_dir="./dl"
cfile="./creds.ini"
section="DEFAULT"
max_mb="500"
chunk_threshold="100000"
chunk_mode="site-library"
download_id=""
view_single=false
has_access_token_env=false

if [[ -n "${M365AT:-}" ]]; then
  has_access_token_env=true
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    -scan) scan_dir=$2; shift 2 ;;
    -out) out_dir=$2; shift 2 ;;
    -cfile) cfile=$2; shift 2 ;;
    -s) section=$2; shift 2 ;;
    -max-mb) max_mb=$2; shift 2 ;;
    -chunk-threshold) chunk_threshold=$2; shift 2 ;;
    -chunk-mode) chunk_mode=$2; shift 2 ;;
    -no-chunk) chunk_mode="none"; shift ;;
    -id) download_id=$2; shift 2 ;;
    -view) view_single=true; shift ;;
    -help|--help|-h) show_help; exit 0 ;;
    *) error "Unknown argument: $1"; show_help >&2; exit 2 ;;
  esac
done

if [[ -z "$scan_dir" ]]; then
  shopt -s nullglob
  scans=(scan-*)
  shopt -u nullglob
  if [[ ${#scans[@]} -gt 0 ]]; then
    scan_dir=$(printf '%s\n' "${scans[@]}" | sort | tail -n 1)
  fi
fi

if [[ "$has_access_token_env" == false && ! -f "$cfile" ]]; then
  error "provide $cfile or set M365AT."
  exit 2
fi
if [[ "$has_access_token_env" == true ]]; then
  info "Auth: downloads will validate the static M365AT token when they start."
else
  info "Auth: downloads will use $cfile [$section]; cached access tokens are validated and refreshed when needed."
fi
if [[ -z "$scan_dir" || ! -f "$scan_dir/inventory.csv" ]]; then
  error "scan directory with inventory.csv not found. Use -scan DIR."
  exit 1
fi
if ! command -v python3 >/dev/null; then
  error "python3 not found."
  exit 1
fi
if [[ -z "$download_id" || "$view_single" == true ]] && ! command -v fzf >/dev/null; then
  error "fzf not found."
  exit 1
fi
case "$chunk_mode" in site-library|site|extension|none) ;; *) error "invalid chunk mode."; exit 2 ;; esac
if ! [[ "$chunk_threshold" =~ ^[0-9]+$ ]]; then
  error "-chunk-threshold must be a non-negative integer."
  exit 2
fi

tmpdir=$(mktemp -d)
stty_state=""
cleanup() {
  if [[ -n "$stty_state" ]]; then stty "$stty_state" < /dev/tty 2>/dev/null || true; fi
  rm -rf -- "$tmpdir"
}
trap cleanup EXIT

if [[ -z "$download_id" ]] && stty_state=$({ stty -g < /dev/tty; } 2>/dev/null); then
  stty -ixon < /dev/tty 2>/dev/null || true
fi

catalog="$tmpdir/catalog.tsv"
python3 "$WORKER_DIR/m365_catalog.py" catalog "$scan_dir/inventory.csv" > "$catalog"
row_count=$(wc -l < "$catalog" | tr -d ' ')
if [[ "$row_count" -eq 0 ]]; then
  error "inventory contains no files."
  exit 1
fi

download_base=(python3 "$WORKER_DIR/m365_download.py" --output "$out_dir" --max-download-mb "$max_mb")
if [[ "$has_access_token_env" == false ]]; then
  download_base+=(--config "$cfile" --section "$section")
fi

if [[ -n "$download_id" ]]; then
  selected="$tmpdir/selected.tsv"
  python3 "$WORKER_DIR/m365_catalog.py" id --id "$download_id" < "$catalog" > "$selected"
  if $view_single; then
    "${download_base[@]}" --view < "$selected"
  else
    "${download_base[@]}" < "$selected"
  fi
  exit $?
fi

q() { printf '%q' "$1"; }
python_q=$(q "$(command -v python3)")
catalog_helper_q=$(q "$WORKER_DIR/m365_catalog.py")
downloader_q=$(q "$WORKER_DIR/m365_download.py")
out_q=$(q "$out_dir")
max_q=$(q "$max_mb")
catalog_q=$(q "$catalog")
dl_cmd="$python_q $downloader_q --output $out_q --max-download-mb $max_q"
if [[ "$has_access_token_env" == false ]]; then
  dl_cmd+=" --config $(q "$cfile") --section $(q "$section")"
fi
preview_cmd="$python_q $catalog_helper_q sum < {+f}"

stats_command() {
  local mode=$1 sort=$2 output=$3 header=$4
  local output_q header_q
  output_q=$(q "$output")
  header_q=$(q "$header")
  printf 'test -s %s || %s %s stats --mode %s --sort %s < %s > %s; fzf --no-sort --header-lines=1 --header %s < %s >/dev/null' \
    "$output_q" "$python_q" "$catalog_helper_q" "$mode" "$sort" "$catalog_q" "$output_q" "$header_q" "$output_q"
}

extension_picker_command() {
  local sort=$1 output=$2 choice=$3 header=$4
  local output_q choice_q header_q
  output_q=$(q "$output")
  choice_q=$(q "$choice")
  header_q=$(q "$header")
  printf ': > %s; test -s %s || %s %s stats --mode extension --sort %s < %s > %s; fzf --no-sort --header-lines=1 --header %s < %s > %s || true' \
    "$choice_q" "$output_q" "$python_q" "$catalog_helper_q" "$sort" "$catalog_q" "$output_q" "$header_q" "$output_q" "$choice_q"
}

extension_apply_command() {
  local choice_q
  choice_q=$(q "$1")
  printf 'current={q}; selected=$(head -n 1 %s 2>/dev/null || true); : > %s; if test -z "$selected"; then printf "%%s\\n" "$current"; exit 0; fi; extension=${selected%%%% *}; if test "$extension" = "[no"; then printf "%%s\\n" "$current"; elif test -z "$current"; then printf "%%s\\n" "$extension"; else printf "%%s %%s\\n" "$current" "$extension"; fi' \
    "$choice_q" "$choice_q"
}

sites_asc_cmd=$(stats_command site-library asc "$tmpdir/sites-asc.txt" "Site/library statistics ascending. ESC returns.")
sites_desc_cmd=$(stats_command site-library desc "$tmpdir/sites-desc.txt" "Site/library statistics descending. ESC returns.")
extension_choice="$tmpdir/extension-choice.txt"
ext_asc_picker_cmd=$(extension_picker_command asc "$tmpdir/ext-asc.txt" "$extension_choice" "Choose extension. ENTER applies it to the current search; ESC returns unchanged.")
ext_desc_picker_cmd=$(extension_picker_command desc "$tmpdir/ext-desc.txt" "$extension_choice" "Choose extension. ENTER applies it to the current search; ESC returns unchanged.")
ext_apply_cmd=$(extension_apply_command "$extension_choice")

run_picker() {
  local input=$1 prompt=$2
  fzf -e -m --delimiter=$'\t' --with-nth=1,2,3,4,6,7,8 \
    --prompt "$prompt > " \
    --header $'ID\tSite\tLibrary\tPath\tSizeMB\tSizeBytes\tModified' \
    --bind 'ctrl-a:toggle-all' \
    --bind "ctrl-s:execute($sites_asc_cmd)" \
    --bind "alt-s:execute($sites_desc_cmd)" \
    --bind "ctrl-f:execute($ext_asc_picker_cmd)+transform-query~$ext_apply_cmd~" \
    --bind "alt-f:execute($ext_desc_picker_cmd)+transform-query~$ext_apply_cmd~" \
    --bind "enter:execute($dl_cmd --view < {f})" \
    --bind "ctrl-space:execute($dl_cmd < {+f}; printf '\nPress ENTER to return'; read -r _)+deselect-all" \
    --preview "$preview_cmd" --preview-window=up,2 \
    < "$input" || true
}

if [[ "$chunk_mode" != "none" && "$row_count" -gt "$chunk_threshold" ]]; then
  info "Loaded $row_count rows; selecting chunks by $chunk_mode."
  while true; do
    group_line=$(python3 "$WORKER_DIR/m365_catalog.py" groups --mode "$chunk_mode" --include-all < "$catalog" |
      fzf --delimiter=$'\t' --with-nth=2 --prompt "Choose chunk > ") || break
    group_key=${group_line%%$'\t'*}
    if [[ "$group_key" == "__all_files__" ]]; then
      run_picker "$catalog" "All files"
      continue
    fi
    chunk="$tmpdir/chunk.tsv"
    python3 "$WORKER_DIR/m365_catalog.py" filter --mode "$chunk_mode" --key "$group_key" < "$catalog" > "$chunk"
    run_picker "$chunk" "$group_key"
  done
else
  run_picker "$catalog" "365enum"
fi
