#!/usr/bin/env python3
"""Exercise capture and targeted input against a real Hyprland session."""

import argparse
import base64
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path(__file__).with_name("e2e-hyprland.lua")


def run(
    args: list[str], env: dict[str, str], timeout: float = 10
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, env=env, text=True, capture_output=True, timeout=timeout, check=False
    )


def wait_for(description: str, probe, timeout: float = 15):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = probe()
            if value:
                return value
        except Exception as exc:  # The service may be half-started while polling.
            last_error = exc
        time.sleep(0.1)
    detail = f": {last_error}" if last_error else ""
    raise RuntimeError(f"timed out waiting for {description}{detail}")


def start_process(
    args: list[str], env: dict[str, str], log: Path
) -> subprocess.Popen[str]:
    handle = log.open("w")
    process = subprocess.Popen(
        args,
        env=env,
        text=True,
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    handle.close()
    return process


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def launch_client(
    env: dict[str, str], role: str, event_file: Path, log: Path
) -> subprocess.Popen[str]:
    script = """
import sys
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, Gtk

role, event_path = sys.argv[1:]
app = Gtk.Application(
    application_id=f"com.openai.codex.hyprland_e2e.{role}",
    flags=Gio.ApplicationFlags.NON_UNIQUE,
)

def record(event):
    with open(event_path, "a", encoding="utf-8") as handle:
        handle.write(event + "\\n")

def activate(application):
    window = Gtk.ApplicationWindow(application=application, title=f"codex-e2e-{role}")
    window.set_default_size(320, 200)
    color = (1.0, 0.0, 1.0) if role == "target" else (0.0, 1.0, 1.0)
    canvas = Gtk.DrawingArea()

    def draw(_area, context, _width, _height):
        context.set_source_rgb(*color)
        context.paint()

    canvas.set_draw_func(draw)
    canvas.set_focusable(True)
    click = Gtk.GestureClick()
    click.connect("released", lambda *_args: record("click"))
    canvas.add_controller(click)
    keys = Gtk.EventControllerKey()
    keys.connect(
        "key-pressed",
        lambda _controller, keyval, _keycode, _state: (
            record(f"key:{Gdk.keyval_name(keyval)}"),
            False,
        )[1],
    )
    window.add_controller(keys)
    window.set_child(canvas)
    window.present()
    canvas.grab_focus()
    record("ready")

app.connect("activate", activate)
raise SystemExit(app.run())
"""
    return start_process([sys.executable, "-c", script, role, str(event_file)], env, log)


def hypr_json(env: dict[str, str], *args: str) -> Any:
    completed = run(["hyprctl", "-j", *args], env)
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    return json.loads(completed.stdout)


def find_window(env: dict[str, str], title: str) -> dict[str, Any] | None:
    return next(
        (
            window
            for window in hypr_json(env, "clients")
            if window.get("title") == title
        ),
        None,
    )


def assert_event(path: Path, event: str) -> None:
    wait_for(event, lambda: path.is_file() and event in path.read_text().splitlines())


def png_center_rgb(raw: bytes) -> tuple[int, int, int]:
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf

    loader = GdkPixbuf.PixbufLoader.new_with_type("png")
    loader.write(raw)
    loader.close()
    pixbuf = loader.get_pixbuf()
    if pixbuf is None:
        raise RuntimeError("decoded PNG has no pixels")
    channels = pixbuf.get_n_channels()
    offset = (
        pixbuf.get_height() // 2 * pixbuf.get_rowstride()
        + pixbuf.get_width() // 2 * channels
    )
    pixels = pixbuf.get_pixels()
    return tuple(pixels[offset : offset + 3])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--drm", action="store_true", help="launch directly on the available DRM seat")
    args = parser.parse_args()

    processes: list[subprocess.Popen[str]] = []
    logs: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="cu-", dir="/tmp") as temporary:
        work = Path(temporary)
        runtime = work
        state = work / "state"
        cache = work / "cache"
        for directory in (state, cache):
            directory.mkdir(mode=0o700)
        runtime.chmod(0o700)

        env = os.environ.copy()
        outer_runtime = Path(env.get("XDG_RUNTIME_DIR", "/tmp"))
        outer_display = env.get("WAYLAND_DISPLAY")
        env.update(
            {
                "XDG_RUNTIME_DIR": str(runtime),
                "XDG_STATE_HOME": str(state),
                "XDG_CACHE_HOME": str(cache),
                "HYPRLAND_NO_SD_VARS": "1",
            }
        )
        env.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
        env.pop("HYPRLAND_CMD", None)

        try:
            if args.drm:
                env.pop("WAYLAND_DISPLAY", None)
            else:
                if not outer_display:
                    raise RuntimeError("WAYLAND_DISPLAY is required unless --drm is used")
                os.symlink(outer_runtime / outer_display, runtime / "codex-e2e-parent")
                env["WAYLAND_DISPLAY"] = "codex-e2e-parent"

            hyprland_log = work / "hyprland.log"
            logs.append(hyprland_log)
            hyprland = start_process(["Hyprland", "--config", str(CONFIG)], env, hyprland_log)
            processes.append(hyprland)

            def nested_instance() -> dict[str, Any] | None:
                completed = run(["hyprctl", "instances", "-j"], env)
                if completed.returncode:
                    return None
                return next(
                    (
                        item
                        for item in json.loads(completed.stdout)
                        if item.get("pid") == hyprland.pid
                    ),
                    None,
                )

            instance = wait_for("nested Hyprland instance", nested_instance)
            inner = env | {
                "HYPRLAND_INSTANCE_SIGNATURE": str(instance["instance"]),
                "WAYLAND_DISPLAY": str(instance["wl_socket"]),
                "GDK_BACKEND": "wayland",
                "PYTHONPATH": str(ROOT / "src"),
            }
            wait_for("nested Hyprland monitor", lambda: hypr_json(inner, "monitors"))

            target_events = work / "target.events"
            sentinel_events = work / "sentinel.events"
            for role, events in (("target", target_events), ("sentinel", sentinel_events)):
                log = work / f"{role}.log"
                logs.append(log)
                processes.append(launch_client(inner, role, events, log))
                assert_event(events, "ready")

            target = wait_for(
                "target window", lambda: find_window(inner, "codex-e2e-target")
            )
            sentinel = wait_for(
                "sentinel window", lambda: find_window(inner, "codex-e2e-sentinel")
            )
            focus = run(
                [
                    "hyprctl",
                    "dispatch",
                    f"hl.dsp.focus({{ window = 'address:{sentinel['address']}' }})",
                ],
                inner,
            )
            if focus.returncode or "ok" not in focus.stdout.lower():
                raise RuntimeError(focus.stderr.strip() or focus.stdout.strip())
            wait_for(
                "sentinel focus",
                lambda: hypr_json(inner, "activewindow").get("address")
                == sentinel["address"],
            )

            os.environ.update(inner)
            from same_session_computer_use import server
            from same_session_computer_use.native_plugin import PLUGIN_PACKAGES

            missing_pkg_config = [
                package
                for package in PLUGIN_PACKAGES
                if run(["pkg-config", "--exists", package], inner).returncode
            ]
            if missing_pkg_config:
                raise RuntimeError(
                    f"missing plugin pkg-config modules: {missing_pkg_config}"
                )

            server._SESSION_ATTACHED = True
            physical_output = hypr_json(inner, "monitors")[0]["name"]
            continuity = server.enable_headless_continuity()
            if continuity.get("enabled") is not True:
                raise RuntimeError(
                    f"headless continuity did not become active: {continuity}"
                )
            continuity_output = continuity["output"]
            disabled = run(
                [
                    "hyprctl",
                    "eval",
                    f"hl.monitor({{ output = {json.dumps(physical_output)}, disabled = true }})",
                ],
                inner,
            )
            if disabled.returncode or "ok" not in disabled.stdout.lower():
                raise RuntimeError(
                    disabled.stderr.strip() or disabled.stdout.strip()
                )

            def only_continuity_output() -> bool:
                names = {
                    monitor.get("name")
                    for monitor in hypr_json(inner, "monitors")
                }
                if names != {continuity_output}:
                    raise RuntimeError(f"active outputs are {sorted(names)}")
                return True

            wait_for(
                "physical output disable",
                only_continuity_output,
            )
            target = wait_for(
                "target migration to continuity output",
                lambda: (
                    candidate
                    if (
                        (candidate := find_window(inner, "codex-e2e-target"))
                        and int(candidate.get("monitor", -1)) >= 0
                    )
                    else None
                ),
            )
            sentinel = wait_for(
                "sentinel migration to continuity output",
                lambda: (
                    candidate
                    if (
                        (candidate := find_window(inner, "codex-e2e-sentinel"))
                        and int(candidate.get("monitor", -1)) >= 0
                    )
                    else None
                ),
            )
            selected = server.resolve_window(target["address"])
            capture = server.capture_result(
                {"window": target["address"]},
                selected=selected,
            )
            if capture["content"][1].get("mimeType") != "image/png":
                raise RuntimeError("exact target capture did not return a PNG")
            png = base64.b64decode(capture["content"][1]["data"], validate=True)
            center = png_center_rgb(png)
            if not (center[0] >= 250 and center[1] <= 5 and center[2] >= 250):
                raise RuntimeError(
                    f"exact target capture center was {center}, not target magenta"
                )

            server.ensure_native_input_safe()
            time.sleep(0.3)
            before = {
                "focus": hypr_json(inner, "activewindow").get("address"),
                "workspace": hypr_json(inner, "activeworkspace").get("id"),
                "cursor": hypr_json(inner, "cursorpos"),
            }
            size = selected["size"]
            result = server._targeted_pointer(
                {"window": target["address"], "x": size[0] / 2, "y": size[1] / 2},
                "click",
                window=selected,
            )
            if result.get("observed_physical_state_unchanged") is not True:
                raise RuntimeError("targeted click changed nested compositor state")
            assert_event(target_events, "click")

            plugin_status = hypr_json(inner, "cutargetstatus")
            clicks_before_rejection = target_events.read_text().splitlines().count("click")
            rejected_batch = hypr_json(
                inner,
                "cutargetbatch",
                "v1",
                plugin_status["identity_token"],
                target["address"],
                "2",
                "click",
                str(size[0] / 2),
                str(size[1] / 2),
                "left",
                "1",
                "click",
                str(size[0] + 10_000),
                str(size[1] / 2),
                "left",
                "1",
            )
            time.sleep(0.2)
            if rejected_batch.get("ok") is not False or (
                target_events.read_text().splitlines().count("click")
                != clicks_before_rejection
            ):
                raise RuntimeError(
                    f"native pointer batch was not prevalidated atomically: {rejected_batch}"
                )

            batch_result = hypr_json(
                inner,
                "cutargetbatch",
                "v1",
                plugin_status["identity_token"],
                target["address"],
                "1",
                "click",
                str(size[0] / 2),
                str(size[1] / 2),
                "left",
                "1",
            )
            if (
                batch_result.get("ok") is not True
                or batch_result.get("batch_protocol_version") != 1
                or batch_result.get("completed") != 1
                or batch_result.get("observed_physical_state_unchanged") is not True
            ):
                raise RuntimeError(f"native pointer batch failed: {batch_result}")
            wait_for(
                "batched click",
                lambda: target_events.is_file()
                and target_events.read_text().splitlines().count("click") >= 2,
            )

            server.send_window_shortcut({"address": target["address"], "key": "x"})
            assert_event(target_events, "key:x")
            after = {
                "focus": hypr_json(inner, "activewindow").get("address"),
                "workspace": hypr_json(inner, "activeworkspace").get("id"),
                "cursor": hypr_json(inner, "cursorpos"),
            }
            if after != before:
                raise RuntimeError(
                    f"background actions changed compositor state: before={before}, after={after}"
                )
            verified = server.capture_result(
                {"window": target["address"]},
                selected=server.resolve_window(target["address"]),
            )
            verified_png = base64.b64decode(
                verified["content"][1]["data"], validate=True
            )
            verified_center = png_center_rgb(verified_png)
            if not (
                verified_center[0] >= 250
                and verified_center[1] <= 5
                and verified_center[2] >= 250
            ):
                raise RuntimeError(
                    f"post-action monitor-off capture center was {verified_center}"
                )
            if hyprland.poll() is not None:
                raise RuntimeError("Hyprland exited during monitor-off Computer Use")
            for title, expected in (
                ("codex-e2e-target", target["pid"]),
                ("codex-e2e-sentinel", sentinel["pid"]),
            ):
                current = find_window(inner, title)
                if current is None or current.get("pid") != expected:
                    raise RuntimeError(
                        f"{title} did not preserve its process while monitor-off"
                    )

            enabled = run(
                [
                    "hyprctl",
                    "eval",
                    (
                        "hl.monitor({ "
                        f"output = {json.dumps(physical_output)}, "
                        'mode = "800x600@60", position = "0x0", scale = 1 })'
                    ),
                ],
                inner,
            )
            if enabled.returncode:
                raise RuntimeError(
                    enabled.stderr.strip() or enabled.stdout.strip()
                )
            physical_monitor = wait_for(
                "physical output re-enable",
                lambda: next(
                    (
                        monitor
                        for monitor in hypr_json(inner, "monitors")
                        if monitor.get("name") == physical_output
                    ),
                    None,
                ),
            )
            physical_monitor_id = int(physical_monitor["id"])
            for title in ("codex-e2e-target", "codex-e2e-sentinel"):
                wait_for(
                    f"{title} return to physical output",
                    lambda title=title: (
                        candidate
                        if (
                            (candidate := find_window(inner, title))
                            and int(candidate.get("monitor", -1))
                            == physical_monitor_id
                        )
                        else None
                    ),
                )
            continuity = server.disable_headless_continuity()
            if continuity.get("removed") is not True:
                raise RuntimeError(
                    f"headless continuity was not removed: {continuity}"
                )
            wait_for(
                "continuity output removal",
                lambda: {
                    monitor.get("name")
                    for monitor in hypr_json(inner, "monitors")
                }
                == {physical_output},
            )
            server.capture_result(
                {"window": target["address"]},
                selected=server.resolve_window(target["address"]),
            )

            print(
                "monitor-off capture, process continuity, targeted click, native batch, and targeted shortcut passed"
            )
            return 0
        except Exception:
            for log in logs:
                if log.is_file():
                    print(f"--- {log.name} ---", file=sys.stderr)
                    print(log.read_text(errors="replace")[-12_000:], file=sys.stderr)
            raise
        finally:
            for process in reversed(processes):
                stop_process(process)


if __name__ == "__main__":
    raise SystemExit(main())
