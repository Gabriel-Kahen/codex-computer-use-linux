import base64
import json
import os
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from subprocess import CompletedProcess
from unittest import TestCase
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from x11_session_computer_use import capture


def png(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big")


class CaptureBuildTests(TestCase):
    def test_parallel_requests_compile_once_and_reuse_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            compile_count = 0
            count_lock = threading.Lock()

            def fake_run(args, **_kwargs):
                nonlocal compile_count
                if args[:2] == ["pkg-config", "--cflags"]:
                    return CompletedProcess(args, 0, "-lX11 -lXcomposite -lpng\n", "")
                if "-o" in args:
                    with count_lock:
                        compile_count += 1
                    output = Path(args[args.index("-o") + 1])
                    output.write_bytes(b"executable")
                    return CompletedProcess(args, 0, "", "")
                raise AssertionError(args)

            with patch.object(capture, "CACHE_ROOT", Path(temporary)), patch.object(capture, "build_requirements", return_value={"all": True}), patch.object(capture.subprocess, "run", side_effect=fake_run):
                with ThreadPoolExecutor(max_workers=4) as executor:
                    results = list(executor.map(lambda _: capture.ensure_capture_helper(), range(4)))
                repeated = capture.ensure_capture_helper()

            self.assertEqual(compile_count, 1)
            self.assertEqual(len(set(results)), 1)
            self.assertEqual(repeated, results[0])
            self.assertTrue(results[0].is_file())
            self.assertTrue(os.access(results[0], os.X_OK))

    def test_build_reports_missing_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(capture, "CACHE_ROOT", Path(temporary)), patch.object(capture, "build_requirements", return_value={"cc": False, "pkg-config": True}):
            with self.assertRaisesRegex(RuntimeError, "cc"):
                capture.ensure_capture_helper()


class BrokerCaptureTests(TestCase):
    def setUp(self) -> None:
        self.window = {
            "xid": "0x01200007",
            "width": 640,
            "height": 480,
            "mapped": True,
            "minimized": False,
        }

    def test_capture_returns_png_and_coordinate_metadata(self) -> None:
        raw = png(1280, 960)

        def fake_run(args, **_kwargs):
            Path(args[2]).write_bytes(raw)
            return CompletedProcess(args, 0, "", "")

        with patch.object(capture, "ensure_capture_helper", return_value=Path("/helper")), patch.object(capture.subprocess, "run", side_effect=fake_run):
            result = capture.capture_window(self.window, None)

        self.assertEqual(base64.b64decode(result["content"][1]["data"]), raw)
        self.assertNotIn("structuredContent", result)
        metadata = json.loads(result["content"][0]["text"])
        self.assertEqual(metadata["coordinate_space"], {
            "window_local": {"width": 640, "height": 480},
            "screenshot_pixels": {"width": 1280, "height": 960},
        })
        self.assertEqual(metadata["capture_backend"], "XComposite named window pixmap")

    def test_largest_capture_fits_the_stdio_json_rpc_limit(self) -> None:
        raw = png(1280, 960) + b"x" * (capture.MAX_CAPTURE_BYTES - 24)

        def fake_run(args, **_kwargs):
            Path(args[2]).write_bytes(raw)
            return CompletedProcess(args, 0, "", "")

        with patch.object(capture, "ensure_capture_helper", return_value=Path("/helper")), patch.object(capture.subprocess, "run", side_effect=fake_run):
            result = capture.capture_window(self.window, None)

        response = {"jsonrpc": "2.0", "id": 1, "result": result}
        self.assertLess(len(json.dumps(response, separators=(",", ":")).encode()), 8 * 1024 * 1024)

    def test_save_path_replaces_destination_only_after_capture_succeeds(self) -> None:
        raw = png(640, 480)
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "capture.png"
            destination.write_bytes(b"old")

            def fake_run(args, **_kwargs):
                self.assertEqual(destination.read_bytes(), b"old")
                output = Path(args[2])
                self.assertNotEqual(output, destination)
                output.write_bytes(raw)
                return CompletedProcess(args, 0, "", "")

            with patch.object(capture, "ensure_capture_helper", return_value=Path("/helper")), patch.object(capture.subprocess, "run", side_effect=fake_run):
                result = capture.capture_window(self.window, str(destination))

            self.assertEqual(destination.read_bytes(), raw)
            self.assertEqual(json.loads(result["content"][0]["text"])["saved_to"], str(destination))
            self.assertEqual(list(Path(temporary).iterdir()), [destination])

    def test_failed_capture_preserves_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "capture.png"
            destination.write_bytes(b"old")
            failed = CompletedProcess([], 1, "", "capture failed")

            with patch.object(capture, "ensure_capture_helper", return_value=Path("/helper")), patch.object(capture.subprocess, "run", return_value=failed):
                with self.assertRaisesRegex(RuntimeError, "capture failed"):
                    capture.capture_window(self.window, str(destination))

            self.assertEqual(destination.read_bytes(), b"old")
            self.assertEqual(list(Path(temporary).iterdir()), [destination])

    def test_invalid_capture_preserves_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "capture.png"
            destination.write_bytes(b"old")

            def fake_run(args, **_kwargs):
                Path(args[2]).write_bytes(b"not a PNG")
                return CompletedProcess(args, 0, "", "")

            with patch.object(capture, "ensure_capture_helper", return_value=Path("/helper")), patch.object(capture.subprocess, "run", side_effect=fake_run):
                with self.assertRaisesRegex(RuntimeError, "invalid PNG"):
                    capture.capture_window(self.window, str(destination))

            self.assertEqual(destination.read_bytes(), b"old")

    def test_oversized_capture_is_not_read_or_installed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "capture.png"
            destination.write_bytes(b"old")

            def fake_run(args, **_kwargs):
                Path(args[2]).write_bytes(png(640, 480) + b"x" * 32)
                return CompletedProcess(args, 0, "", "")

            original_read_bytes = Path.read_bytes

            def checked_read_bytes(path):
                if path != destination:
                    self.fail("oversized capture was read into memory")
                return original_read_bytes(path)

            with patch.object(capture, "MAX_CAPTURE_BYTES", 31), patch.object(capture, "ensure_capture_helper", return_value=Path("/helper")), patch.object(capture.subprocess, "run", side_effect=fake_run), patch.object(Path, "read_bytes", checked_read_bytes):
                with self.assertRaisesRegex(RuntimeError, "maximum transport size is 31 bytes"):
                    capture.capture_window(self.window, str(destination))

            self.assertEqual(destination.read_bytes(), b"old")
            self.assertEqual(list(Path(temporary).iterdir()), [destination])
