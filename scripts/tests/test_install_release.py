import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

import importlib.util

spec = importlib.util.spec_from_file_location(
    "installer", Path(__file__).resolve().parents[1] / "install_release.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
unpack, verify_checksum = installer.unpack, installer.verify_checksum


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = self.root / "release.tar.gz"

    def archive_with(self, name, contents=b"{}"):
        with tarfile.open(self.archive, "w:gz") as bundle:
            member = tarfile.TarInfo(name)
            member.size = len(contents)
            bundle.addfile(member, io.BytesIO(contents))

    def test_checksum_detects_tampering_and_wrong_filename(self):
        self.archive.write_bytes(b"release")
        checksum = self.root / "release.sha256"
        checksum.write_text(hashlib.sha256(b"release").hexdigest() + "  release.tar.gz\n")
        verify_checksum(self.archive, checksum)
        self.archive.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "mismatch"):
            verify_checksum(self.archive, checksum)
        checksum.write_text("0" * 64 + "  other.tar.gz\n")
        with self.assertRaisesRegex(ValueError, "selected archive"):
            verify_checksum(self.archive, checksum)

    def test_extract_preserves_previous_release(self):
        self.archive_with(".agents/plugins/marketplace.json")
        destination = self.root / "installed"
        unpack(self.archive, destination)
        self.assertEqual((destination / ".agents/plugins/marketplace.json").read_bytes(), b"{}")
        with self.assertRaisesRegex(ValueError, "already exists"):
            unpack(self.archive, destination)

    def test_traversal_never_reaches_destination(self):
        self.archive_with("../outside", b"bad")
        destination = self.root / "installed"
        with self.assertRaisesRegex(ValueError, "unexpected"):
            unpack(self.archive, destination)
        self.assertFalse(destination.exists())
        self.assertFalse((self.root / "outside").exists())


if __name__ == "__main__":
    unittest.main()
