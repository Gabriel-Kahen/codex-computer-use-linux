import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest import skipUnless
from unittest.mock import patch

from support import MODULE_ROOT
from support import ROOT

import sys

sys.path.insert(0, str(MODULE_ROOT))

from plasma_same_session import kwin
from plasma_same_session import server


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
WINDOW = {
    "id": "{target}",
    "capture_id": "target",
    "title": "Editor",
    "class": "code",
    "pid": 123,
    "desktop": 3,
    "active": False,
    "minimized": False,
    "fullscreen": False,
    "excluded_from_capture": False,
    "geometry": {"x": 0, "y": 0, "width": 1000, "height": 700},
}


class BrokerCaptureTests(TestCase):
    @patch.object(kwin, "screen_locked", return_value=False)
    @patch.object(kwin, "pointer_position", side_effect=[{"x": 4, "y": 5}, {"x": 4, "y": 5}])
    @patch.object(kwin, "current_desktop", side_effect=[2, 2])
    @patch.object(kwin, "active_window_id", side_effect=["{other}", "{other}"])
    @patch.object(kwin, "resolve_window", return_value=WINDOW)
    def test_capture_result_atomically_replaces_destination_and_returns_png_metadata(
        self, _resolve, _active, _desktop, _pointer, _locked
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "capture.png"
            destination.write_bytes(b"old-image")

            def write_capture(window_id: str, temporary: Path) -> None:
                self.assertEqual(window_id, "{target}")
                self.assertNotEqual(temporary, destination)
                self.assertEqual(destination.read_bytes(), b"old-image")
                temporary.write_bytes(PNG)

            with patch.object(kwin, "capture_window", side_effect=write_capture):
                result = server.capture_result({"window": "target", "save_path": str(destination)})

            self.assertEqual(destination.read_bytes(), PNG)
            self.assertEqual(base64.b64decode(result["content"][1]["data"]), PNG)
            self.assertNotIn("structuredContent", result)
            self.assertEqual(
                json.loads(result["content"][0]["text"]),
                {
                    "window": WINDOW,
                    "saved_to": str(destination),
                    "compositor_capture": "org.kde.KWin.ScreenShot2.CaptureWindow",
                    "observed_physical_state_unchanged": True,
                    "physical_state_before": {"focus": "{other}", "desktop": 2, "pointer": {"x": 4, "y": 5}},
                    "physical_state_after": {"focus": "{other}", "desktop": 2, "pointer": {"x": 4, "y": 5}},
                },
            )

    @patch.object(kwin, "resolve_window")
    @patch.object(kwin, "screen_locked", return_value=True)
    def test_capture_refuses_locked_session(self, _locked, resolve) -> None:
        with self.assertRaisesRegex(RuntimeError, "session is locked"):
            server.capture_result({"window": "target"})
        resolve.assert_not_called()

    @patch.object(kwin, "resolve_window", return_value=WINDOW)
    @patch.object(kwin, "screen_locked", side_effect=[False, True])
    def test_capture_rechecks_lock_immediately_before_compositor_action(self, _locked, _resolve) -> None:
        with patch.object(kwin, "capture_window") as capture:
            with self.assertRaisesRegex(RuntimeError, "session is locked"):
                server.capture_result({"window": "target"})

        capture.assert_not_called()

    @patch.object(kwin, "pointer_position", return_value={"x": 4, "y": 5})
    @patch.object(kwin, "current_desktop", return_value=2)
    @patch.object(kwin, "active_window_id", return_value="{other}")
    @patch.object(kwin, "resolve_window", return_value=WINDOW)
    @patch.object(kwin, "screen_locked", return_value=False)
    def test_capture_rejects_a_png_above_the_transport_limit(self, *_mocks) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "capture.png"
            destination.write_bytes(b"old-image")

            def write_capture(_window_id: str, temporary: Path) -> None:
                temporary.write_bytes(PNG)

            with (
                patch.object(kwin, "capture_window", side_effect=write_capture),
                patch.object(server, "MAX_CAPTURE_BYTES", len(PNG) - 1),
            ):
                with self.assertRaisesRegex(RuntimeError, "safety limit"):
                    server.capture_result({"window": "target", "save_path": str(destination)})

            self.assertEqual(destination.read_bytes(), b"old-image")


class KWinCaptureTests(TestCase):
    def test_capture_window_invokes_helper_and_records_session_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "plasma-same-session-capture"
            helper.write_bytes(b"helper")
            helper.chmod(0o755)
            output = root / "window.png"
            state = root / "state"
            identity = {
                "uid": os.getuid(),
                "boot_id": "boot-test",
                "wayland_display": "wayland-test",
                "wayland_socket": None,
                "session_id": "session-test",
                "kwin_service_owner": ":1.42",
            }

            def run_helper(args: list[str], *, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
                self.assertEqual(args, [str(helper), "target", str(output)])
                self.assertEqual(timeout, 30)
                self.assertEqual(session_identity.call_count, 1)
                output.write_bytes(PNG)
                return subprocess.CompletedProcess(args, 0, "", "")

            with (
                patch.object(kwin, "STATE_DIR", state),
                patch.object(kwin, "build_capture_helper", return_value=helper),
                patch.object(kwin, "run", side_effect=run_helper),
                patch.object(kwin, "session_identity", return_value=identity) as session_identity,
            ):
                kwin.capture_window("{target}", output)
                self.assertTrue(kwin.capture_authorized_in_current_session())

            marker = state / "exact-capture-authorized"
            self.assertEqual(output.read_bytes(), PNG)
            self.assertEqual(json.loads(marker.read_text()), identity)
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)

    def test_capture_window_does_not_publish_authorization_after_a_session_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "plasma-same-session-capture"
            output = root / "window.png"
            state = root / "state"
            identity = {
                "uid": os.getuid(),
                "boot_id": "boot-test",
                "wayland_display": "wayland-test",
                "wayland_socket": None,
                "session_id": "session-test",
                "kwin_service_owner": ":1.42",
            }
            changed = {**identity, "kwin_service_owner": ":1.99"}

            def run_helper(_args: list[str], *, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
                self.assertEqual(timeout, 30)
                output.write_bytes(PNG)
                return subprocess.CompletedProcess(_args, 0, "", "")

            with (
                patch.object(kwin, "STATE_DIR", state),
                patch.object(kwin, "build_capture_helper", return_value=helper),
                patch.object(kwin, "run", side_effect=run_helper),
                patch.object(kwin, "session_identity", side_effect=[identity, changed]),
            ):
                with self.assertRaisesRegex(RuntimeError, "session identity changed"):
                    kwin.capture_window("{target}", output)

            self.assertTrue(output.exists())
            self.assertFalse((state / "exact-capture-authorized").exists())

    def test_capture_window_requires_positive_kwin_ownership_after_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "plasma-same-session-capture"
            output = root / "window.png"
            state = root / "state"
            identity = {
                "uid": os.getuid(),
                "boot_id": "boot-test",
                "wayland_display": "wayland-test",
                "wayland_socket": None,
                "session_id": "session-test",
                "kwin_service_owner": ":1.42",
            }

            def run_helper(_args: list[str], *, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
                self.assertEqual(timeout, 30)
                output.write_bytes(PNG)
                return subprocess.CompletedProcess(_args, 0, "", "")

            with (
                patch.object(kwin, "STATE_DIR", state),
                patch.object(kwin, "build_capture_helper", return_value=helper),
                patch.object(kwin, "run", side_effect=run_helper),
                patch.object(
                    kwin,
                    "session_identity",
                    side_effect=[identity, {**identity, "kwin_service_owner": None}],
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "ownership could not be positively identified"):
                    kwin.capture_window("{target}", output)

            self.assertTrue(output.exists())
            self.assertFalse((state / "exact-capture-authorized").exists())

    def test_successful_helper_build_atomically_replaces_cache_and_installs_desktop_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugin"
            source = root / "kwin/capture-helper.cpp"
            source.parent.mkdir(parents=True)
            source.write_text("// capture helper source\n")
            cache = Path(directory) / "cache"
            data = Path(directory) / "data"
            key = hashlib.sha256(source.read_bytes()).hexdigest()
            helper = cache / "capture-helper" / key / "plasma-same-session-capture"
            helper.parent.mkdir(parents=True)
            helper.write_bytes(b"stale")
            helper.chmod(0o644)
            compile_commands: list[list[str]] = []

            def fake_run(args: list[str], *, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
                if args[:2] == ["pkg-config", "--exists"]:
                    return subprocess.CompletedProcess(args, 0, "", "")
                if args[:2] == ["pkg-config", "--cflags"]:
                    return subprocess.CompletedProcess(args, 0, "-I'/tmp/qt include' -lQt6Core", "")
                compile_commands.append(args)
                self.assertEqual(timeout, 60)
                output = Path(args[args.index("-o") + 1])
                self.assertNotEqual(output, helper)
                self.assertEqual(helper.read_bytes(), b"stale")
                output.write_bytes(b"new-helper")
                return subprocess.CompletedProcess(args, 0, "", "")

            with (
                patch.object(kwin, "ROOT", root),
                patch.object(kwin, "CACHE_DIR", cache),
                patch.object(kwin, "run", side_effect=fake_run),
                patch.object(kwin.shutil, "which", return_value=None),
                patch.dict(os.environ, {"CXX": "c++", "XDG_DATA_HOME": str(data)}, clear=False),
            ):
                built = kwin.build_capture_helper()

            self.assertEqual(built, helper)
            self.assertEqual(helper.read_bytes(), b"new-helper")
            self.assertEqual(stat.S_IMODE(helper.stat().st_mode), 0o755)
            self.assertEqual(len(compile_commands), 1)
            self.assertIn(str(source), compile_commands[0])
            self.assertIn("-I/tmp/qt include", compile_commands[0])
            desktop = data / "applications/plasma-same-session-capture.desktop"
            desktop_text = desktop.read_text()
            self.assertIn(f'Exec="{helper}" %U', desktop_text)
            self.assertIn("X-KDE-DBUS-Restricted-Interfaces=org.kde.KWin.ScreenShot2", desktop_text)
            self.assertNotIn("X-KDE-Wayland-Interfaces", desktop_text)

    def test_authorization_marker_without_a_session_discriminator_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            identity = {
                "uid": os.getuid(),
                "boot_id": "boot-test",
                "wayland_display": "wayland-0",
                "wayland_socket": None,
                "session_id": None,
                "kwin_service_owner": ":1.42",
            }
            marker = state / "exact-capture-authorized"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps(identity))

            with (
                patch.object(kwin, "STATE_DIR", state),
                patch.object(kwin, "session_identity", return_value=identity),
            ):
                self.assertFalse(kwin.capture_authorized_in_current_session())


QT_DBUS_AVAILABLE = (
    shutil.which("c++") is not None
    and shutil.which("pkg-config") is not None
    and shutil.which("dbus-run-session") is not None
    and subprocess.run(
        ["pkg-config", "--exists", "Qt6Core", "Qt6Gui", "Qt6DBus"],
        capture_output=True,
        check=False,
    ).returncode == 0
)


@skipUnless(QT_DBUS_AVAILABLE, "Qt 6 DBus development files and dbus-run-session are required")
class CaptureHelperIntegrationTests(TestCase):
    def test_helper_uses_screenshot2_pipe_fd_contract_and_writes_png(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            helper = directory_path / "capture-helper"
            service = directory_path / "mock-screenshot-service"
            flags = subprocess.check_output(
                ["pkg-config", "--cflags", "--libs", "Qt6Core", "Qt6Gui", "Qt6DBus"],
                text=True,
            ).split()
            for source, output in (
                (ROOT / "kwin/capture-helper.cpp", helper),
                (ROOT / "tests/mock_screenshot_service.cpp", service),
            ):
                subprocess.run(
                    ["c++", "-fPIC", "-std=c++17", str(source), "-o", str(output), *flags],
                    check=True,
                    capture_output=True,
                    text=True,
                )

            trace = directory_path / "trace.json"
            ready = directory_path / "ready"
            output = directory_path / "capture.png"
            script = """
"$1" "$3" "$4" &
service_pid=$!
i=0
while [ ! -f "$4" ] && [ "$i" -lt 100 ]; do
  sleep 0.02
  i=$((i + 1))
done
if [ ! -f "$4" ]; then
  kill "$service_pid" 2>/dev/null || true
  exit 1
fi
"$2" test-window-uuid "$5"
status=$?
if [ "$status" -ne 0 ]; then
  kill "$service_pid" 2>/dev/null || true
fi
wait "$service_pid" 2>/dev/null || true
exit "$status"
"""
            subprocess.run(
                [
                    "dbus-run-session",
                    "--",
                    "sh",
                    "-c",
                    script,
                    "sh",
                    str(service),
                    str(helper),
                    str(trace),
                    str(ready),
                    str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            )

            self.assertTrue(output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertEqual(json.loads(trace.read_text()), {
                "bytesWritten": 512 * 512 * 4,
                "handle": "test-window-uuid",
                "includeDecoration": False,
                "includeShadow": False,
                "nativeResolution": True,
                "pipeFileDescriptor": True,
            })
