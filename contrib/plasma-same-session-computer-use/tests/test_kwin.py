import inspect
import subprocess
from unittest import TestCase
from unittest.mock import patch

from support import MODULE_ROOT

import sys

sys.path.insert(0, str(MODULE_ROOT))

from plasma_same_session import kwin


class KWinTests(TestCase):
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
