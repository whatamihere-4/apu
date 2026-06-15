#!/usr/bin/env bash
set -euo pipefail

readonly COLS=4
readonly ROWS=5
readonly N=20
TW=320
TH=180
CARD_BORDER=1
CARD_SHADOW=2
GUT_X=6
GUT_Y=6
HDR_H=110
PAD_X=16
PAD_Y=16

log() { printf '[thumber] %s\n' "$*" >&2; }

# Per-frame ffmpeg can hang on some inputs (esp. full_decode_fallback). Alpine/busybox `timeout` kills it.
# Grid/compose need a higher cap (large sheets).
: "${THUMBER_FFMPEG_TIMEOUT_SEC:=300}"
: "${THUMBER_FFMPEG_TIMEOUT_SLOW_SEC:=600}"

_ffmpeg_timeout() {
  local to="$1"
  shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "$to" "$@"
  else
    "$@"
  fi
}

elapsed_since() {
  local start="$1"
  local now
  now=$(date +%s%N 2>/dev/null || date +%s)
  if [[ ${#now} -gt 12 ]]; then
    awk -v s="$start" -v n="$now" 'BEGIN { printf "%.1fs", (n - s) / 1e9 }'
  else
    awk -v s="$start" -v n="$now" 'BEGIN { printf "%ds", n - s }'
  fi
}

pick_font() {
  local f
  f=$(find /usr/share/fonts -name 'DejaVuSans.ttf' 2>/dev/null | head -1)
  [[ -n "$f" ]] || f="/usr/share/fonts/ttf-dejavu/DejaVuSans.ttf"
  printf '%s' "$f"
}

FONT="$(pick_font)"

usage() {
  echo "Usage: thumbs [-o OUT.png] <video> [video ...]" >&2
  echo "  Writes <stem>_thumbs.png per file (cwd, or THUMBER_OUT_DIR if set)." >&2
  echo "  With -o, exactly one input and writes to OUT.png (basename + THUMBER_OUT_DIR => under that dir)." >&2
  echo "  Bare filenames: if THUMBER_IN_DIR is set and that dir contains the file, it is used (Docker: /downloads)." >&2
  exit 1
}

# Bare basename + THUMBER_IN_DIR => full input path; absolute and multi-segment paths unchanged.
resolve_input() {
  local v="$1"
  if [[ "$v" == /* ]]; then
    printf '%s' "$v"
    return
  fi
  if [[ "$v" == */* ]]; then
    printf '%s' "$v"
    return
  fi
  if [[ -n "${THUMBER_IN_DIR:-}" ]]; then
    local cand="${THUMBER_IN_DIR%/}/$v"
    if [[ -f "$cand" ]]; then
      printf '%s' "$cand"
      return
    fi
  fi
  printf '%s' "$v"
}

# Basename-only -o + THUMBER_OUT_DIR => under output dir; absolute paths unchanged.
resolve_out_path() {
  local o="$1"
  if [[ "$o" == /* ]]; then
    printf '%s' "$o"
    return
  fi
  if [[ "$o" == */* ]]; then
    printf '%s' "$o"
    return
  fi
  if [[ -n "${THUMBER_OUT_DIR:-}" ]]; then
    printf '%s' "${THUMBER_OUT_DIR%/}/$o"
    return
  fi
  printf '%s' "$o"
}

sec_to_hms() {
  awk -v t="$1" 'BEGIN {
    if (t < 0) t = 0
    h = int(t / 3600)
    m = int((t - h * 3600) / 60)
    s = int(t - h * 3600 - m * 60 + 0.5)
    if (s >= 60) { s = 0; m++ }
    if (m >= 60) { m = 0; h++ }
    printf "%02d:%02d:%02d", h, m, s
  }'
}

process_one() {
  local in_path="$1"
  local out_path="$2"
  local t_total
  t_total=$(date +%s%N 2>/dev/null || date +%s)

  if [[ ! -f "$in_path" ]]; then
    echo "error: not a file: $in_path" >&2
    echo "hint: container only sees mounted paths. Use a bare filename with THUMBER_IN_DIR, or /downloads/..., /app/..., /work/..." >&2
    return 1
  fi

  local tmp
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' RETURN

  local base size_bytes dur w h
  base=$(basename "$in_path")
  log "Processing: $base"

  local t0
  t0=$(date +%s%N 2>/dev/null || date +%s)
  size_bytes=$(stat -c%s "$in_path" 2>/dev/null || wc -c <"$in_path")
  # stderr must not flood the pipe to thumber-http (Python readline); keep only stdout lines.
  dur=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$in_path" 2>/dev/null | head -1 | tr -d '\r')
  [[ "$dur" =~ ^[0-9]*\.?[0-9]+$ ]] || dur=0

  w=$(ffprobe -v error -select_streams v:0 -show_entries stream=width -of default=noprint_wrappers=1:nokey=1 "$in_path" 2>/dev/null | head -1 | tr -d '\r')
  h=$(ffprobe -v error -select_streams v:0 -show_entries stream=height -of default=noprint_wrappers=1:nokey=1 "$in_path" 2>/dev/null | head -1 | tr -d '\r')
  [[ "$w" =~ ^[0-9]+$ ]] || w="?"
  [[ "$h" =~ ^[0-9]+$ ]] || h="?"

  local size_human
  if [[ "$size_bytes" =~ ^[0-9]+$ ]] && (( size_bytes >= 1048576 )); then
    size_human=$(awk -v b="$size_bytes" 'BEGIN { printf "%.2f MB", b/1048576 }')
  elif [[ "$size_bytes" =~ ^[0-9]+$ ]]; then
    size_human=$(awk -v b="$size_bytes" 'BEGIN { printf "%.2f KB", b/1024 }')
  else
    size_human="?"
  fi

  local dur_hms
  dur_hms=$(sec_to_hms "$dur")
  log "  ${w}x${h}, ${size_human}, duration ${dur_hms} (probe: $(elapsed_since "$t0"))"

  printf '%s' "File Name: ${base}" >"$tmp/l1.txt"
  printf '%s' "File Size: ${size_human} (${size_bytes} bytes)" >"$tmp/l2.txt"
  printf '%s' "Resolution: ${w}x${h}" >"$tmp/l3.txt"
  printf '%s' "Duration: ${dur_hms}" >"$tmp/l4.txt"

  log "  Extracting $N keyframe thumbnails..."
  local t_grab
  t_grab=$(date +%s%N 2>/dev/null || date +%s)

  local i t label tsf vf t_one
  for ((i = 0; i < N; i++)); do
    if awk -v d="$dur" 'BEGIN { exit !(d > 0) }'; then
      t=$(awk -v d="$dur" -v i="$i" -v n="$N" 'BEGIN {
        if (d <= 0) { print 0; exit }
        if (n <= 1) { print 0; exit }
        v = d * i / (n - 1)
        marg = (d > 2.0) ? 0.5 : (d > 0.2) ? 0.1 : d * 0.25
        if (marg >= d * 0.45) marg = d * 0.2
        if (v > d - marg) v = d - marg
        if (v < 0) v = 0
        print v
      }')
    else
      t=0
    fi

    label=$(sec_to_hms "$t")
    # textfile avoids colons in HH:MM:SS being parsed as drawtext option separators.
    tsf="$tmp/ts_$(printf '%02d' "$i").txt"
    printf '%s' "$label" >"$tsf"
    vf="scale=${TW}:${TH}:force_original_aspect_ratio=decrease,"\
"pad=${TW}:${TH}:(ow-iw)/2:(oh-ih)/2:color=black,"\
"drawtext=fontfile=${FONT}:textfile=${tsf}:fontsize=14:fontcolor=white:"\
"x=w-text_w-6:y=h-text_h-4:box=1:boxcolor=black@0.55:boxborderw=4,"\
"pad=iw+$((CARD_BORDER*2)):ih+$((CARD_BORDER*2)):${CARD_BORDER}:${CARD_BORDER}:color=white,"\
"pad=iw+${CARD_SHADOW}:ih+${CARD_SHADOW}:0:0:color=#aaaaaa,"\
"pad=iw+${CARD_SHADOW}:ih+${CARD_SHADOW}:0:0:color=#c4c4c4"

    local out_png="$tmp/t$(printf '%02d' "$i").png"
    t_one=$(date +%s%N 2>/dev/null || date +%s)
    log "    → ffmpeg [$((i+1))/$N] seek=${t}s keyframe_try (timeout ${THUMBER_FFMPEG_TIMEOUT_SEC}s)"
    # Keyframe-only decode is fast but can output nothing near EOF if no keyframe after -ss.
    _fc=0
    rm -f "$out_png"
    _ffmpeg_timeout "${THUMBER_FFMPEG_TIMEOUT_SEC}" \
      ffmpeg -nostdin -hide_banner -loglevel error -y -threads 1 \
      -skip_frame nokey -ss "$t" -i "$in_path" -frames:v 1 -vf "$vf" \
      "$out_png" 2>/dev/null || _fc=$?
    if [[ "$_fc" -eq 124 ]]; then
      log "    TIMEOUT keyframe_try [$((i+1))/$N] after ${THUMBER_FFMPEG_TIMEOUT_SEC}s"
      rm -f "$out_png"
    elif [[ "$_fc" -ne 0 ]]; then
      log "    ERROR keyframe_try [$((i+1))/$N] exit=$_fc"
      rm -f "$out_png"
    fi
    if [[ ! -s "$out_png" ]]; then
      rm -f "$out_png"
      log "    → ffmpeg [$((i+1))/$N] seek=${t}s full_decode_fallback (timeout ${THUMBER_FFMPEG_TIMEOUT_SEC}s)"
      _fc=0
      _ffmpeg_timeout "${THUMBER_FFMPEG_TIMEOUT_SEC}" \
        ffmpeg -nostdin -hide_banner -loglevel error -y -threads 1 \
        -ss "$t" -i "$in_path" -frames:v 1 -vf "$vf" \
        "$out_png" 2>/dev/null || _fc=$?
      if [[ "$_fc" -eq 124 ]]; then
        log "    TIMEOUT full_decode_fallback [$((i+1))/$N] after ${THUMBER_FFMPEG_TIMEOUT_SEC}s"
        rm -f "$out_png"
      elif [[ "$_fc" -ne 0 ]]; then
        log "    ERROR full_decode_fallback [$((i+1))/$N] exit=$_fc"
        rm -f "$out_png"
      fi
    fi
    if [[ ! -s "$out_png" && "$i" -gt 0 ]]; then
      prev_png="$tmp/t$(printf '%02d' "$((i-1))").png"
      if [[ -s "$prev_png" ]]; then
        cp -f "$prev_png" "$out_png"
        log "    WARN using previous thumbnail for [$((i+1))/$N]"
      fi
    fi
    log "    [$((i+1))/$N] @${label} ($(elapsed_since "$t_one"))"
  done
  log "  All grabs done ($(elapsed_since "$t_grab"))"

  for ((i = 0; i < N; i++)); do
    [[ -f "$tmp/t$(printf '%02d' "$i").png" ]] || {
      log "  ERROR: missing thumbnail $i"
      return 1
    }
  done

  log "  Tiling grid..."
  t0=$(date +%s%N 2>/dev/null || date +%s)
  log "  → ffmpeg tile build grid.png"

  local sheet_w sheet_h ox oy grid_inner_w grid_inner_h
  local cell_w cell_h
  cell_w=$((TW + CARD_BORDER * 2 + CARD_SHADOW * 2))
  cell_h=$((TH + CARD_BORDER * 2 + CARD_SHADOW * 2))
  sheet_w=$((PAD_X * 2 + COLS * cell_w + (COLS - 1) * GUT_X))
  sheet_h=$((PAD_Y + HDR_H + PAD_Y + ROWS * cell_h + (ROWS - 1) * GUT_Y + PAD_Y))

  _fc=0
  _ffmpeg_timeout "${THUMBER_FFMPEG_TIMEOUT_SLOW_SEC}" \
    ffmpeg -nostdin -hide_banner -loglevel error -y \
    -framerate 1 -start_number 0 -i "$tmp/t%02d.png" \
    -vf "tile=4x5:margin=$((GUT_X/2)):padding=$((GUT_Y/2)):color=#c4c4c4" \
    "$tmp/grid.png" 2>/dev/null || _fc=$?
  if [[ "$_fc" -ne 0 ]]; then
    if [[ "$_fc" -eq 124 ]]; then
      log "  TIMEOUT tiling grid after ${THUMBER_FFMPEG_TIMEOUT_SLOW_SEC}s"
    else
      log "  ERROR: ffmpeg tile failed (exit $_fc)"
    fi
    return 1
  fi
  log "  Tiled ($(elapsed_since "$t0"))"

  grid_inner_w=$(ffprobe -v error -show_entries stream=width -of default=noprint_wrappers=1:nokey=1 "$tmp/grid.png" 2>/dev/null | head -1)
  grid_inner_h=$(ffprobe -v error -show_entries stream=height -of default=noprint_wrappers=1:nokey=1 "$tmp/grid.png" 2>/dev/null | head -1)
  [[ "$grid_inner_w" =~ ^[0-9]+$ ]] || grid_inner_w=$((COLS * cell_w + (COLS - 1) * GUT_X))
  [[ "$grid_inner_h" =~ ^[0-9]+$ ]] || grid_inner_h=$((ROWS * cell_h + (ROWS - 1) * GUT_Y))

  ox=$((PAD_X + (sheet_w - 2 * PAD_X - grid_inner_w) / 2))
  oy=$((PAD_Y + HDR_H + PAD_Y))

  log "  Composing final sheet..."
  t0=$(date +%s%N 2>/dev/null || date +%s)
  log "  → ffmpeg compose final sheet"

  _fc=0
  _ffmpeg_timeout "${THUMBER_FFMPEG_TIMEOUT_SLOW_SEC}" \
    ffmpeg -nostdin -hide_banner -loglevel error -y \
    -f lavfi -i "color=c=#c4c4c4:s=${sheet_w}x${sheet_h}:d=1" \
    -i "$tmp/grid.png" \
    -filter_complex "\
[0:v]drawtext=fontfile=${FONT}:textfile=$tmp/l1.txt:fontsize=15:fontcolor=black:x=${PAD_X}:y=${PAD_Y},\
drawtext=fontfile=${FONT}:textfile=$tmp/l2.txt:fontsize=15:fontcolor=black:x=${PAD_X}:y=$((PAD_Y + 22)),\
drawtext=fontfile=${FONT}:textfile=$tmp/l3.txt:fontsize=15:fontcolor=black:x=${PAD_X}:y=$((PAD_Y + 44)),\
drawtext=fontfile=${FONT}:textfile=$tmp/l4.txt:fontsize=15:fontcolor=black:x=${PAD_X}:y=$((PAD_Y + 66))[bg];\
[bg][1:v]overlay=${ox}:${oy}" \
    -frames:v 1 "$out_path" 2>/dev/null || _fc=$?
  if [[ "$_fc" -ne 0 ]]; then
    if [[ "$_fc" -eq 124 ]]; then
      log "  TIMEOUT compose final sheet after ${THUMBER_FFMPEG_TIMEOUT_SLOW_SEC}s"
    else
      log "  ERROR: ffmpeg compose failed (exit $_fc)"
    fi
    return 1
  fi

  log "  Composed ($(elapsed_since "$t0"))"
  log "Done: $out_path (total $(elapsed_since "$t_total"))"
}

OUT_GLOBAL=""
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage ;;
    -o)
      shift
      [[ $# -gt 0 ]] || usage
      OUT_GLOBAL="$1"
      shift
      ;;
    *) ARGS+=("$1"); shift ;;
  esac
done

[[ ${#ARGS[@]} -gt 0 ]] || usage

if [[ -n "${THUMBER_OUT_DIR:-}" ]]; then
  mkdir -p "${THUMBER_OUT_DIR}"
fi

if [[ -n "$OUT_GLOBAL" ]]; then
  if [[ ${#ARGS[@]} -ne 1 ]]; then
    echo "error: -o requires exactly one input video" >&2
    exit 1
  fi
  in_resolved=$(resolve_input "${ARGS[0]}")
  out_resolved=$(resolve_out_path "$OUT_GLOBAL")
  mkdir -p "$(dirname "$out_resolved")"
  process_one "$in_resolved" "$out_resolved"
  exit 0
fi

for v in "${ARGS[@]}"; do
  in_resolved=$(resolve_input "$v")
  b=$(basename "$in_resolved")
  stem="${b%.*}"
  [[ "$stem" == "$b" ]] && stem="$b"
  if [[ -n "${THUMBER_OUT_DIR:-}" ]]; then
    out="${THUMBER_OUT_DIR%/}/${stem}_thumbs.png"
  else
    out="${stem}_thumbs.png"
  fi
  mkdir -p "$(dirname "$out")"
  process_one "$in_resolved" "$out"
done
