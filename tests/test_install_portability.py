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
            # 2026-10-03: a 68-minute gateway stall outlasted the runner's
            # one-hour idle exit and the chat closed with no message. The
            # runner itself must read the run's longer limit.
            from omnigent.runner import _entry

            with mock.patch.dict(
                os.environ,
                {"OMNIGENT_CONFIG_HOME": str(isolated_home / ".omnigent")},
            ):
                self.assertEqual(
                    _entry._load_runner_idle_timeout_s_from_config(),
                    float(launcher.RUNNER_IDLE_TIMEOUT_S),
                )
            self.assertGreaterEqual(launcher.RUNNER_IDLE_TIMEOUT_S, 2 * 60 * 60)

    def test_public_only_run_gets_no_mcp_servers(self) -> None:
        """``TRIPLE_STAMP_INTERNAL_SOURCES=off`` matches a Mac outside Databricks."""

        catalog = {
            "oauthAccount": {"emailAddress": "someone@example.com"},
            "mcpServers": {"glean": {"type": "stdio", "command": "dbexec"}},
            "projects": {
                "/work": {"mcpServers": {"slack": {"type": "stdio"}}, "history": []}
            },
        }
        for value, servers in (("off", {}), ("", catalog["mcpServers"])):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as root:
                base = Path(root)
                real_home = base / "real-home"
                real_home.mkdir()
                (real_home / ".claude.json").write_text(json.dumps(catalog), encoding="utf-8")
                with mock.patch.dict(
                    os.environ, {launcher.INTERNAL_SOURCES_ENV: value}
                ), mock.patch.object(launcher, "_eprint"):
                    launcher._seed_isolated_home(
                        ROOT, real_home, base / "isolated", base / "python"
                    )
                seeded = json.loads(
                    (base / "isolated/.claude.json").read_text(encoding="utf-8")
                )
                self.assertEqual(seeded["mcpServers"], servers)
                self.assertEqual(seeded["oauthAccount"], catalog["oauthAccount"])
                if value == "off":
                    self.assertEqual(seeded["projects"]["/work"]["mcpServers"], {})
                    self.assertEqual(seeded["projects"]["/work"]["history"], [])
                # The user's own file is never changed.
                self.assertEqual(
                    json.loads((real_home / ".claude.json").read_text(encoding="utf-8")),
                    catalog,
                )

    def test_public_only_run_gives_codex_no_mcp_servers(self) -> None:
        """2026-10-03: public-only judges still started Glean, Slack, and Jira."""

        tables = (
            'model_provider = "example"\n'
            "# the judge's provider\n"
            "[model_providers.example]\n"
            'base_url = "https://example.invalid/v1"\n'
            "\n"
            "[mcp_servers.glean]\n"
            'command = "dbexec"\n'
            "args = [\n"
            '  "glean",\n'
            "]\n"
            "[mcp_servers.glean.env]\n"
            'TOKEN = "x"\n'
            "[ \"mcp_servers\" . 'slack' ]  # quoted\n"
            'command = "dbexec"\n'
            "[[mcp_servers.extra]]\n"
            'name = "x"\n'
            "[tui]\n"
            'theme = "dark"\n'
        )
        dotted = 'mcp_servers.glean.command = "dbexec"\nmodel = "m"\n[tui]\nx = 1\n'
        for value, text, kept in (
            (
                "off",
                tables,
                {
                    "model_provider": "example",
                    "model_providers": {
                        "example": {"base_url": "https://example.invalid/v1"}
                    },
                    "tui": {"theme": "dark"},
                },
            ),
            ("off", dotted, {"model": "m", "tui": {"x": 1}}),
            ("", tables, None),
        ):
            with self.subTest(value=value, text=text[:20]), tempfile.TemporaryDirectory() as root:
                base = Path(root)
                real_home = base / "real-home"
                (real_home / ".codex").mkdir(parents=True)
                (real_home / ".codex/config.toml").write_text(text, encoding="utf-8")
                with mock.patch.dict(
                    os.environ, {launcher.INTERNAL_SOURCES_ENV: value}
                ), mock.patch.object(launcher, "_eprint"):
                    launcher._seed_isolated_home(
                        ROOT, real_home, base / "isolated", base / "python"
                    )
                seeded = (base / "isolated/.codex/config.toml").read_text(encoding="utf-8")
                if kept is None:
                    self.assertEqual(seeded, text)
                else:
                    self.assertEqual(launcher.tomllib.loads(seeded), kept)
                    self.assertNotIn("mcp_servers", seeded)
                if text is tables and kept is not None:
                    # Every kept line is the user's own line, comments included.
                    self.assertIn("# the judge's provider\n", seeded)
                self.assertEqual(
                    (real_home / ".codex/config.toml").read_text(encoding="utf-8"), text
                )

        # A header-like line inside a string cannot be removed safely: fail closed.
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "config.toml"
            config.write_text(
                '[mcp_servers.glean]\nnote = """\n[tui]\n"""\ncommand = "dbexec"\n',
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {launcher.INTERNAL_SOURCES_ENV: "off"}):
                with self.assertRaises(launcher.LaunchError) as raised:
                    launcher._drop_codex_mcp_servers(config)
            self.assertIn("could not remove the MCP servers", str(raised.exception))

    def test_judge_codex_never_stops_on_a_new_model_screen(self) -> None:
        """2026-10-09: judges' Codex waited on "Meet GPT-6 Sol" until they timed out.

        Codex keeps a new TUI on that screen until someone picks a model,
        unless the upgrade is already recorded as seen, which is what picking
        "Use existing model" records.
        """

        catalog = {
            "models": [
                {"slug": "gpt-5.6-sol", "upgrade": {"model": "gpt-7-sol"}},
                {"slug": "gpt-5.6-luna", "upgrade": {"model": "gpt-6-luna"}},
                {"slug": "gpt-6-sol", "upgrade": None},
                {"slug": "broken", "upgrade": {"model": ""}},
                "not a model",
            ]
        }
        user_config = (
            '# kept comment\nmodel = "gpt-5.6-sol"\n\n'
            "[notice]\nhide_full_access_warning = true\n\n"
            '[mcp_servers.demo]\ncommand = "demo"\n'
        )
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            codex = base / "codex"
            codex.write_text(
                "#!/bin/sh\n"
                '[ "$1 $2" = "debug models" ] || exit 2\n'
                '[ -n "$CODEX_HOME" ] && [ "$CODEX_HOME" != "$HOME/.codex" ] || exit 3\n'
                f"printf '%s' '{json.dumps(catalog)}'\n",
                encoding="utf-8",
            )
            codex.chmod(0o755)
            broken = base / "broken-codex"
            broken.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            broken.chmod(0o755)
            config = base / "home/.codex/config.toml"
            config.parent.mkdir(parents=True)
            config.write_text(user_config, encoding="utf-8")
            env = {"PATH": "/usr/bin:/bin", "HOME": str(base / "home")}

            launcher._acknowledge_codex_upgrades(config, [broken, codex], env)
            seeded = config.read_text(encoding="utf-8")
            settings = launcher.tomllib.loads(seeded)
            self.assertIn("# kept comment\n", seeded)
            self.assertEqual(settings["model"], "gpt-5.6-sol")
            self.assertEqual(settings["mcp_servers"], {"demo": {"command": "demo"}})
            self.assertEqual(
                settings["notice"],
                {
                    "hide_full_access_warning": True,
                    "model_migrations": {
                        # A listed upgrade replaces the built-in default.
                        "gpt-5.5": "gpt-6-sol",
                        "gpt-5.6-luna": "gpt-6-luna",
                        "gpt-5.6-sol": "gpt-7-sol",
                        "gpt-5.6-terra": "gpt-6-sol",
                    },
                },
            )
            # Run again: the table is updated in place, not added twice.
            launcher._acknowledge_codex_upgrades(config, [codex], env)
            self.assertEqual(launcher.tomllib.loads(config.read_text(encoding="utf-8")), settings)

            # No Codex config yet, and no Codex that can list its models.
            fresh = base / "fresh/config.toml"
            launcher._acknowledge_codex_upgrades(fresh, [broken], env)
            self.assertEqual(
                launcher.tomllib.loads(fresh.read_text(encoding="utf-8")),
                {"notice": {"model_migrations": launcher._KNOWN_CODEX_UPGRADES}},
            )

            # A config that does not parse is left alone with a warning.
            bad = base / "bad.toml"
            bad.write_text("model = [\n", encoding="utf-8")
            with mock.patch.object(launcher, "_eprint") as warned:
                launcher._acknowledge_codex_upgrades(bad, [], env)
            self.assertEqual(bad.read_text(encoding="utf-8"), "model = [\n")
            self.assertIn("'Meet <model>' screen", warned.call_args.args[0])

        # The run's Codex, then the one on the run's PATH, whose list wins.
        with tempfile.TemporaryDirectory() as root:
            first, second = Path(root) / "a/codex", Path(root) / "b/codex"
            for path in (first, second):
                path.parent.mkdir()
                path.write_text("#!/bin/sh\n", encoding="utf-8")
                path.chmod(0o755)
            self.assertEqual(
                launcher._codex_binaries(first, {"PATH": str(second.parent)}),
                [first.resolve(), second.resolve()],
            )
            self.assertEqual(
                launcher._codex_binaries(first, {"PATH": str(first.parent)}),
                [first.resolve()],
            )
        source = (ROOT / ".omnigent/launcher.py").read_text(encoding="utf-8")
        self.assertIn(
            '_acknowledge_codex_upgrades(\n            run_dir / "home/.codex/config.toml",',
            source,
        )

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
