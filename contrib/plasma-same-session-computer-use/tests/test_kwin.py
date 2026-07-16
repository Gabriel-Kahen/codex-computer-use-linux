import inspect
import subprocess
from unittest import TestCase
from unittest.mock import patch

from support import MODULE_ROOT

import sys

sys.path.insert(0, str(MODULE_ROOT))

from plasma_same_session import kwin


class KWinTests(TestCase):
    @patch.object(kwin, "kdotool")
    def test_active_window_id_rejects_unbounded_or_control_text(self, tool) -> None:
        tool.return_value = "x" * (kwin.MAX_WINDOW_ID_CHARS + 1)
        self.assertIsNone(kwin.active_window_id())
        tool.return_value = "{window}\nspoof"
        self.assertEqual(kwin.active_window_id(), "spoof")
        tool.return_value = "{window}\x00"
        self.assertIsNone(kwin.active_window_id())

    @patch.object(kwin.shutil, "which", return_value="/usr/bin/gdbus")
    @patch.object(kwin, "run", return_value=subprocess.CompletedProcess([], 0, "(':1.42',)\n", ""))
    def test_kwin_service_owner_uses_the_dbus_unique_name(self, _run, _which) -> None:
        self.assertEqual(kwin.kwin_service_owner(), ":1.42")

    @patch.object(kwin.shutil, "which", return_value=None)
    def test_kwin_service_owner_requires_gdbus(self, _which) -> None:
        self.assertIsNone(kwin.kwin_service_owner())

    @patch.object(kwin, "list_windows", return_value=[])
    def test_resolve_window_rejects_empty_queries(self, _windows) -> None:
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            kwin.resolve_window(" ")

    @patch.object(kwin, "list_windows")
    def test_resolve_window_bounds_ambiguous_external_titles(self, windows) -> None:
        windows.return_value = [
            {"id": f"{{{index}}}", "capture_id": str(index), "class": "c" * 500, "title": "t" * 500}
            for index in range(20)
        ]

        with self.assertRaisesRegex(RuntimeError, "ambiguous") as raised:
            kwin.resolve_window("t")

        self.assertLessEqual(len(str(raised.exception)), 1000)

    def test_geometry_parser_supports_negative_monitor_coordinates(self) -> None:
        self.assertEqual(
            kwin._geometry("Window {id}\n  Position: -1920,25 (screen: 0)\n  Geometry: 1280x720"),
            {"x": -1920, "y": 25, "width": 1280, "height": 720},
        )

    @patch.object(kwin.shutil, "which", return_value="/usr/bin/qdbus6")
    @patch.object(
        kwin,
        "run",
        return_value=subprocess.CompletedProcess([], 0, "desktops:\nminimized: false\n", ""),
    )
    def test_window_info_preserves_empty_qdbus_values(self, _run, _which) -> None:
        self.assertEqual(kwin.window_info("{one}"), {"desktops": "", "minimized": "false"})

    @patch.object(kwin, "window_info", return_value={})
    @patch.object(kwin, "kdotool", return_value="-1")
    def test_all_desktops_is_distinct_from_query_failure(self, tool, _info) -> None:
        self.assertEqual(kwin.window_desktop("{one}"), -1)
        tool.side_effect = RuntimeError("query failed")
        self.assertIsNone(kwin.window_desktop("{one}"))

    @patch.object(kwin, "window_info", return_value={"desktops": ""})
    @patch.object(kwin, "kdotool", side_effect=RuntimeError("undefined"))
    def test_qdbus_empty_desktop_list_means_all_desktops(self, _tool, _info) -> None:
        self.assertEqual(kwin.window_desktop("{one}"), -1)

    @patch.object(kwin.shutil, "which", side_effect=lambda command: f"/usr/bin/{command}")
    @patch.object(kwin, "run")
    def test_capture_helper_requires_qt6_and_never_falls_back_to_qt5(self, run, _which) -> None:
        run.return_value = subprocess.CompletedProcess([], 1, "", "missing")

        self.assertFalse(kwin.helper_requirements()["qt6_development_files"])
        with self.assertRaisesRegex(RuntimeError, "Qt 6"):
            kwin.build_capture_helper()
        self.assertNotIn("Qt5", inspect.getsource(kwin.build_capture_helper))

    @patch.object(kwin.shutil, "which", return_value=None)
    def test_kdotool_calls_fail_with_an_explicit_capability_error(self, _which) -> None:
        with self.assertRaisesRegex(RuntimeError, "required.*exact capture"):
            kwin.kdotool("search", ".")

    @patch.object(kwin, "active_window_id", return_value="{one}")
    @patch.object(kwin, "window_info", return_value={"minimized": "false", "fullscreen": "true", "excludeFromCapture": "false"})
    @patch.object(kwin, "kdotool")
    def test_list_windows_returns_complete_objects(self, tool, _info, _active) -> None:
        values = {
            ("search", "."): "{one}\n{gone}",
            ("getwindowgeometry", "{one}"): "Position: 10,20 (screen: 0)\nGeometry: 800x600",
            ("getwindowname", "{one}"): "Terminal",
            ("getwindowclassname", "{one}"): "org.kde.konsole",
            ("getwindowpid", "{one}"): "42",
            ("get_desktop_for_window", "{one}"): "2",
            ("getwindowgeometry", "{gone}"): "window closed",
        }
        tool.side_effect = lambda *args: values[args]

        self.assertEqual(kwin.list_windows(), [{
            "id": "{one}",
            "capture_id": "one",
            "title": "Terminal",
            "class": "org.kde.konsole",
            "pid": 42,
            "desktop": 2,
            "active": True,
            "minimized": False,
            "fullscreen": True,
            "excluded_from_capture": False,
            "geometry": {"x": 10, "y": 20, "width": 800, "height": 600},
        }])

    @patch.object(kwin, "active_window_id", return_value="{one}")
    @patch.object(kwin, "window_info", return_value={})
    @patch.object(kwin, "kdotool")
    def test_list_windows_keeps_windows_without_optional_process_metadata(self, tool, _info, _active) -> None:
        values = {
            ("search", "."): "{one}",
            ("getwindowgeometry", "{one}"): "Position: 10,20 (screen: 0)\nGeometry: 800x600",
            ("getwindowname", "{one}"): "Terminal",
            ("getwindowclassname", "{one}"): "org.kde.konsole",
            ("get_desktop_for_window", "{one}"): "2",
        }

        def result(*args: str) -> str:
            if args == ("getwindowpid", "{one}"):
                raise RuntimeError("pid unavailable")
            return values[args]

        tool.side_effect = result

        self.assertEqual(kwin.list_windows(), [{
            "id": "{one}",
            "capture_id": "one",
            "title": "Terminal",
            "class": "org.kde.konsole",
            "pid": None,
            "desktop": 2,
            "active": True,
            "minimized": None,
            "fullscreen": None,
            "excluded_from_capture": None,
            "geometry": {"x": 10, "y": 20, "width": 800, "height": 600},
        }])
