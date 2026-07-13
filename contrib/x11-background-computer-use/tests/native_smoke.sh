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
xcompmgr -a >"$temporary/xcompmgr.log" 2>&1 &
pids+=("$!")
xterm -fa monospace -title Codex-X11-Native-Smoke -e sh -c 'exec sleep 30' >"$temporary/xterm.log" 2>&1 &
xterm_pid=$!
pids+=("$xterm_pid")

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
  cat "$temporary/openbox.log" "$temporary/xterm.log"
  echo "EWMH window manager or xterm client did not become ready" >&2
  exit 1
fi

authenticated_pid=$($helper --pid "$window_id")
if [[ $authenticated_pid != "$xterm_pid" ]]; then
  echo "XRes authenticated PID $authenticated_pid, expected $xterm_pid" >&2
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

raw = Path(sys.argv[1]).read_bytes()
if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n":
    raise SystemExit("capture is not a PNG")
width = int.from_bytes(raw[16:20], "big")
height = int.from_bytes(raw[20:24], "big")
if width <= 0 or height <= 0:
    raise SystemExit(f"invalid PNG dimensions: {width}x{height}")
print(f"XComposite PNG {width}x{height}")
PY
