from __future__ import annotations

import importlib.util
import json
import os
import shlex
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "portable_launcher_under_test", ROOT / ".omnigent/launcher.py"
)
assert SPEC is not None and SPEC.loader is not None
launcher = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = launcher
SPEC.loader.exec_module(launcher)


class InstallPortabilityTests(unittest.TestCase):
    def _tools(self) -> object:
        return launcher.Toolchain(
            isaac=None,
            dbcert=None,
            databricks=None,
            uv=Path("/custom/bin/uv"),
            omnigent=Path("/custom/bin/omnigent"),
            omnigent_python=Path("/custom/omnigent/bin/python"),
            cursor_agent=Path("/custom/npm/bin/cursor-agent"),
            sandbox_exec=Path("/usr/bin/sandbox-exec"),
            security=Path("/usr/bin/security"),
            claude=Path("/custom/npm/bin/claude"),
            codex=Path("/custom/npm/bin/codex"),
        )

    def test_host_env_preserves_package_network_config_but_not_secrets(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "UV_DEFAULT_INDEX": "https://packages.example/simple/",
                "PIP_EXTRA_INDEX_URL": "https://extra.example/simple/",
                "https_proxy": "https://proxy.example",
                "OPENAI_API_KEY": "do-not-forward",
            },
            clear=True,
        ):
            env = launcher._host_env(Path("/tmp/real-home"))
        self.assertEqual(
            env["UV_DEFAULT_INDEX"], "https://packages.example/simple/"
        )
        self.assertEqual(
            env["PIP_EXTRA_INDEX_URL"], "https://extra.example/simple/"
        )
        self.assertEqual(env["https_proxy"], "https://proxy.example")
        self.assertNotIn("OPENAI_API_KEY", env)

    def test_newer_sdk_is_repaired_with_exact_pin(self) -> None:
        check = launcher.subprocess.CompletedProcess([], 0, "0.2.153\n", "")
        installed = launcher.subprocess.CompletedProcess([], 0, "", "")
        with (
            mock.patch.object(
                launcher, "_run", side_effect=[check, check, installed]
            ) as run,
            mock.patch.object(launcher, "_PluginInstallLock") as lock,
        ):
            launcher._ensure_claude_agent_sdk_pin(self._tools(), {})
        lock.assert_called_once_with(self._tools().omnigent_python)
        self.assertEqual(
            run.call_args_list[2].args[0],
            [
                "/custom/bin/uv",
                "pip",
                "install",
                "--python",
                "/custom/omnigent/bin/python",
                "claude-agent-sdk==0.2.152",
            ],
        )

    def test_sdk_pin_is_rechecked_after_install_lock(self) -> None:
        newer = launcher.subprocess.CompletedProcess([], 0, "0.2.153\n", "")
        repaired = launcher.subprocess.CompletedProcess([], 0, "0.2.152\n", "")
        with (
            mock.patch.object(
                launcher, "_run", side_effect=[newer, repaired]
            ) as run,
            mock.patch.object(launcher, "_PluginInstallLock") as lock,
        ):
            launcher._ensure_claude_agent_sdk_pin(self._tools(), {})
        lock.assert_called_once_with(self._tools().omnigent_python)
        self.assertEqual(run.call_count, 2)

    def test_install_lock_is_keyed_by_target_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            interpreter = base / "shared/bin/python"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_text("", encoding="utf-8")
            alias = base / "clone/bin/python"
            alias.parent.mkdir(parents=True)
            alias.symlink_to(interpreter)
            other = base / "other/bin/python"
            first = launcher._PluginInstallLock(interpreter)
            same_target = launcher._PluginInstallLock(alias)
            different_target = launcher._PluginInstallLock(other)
        self.assertEqual(first.path, same_target.path)
        self.assertNotEqual(first.path, different_target.path)

    def test_install_lock_lives_under_sandbox_allowed_tmp_root(self) -> None:
        # The outer Seatbelt only grants /tmp writes under /tmp/claude-<uid>;
        # the lock must live there so sandboxed plugin installs are not denied.
        lock = launcher._PluginInstallLock(Path("/some/managed/bin/python"))
        allowed_root = Path("/tmp") / f"claude-{launcher.os.getuid()}"
        self.assertTrue(
            lock.path.is_relative_to(allowed_root),
            f"{lock.path} must live under {allowed_root}",
        )
        self.assertEqual(lock.path.suffix, ".lock")

    def test_sdk_pin_failure_is_sanitized_and_shell_quoted(self) -> None:
        tools = self._tools()
        tools = launcher.Toolchain(
            **{
                **tools.__dict__,
                "uv": Path("/custom tools/uv"),
                "omnigent_python": Path("/custom env/bin/python"),
            }
        )
        newer = launcher.subprocess.CompletedProcess([], 0, "0.2.153\n", "")
        failed = launcher.subprocess.CompletedProcess(
            [],
            1,
            "",
            "Authorization: Bearer top-secret https://proxy.example/simple",
        )
        with (
            mock.patch.object(
                launcher,
                "_run",
                side_effect=[newer, newer, failed],
            ),
            mock.patch.object(launcher, "_PluginInstallLock"),
            self.assertRaises(launcher.LaunchError) as raised,
        ):
            launcher._ensure_claude_agent_sdk_pin(tools, {})
        message = str(raised.exception)
        self.assertNotIn("top-secret", message)
        self.assertNotIn("proxy.example", message)
        self.assertIn("[REDACTED_URL]", message)
        self.assertIn(shlex.quote(str(tools.uv)), message)
        self.assertIn(shlex.quote(str(tools.omnigent_python)), message)

    def test_strict_validator_still_rejects_sdk_0_2_153(self) -> None:
        probe = launcher.subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "omnigent_version": "0.12.0",
                    "supported": True,
                    "surfaces_ok": True,
                    "missing_surfaces": [],
                }
            ),
            "",
        )
        results = [
            probe,
            launcher.subprocess.CompletedProcess([], 0, "0.2.153\n", ""),
        ]
        with (
            mock.patch.object(launcher, "_run", side_effect=results),
            self.assertRaises(launcher.LaunchError) as raised,
        ):
            launcher._validate_versions(self._tools(), {}, "direct")
        self.assertIn("must use claude-agent-sdk 0.2.152", str(raised.exception))

    def test_required_clis_are_discovered_from_path(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            path_bin = base / "npm-global/bin"
            path_bin.mkdir(parents=True)
            for name in ("cursor-agent", "claude", "codex"):
                executable = path_bin / name
                executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                executable.chmod(
                    executable.stat().st_mode
                    | stat.S_IXUSR
                    | stat.S_IXGRP
                    | stat.S_IXOTH
                )
                canonical = base / f"missing-canonical/{name}"
                with mock.patch.object(
                    launcher.shutil,
                    "which",
                    return_value=str(executable),
                ):
                    candidates = launcher._path_candidates(name, [canonical])
                self.assertEqual(
                    launcher._first_executable(name, candidates),
                    executable,
                )

    def test_cli_install_prefix_covers_node_and_homebrew_layouts(self) -> None:
        cases = {
            "/Users/unit/.nvm/versions/node/v22.1.0/bin/claude": (
                "/Users/unit/.nvm/versions/node/v22.1.0"
            ),
            (
                "/Users/unit/.local/share/fnm/node-versions/v22.1.0/"
                "installation/bin/codex"
            ): (
                "/Users/unit/.local/share/fnm/node-versions/v22.1.0/"
                "installation"
            ),
            "/Users/unit/.volta/bin/cursor-agent": "/Users/unit/.volta",
            "/Users/unit/.npm-global/bin/claude": "/Users/unit/.npm-global",
            "/opt/homebrew/bin/codex": "/opt/homebrew",
            "/opt/homebrew/Cellar/node/22.1.0/bin/claude": (
                "/opt/homebrew/Cellar/node/22.1.0"
            ),
            "/custom/tools/cursor-agent": "/custom/tools",
        }
        for executable, expected in cases.items():
            with self.subTest(executable=executable):
                self.assertEqual(
                    launcher._cli_install_prefix(Path(executable)),
                    Path(expected),
                )
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            target = base / ".volta/bin/claude"
            target.parent.mkdir(parents=True)
            target.write_text("", encoding="utf-8")
            linked = base / ".local/bin/claude"
            linked.parent.mkdir(parents=True)
            linked.symlink_to(target)
            self.assertEqual(
                launcher._cli_install_prefix(linked),
                (base / ".volta").resolve(),
            )

    def test_wrappers_and_isolated_home_use_resolved_omnigent_python(
        self,
    ) -> None:
        launcher_wrapper = (ROOT / "triple-stamp").read_text(encoding="utf-8")
        cursor_wrapper = (
            ROOT / ".omnigent/cursor-via-login"
        ).read_text(encoding="utf-8")
        bootstrap = (ROOT / ".omnigent/bootstrap.sh").read_text(encoding="utf-8")
        self.assertIn("STABLE_OMNIGENT_PY", launcher_wrapper)
        self.assertIn("STABLE_OMNIGENT_PY", cursor_wrapper)
        self.assertIn("TRIPLE_STAMP_MANAGED_RUNTIME/bin/python", bootstrap)
        self.assertIn("clone-$root_identity", bootstrap)
        self.assertNotIn("command -v omnigent", bootstrap)
        self.assertNotIn('"$TRIPLE_STAMP_UV" tool dir', bootstrap)
        self.assertIn("triple_stamp_bootstrap", launcher_wrapper)
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            real_home = base / "real-home"
            isolated_home = base / "isolated-home"
            stable_python = base / "custom-tools/omnigent/bin/python"
            real_home.mkdir()
            launcher._seed_isolated_home(
                ROOT,
                real_home,
                isolated_home,
                stable_python,
            )
            for name in ("python", "python3"):
                self.assertEqual(
                    (isolated_home / ".local/bin" / name).readlink(),
                    stable_python,
                )
            config = (isolated_home / ".omnigent/config.yaml").read_text(
                encoding="utf-8"
            )
            self.assertIn("theme: dark", config)

    def test_shell_launcher_uses_bootstrapped_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            bin_dir = base / "bin"
            home = base / "home"
            bin_dir.mkdir()
            home.mkdir()

            for source in ("entrypoint", "managed-runtime"):
                with self.subTest(source=source):
                    capture = base / f"{source}.args"
                    python = base / f"{source}-env/bin/python"
                    python.parent.mkdir(parents=True)
                    python.write_text(
                        "#!/bin/sh\n"
                        "printf '%s\\n' \"$@\" >\"$CAPTURE\"\n",
                        encoding="utf-8",
                    )
                    python.chmod(0o755)
                    result = launcher.subprocess.run(
                        ["/bin/sh", str(ROOT / "triple-stamp"), "--self-test"],
                        env={
                            "PATH": f"{bin_dir}:/usr/bin:/bin",
                            "HOME": str(home),
                            "CAPTURE": str(capture),
                            "STABLE_OMNIGENT_PY": str(python),
                            "TRIPLE_STAMP_BOOTSTRAP": "0",
                        },
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        capture.read_text(encoding="utf-8").splitlines(),
                        [
                            "-I",
                            str(ROOT / ".omnigent/launcher.py"),
                            "--self-test",
                        ],
                    )

    def test_canonical_cli_paths_precede_ambient_path(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            canonical = base / "canonical/cursor-agent"
            ambient = base / "ambient/cursor-agent"
            for executable in (canonical, ambient):
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                executable.chmod(0o755)
            with mock.patch.object(
                launcher.shutil,
                "which",
                return_value=str(ambient),
            ):
                candidates = launcher._path_candidates(
                    "cursor-agent",
                    [canonical],
                )
        self.assertEqual(candidates, [canonical, ambient])

    def test_canonical_cli_wins_and_runtime_path_only_gets_selected_parents(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            canonical = base / "canonical/cursor-agent"
            ambient = base / "ambient/cursor-agent"
            for executable in (canonical, ambient):
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                executable.chmod(0o755)
            with mock.patch.object(
                launcher.shutil,
                "which",
                return_value=str(ambient),
            ):
                selected = launcher._first_executable(
                    "Cursor Agent",
                    launcher._path_candidates("cursor-agent", [canonical]),
                )
            self.assertEqual(selected, canonical.absolute())
            self.assertNotEqual(selected, ambient.absolute())

            npm_bin = base / ".npm-global/bin"
            npm_bin.mkdir(parents=True)
            tools = launcher.Toolchain(
                isaac=None,
                dbcert=None,
                databricks=None,
                uv=base / "uv",
                omnigent=base / "omnigent",
                omnigent_python=base / "omnigent-python",
                cursor_agent=npm_bin / "cursor-agent",
                sandbox_exec=Path("/usr/bin/sandbox-exec"),
                security=Path("/usr/bin/security"),
                claude=npm_bin / "claude",
                codex=npm_bin / "codex",
            )
            with mock.patch.dict(
                launcher.os.environ,
                {"PATH": "/evil/bin:/usr/bin:/bin"},
                clear=False,
            ):
                runtime = launcher._runtime_env(
                    root=ROOT,
                    real_home=base / "home",
                    run_dir=base / "run",
                    bundle=base / "run/bundle",
                    run_id="security-regression",
                    sandbox_token="sandbox",
                    cursor_token="cursor",
                    omnigent_token="",
                    harness_tmp=base / "short",
                    tools=tools,
                    managed_python=tools.omnigent_python,
                    voice_profile_sha256="",
                    provider="direct",
                    models=launcher._provider_models(ROOT, "direct"),
                )
            runtime_path = runtime["PATH"].split(":")
            self.assertNotIn("/evil/bin", runtime_path)
            self.assertIn(str(npm_bin), runtime_path)
            self.assertEqual(
                runtime["CURSOR_AGENT_BIN"],
                str(tools.cursor_agent),
            )

            captured: list[str] = []

            def build_result(
                argv: list[str],
                **_kwargs: object,
            ) -> launcher.subprocess.CompletedProcess[str]:
                captured.extend(argv)
                profile = Path(argv[4])
                bundle = Path(argv[6])
                profile.parent.mkdir(parents=True, exist_ok=True)
                profile.write_text("(version 1)\n", encoding="utf-8")
                bundle.mkdir(parents=True, exist_ok=True)
                (bundle / "config.yaml").write_text("{}\n", encoding="utf-8")
                return launcher.subprocess.CompletedProcess(argv, 0, "", "")

            with mock.patch.object(launcher, "_run", side_effect=build_result):
                launcher._build_profile_and_bundle(
                    ROOT,
                    base / "home",
                    base / "build-run",
                    tools,
                    runtime,
                )
            self.assertEqual(
                json.loads(captured[9]),
                [str(base / ".npm-global")],
            )

        seatbelt_builder = (
            ROOT / ".omnigent/build_outer_seatbelt.py"
        ).read_text(encoding="utf-8")
        self.assertIn("read_paths: set[str] = {", seatbelt_builder)
        self.assertIn("read_paths.update(cli_read_dirs)", seatbelt_builder)
        self.assertIn("CLI_READ_DIRS_JSON", seatbelt_builder)
        self.assertIn("OMNIGENT_HARNESS_TMP_PARENT", seatbelt_builder)
        self.assertIn("sys.prefix", seatbelt_builder)
        self.assertIn(
            'write_paths.add(str(real_home / ".local/share/isaac"))',
            seatbelt_builder,
        )
        plugin_meta = (
            ROOT / ".omnigent/isaac-launcher/pyproject.toml"
        ).read_text(encoding="utf-8")
        self.assertIn('requires-python = ">=3.12,<3.15"', plugin_meta)
        self.assertNotIn("cursor_agent.parent", seatbelt_builder)
        self.assertNotIn('os.environ.get("PATH"', seatbelt_builder)
        self.assertNotIn("os.environ['PATH']", seatbelt_builder)


if __name__ == "__main__":
    unittest.main()
