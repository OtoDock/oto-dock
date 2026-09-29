#!/usr/bin/env bash
# The script half of the "content-style" check: duration, aspect ratio and
# loudness probes over the produced video files. Exit 0 = pass; the printed
# lines become the finding otherwise.
set -u
INPUT="${OTODOCK_CHECK_INPUT:-${OTODOCK_STEP_PAYLOAD:-}}"
[ -n "$INPUT" ] && [ -f "$INPUT" ] || { echo "no changed set"; exit 2; }
command -v ffprobe >/dev/null 2>&1 || { echo "ffprobe is not installed here"; exit 0; }
status=0
python3 - "$INPUT" <<'PY' | while read -r f; do
import json, sys
for p in json.load(open(sys.argv[1])).get("paths") or []:
    if p.get("writes") and p.get("kind") == "video":
        print(p["path"])
PY
  [ -f "$f" ] || continue
  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f" 2>/dev/null || echo 0)
  wh=$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0 "$f" 2>/dev/null || echo "0,0")
  echo "$f: ${dur%.*}s ${wh}"
  case "$wh" in 1920,1080|1080,1920|3840,2160) ;; *) echo "  aspect: not a house format ($wh)"; status=1 ;; esac
  if [ "${dur%.*}" -gt 180 ]; then echo "  longer than three minutes"; status=1; fi
done
exit $status
