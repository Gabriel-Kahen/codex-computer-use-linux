import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "upstream/tests/fixtures/coordination/v2_claim_state.json"


def canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class CoordinationProtocolTests(unittest.TestCase):
    def test_fixture_keys_match_python_canonical_json(self) -> None:
        state = json.loads(FIXTURE.read_text())
        self.assertEqual(state["version"], 2)
        self.assertEqual(len(state["sessions"]), 1)

        session_key, session = next(iter(state["sessions"].items()))
        self.assertEqual(session_key, canonical_digest(session["identity"]))

        window_key, claim = next(iter(session["claims"].items()))
        lock_identity = {
            "session": session["identity"],
            "window": claim["window"]["identity"],
        }
        self.assertEqual(window_key, canonical_digest(lock_identity))

    def test_fixture_contains_no_unknown_backend(self) -> None:
        state = json.loads(FIXTURE.read_text())
        allowed = {"cosmic", "gnome", "hyprland", "i3", "niri", "plasma", "x11"}
        for session in state["sessions"].values():
            self.assertIn(session["identity"]["backend"], allowed)
            for claim in session["claims"].values():
                self.assertEqual(
                    claim["window"]["identity"]["backend"],
                    session["identity"]["backend"],
                )


if __name__ == "__main__":
    unittest.main()
