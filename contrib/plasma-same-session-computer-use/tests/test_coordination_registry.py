import os
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from support import MODULE_ROOT

import sys

sys.path.insert(0, str(MODULE_ROOT))

from plasma_same_session import coordination


SESSION = {
    "uid": os.getuid(),
    "boot_id": "boot-test",
    "wayland_display": "wayland-test",
    "wayland_socket": {"device": 1, "inode": 2},
    "session_id": "session-test",
    "kwin_service_owner": ":1.42",
}


def window(window_id: str) -> dict:
    return {
        "id": window_id,
        "capture_id": window_id.strip("{}"),
        "title": window_id,
        "class": "test",
        "pid": 123,
        "desktop": 1,
        "active": False,
        "minimized": False,
        "fullscreen": False,
        "excluded_from_capture": False,
        "geometry": {"x": 0, "y": 0, "width": 800, "height": 600},
    }


class CoordinationRegistryTests(TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.claims_dir = Path(self.directory.name) / "claims"
        self.claims_patch = patch.object(coordination, "CLAIMS_DIR", self.claims_dir)
        self.claims_patch.start()
        self.session_patch = patch.object(coordination, "current_session_identity", return_value=SESSION)
        self.session_patch.start()
        self.session_dir = coordination._session_claims_dir(SESSION)

    def tearDown(self) -> None:
        self.session_patch.stop()
        self.claims_patch.stop()
        self.directory.cleanup()

    def write_claim(self, window_id: str, session: dict = SESSION) -> Path:
        record = coordination._new_claim(window(window_id), "owner-a", 60, session, implicit=False)
        path = self.session_dir / f"{coordination._claim_key(window_id)}.json"
        coordination.write_private_json(path, record)
        return path

    def test_list_claims_validates_records_and_redacts_tokens(self) -> None:
        self.write_claim("{shared}")

        result = coordination.list_claims("observer", 0, 20)

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["claims"][0]["owner_thread_id"], "owner-a")
        self.assertNotIn("claim_token", result["claims"][0])
        malformed = self.write_claim("{malformed}")
        coordination.write_private_json(malformed, {"version": 2})
        with self.assertRaisesRegex(RuntimeError, "binding is invalid"):
            coordination.list_claims("observer", 0, 20)

    def test_public_claim_rejects_oversized_persisted_timestamps(self) -> None:
        record = coordination._new_claim(window("{shared}"), "owner-a", 60, SESSION, implicit=False)
        record["expires_at"] = coordination.MAX_TIMESTAMP + 1

        with self.assertRaisesRegex(RuntimeError, "timing metadata"):
            coordination._public_claim(record, include_token=True)

    def test_list_ignores_and_preserves_foreign_session_records(self) -> None:
        foreign_path = self.write_claim("{foreign}", {**SESSION, "kwin_service_owner": ":1.99"})

        result = coordination.list_claims("observer", 0, 20)

        self.assertEqual(result["claims"], [])
        self.assertTrue(foreign_path.is_file())

    def test_capacity_ignores_foreign_records_but_caps_local_records(self) -> None:
        foreign_path = self.write_claim("{foreign}", {**SESSION, "kwin_service_owner": ":1.99"})
        target_path = self.session_dir / f"{coordination._claim_key('{target}')}.json"
        with patch.object(coordination, "MAX_ACTIVE_CLAIMS", 1):
            coordination._enforce_claim_capacity(SESSION, target_path)
            self.write_claim("{local}")
            with self.assertRaisesRegex(RuntimeError, "at most 1"):
                coordination._enforce_claim_capacity(SESSION, target_path)

        self.assertTrue(foreign_path.is_file())
