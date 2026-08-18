#!/usr/bin/env bash
# Merge .PART1 / .PART2 / … in a folder with ffmpeg concat (stream copy).
#
#   ./scripts/merge-parts.sh
#   ./scripts/merge-parts.sh "/path/to/parts/folder"
set -euo pipefail

cd "${1:-.}"

ffmpeg_bin="${FFMPEG_BIN:-ffmpeg}"
shopt -s nullglob
part1=( *.PART1.* )
if (( ${#part1[@]} != 1 )); then
  echo "Need exactly one *.PART1.* file in $(pwd)" >&2
  exit 1
fi

file="${part1[0]}"
if [[ ! "$file" =~ ^(.+)\.PART[0-9]+(\..+)$ ]]; then
  echo "Unexpected part name: $file" >&2
  exit 1
fi
stem="${BASH_REMATCH[1]}"
ext="${BASH_REMATCH[2]}"
out="${stem}${ext}"

parts=("${stem}.PART1${ext}")
i=2
while [[ -f "${stem}.PART${i}${ext}" ]]; do
  parts+=("${stem}.PART${i}${ext}")
  i=$((i + 1))
done
if (( ${#parts[@]} < 2 )); then
  echo "Only one part file found." >&2
  exit 1
fi
if [[ -f "$out" ]]; then
  echo "Already exists: $out" >&2
  exit 1
fi

list=.parts.txt
rm -f "$list"
for p in "${parts[@]}"; do
  printf "file '%s'\n" "$p" >> "$list"
done

echo "Merging ${#parts[@]} parts -> $out"
"$ffmpeg_bin" -y -f concat -safe 0 -i "$list" -c copy -movflags +faststart "$out"
rm -f "$list"
echo "Done."
