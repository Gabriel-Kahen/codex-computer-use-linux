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

    def test_window_actor_capture_observes_mapped_inactive_window_without_focus(self) -> None:
        window = {
            "id": "11",
            "title": "Editor",
            "focused": False,
            "minimized": False,
            "frame": {"width": 400, "height": 300},
        }
        integration = {
            "shell_instance": "shell-a",
            "protocol_version": server.BRIDGE_CONTRACT_PROTOCOL_VERSION,
            "capabilities": [
                server.WINDOW_ACTOR_CAPTURE_CAPABILITY,
                server.BRIDGE_CONTRACT_CAPABILITY,
            ],
            "bridge_contract": {
                **server.BRIDGE_CONTRACT,
                "role": "background-computer-use",
                "features": ["window-actor-capture"],
            },
        }
        captured = {
            "window": window,
            "shell_instance": "shell-a",
            "source": "meta-window-actor",
            "potentially_stale": False,
            "operation_identity": {
                "contract_version": server.BRIDGE_CONTRACT["contract_version"],
                "shell_instance": "shell-a",
                "window": {
                    "scheme": server.BRIDGE_CONTRACT["window_identity"],
                    "id": "11",
                },
                "kind": "capture",
                "generation": None,
            },
        }
        unrelated_lease = {
            "owner_thread_id": "other-agent",
            "phase": "active",
            "window": {"id": "22"},
        }
        with (
            patch.object(server, "load_lease", return_value=unrelated_lease) as load_lease,
            patch.object(server, "shell_status", return_value=integration),
            patch.object(server, "dbus_capture_window", return_value=(png(800, 600), captured)) as capture,
            patch.object(server, "run") as run,
        ):
            result = server.capture_window(
                {"window": "11"},
                selected=window,
                expected_shell_instance="shell-a",
            )

        capture.assert_called_once_with("11")
        load_lease.assert_not_called()
        run.assert_not_called()
        metadata = json.loads(result["content"][0]["text"])
        self.assertFalse(metadata["capture_requires_focus"])
        self.assertEqual(metadata["capture_source"], "meta-window-actor")
        self.assertFalse(metadata["potentially_stale"])
        self.assertEqual(metadata["coordinate_space"]["pixel_to_window_scale"], {"x": 0.5, "y": 0.5})

    def test_window_actor_capture_rejects_minimized_buffers_before_dbus(self) -> None:
        window = {"id": "11", "focused": False, "minimized": True}
        integration = {
            "shell_instance": "shell-a",
            "protocol_version": server.WINDOW_ACTOR_CAPTURE_PROTOCOL_VERSION,
            "capabilities": [server.WINDOW_ACTOR_CAPTURE_CAPABILITY],
        }
        with (
            patch.object(server, "load_lease", return_value=None),
            patch.object(server, "shell_status", return_value=integration),
            patch.object(server, "dbus_capture_window") as capture,
        ):
            with self.assertRaisesRegex(RuntimeError, "stale compositor buffers"):
                server.capture_window(
                    {"window": "11"},
                    selected=window,
                    expected_shell_instance="shell-a",
                )

        capture.assert_not_called()

    def test_fresh_minimized_capture_requires_acknowledgement(self) -> None:
        window = {"id": "11", "minimized": True}

        with self.assertRaisesRegex(ValueError, "acknowledge_interference"):
            server.capture_minimized_window(
                {"window": "11", "acknowledge_interference": False},
                window,
                "shell-a",
            )

    def test_fresh_minimized_capture_validates_damage_identity_and_restoration(self) -> None:
        window = {
            "id": "11",
            "title": "Editor",
            "focused": False,
            "minimized": True,
            "frame": {"width": 400, "height": 300},
        }
        integration = {
            "shell_instance": "shell-a",
            "protocol_version": server.FRESH_MINIMIZED_CAPTURE_PROTOCOL_VERSION,
            "capabilities": [
                server.WINDOW_ACTOR_CAPTURE_CAPABILITY,
                server.BRIDGE_CONTRACT_CAPABILITY,
                server.FRESH_MINIMIZED_CAPTURE_CAPABILITY,
            ],
            "bridge_contract": {
                **server.BRIDGE_CONTRACT,
                "role": "background-computer-use",
                "features": ["fresh-minimized-capture"],
            },
        }

        capability = "c" * 64
        generation = "g" * 64

        def capture(actual_capability: str):
            self.assertEqual(actual_capability, capability)
            return png(800, 600), {
                "window": window,
                "shell_instance": "shell-a",
                "source": "meta-window-actor-freshened",
                "potentially_stale": False,
                "freshness": "client-damage-after-unminimize",
                "operation_identity": {
                    "contract_version": server.BRIDGE_CONTRACT["contract_version"],
                    "shell_instance": "shell-a",
                    "window": {
                        "scheme": server.BRIDGE_CONTRACT["window_identity"],
                        "id": "11",
                    },
                    "kind": "fresh-minimized-capture",
                    "generation": generation,
                },
                "transaction": {
                    "settle": {"reason": "damaged-and-painted"},
                    "restoration": {
                        "restored": True,
                        "recovery_complete": True,
                        "errors": [],
                        "focus_changed_during_transaction": True,
                    },
                },
            }

        prepared = {
            "capability": capability,
            "lease_generation": generation,
            "target": window,
            "original": {"focused_window": "22", "workspace": 0},
            "shell_instance": "shell-a",
        }
        with TemporaryDirectory() as directory:
            lease_file = Path(directory) / "focus-lease.json"
            with (
                patch.object(server, "LEASE_FILE", lease_file),
                patch.object(server, "LEGACY_LEASE_FILE", Path(directory) / "legacy.json"),
                patch.object(server, "shell_status", return_value=integration),
                patch.object(server, "dbus_call", return_value=prepared) as dbus,
                patch.object(
                    server, "dbus_capture_minimized_window", side_effect=capture
                ) as dispatch,
            ):
                result = server.capture_minimized_window(
                    {"window": "11", "acknowledge_interference": True},
                    window,
                    "shell-a",
                )
            self.assertFalse(lease_file.exists())

        dbus.assert_called_once_with("BeginLease", "11")
        dispatch.assert_called_once()
        metadata = json.loads(result["content"][0]["text"])
        self.assertEqual(
            metadata["capture_source"], "meta-window-actor-freshened"
        )
        self.assertFalse(metadata["potentially_stale"])
        self.assertTrue(metadata["desktop_restored_before_return"])
        self.assertTrue(metadata["focus_changed_by_capture"])
        self.assertEqual(
            metadata["coordinate_space"]["pixel_to_window_scale"],
            {"x": 0.5, "y": 0.5},
        )

    def test_fresh_minimized_capture_rejects_missing_damage_proof(self) -> None:
        window = {
            "id": "11",
            "minimized": True,
            "frame": {"width": 1, "height": 1},
        }
        integration = {
            "shell_instance": "shell-a",
            "protocol_version": server.FRESH_MINIMIZED_CAPTURE_PROTOCOL_VERSION,
            "capabilities": [
                server.FRESH_MINIMIZED_CAPTURE_CAPABILITY,
                server.BRIDGE_CONTRACT_CAPABILITY,
            ],
            "bridge_contract": {
                **server.BRIDGE_CONTRACT,
                "role": "background-computer-use",
                "features": ["fresh-minimized-capture"],
            },
        }
        capability = "c" * 64
        generation = "g" * 64
        prepared = {
            "capability": capability,
            "lease_generation": generation,
            "target": window,
            "original": {"focused_window": "22", "workspace": 0},
            "shell_instance": "shell-a",
        }

        def capture(actual_capability: str):
            self.assertEqual(actual_capability, capability)
            return png(1, 1), {
                "window": window,
                "shell_instance": "shell-a",
                "potentially_stale": False,
                "freshness": "client-damage-after-unminimize",
                "operation_identity": {
                    "contract_version": server.BRIDGE_CONTRACT["contract_version"],
                    "shell_instance": "shell-a",
                    "window": {
                        "scheme": server.BRIDGE_CONTRACT["window_identity"],
                        "id": "11",
                    },
                    "kind": "fresh-minimized-capture",
                    "generation": generation,
                },
                "transaction": {
                    "settle": {"reason": "timeout"},
                    "restoration": {"recovery_complete": True, "errors": []},
                },
            }
        def dbus(method: str, *args: str):
            if method == "BeginLease":
                return prepared
            if method == "RecoverLease":
                self.assertEqual(args, (capability,))
                return {"restored": True, "recovery_complete": True, "errors": []}
            self.fail(f"unexpected D-Bus method {method}")

        with TemporaryDirectory() as directory:
            lease_file = Path(directory) / "focus-lease.json"
            with (
                patch.object(server, "LEASE_FILE", lease_file),
                patch.object(server, "LEGACY_LEASE_FILE", Path(directory) / "legacy.json"),
                patch.object(server, "shell_status", return_value=integration),
                patch.object(server, "dbus_call", side_effect=dbus),
                patch.object(
                    server,
                    "dbus_capture_minimized_window",
                    side_effect=capture,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "client-damaged painted frame"):
                    server.capture_minimized_window(
                        {"window": "11", "acknowledge_interference": True},
                        window,
                        "shell-a",
                    )
            self.assertFalse(lease_file.exists())

    def test_protocol_three_mapped_capture_keeps_legacy_metadata_compatibility(self) -> None:
        window = {
            "id": "11",
            "focused": False,
            "minimized": False,
            "frame": {"width": 1, "height": 1},
        }
        integration = {
            "shell_instance": "shell-a",
            "protocol_version": server.WINDOW_ACTOR_CAPTURE_PROTOCOL_VERSION,
            "capabilities": [server.WINDOW_ACTOR_CAPTURE_CAPABILITY],
        }
        captured = {
            "window": window,
            "shell_instance": "shell-a",
            "potentially_stale": False,
        }
        with (
            patch.object(server, "load_lease", return_value=None),
            patch.object(server, "shell_status", return_value=integration),
            patch.object(server, "dbus_capture_window", return_value=(png(1, 1), captured)),
        ):
            result = server.capture_window(
                {"window": "11"},
                selected=window,
                expected_shell_instance="shell-a",
            )

        metadata = json.loads(result["content"][0]["text"])
        self.assertEqual(metadata["capture_source"], "meta-window-actor")

    def test_window_actor_capture_rejects_shell_restart(self) -> None:
        window = {"id": "11", "focused": False, "frame": {"width": 1, "height": 1}}
        integration = {
            "shell_instance": "shell-a",
            "protocol_version": server.WINDOW_ACTOR_CAPTURE_PROTOCOL_VERSION,
            "capabilities": [server.WINDOW_ACTOR_CAPTURE_CAPABILITY],
        }
        captured = {"window": window, "shell_instance": "shell-b"}
        with (
            patch.object(server, "load_lease", return_value=None),
            patch.object(server, "shell_status", return_value=integration),
            patch.object(server, "dbus_capture_window", return_value=(png(1, 1), captured)),
        ):
            with self.assertRaisesRegex(RuntimeError, "restarted"):
                server.capture_window(
                    {"window": "11"},
                    selected=window,
                    expected_shell_instance="shell-a",
                )

    def test_act_and_observe_restores_before_returning_capture(self) -> None:
        window = {
            "id": "11",
            "title": "Editor",
            "focused": False,
            "frame": {"width": 400, "height": 300},
        }
        integration = {
            "shell_instance": "shell-a",
            "protocol_version": server.BRIDGE_CONTRACT_PROTOCOL_VERSION,
            "capabilities": [
                server.ACT_AND_CAPTURE_CAPABILITY,
                server.BRIDGE_CONTRACT_CAPABILITY,
            ],
            "bridge_contract": {
                **server.BRIDGE_CONTRACT,
                "role": "background-computer-use",
                "features": ["act-and-capture"],
            },
            "lease_phase": None,
        }
        state = {
            "token": "c" * 64,
            "phase": "active",
            "target": window,
            "shell_instance": "shell-a",
            "lease_generation": "g" * 64,
        }
        transaction = {
            "settle": {"reason": "damaged-and-painted"},
            "restoration": {"restored": True, "recovery_complete": True, "errors": []},
            "interference_milliseconds": 23,
        }
        captured = {
            "window": {**window, "focused": True},
            "shell_instance": "shell-a",
            "potentially_stale": False,
            "transaction": transaction,
            "operation_identity": {
                "contract_version": server.BRIDGE_CONTRACT["contract_version"],
                "shell_instance": "shell-a",
                "window": {
                    "scheme": server.BRIDGE_CONTRACT["window_identity"],
                    "id": "11",
                },
                "kind": "act-and-capture",
                "generation": "g" * 64,
            },
        }
        arguments = {
            "window": "11",
            "acknowledge_interference": True,
            "action": {"type": "click", "x": 20, "y": 30},
        }
        with (
            TemporaryDirectory() as directory,
            patch.object(server, "LEASE_FILE", Path(directory) / "lease.json"),
            patch.object(server, "shell_status", return_value=integration),
            patch.object(server, "begin_lease") as begin,
            patch.object(server, "load_lease", return_value=state),
            patch.object(server, "dbus_act_and_capture", return_value=(png(800, 600), captured)) as act,
            patch.object(server, "restore_lease") as recover,
        ):
            result = server.act_and_observe(arguments, "thread-a", window, None, "shell-a")

        begin.assert_called_once()
        act.assert_called_once_with(
            "c" * 64,
            {
                "kind": "pointer",
                "action": {
                    "action": "click",
                    "button": "left",
                    "point": {"x": 20.0, "y": 30.0},
                    "count": 1,
                },
            },
        )
        recover.assert_not_called()
        metadata = json.loads(result["content"][0]["text"])
        self.assertTrue(metadata["desktop_restored_before_return"])
        self.assertEqual(metadata["transaction"]["settle"]["reason"], "damaged-and-painted")
        self.assertEqual(metadata["coordinate_space"]["pixel_to_window_scale"], {"x": 0.5, "y": 0.5})

    def test_act_and_observe_rejects_a_different_lease_generation(self) -> None:
        window = {"id": "11", "focused": False, "frame": {"width": 1, "height": 1}}
        integration = {
            "shell_instance": "shell-a",
            "protocol_version": server.BRIDGE_CONTRACT_PROTOCOL_VERSION,
            "capabilities": [
                server.ACT_AND_CAPTURE_CAPABILITY,
                server.BRIDGE_CONTRACT_CAPABILITY,
            ],
            "bridge_contract": {
                **server.BRIDGE_CONTRACT,
                "role": "background-computer-use",
                "features": ["act-and-capture"],
            },
            "lease_phase": None,
        }
        state = {
            "token": "c" * 64,
            "phase": "active",
            "target": window,
            "lease_generation": "g" * 64,
        }
        captured = {
            "window": window,
            "shell_instance": "shell-a",
            "potentially_stale": False,
            "transaction": {
                "restoration": {"recovery_complete": True},
            },
            "operation_identity": {
                "contract_version": server.BRIDGE_CONTRACT["contract_version"],
                "shell_instance": "shell-a",
                "window": {
                    "scheme": server.BRIDGE_CONTRACT["window_identity"],
                    "id": "11",
                },
                "kind": "act-and-capture",
                "generation": "x" * 64,
            },
        }
        arguments = {
            "window": "11",
            "acknowledge_interference": True,
            "action": {"type": "shortcut", "key": "F6"},
        }
        with (
            patch.object(server, "shell_status", return_value=integration),
            patch.object(server, "begin_lease"),
            patch.object(server, "load_lease", return_value=state),
            patch.object(server, "dbus_act_and_capture", return_value=(png(1, 1), captured)),
            patch.object(
                server,
                "restore_lease",
                return_value={"recovery_complete": True, "errors": []},
            ) as recover,
        ):
            with self.assertRaisesRegex(RuntimeError, "mismatched operation identity"):
                server.act_and_observe(arguments, "thread-a", window, None, "shell-a")

        recover.assert_called_once_with(state, recovery=True)

    def test_act_and_observe_recovers_after_invalid_capture(self) -> None:
        window = {"id": "11", "focused": False, "frame": {"width": 1, "height": 1}}
        integration = {
            "shell_instance": "shell-a",
            "protocol_version": server.ACT_AND_CAPTURE_PROTOCOL_VERSION,
            "capabilities": [server.ACT_AND_CAPTURE_CAPABILITY],
        }
        state = {"token": "c" * 64, "phase": "active", "target": window}
        arguments = {
            "window": "11",
            "acknowledge_interference": True,
            "action": {"type": "shortcut", "key": "F6"},
        }
        with (
            patch.object(server, "shell_status", return_value=integration),
            patch.object(server, "begin_lease"),
            patch.object(server, "load_lease", return_value=state),
            patch.object(server, "dbus_act_and_capture", return_value=(b"invalid", {})),
            patch.object(server, "restore_lease", return_value={"recovery_complete": True, "errors": []}) as recover,
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid PNG"):
                server.act_and_observe(arguments, "thread-a", window, None, "shell-a")

        recover.assert_called_once_with(state, recovery=True)

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

    def test_shell_restart_prevents_or_discards_capture(self) -> None:
        cases = (
            ("before screenshot", ["shell-b"], False, False),
            ("after screenshot", ["shell-a", "shell-b"], True, False),
            ("before accepting focus check", ["shell-a", "shell-a", "shell-b"], True, True),
        )
        window = {
            "id": "11",
            "focused": True,
            "frame": {"width": 1, "height": 1},
        }
        for label, instances, screenshot_started, focus_checked in cases:
            with self.subTest(label=label), TemporaryDirectory() as directory:
                destination = Path(directory) / "capture.png"
                destination.write_bytes(b"old")

                def capture(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                    Path(args[-1]).write_bytes(png(1, 1))
                    return subprocess.CompletedProcess(args, 0, "", "")

                statuses = [
                    {"shell_instance": shell_instance}
                    for shell_instance in instances
                ]
                with (
                    patch.object(server, "load_lease", return_value=None),
                    patch.object(server, "shell_status", side_effect=statuses),
                    patch.object(server, "resolve_window", return_value=window) as resolve,
                    patch.object(server, "run", side_effect=capture) as run,
                ):
                    with self.assertRaisesRegex(RuntimeError, "GNOME Shell restarted"):
                        server.capture_window(
                            {"window": "11", "save_path": str(destination)},
                            selected=window,
                            expected_shell_instance="shell-a",
                        )

                self.assertEqual(run.call_count, int(screenshot_started))
                self.assertEqual(resolve.call_count, int(focus_checked))
                self.assertEqual(destination.read_bytes(), b"old")
                self.assertEqual(list(destination.parent.glob(".capture.png.*.png")), [])

    def test_screenshot_signature_and_scaled_coordinate_transform(self) -> None:
        window = {
            "id": "11",
            "title": "x" * server.MAX_MCP_STDOUT_LINE_BYTES,
            "focused": True,
            "frame": {"width": 400, "height": 300},
        }
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
        self.assertEqual(len(metadata["window"]["title"]), server.MAX_WINDOW_TEXT_CHARS)
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

    def test_excessive_pixel_count_is_rejected_before_replacing_destination(self) -> None:
        window = {"id": "11", "focused": True, "frame": {"width": 1, "height": 1}}
        width = server.MAX_CAPTURE_PIXELS + 1
        header = struct.pack(">IIBBBBB", width, 1, 8, 2, 0, 0, 0)
        raw = b"\x89PNG\r\n\x1a\n" + b"".join(
            (
                png_chunk(b"IHDR", header),
                png_chunk(b"IDAT", zlib.compress(b"")),
                png_chunk(b"IEND", b""),
            )
        )
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "capture.png"
            destination.write_bytes(b"old")

            def capture(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                Path(args[-1]).write_bytes(raw)
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
