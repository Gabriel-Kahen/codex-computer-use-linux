from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from same_session_computer_use import server


def add_process(root: Path, pid: int, *, instance: str, wayland: str, display: str) -> None:
    process = root / str(pid)
    process.mkdir()
    (process / "comm").write_text("Xwayland\n")
    environment = {
        "HYPRLAND_INSTANCE_SIGNATURE": instance,
        "WAYLAND_DISPLAY": wayland,
        "DISPLAY": display,
    }
    (process / "environ").write_bytes(b"\0".join(f"{key}={value}".encode() for key, value in environment.items()))


class XWaylandDisplayTests(TestCase):
    def test_matches_the_selected_hyprland_instance(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            add_process(root, 101, instance="old", wayland="wayland-0", display=":0")
            add_process(root, 202, instance="current", wayland="wayland-1", display=":1")

            self.assertEqual(server.find_xwayland_display("current", "wayland-1", root), ":1")

    def test_ignores_unrelated_processes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            add_process(root, 101, instance="other", wayland="wayland-1", display=":9")
            unrelated = root / "202"
            unrelated.mkdir()
            (unrelated / "comm").write_text("python\n")
            (unrelated / "environ").write_bytes(b"DISPLAY=:2\0")

            self.assertIsNone(server.find_xwayland_display("current", "wayland-1", root))


class WindowListingTests(TestCase):
    def test_pages_windows_and_bounds_compositor_text(self) -> None:
        raw_windows = [
            {
                "address": f"0x{index}",
                "title": str(index) * (server.MAX_WINDOW_TEXT_CHARS + 10),
                "class": "class",
                "workspace": {"id": index, "name": "workspace"},
                "stableId": index,
            }
            for index in range(3)
        ]
        with patch.object(server, "hypr_windows", return_value=raw_windows):
            first = server.call_tool("list_session_windows", {"limit": 2})["structuredContent"]
            second = server.call_tool("list_session_windows", {"cursor": first["next_cursor"]})[
                "structuredContent"
            ]

        self.assertEqual(first["next_cursor"], "2")
        self.assertEqual(len(first["windows"]), 2)
        self.assertEqual(len(first["windows"][0]["title"]), server.MAX_WINDOW_TEXT_CHARS)
        self.assertEqual(second["next_cursor"], None)
        self.assertEqual([window["address"] for window in second["windows"]], ["0x2"])

    def test_rejects_invalid_pagination(self) -> None:
        with self.assertRaisesRegex(ValueError, "limit"):
            server.call_tool("list_session_windows", {"limit": True})
        with self.assertRaisesRegex(ValueError, "cursor"):
            server.call_tool("list_session_windows", {"cursor": "not-a-cursor"})
        with self.assertRaisesRegex(ValueError, "cursor"):
            server.call_tool("list_session_windows", {"cursor": "1" * 21})

    def test_accepts_explicit_null_limit(self) -> None:
        with patch.object(server, "hypr_windows", return_value=[]):
            result = server.call_tool("list_session_windows", {"limit": None})

        self.assertEqual(result["structuredContent"], {"windows": [], "next_cursor": None})

    def test_keeps_full_titles_for_resolution_but_bounds_results(self) -> None:
        title = "x" * (server.MAX_WINDOW_TEXT_CHARS + 10) + "needle"
        raw = {
            "address": "0x1",
            "title": title,
            "class": "class",
            "workspace": {"id": 1, "name": "workspace"},
            "stableId": 1,
        }
        with patch.object(server, "hypr_windows", return_value=[raw]):
            resolved = server.resolve_window("needle")
            listed = server.call_tool("list_session_windows", {})["structuredContent"]["windows"][0]

        self.assertEqual(resolved["title"], title)
        self.assertEqual(len(listed["title"]), server.MAX_WINDOW_TEXT_CHARS)

    def test_xwayland_resolution_compares_the_full_title(self) -> None:
        title = "x" * (server.MAX_WINDOW_TEXT_CHARS + 1) + "current"
        results = [
            server.subprocess.CompletedProcess([], 0, "10\n20\n", ""),
            server.subprocess.CompletedProcess([], 0, "other\n", ""),
            server.subprocess.CompletedProcess([], 0, f"{title}\n", ""),
        ]
        with patch.object(server, "run", side_effect=results):
            xid = server.resolve_xwindow_id({"pid": 42, "title": title})

        self.assertEqual(xid, "20")


class ProtocolTests(TestCase):
    def test_initialize_echoes_a_supported_older_protocol_version(self) -> None:
        response = server.dispatch(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )

        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")
