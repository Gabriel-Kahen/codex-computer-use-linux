import os
import stat
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from support import MODULE_ROOT

import sys

sys.path.insert(0, str(MODULE_ROOT))

from plasma_same_session import coordination_state


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
    "geometry": {"x": -20, "y": 30, "width": 1000, "height": 700},
}


class CoordinationStateTests(TestCase):
    def test_window_metadata_is_preserved_with_individual_bounds(self) -> None:
        result = coordination_state.window_for_model({
            **WINDOW,
            "title": "t" * 1000,
            "class": "c" * 1000,
        })

        self.assertEqual(result, {
            **WINDOW,
            "title": "t" * coordination_state.MAX_WINDOW_TITLE_CHARS,
            "class": "c" * coordination_state.MAX_WINDOW_CLASS_CHARS,
        })

    def test_window_metadata_rejects_invalid_geometry_and_flags(self) -> None:
        invalid = [
            {**WINDOW, "geometry": {**WINDOW["geometry"], "width": -1}},
            {**WINDOW, "geometry": {**WINDOW["geometry"], "x": coordination_state.MAX_WINDOW_COORDINATE + 1}},
            {**WINDOW, "active": 1},
            {**WINDOW, "pid": -1},
        ]

        for window in invalid:
            with self.subTest(window=window):
                with self.assertRaisesRegex(RuntimeError, "window"):
                    coordination_state.window_for_model(window)

    def test_live_desktop_and_pointer_snapshots_are_bounded(self) -> None:
        self.assertEqual(coordination_state.optional_desktop(3, "desktop"), 3)
        self.assertEqual(coordination_state.optional_pointer({"x": 5, "y": -7}), {"x": 5, "y": -7})
        with self.assertRaisesRegex(RuntimeError, "desktop is invalid"):
            coordination_state.optional_desktop(10**100, "desktop")
        with self.assertRaisesRegex(RuntimeError, "pointer position is invalid"):
            coordination_state.optional_pointer({"x": 10**100, "y": 0})

    def test_private_json_state_is_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "record.json"

            coordination_state.write_private_json(path, {"value": 1})

            self.assertEqual(coordination_state.read_private_json(path), {"value": 1})
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.stat().st_uid, os.getuid())

    def test_current_process_identity_fails_closed_without_proc_start_time(self) -> None:
        with patch.object(
            coordination_state,
            "process_identity",
            return_value={"pid": 123, "start_time": None, "state": None},
        ):
            with self.assertRaisesRegex(RuntimeError, "positively verified"):
                coordination_state.current_process_identity()
