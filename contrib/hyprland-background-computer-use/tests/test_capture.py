import json
import os
import subprocess
import struct
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from same_session_computer_use import server


WINDOW = {
    "capture_id": "42",
    "address": "0x1",
    "pid": 123,
    "monitor": 0,
    "size": [100, 100],
}


def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", zlib.crc32(data, zlib.crc32(chunk_type)))


def png(width: int = 100, height: int = 100) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = b"".join(b"\0" + b"\0\0\0" * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header) + png_chunk(b"IDAT", zlib.compress(pixels)) + png_chunk(b"IEND", b"")


PNG = png()


class CaptureSaveTests(TestCase):
    def test_monitorless_window_never_reaches_grim(self) -> None:
        orphaned = {**WINDOW, "monitor": -1}
        with patch.object(
            server, "resolve_window", return_value=orphaned
        ), patch.object(server, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "no active output"):
                server.capture_result({"window": "target"}, selected=WINDOW)

        run.assert_not_called()

    def test_changed_window_identity_never_reaches_grim(self) -> None:
        replacement = {**WINDOW, "capture_id": "99"}
        with patch.object(
            server, "resolve_window", return_value=replacement
        ), patch.object(server, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                server.capture_result({"window": "target"}, selected=WINDOW)

        run.assert_not_called()

    def test_failed_capture_preserves_existing_destination(self) -> None:
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "capture.png"
            destination.write_bytes(b"keep me")
            failed = subprocess.CompletedProcess([], 1, "", "grim failed")

            with patch.object(server, "resolve_window", return_value=WINDOW), patch.object(server, "run", return_value=failed):
                with self.assertRaisesRegex(RuntimeError, "grim failed"):
                    server.capture_result({"window": "target", "save_path": str(destination)})

            self.assertEqual(destination.read_bytes(), b"keep me")

    def test_successful_capture_atomically_replaces_destination(self) -> None:
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "capture.png"
            destination.write_bytes(b"old")

            def capture(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                Path(args[-1]).write_bytes(PNG)
                return subprocess.CompletedProcess(args, 0, "", "")

            with patch.object(server, "resolve_window", return_value=WINDOW), patch.object(server, "run", side_effect=capture):
                result = server.capture_result({"window": "target", "save_path": str(destination)})

            self.assertEqual(destination.read_bytes(), PNG)
            self.assertNotIn("structuredContent", result)
            self.assertEqual(json.loads(result["content"][0]["text"])["saved_to"], str(destination))
            self.assertEqual(result["content"][1]["mimeType"], "image/png")

    def test_coordinate_capture_keeps_the_image_content_visible_to_codex(self) -> None:
        state = {
            "token": "secret",
            "output": "HEADLESS-1",
            "target": WINDOW,
            "fallback": {"fullscreen_applied": False},
        }

        def capture(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            Path(args[-1]).write_bytes(PNG)
            return subprocess.CompletedProcess(args, 0, "", "")

        monitor = {"name": "HEADLESS-1", "x": 0, "y": 0, "width": 100, "height": 100, "scale": 1}
        with (
            patch.object(server, "require_lease", return_value=state),
            patch.object(server, "run", side_effect=capture),
            patch.object(server, "hypr_json", return_value=[monitor]),
            patch.object(server, "combine_windows", return_value=[WINDOW]),
        ):
            result = server.capture_lease("secret")

        self.assertNotIn("structuredContent", result)
        self.assertEqual(result["content"][1]["mimeType"], "image/png")

    def test_coordinate_capture_rejects_invalid_png_and_removes_temporary_file(self) -> None:
        state = {"token": "secret", "output": "HEADLESS-1", "target": WINDOW}
        with TemporaryDirectory() as directory:
            capture_path = Path(directory) / "capture.png"
            descriptor = os.open(capture_path, os.O_CREAT | os.O_RDWR, 0o600)

            def capture(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                Path(args[-1]).write_bytes(b"not a PNG")
                return subprocess.CompletedProcess(args, 0, "", "")

            with (
                patch.object(server, "require_lease", return_value=state),
                patch.object(server.tempfile, "mkstemp", return_value=(descriptor, str(capture_path))),
                patch.object(server, "run", side_effect=capture),
            ):
                with self.assertRaisesRegex(RuntimeError, "invalid PNG"):
                    server.capture_lease("secret")

            self.assertFalse(capture_path.exists())

    def test_coordinate_capture_rejects_oversized_png_and_removes_temporary_file(self) -> None:
        state = {"token": "secret", "output": "HEADLESS-1", "target": WINDOW}
        with TemporaryDirectory() as directory:
            capture_path = Path(directory) / "capture.png"
            descriptor = os.open(capture_path, os.O_CREAT | os.O_RDWR, 0o600)

            def capture(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                Path(args[-1]).write_bytes(PNG + b"x" * server.MAX_CAPTURE_PNG_BYTES)
                return subprocess.CompletedProcess(args, 0, "", "")

            with (
                patch.object(server, "require_lease", return_value=state),
                patch.object(server.tempfile, "mkstemp", return_value=(descriptor, str(capture_path))),
                patch.object(server, "run", side_effect=capture),
            ):
                with self.assertRaisesRegex(RuntimeError, "MCP transport limit"):
                    server.capture_lease("secret")

            self.assertFalse(capture_path.exists())

    def test_invalid_capture_preserves_existing_destination(self) -> None:
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "capture.png"
            destination.write_bytes(b"keep me")

            def capture(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                Path(args[-1]).write_bytes(b"not a PNG")
                return subprocess.CompletedProcess(args, 0, "", "")

            with patch.object(server, "resolve_window", return_value=WINDOW), patch.object(
                server, "run", side_effect=capture
            ):
                with self.assertRaisesRegex(RuntimeError, "invalid PNG"):
                    server.capture_result({"window": "target", "save_path": str(destination)})

            self.assertEqual(destination.read_bytes(), b"keep me")

    def test_header_only_capture_preserves_existing_destination(self) -> None:
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "capture.png"
            destination.write_bytes(b"keep me")

            def capture(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                Path(args[-1]).write_bytes(PNG[:24])
                return subprocess.CompletedProcess(args, 0, "", "")

            with patch.object(server, "resolve_window", return_value=WINDOW), patch.object(
                server, "run", side_effect=capture
            ):
                with self.assertRaisesRegex(RuntimeError, "invalid PNG"):
                    server.capture_result({"window": "target", "save_path": str(destination)})

            self.assertEqual(destination.read_bytes(), b"keep me")

    def test_corrupt_scanline_capture_preserves_existing_destination(self) -> None:
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "capture.png"
            destination.write_bytes(b"keep me")
            corrupt = bytearray(png(1, 1))
            idat = corrupt.index(b"IDAT")
            data_start = idat + 4
            data_length = int.from_bytes(corrupt[idat - 4:idat], "big")
            invalid_pixels = zlib.compress(b"\x05\0\0\0")
            self.assertEqual(len(invalid_pixels), data_length)
            corrupt[data_start : data_start + data_length] = invalid_pixels
            corrupt[data_start + data_length : data_start + data_length + 4] = struct.pack(
                ">I", zlib.crc32(invalid_pixels, zlib.crc32(b"IDAT"))
            )

            def capture(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                Path(args[-1]).write_bytes(corrupt)
                return subprocess.CompletedProcess(args, 0, "", "")

            with patch.object(server, "resolve_window", return_value=WINDOW), patch.object(
                server, "run", side_effect=capture
            ):
                with self.assertRaisesRegex(RuntimeError, "invalid PNG"):
                    server.capture_result({"window": "target", "save_path": str(destination)})

            self.assertEqual(destination.read_bytes(), b"keep me")

    def test_oversized_capture_preserves_existing_destination(self) -> None:
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "capture.png"
            destination.write_bytes(b"keep me")

            def capture(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                Path(args[-1]).write_bytes(PNG + b"x" * server.MAX_CAPTURE_PNG_BYTES)
                return subprocess.CompletedProcess(args, 0, "", "")

            with patch.object(server, "resolve_window", return_value=WINDOW), patch.object(
                server, "run", side_effect=capture
            ):
                with self.assertRaisesRegex(RuntimeError, "MCP transport limit"):
                    server.capture_result({"window": "target", "save_path": str(destination)})

            self.assertEqual(destination.read_bytes(), b"keep me")

    def test_capture_timeout_removes_temporary_file(self) -> None:
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "capture.png"
            destination.write_bytes(b"keep me")

            with patch.object(server, "resolve_window", return_value=WINDOW), patch.object(
                server, "run", side_effect=subprocess.TimeoutExpired("grim", 20)
            ):
                with self.assertRaises(subprocess.TimeoutExpired):
                    server.capture_result({"window": "target", "save_path": str(destination)})

            self.assertEqual(destination.read_bytes(), b"keep me")
            self.assertEqual(list(destination.parent.glob(".capture.png.*.tmp")), [])
