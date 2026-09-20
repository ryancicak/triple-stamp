from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / ".omnigent/bootstrap.sh"


class BootstrapContractTests(unittest.TestCase):
    def _shell(
        self,
        script: str,
        *,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/sh", "-c", f'. "{BOOTSTRAP}"\n{script}'],
            cwd=ROOT,
            env={**os.environ, **(env or {})},
            text=True,
            capture_output=True,
            check=False,
        )

    def test_official_uv_installer_path_creates_private_executable(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            home = base / "home"
            home.mkdir()
            fixture = base / "install-uv.sh"
            fixture.write_text(
                "#!/bin/sh\n"
                "mkdir -p \"$UV_UNMANAGED_INSTALL\"\n"
                "printf '#!/bin/sh\\nexit 0\\n' >\"$UV_UNMANAGED_INSTALL/uv\"\n"
                "chmod +x \"$UV_UNMANAGED_INSTALL/uv\"\n",
                encoding="utf-8",
            )
            result = self._shell(
                "curl() {\n"
                "  for value do previous=$output; output=$value; done\n"
                f"  cp '{fixture}' \"$output\"\n"
                "}\n"
                f'HOME="{home}"\n'
                "export HOME\n"
                "triple_stamp_install_uv",
                env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = home / ".local/bin/uv"
            self.assertTrue(installed.is_file())
            self.assertTrue(os.access(installed, os.X_OK))
            self.assertEqual(result.stdout.strip(), str(installed))

    def test_bootstrap_is_user_scoped_and_never_uses_sudo(self) -> None:
        source = BOOTSTRAP.read_text(encoding="utf-8")
        self.assertNotIn("sudo", source)
        self.assertIn("$HOME/.local/bin", source)
        self.assertIn("$HOME/.local/share/triple-stamp", source)
        self.assertIn("claude-agent-sdk==0.2.152", source)
        self.assertIn("0.12.*|0.14.*", source)
        self.assertIn("omnigent_version", (  # capability probe owns acceptance
            ROOT / ".omnigent/runtime-python/triple_stamp_omnigent_compat.py"
        ).read_text(encoding="utf-8"))
        self.assertIn("clone-$root_identity", source)
        self.assertIn("--connect-timeout 10 --max-time 60", source)
        self.assertIn("trap 'rm -f \"$lock\"; exit 130'", source)
        self.assertNotIn("command -v omnigent", source)

    def test_interrupted_runtime_swap_recovers_valid_backup(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            managed = base / "runtime"
            backup = base / "runtime.old.123"
            staging = base / "runtime.build.999"
            staging.mkdir()
            python = backup / "bin/python"
            python.parent.mkdir(parents=True)
            python.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *--probe*) printf '%s\\n' "
                "'{\"omnigent_version\":\"0.14.0\","
                "\"supported\":true,\"surfaces_ok\":true}' ;;\n"
                "  *json,sys*) exec /usr/bin/python3 \"$@\" ;;\n"
                "  *claude-agent-sdk*) exit 0 ;;\n"
                "  *) exit 1 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            python.chmod(0o755)
            entrypoint = backup / "bin/omnigent"
            entrypoint.write_text(
                f"#!{python}\nexit 0\n",
                encoding="utf-8",
            )
            entrypoint.chmod(0o755)
            result = self._shell(
                f'TRIPLE_STAMP_ROOT="{ROOT}"\n'
                f'TRIPLE_STAMP_MANAGED_RUNTIME="{managed}"\n'
                "export TRIPLE_STAMP_ROOT TRIPLE_STAMP_MANAGED_RUNTIME\n"
                "triple_stamp_recover_runtime_swap"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((managed / "bin/python").is_file())
            self.assertFalse(staging.exists())

    def test_transient_download_failure_is_retried(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            counter = base / "counter"
            command = base / "flaky"
            command.write_text(
                "#!/bin/sh\n"
                f"count=$(cat '{counter}' 2>/dev/null || printf 0)\n"
                "count=$((count + 1))\n"
                f"printf '%s' \"$count\" >'{counter}'\n"
                "[ \"$count\" -ge 3 ]\n",
                encoding="utf-8",
            )
            command.chmod(0o755)
            result = self._shell(
                f'triple_stamp_run_retry 3 0 5 "{command}"'
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(counter.read_text(encoding="utf-8"), "3")

    def test_venv_retry_removes_partial_staging_directory(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            managed = base / "runtime"
            counter = base / "counter"
            fake_uv = base / "uv"
            fake_uv.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = venv ]; then\n"
                f"  count=$(cat '{counter}' 2>/dev/null || printf 0)\n"
                "  count=$((count + 1))\n"
                f"  printf '%s' \"$count\" >'{counter}'\n"
                "  if [ \"$count\" = 1 ]; then mkdir -p \"$4/partial\"; exit 1; fi\n"
                "  [ ! -e \"$4\" ] || exit 91\n"
                "  mkdir -p \"$4/bin\"\n"
                "  cat >\"$4/bin/python\" <<'PY'\n"
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *--probe*) printf '%s\\n' "
                "'{\"omnigent_version\":\"0.14.0\","
                "\"supported\":true,\"surfaces_ok\":true}' ;;\n"
                "  *json,sys*) exec /usr/bin/python3 \"$@\" ;;\n"
                "  *claude-agent-sdk*) exit 0 ;;\n"
                "  *) exit 1 ;;\n"
                "esac\n"
                "PY\n"
                "  chmod +x \"$4/bin/python\"\n"
                "  printf '#!%s\\nexit 0\\n' \"$4/bin/python\" >\"$4/bin/omnigent\"\n"
                "  chmod +x \"$4/bin/omnigent\"\n"
                "  exit 0\n"
                "fi\n"
                "[ \"$1\" = pip ] && exit 0\n"
                "exit 2\n",
                encoding="utf-8",
            )
            fake_uv.chmod(0o755)
            result = self._shell(
                "sleep() { :; }\n"
                f'TRIPLE_STAMP_ROOT="{ROOT}"\n'
                f'TRIPLE_STAMP_MANAGED_RUNTIME="{managed}"\n'
                f'TRIPLE_STAMP_UV="{fake_uv}"\n'
                "export TRIPLE_STAMP_ROOT TRIPLE_STAMP_MANAGED_RUNTIME "
                "TRIPLE_STAMP_UV\n"
                "triple_stamp_install_runtime"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(counter.read_text(encoding="utf-8"), "2")
            self.assertTrue((managed / "bin/python").is_file())

    def test_distinct_clone_roots_receive_distinct_runtime_identities(self) -> None:
        first = self._shell('triple_stamp_root_identity "/tmp/clone-a"')
        second = self._shell('triple_stamp_root_identity "/tmp/clone-b"')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotEqual(first.stdout, second.stdout)

    def test_recovery_preserves_every_version_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            parent = Path(value)
            build_012 = parent / "runtime.build.12"
            build_014 = parent / "runtime.build.14"
            orphan = parent / "runtime.build.99"
            for path in (build_012, build_014, orphan):
                path.mkdir()
            managed_012 = parent / "omnigent-0.12.0-sdk-0.2.152-py3.13"
            managed_014 = parent / "omnigent-0.14.0-sdk-0.2.152-py3.13"
            managed_012.symlink_to(build_012.name)
            managed_014.symlink_to(build_014.name)
            result = self._shell(
                f'TRIPLE_STAMP_ROOT="{ROOT}"\n'
                f'TRIPLE_STAMP_MANAGED_RUNTIME="{managed_014}"\n'
                "export TRIPLE_STAMP_ROOT TRIPLE_STAMP_MANAGED_RUNTIME\n"
                "triple_stamp_recover_runtime_swap"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(build_012.is_dir())
            self.assertTrue(build_014.is_dir())
            self.assertFalse(orphan.exists())

    def test_supported_runtime_probe_is_capability_based(self) -> None:
        payloads = (
            '{"omnigent_version":"0.14.0","surfaces_ok":true,"supported":true}',
            '{ "supported" : true, "omnigent_version": "0.12.9", '
            '"surfaces_ok" : true, "extra" : 1 }',
        )
        for payload in payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as value:
                fake = Path(value) / "python"
                fake.write_text(
                    "#!/bin/sh\n"
                    "case \"$*\" in\n"
                    f"  *--probe*) printf '%s\\n' '{payload}' ;;\n"
                    "  *) exec /usr/bin/python3 \"$@\" ;;\n"
                    "esac\n",
                    encoding="utf-8",
                )
                fake.chmod(0o755)
                result = self._shell(
                    f'TRIPLE_STAMP_ROOT="{ROOT}"\n'
                    f'triple_stamp_runtime_supported "{fake}"'
                )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_runtime_probe_requires_supported_version(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            fake = Path(value) / "python"
            fake.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *--probe*) printf '%s\\n' "
                "'{\"supported\":true,\"surfaces_ok\":true}' ;;\n"
                "  *) exec /usr/bin/python3 \"$@\" ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            result = self._shell(
                f'TRIPLE_STAMP_ROOT="{ROOT}"\n'
                f'triple_stamp_runtime_supported "{fake}"'
            )
        self.assertNotEqual(result.returncode, 0)

    def test_full_fresh_home_bootstrap_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            home = base / "home"
            home.mkdir()
            state = base / "state"
            log = base / "uv.log"
            handoff = base / "handoff.log"
            fake_uv = base / "uv"
            fake_uv.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >>'{log}'\n"
                "if [ \"$1\" = tool ] && [ \"$2\" = dir ]; then exit 0; fi\n"
                "if [ \"$1\" = venv ]; then\n"
                "  target=$4\n"
                "  mkdir -p \"$target/bin\"\n"
                "  cat >\"$target/bin/python\" <<'PY'\n"
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *--probe*) printf '%s\\n' "
                "'{\"omnigent_version\":\"0.14.0\","
                "\"surfaces_ok\":true,\"supported\":true}' ;;\n"
                "  *json,sys*) exec /usr/bin/python3 \"$@\" ;;\n"
                "  *claude-agent-sdk*) exit 0 ;;\n"
                "  *outsider_preflight.py*) "
                f"printf '%s\\n' \"$STABLE_OMNIGENT_PY\" >>'{handoff}'; exit 0 ;;\n"
                "  *) exit 1 ;;\n"
                "esac\n"
                "PY\n"
                "  chmod +x \"$target/bin/python\"\n"
                "  printf '#!%s\\nexit 0\\n' \"$target/bin/python\" "
                ">\"$target/bin/omnigent\"\n"
                "  chmod +x \"$target/bin/omnigent\"\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"$1\" = pip ] && [ \"$2\" = install ]; then exit 0; fi\n"
                "exit 2\n",
                encoding="utf-8",
            )
            fake_uv.chmod(0o755)
            env = {
                "HOME": str(home),
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "TRIPLE_STAMP_ROOT": str(ROOT),
                "TRIPLE_STAMP_STATE_DIR": str(state),
                "TRIPLE_STAMP_MANAGED_RUNTIME": str(state / "runtime"),
                "TRIPLE_STAMP_UV_BIN": str(fake_uv),
                "TRIPLE_STAMP_BOOTSTRAP_SKIP_LOGIN": "1",
                "STABLE_OMNIGENT_PY": "",
            }
            first = self._shell("triple_stamp_bootstrap --self-test", env=env)
            second = self._shell("triple_stamp_bootstrap --self-test", env=env)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(sum(line.startswith("venv ") for line in calls), 1)
            self.assertEqual(sum(line.startswith("pip install ") for line in calls), 1)
            handed_off = handoff.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(handed_off), 2)
            self.assertTrue(all(Path(path).is_file() for path in handed_off))
            self.assertFalse((state / "direct-preflight-v1").exists())

    def test_unsupported_runtime_probe_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            fake = Path(value) / "python"
            fake.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *--probe*) printf '%s\\n' "
                "'{\"omnigent_version\":\"0.13.0\","
                "\"supported\": false,\"surfaces_ok\": true}' ;;\n"
                "  *) exec /usr/bin/python3 \"$@\" ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            result = self._shell(
                f'TRIPLE_STAMP_ROOT="{ROOT}"\n'
                f'triple_stamp_runtime_supported "{fake}"'
            )
        self.assertNotEqual(result.returncode, 0)

    def test_external_runtime_with_wrong_sdk_is_not_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            fake = base / "python"
            fake.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *--probe*) printf '%s\\n' "
                "'{\"omnigent_version\":\"0.14.0\","
                "\"surfaces_ok\": true,\"supported\": true}' ; exit 0 ;;\n"
                "  *json,sys*) exec /usr/bin/python3 \"$@\" ;;\n"
                "  *) exit 1 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            result = self._shell(
                f'TRIPLE_STAMP_ROOT="{ROOT}"\n'
                f'STABLE_OMNIGENT_PY="{fake}"\n'
                f'TRIPLE_STAMP_MANAGED_RUNTIME="{base}/missing-runtime"\n'
                'TRIPLE_STAMP_UV="/nonexistent/uv"\n'
                "export TRIPLE_STAMP_ROOT STABLE_OMNIGENT_PY "
                "TRIPLE_STAMP_MANAGED_RUNTIME TRIPLE_STAMP_UV\n"
                "triple_stamp_find_supported_runtime",
                env={
                    "HOME": str(base),
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                },
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_managed_runtime_install_is_staged_and_usable(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            fake_uv = base / "uv"
            fake_uv.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = venv ]; then\n"
                "  target=$4\n"
                "  mkdir -p \"$target/bin\"\n"
                "  cat >\"$target/bin/python\" <<'PY'\n"
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *--probe*) printf '%s\\n' "
                "'{\"omnigent_version\":\"0.14.0\","
                "\"supported\": true,\"surfaces_ok\": true}' ;;\n"
                "  *claude-agent-sdk*) exit 0 ;;\n"
                "  *) exec /usr/bin/python3 \"$@\" ;;\n"
                "esac\n"
                "PY\n"
                "  chmod +x \"$target/bin/python\"\n"
                "  printf '#!%s\\nexit 0\\n' \"$target/bin/python\" "
                ">\"$target/bin/omnigent\"\n"
                "  chmod +x \"$target/bin/omnigent\"\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"$1\" = pip ] && [ \"$2\" = install ]; then exit 0; fi\n"
                "exit 2\n",
                encoding="utf-8",
            )
            fake_uv.chmod(0o755)
            managed = base / "state/runtime"
            result = self._shell(
                f'TRIPLE_STAMP_ROOT="{ROOT}"\n'
                f'TRIPLE_STAMP_UV="{fake_uv}"\n'
                f'TRIPLE_STAMP_MANAGED_RUNTIME="{managed}"\n'
                "TRIPLE_STAMP_BOOTSTRAP_OMNIGENT_VERSION=0.14.0\n"
                "export TRIPLE_STAMP_ROOT TRIPLE_STAMP_UV "
                "TRIPLE_STAMP_MANAGED_RUNTIME "
                "TRIPLE_STAMP_BOOTSTRAP_OMNIGENT_VERSION\n"
                "triple_stamp_install_runtime",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(managed.is_symlink())
            self.assertTrue((managed / "bin/python").is_file())
            self.assertTrue(os.access(managed / "bin/omnigent", os.X_OK))
            self.assertFalse(any(managed.parent.glob("runtime.link.*")))

    def test_invalid_managed_version_is_rejected_before_install(self) -> None:
        result = self._shell(
            'TRIPLE_STAMP_BOOTSTRAP_OMNIGENT_VERSION="0.13.0"\n'
            'TRIPLE_STAMP_MANAGED_RUNTIME="/tmp/must-not-be-created"\n'
            "export TRIPLE_STAMP_BOOTSTRAP_OMNIGENT_VERSION "
            "TRIPLE_STAMP_MANAGED_RUNTIME\n"
            "triple_stamp_install_runtime"
        )
        self.assertEqual(result.returncode, 78)
        self.assertIn("must be 0.12.x or 0.14.x", result.stderr)

    def test_help_has_no_bootstrap_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            home = Path(value) / "empty-home"
            home.mkdir()
            state = home / ".local/share/triple-stamp"
            result = subprocess.run(
                ["/bin/sh", str(ROOT / "triple-stamp"), "--help"],
                cwd=ROOT,
                env={
                    "HOME": str(home),
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Usage: ./triple-stamp", result.stdout)
            self.assertFalse(state.exists())

    def test_stale_bootstrap_lock_is_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            lock = Path(value) / "bootstrap.lock"
            lock.write_text("99999999\n", encoding="utf-8")
            result = self._shell(
                f'triple_stamp_acquire_bootstrap_lock "{lock}"\n'
                f'rm -f "{lock}"'
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_two_processes_serialize_on_bootstrap_lock(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            lock = base / "bootstrap.lock"
            marker = base / "first-acquired"
            attempted = base / "second-attempted"
            acquired = base / "second-acquired"
            release = base / "release-first"
            first_script = (
                f'. "{BOOTSTRAP}"\n'
                f'triple_stamp_acquire_bootstrap_lock "{lock}"\n'
                f': >"{marker}"\n'
                f'while [ ! -e "{release}" ]; do sleep 0.05; done\n'
                f'rm -f "{lock}"\n'
            )
            first = subprocess.Popen(
                ["/bin/sh", "-c", first_script],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), "first process never acquired lock")
            second_script = (
                f'. "{BOOTSTRAP}"\n'
                f': >"{attempted}"\n'
                f'triple_stamp_acquire_bootstrap_lock "{lock}"\n'
                f': >"{acquired}"\n'
                f'rm -f "{lock}"\n'
            )
            second = subprocess.Popen(
                ["/bin/sh", "-c", second_script],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.monotonic() + 5
            while not attempted.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(attempted.exists(), "second process did not start")
            self.assertFalse(acquired.exists(), "second process bypassed held lock")
            release.touch()
            first_stdout, first_stderr = first.communicate(timeout=5)
            second_stdout, second_stderr = second.communicate(timeout=5)
            self.assertEqual(first.returncode, 0, first_stderr or first_stdout)
            self.assertEqual(second.returncode, 0, second_stderr or second_stdout)
            self.assertTrue(acquired.exists())


if __name__ == "__main__":
    unittest.main()
