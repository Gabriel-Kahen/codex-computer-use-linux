import json
import subprocess
import struct
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from gnome_same_session import server


def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(data, zlib.crc32(chunk_type))
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def png(width: int, height: int, padding: int = 0) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = b"".join(b"\0" + b"\0\0\0" * width for _ in range(height))
    chunks = [png_chunk(b"IHDR", header)]
    if padding:
        chunks.append(png_chunk(b"npAD", b"x" * padding))
    chunks.extend((png_chunk(b"IDAT", zlib.compress(pixels)), png_chunk(b"IEND", b"")))
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks)


class CaptureBoundaryTests(TestCase):
    def test_inactive_window_requires_lease(self) -> None:
        window = {"id": "11", "focused": False}
        with patch.object(server, "resolve_window", return_value=window), patch.object(server, "load_lease", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "begin_focus_lease"):
                server.capture_window({"window": "11"})

    def test_failed_capture_preserves_destination(self) -> None:
        window = {"id": "11", "focused": True, "frame": {"width": 1, "height": 1}}
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "capture.png"
            destination.write_bytes(b"old")
            with (
                patch.object(server, "resolve_window", return_value=window),
                patch.object(server, "load_lease", return_value=None),
                patch.object(server, "run", side_effect=lambda *_args, **_kwargs: type("Result", (), {"returncode": 1, "stderr": "failed"})()),
                patch.object(server.shutil, "which", return_value=None),
            ):
                with self.assertRaisesRegex(RuntimeError, "failed"):
                    server.capture_window({"window": "11", "save_path": str(destination)})

            self.assertEqual(destination.read_bytes(), b"old")
            self.assertEqual(list(destination.parent.glob(".capture.png.*.png")), [])

    def test_screenshot_signature_and_scaled_coordinate_transform(self) -> None:
        window = {"id": "11", "focused": True, "frame": {"width": 400, "height": 300}}
        commands: list[list[str]] = []

        def capture(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            commands.append(args)
            Path(args[-1]).write_bytes(png(800, 600))
            return subprocess.CompletedProcess(args, 0, "", "")

        with (
            patch.object(server, "resolve_window", return_value=window),
            patch.object(server, "load_lease", return_value=None),
            patch.object(server, "run", side_effect=capture),
        ):
            result = server.capture_window({"window": "11"})

        self.assertTrue(commands[0][-5].endswith(".ScreenshotWindow"))
        self.assertEqual(commands[0][-4:-1], ["true", "false", "false"])
        metadata = json.loads(result["content"][0]["text"])
        self.assertNotIn("structuredContent", result)
        self.assertEqual(result["content"][1]["type"], "image")
        self.assertEqual(
            metadata["coordinate_space"],
            {
                "window_local": {"width": 400, "height": 300},
                "screenshot_pixels": {"width": 800, "height": 600},
                "pixel_to_window_scale": {"x": 0.5, "y": 0.5},
                "note": "Pointer tools use logical window-local coordinates; multiply screenshot x/y by pixel_to_window_scale.",
            },
        )

    def test_oversized_capture_is_rejected_before_replacing_destination(self) -> None:
        window = {"id": "11", "focused": True, "frame": {"width": 1, "height": 1}}
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "capture.png"
            destination.write_bytes(b"old")

            def capture(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                Path(args[-1]).write_bytes(b"x" * (server.MAX_CAPTURE_PNG_BYTES + 1))
                return subprocess.CompletedProcess(args, 0, "", "")

            with (
                patch.object(server, "resolve_window", return_value=window),
                patch.object(server, "load_lease", return_value=None),
                patch.object(server, "run", side_effect=capture),
            ):
                with self.assertRaisesRegex(RuntimeError, "MCP transport limit"):
                    server.capture_window({"window": "11", "save_path": str(destination)})

            self.assertEqual(destination.read_bytes(), b"old")
            self.assertEqual(list(destination.parent.glob(".capture.png.*.png")), [])

    def test_header_only_capture_is_rejected_before_replacing_destination(self) -> None:
        window = {"id": "11", "focused": True, "frame": {"width": 1, "height": 1}}
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "capture.png"
            destination.write_bytes(b"old")

            def capture(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                Path(args[-1]).write_bytes(png(1, 1)[:24])
                return subprocess.CompletedProcess(args, 0, "", "")

            with (
                patch.object(server, "resolve_window", return_value=window),
                patch.object(server, "load_lease", return_value=None),
                patch.object(server, "run", side_effect=capture),
            ):
                with self.assertRaisesRegex(RuntimeError, "invalid PNG"):
                    server.capture_window({"window": "11", "save_path": str(destination)})

            self.assertEqual(destination.read_bytes(), b"old")

    def test_corrupt_scanline_is_rejected_before_replacing_destination(self) -> None:
        window = {"id": "11", "focused": True, "frame": {"width": 1, "height": 1}}
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
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "capture.png"
            destination.write_bytes(b"old")

            def capture(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                Path(args[-1]).write_bytes(corrupt)
                return subprocess.CompletedProcess(args, 0, "", "")

            with (
                patch.object(server, "resolve_window", return_value=window),
                patch.object(server, "load_lease", return_value=None),
                patch.object(server, "run", side_effect=capture),
            ):
                with self.assertRaisesRegex(RuntimeError, "invalid PNG"):
                    server.capture_window({"window": "11", "save_path": str(destination)})

            self.assertEqual(destination.read_bytes(), b"old")

    def test_maximum_capture_fits_codex_stdio_line(self) -> None:
        window = {"id": "11", "focused": True, "frame": {"width": 1, "height": 1}}

        def capture(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            base = png(1, 1)
            raw = png(1, 1, server.MAX_CAPTURE_PNG_BYTES - len(base) - 12)
            self.assertEqual(len(raw), server.MAX_CAPTURE_PNG_BYTES)
            Path(args[-1]).write_bytes(raw)
            return subprocess.CompletedProcess(args, 0, "", "")

        with (
            patch.object(server, "resolve_window", return_value=window),
            patch.object(server, "load_lease", return_value=None),
            patch.object(server, "run", side_effect=capture),
        ):
            result = server.capture_window({"window": "11"})

        response = {"jsonrpc": "2.0", "id": 1, "result": result}
        encoded = json.dumps(response, separators=(",", ":")).encode()
        self.assertLess(len(encoded), server.MAX_MCP_STDOUT_LINE_BYTES)
