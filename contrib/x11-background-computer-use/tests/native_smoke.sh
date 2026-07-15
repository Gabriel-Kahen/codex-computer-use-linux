#!/usr/bin/env bash
set -euo pipefail

helper=${1:?usage: native_smoke.sh /path/to/x11-window-capture}
display=:99
temporary=$(mktemp -d)
pids=()

cleanup() {
  for pid in "${pids[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  for pid in "${pids[@]}"; do
    wait "$pid" 2>/dev/null || true
  done
  rm -rf "$temporary"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

export DISPLAY=$display
export HOME=$temporary/home
mkdir -p "$HOME"
cc -O2 -Wall -Wextra -Werror \
  -o "$temporary/x11-test-window" \
  "$(dirname "$0")/x11-test-window.c" \
  $(pkg-config --cflags --libs x11)

Xvfb "$display" -screen 0 1024x768x24 -nolisten tcp >"$temporary/xvfb.log" 2>&1 &
pids+=("$!")
ready=false
for _ in $(seq 1 100); do
  if xdpyinfo >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 0.05
done
if [[ $ready != true ]]; then
  cat "$temporary/xvfb.log"
  echo "Xvfb did not become ready" >&2
  exit 1
fi

openbox --sm-disable >"$temporary/openbox.log" 2>&1 &
pids+=("$!")
xcompmgr -n >"$temporary/xcompmgr.log" 2>&1 &
pids+=("$!")
"$temporary/x11-test-window" >"$temporary/test-window.log" 2>&1 &
window_pid=$!
pids+=("$window_pid")

window_id=
for _ in $(seq 1 100); do
  window_id=$(xdotool search --onlyvisible --name '^Codex-X11-Native-Smoke$' 2>/dev/null | head -n 1 || true)
  if [[ -n $window_id ]] && xprop -root _NET_SUPPORTING_WM_CHECK 2>/dev/null | grep -q 'window id'; then
    break
  fi
  window_id=
  sleep 0.05
done
if [[ -z $window_id ]]; then
  cat "$temporary/openbox.log" "$temporary/test-window.log"
  echo "EWMH window manager or test client did not become ready" >&2
  exit 1
fi

authenticated_pid=$($helper --pid "$window_id")
if [[ $authenticated_pid != "$window_pid" ]]; then
  echo "XRes authenticated PID $authenticated_pid, expected $window_pid" >&2
  exit 1
fi

captured=false
for _ in $(seq 1 100); do
  if "$helper" "$window_id" "$temporary/window.png" 2>"$temporary/capture.log"; then
    captured=true
    break
  fi
  sleep 0.05
done
if [[ $captured != true ]]; then
  cat "$temporary/xcompmgr.log" "$temporary/capture.log"
  echo "XComposite window capture did not succeed" >&2
  exit 1
fi

python3 - "$temporary/window.png" <<'PY'
from pathlib import Path
import sys
import zlib

raw = Path(sys.argv[1]).read_bytes()
if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n":
    raise SystemExit("capture is not a PNG")
width = int.from_bytes(raw[16:20], "big")
height = int.from_bytes(raw[20:24], "big")
if width <= 0 or height <= 0:
    raise SystemExit(f"invalid PNG dimensions: {width}x{height}")
chunks = []
offset = 8
while offset < len(raw):
    size = int.from_bytes(raw[offset:offset + 4], "big")
    kind = raw[offset + 4:offset + 8]
    data = raw[offset + 8:offset + 8 + size]
    offset += size + 12
    if kind == b"IDAT":
        chunks.append(data)
pixels = zlib.decompress(b"".join(chunks))
stride = width * 3
previous = bytearray(stride)
rows = []
offset = 0
for _ in range(height):
    filter_type = pixels[offset]
    row = bytearray(pixels[offset + 1:offset + 1 + stride])
    offset += stride + 1
    for index in range(stride):
        left = row[index - 3] if index >= 3 else 0
        above = previous[index]
        upper_left = previous[index - 3] if index >= 3 else 0
        if filter_type == 1:
            row[index] = (row[index] + left) & 0xff
        elif filter_type == 2:
            row[index] = (row[index] + above) & 0xff
        elif filter_type == 3:
            row[index] = (row[index] + ((left + above) // 2)) & 0xff
        elif filter_type == 4:
            estimate = left + above - upper_left
            nearest = min((left, above, upper_left), key=lambda value: abs(estimate - value))
            row[index] = (row[index] + nearest) & 0xff
        elif filter_type != 0:
            raise SystemExit(f"unsupported PNG filter: {filter_type}")
    rows.append(row)
    previous = row
sample_offset = (width // 2) * 3
sample = bytes(rows[height * 3 // 4][sample_offset:sample_offset + 3])
if sample != bytes.fromhex("123456"):
    raise SystemExit(f"captured background pixel is {sample.hex()}, expected 123456")
print(f"XComposite PNG {width}x{height}")
PY
