import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


PLUGIN_ROOT = Path(__file__).resolve().parents[2]


class LauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name) / "plugin"
        (self.root / "bin").mkdir(parents=True)
        (self.root / "upstream").mkdir()
        shutil.copy2(PLUGIN_ROOT / "bin/codex-computer-use", self.root / "bin")
        shutil.copy2(PLUGIN_ROOT / "prebuilt-build.env", self.root)
        shutil.copy2(PLUGIN_ROOT / "PREBUILT_VERSION", self.root)
        (self.root / "upstream/Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
        self.log = Path(self.temp_dir.name) / "backend.log"
        self.default_bin = Path(self.temp_dir.name) / "default-bin"
        self.default_bin.mkdir()
        self.write_fake_uname(self.default_bin, "x86_64")

    def run_launcher(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        process_env = os.environ.copy()
        process_env.update(
            {
                "HOME": self.temp_dir.name,
                "PATH": f"{self.default_bin}:{os.environ['PATH']}",
                "XDG_CACHE_HOME": str(Path(self.temp_dir.name) / ".cache"),
                "TEST_BACKEND_LOG": str(self.log),
            }
        )
        if env:
            process_env.update(env)
        return subprocess.run(
            [str(self.root / "bin/codex-computer-use"), *args],
            text=True,
            capture_output=True,
            env=process_env,
            check=False,
        )

    def add_prebuilt_bundle(self, target: str = "x86_64-unknown-linux-gnu") -> Path:
        (self.root / "prebuilt/RELEASE_BUNDLE").parent.mkdir()
        version = (self.root / "PREBUILT_VERSION").read_text(encoding="utf-8")
        (self.root / "prebuilt/RELEASE_BUNDLE").write_text(version, encoding="utf-8")
        prebuilt = self.root / "prebuilt" / target
        prebuilt.mkdir()
        backend = prebuilt / "computer-use-linux"
        helper = prebuilt / "computer-use-linux-cosmic"
        backend.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" > \"$TEST_BACKEND_LOG\"\n"
            "printf '%s\\n' \"${COMPUTER_USE_LINUX_COSMIC_HELPER:-}\" >> \"$TEST_BACKEND_LOG\"\n",
            encoding="utf-8",
        )
        helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        backend.chmod(0o755)
        helper.chmod(0o755)
        checksums = "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in (backend, helper)
        )
        (prebuilt / "SHA256SUMS").write_text(checksums, encoding="utf-8")
        return prebuilt

    def add_fake_cargo(self) -> tuple[Path, dict[str, str]]:
        fake_bin = Path(self.temp_dir.name) / "fake-bin"
        fake_bin.mkdir(exist_ok=True)
        cargo_log = Path(self.temp_dir.name) / "cargo.log"
        cargo = fake_bin / "cargo"
        cargo.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" > \"$TEST_CARGO_LOG\"\n"
            "printf '%s|%s|%s\\n' \"$CUL_GNOME_EXTENSION_UUID\" \"$CUL_DBUS_SERVICE\" \"$CUL_DBUS_OBJECT_PATH\" >> \"$TEST_CARGO_LOG\"\n"
            "mkdir -p \"$CARGO_TARGET_DIR/release\"\n"
            "cp \"$TEST_FAKE_BACKEND\" \"$CARGO_TARGET_DIR/release/computer-use-linux\"\n"
            "cp \"$TEST_FAKE_BACKEND\" \"$CARGO_TARGET_DIR/release/computer-use-linux-cosmic\"\n",
            encoding="utf-8",
        )
        cargo.chmod(0o755)
        backend = Path(self.temp_dir.name) / "fake-source-backend"
        backend.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$TEST_BACKEND_LOG\"\n",
            encoding="utf-8",
        )
        backend.chmod(0o755)
        env = {
            "PATH": f"{fake_bin}:{self.default_bin}:{os.environ['PATH']}",
            "TEST_CARGO_LOG": str(cargo_log),
            "TEST_FAKE_BACKEND": str(backend),
        }
        return cargo_log, env

    def write_fake_uname(self, directory: Path, machine: str) -> None:
        uname = directory / "uname"
        uname.write_text(
            f'#!/bin/sh\nif [ "$1" = "-s" ]; then echo Linux; else echo "{machine}"; fi\n',
            encoding="utf-8",
        )
        uname.chmod(0o755)

    def fake_uname_env(self, machine: str, *, without_cargo: bool = False) -> dict[str, str]:
        fake_bin = Path(self.temp_dir.name) / f"uname-{machine}"
        fake_bin.mkdir()
        self.write_fake_uname(fake_bin, machine)
        if without_cargo:
            for command in ("bash", "dirname", "sha256sum"):
                (fake_bin / command).symlink_to(shutil.which(command))
            return {"PATH": str(fake_bin)}
        return {"PATH": f"{fake_bin}:{os.environ['PATH']}"}

    def test_release_bundle_verifies_and_runs_without_cargo(self) -> None:
        prebuilt = self.add_prebuilt_bundle()

        result = self.run_launcher(
            "mcp",
            "--demo",
            env=self.fake_uname_env("x86_64", without_cargo=True),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.log.read_text(encoding="utf-8").splitlines(),
            ["mcp --demo", str(prebuilt / "computer-use-linux-cosmic")],
        )

    def test_release_bundle_selects_aarch64(self) -> None:
        prebuilt = self.add_prebuilt_bundle("aarch64-unknown-linux-gnu")

        result = self.run_launcher("mcp", env=self.fake_uname_env("aarch64"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.log.read_text(encoding="utf-8").splitlines(),
            ["mcp", str(prebuilt / "computer-use-linux-cosmic")],
        )

    def test_release_bundle_rejects_unsupported_architecture(self) -> None:
        self.add_prebuilt_bundle()

        result = self.run_launcher("mcp", env=self.fake_uname_env("riscv64"))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("support Linux x86_64 and aarch64", result.stderr)
        self.assertFalse(self.log.exists())

    def test_release_bundle_checksum_failure_is_fatal(self) -> None:
        prebuilt = self.add_prebuilt_bundle()
        (prebuilt / "computer-use-linux").write_text("tampered\n", encoding="utf-8")
        _, env = self.add_fake_cargo()

        result = self.run_launcher("mcp", env=env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum verification failed", result.stderr)
        self.assertFalse(self.log.exists())

    def test_release_bundle_version_mismatch_is_fatal(self) -> None:
        self.add_prebuilt_bundle()
        (self.root / "prebuilt/RELEASE_BUNDLE").write_text("old-version\n", encoding="utf-8")

        result = self.run_launcher("mcp")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("version does not match", result.stderr)
        self.assertFalse(self.log.exists())

    def test_release_bundle_rejects_unexpected_checksum_entries(self) -> None:
        prebuilt = self.add_prebuilt_bundle()
        with (prebuilt / "SHA256SUMS").open("a", encoding="utf-8") as checksums:
            checksums.write(f"{'0' * 64}  ../../unexpected\n")

        result = self.run_launcher("mcp")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid checksum manifest", result.stderr)

    def test_codex_cosmic_override_wins_over_bundle_default(self) -> None:
        self.add_prebuilt_bundle()

        result = self.run_launcher(
            "mcp",
            env={"CODEX_COMPUTER_USE_COSMIC_HELPER": "/custom/cosmic"},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.log.read_text(encoding="utf-8").splitlines(),
            ["mcp", "/custom/cosmic"],
        )

    def test_source_checkout_builds_with_codex_identity(self) -> None:
        cargo_log, env = self.add_fake_cargo()

        result = self.run_launcher("mcp", env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        cargo_lines = cargo_log.read_text(encoding="utf-8").splitlines()
        self.assertIn("--locked --release", cargo_lines[0])
        self.assertIn("--bin computer-use-linux --bin computer-use-linux-cosmic", cargo_lines[0])
        self.assertEqual(
            cargo_lines[1],
            "codex-window-control@openai.com|com.openai.Codex.WindowControl|/com/openai/Codex/WindowControl",
        )
        self.assertEqual(self.log.read_text(encoding="utf-8").strip(), "mcp")

    def test_build_only_preserves_source_build_contract(self) -> None:
        cargo_log, env = self.add_fake_cargo()

        result = self.run_launcher("--build-only", env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(cargo_log.is_file())
        self.assertEqual(
            result.stdout.strip(),
            str(Path(self.temp_dir.name) / ".cache/codex-computer-use-linux/target/release/computer-use-linux"),
        )
        self.assertFalse(self.log.exists())

    def test_explicit_source_build_bypasses_release_bundle(self) -> None:
        self.add_prebuilt_bundle()
        _, env = self.add_fake_cargo()
        env["CODEX_COMPUTER_USE_LINUX_BUILD_FROM_SOURCE"] = "1"

        result = self.run_launcher("mcp", env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text(encoding="utf-8").strip(), "mcp")


if __name__ == "__main__":
    unittest.main()
