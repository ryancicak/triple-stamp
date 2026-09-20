from __future__ import annotations

import importlib.util
import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "outsider_preflight_under_test", ROOT / ".omnigent/outsider_preflight.py"
)
assert SPEC is not None and SPEC.loader is not None
preflight = importlib.util.module_from_spec(SPEC)
# Register before exec so the frozen dataclass can resolve its own module while
# processing PEP 563 string annotations.
sys.modules[SPEC.name] = preflight
SPEC.loader.exec_module(preflight)


def _make_tool(**overrides) -> "preflight.Tool":
    base = dict(
        key="demo",
        label="Demo CLI",
        binary="demo",
        search_paths=(),
        model_display="Demo Model",
        model_id="demo-1",
        entitlement="your account must be entitled to it",
        install=("install demo",),
        login="demo login",
    )
    base.update(overrides)
    return preflight.Tool(**base)


class PresenceDetectionTests(unittest.TestCase):
    """`is_present` logic, isolated from whatever the host has installed."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _executable(self, name: str) -> Path:
        path = self.tmp / name
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return path

    def test_found_on_path(self) -> None:
        tool = _make_tool(search_paths=(self.tmp / "nope",))
        with mock.patch.object(preflight.shutil, "which", return_value="/bin/demo"):
            self.assertTrue(preflight.is_present(tool))

    def test_found_at_known_install_location(self) -> None:
        installed = self._executable("demo")
        tool = _make_tool(search_paths=(self.tmp / "missing", installed))
        with mock.patch.object(preflight.shutil, "which", return_value=None):
            self.assertTrue(preflight.is_present(tool))

    def test_absent_when_neither_path_nor_known_location(self) -> None:
        tool = _make_tool(search_paths=(self.tmp / "missing",))
        with mock.patch.object(preflight.shutil, "which", return_value=None):
            self.assertFalse(preflight.is_present(tool))

    def test_non_executable_known_location_is_not_present(self) -> None:
        plain = self.tmp / "demo"
        plain.write_text("not executable\n", encoding="utf-8")
        tool = _make_tool(search_paths=(plain,))
        with mock.patch.object(preflight.shutil, "which", return_value=None):
            self.assertFalse(preflight.is_present(tool))


class RequiredToolsTests(unittest.TestCase):
    def test_all_three_clis_are_required(self) -> None:
        keys = {tool.key for tool in preflight.tools(Path("/nonexistent-home"))}
        self.assertEqual(keys, {"cursor-agent", "claude", "codex"})

    def test_search_paths_are_anchored_under_the_given_home(self) -> None:
        home = Path("/tmp/fake-home-xyz")
        by_key = {t.key: t for t in preflight.tools(home)}
        # ~/.local/bin variants must follow the supplied home.
        self.assertIn(home / ".local/bin/cursor-agent", by_key["cursor-agent"].search_paths)
        self.assertIn(home / ".local/bin/claude", by_key["claude"].search_paths)
        self.assertIn(home / ".local/bin/codex", by_key["codex"].search_paths)


class MessageContentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.by_key = {t.key: t for t in preflight.tools(Path("/nonexistent-home"))}

    def test_cursor_block_names_install_login_and_model(self) -> None:
        block = preflight.format_block(self.by_key["cursor-agent"])
        self.assertIn("curl https://cursor.com/install -fsS | bash", block)
        self.assertIn("cursor-agent login", block)
        self.assertIn("Cursor Grok 4.6 Extra High", block)
        self.assertIn("cursor-grok-4.6-xhigh", block)

    def test_claude_block_names_install_login_and_model(self) -> None:
        block = preflight.format_block(self.by_key["claude"])
        self.assertIn("curl -fsSL https://claude.ai/install.sh | bash", block)
        self.assertIn("claude", block)
        self.assertIn("Claude Opus 5", block)
        self.assertIn("claude-opus-5", block)

    def test_codex_block_names_install_login_and_model(self) -> None:
        block = preflight.format_block(self.by_key["codex"])
        self.assertIn("https://chatgpt.com/codex/install.sh", block)
        self.assertIn("codex login", block)
        self.assertIn("gpt-5.6-sol", block)

    def test_codex_model_pin_is_env_overridable_and_named(self) -> None:
        with mock.patch.dict(
            os.environ, {"TRIPLE_STAMP_CODEX_MODEL": "gpt-5.6-sol-custom"}, clear=False
        ):
            codex = next(
                t for t in preflight.tools(Path("/nonexistent-home")) if t.key == "codex"
            )
            block = preflight.format_block(codex)
        self.assertIn("gpt-5.6-sol-custom", block)


class ReportTests(unittest.TestCase):
    def _report(self) -> tuple[int, str]:
        stream = io.StringIO()
        code = preflight.report(home=Path("/nonexistent-home"), stream=stream)
        return code, stream.getvalue()

    def test_nothing_installed_reports_all_and_fails_closed(self) -> None:
        with mock.patch.object(preflight, "is_present", return_value=False):
            code, text = self._report()
        self.assertEqual(code, preflight.EXIT_MISSING_TOOL)
        self.assertNotEqual(code, 0)  # fail closed
        for installer in (
            "curl https://cursor.com/install -fsS | bash",
            "curl -fsSL https://claude.ai/install.sh | bash",
            "curl -fsSL https://chatgpt.com/codex/install.sh | sh",
        ):
            self.assertIn(installer, text)
        self.assertIn(
            "Omnigent 0.12 or 0.14 plus Cursor Agent, Claude Code, and Codex", text
        )
        self.assertIn("./triple-stamp --self-test", text)

    def test_partial_missing_reports_only_the_missing_tool(self) -> None:
        def present(tool: "preflight.Tool") -> bool:
            return tool.key != "codex"

        with mock.patch.object(preflight, "is_present", side_effect=present):
            code, text = self._report()
        self.assertEqual(code, preflight.EXIT_MISSING_TOOL)
        self.assertIn("https://chatgpt.com/codex/install.sh", text)
        self.assertNotIn("curl https://cursor.com/install", text)
        self.assertNotIn("curl -fsSL https://claude.ai/install.sh", text)

    def test_all_present_prints_nothing_and_succeeds(self) -> None:
        with mock.patch.object(preflight, "is_present", return_value=True):
            code, text = self._report()
        self.assertEqual(code, 0)
        self.assertEqual(text, "")

    def test_messages_reference_no_databricks_internal_surface(self) -> None:
        with mock.patch.object(preflight, "is_present", return_value=False):
            _code, text = self._report()
        lowered = text.lower()
        for forbidden in (
            "databricks",
            "isaac",
            "dbcert",
            "ai-gateway",
            "model-serving",
            "proxy",
        ):
            self.assertNotIn(forbidden, lowered)


class AutomaticInstallTests(unittest.TestCase):
    def test_installer_download_uses_bounded_official_curl_contract(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            destination = Path(value) / "installer.sh"

            def run(argv, **_kwargs):
                Path(argv[-1]).write_bytes(b"x" * 100)
                return preflight.subprocess.CompletedProcess(argv, 0)

            with mock.patch.object(
                preflight.subprocess, "run", side_effect=run
            ) as invoked:
                preflight._download_installer(
                    "https://example.test/install.sh", destination
                )
        argv = invoked.call_args.args[0]
        self.assertEqual(argv[0], "/usr/bin/curl")
        self.assertIn("--fail", argv)
        self.assertIn("--retry", argv)
        self.assertEqual(argv[-2:], ["--output", str(destination)])

    def test_codex_installer_receives_private_home_and_install_dir(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            home = Path(value)
            codex = next(tool for tool in preflight.tools(home) if tool.key == "codex")
            codex = preflight.Tool(
                **{
                    **codex.__dict__,
                    "search_paths": (home / ".local/bin/codex",),
                }
            )
            observed_env: dict[str, str] = {}

            def download(_url: str, destination: Path) -> None:
                destination.write_bytes(b"x" * 100)

            def run(argv, *, env, **_kwargs):
                observed_env.update(env)
                binary = home / ".local/bin/codex"
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.write_text("#!/bin/sh\n", encoding="utf-8")
                binary.chmod(0o755)
                return preflight.subprocess.CompletedProcess(argv, 0)

            with (
                mock.patch.object(
                    preflight, "_download_installer", side_effect=download
                ),
                mock.patch.object(preflight, "_run_bounded", side_effect=run),
                mock.patch.object(preflight.shutil, "which", return_value=None),
            ):
                resolved = preflight.install_tool(codex, home, stream=io.StringIO())
            self.assertEqual(resolved, home / ".local/bin/codex")
            self.assertEqual(observed_env["HOME"], str(home))
            self.assertEqual(
                observed_env["CODEX_INSTALL_DIR"], str(home / ".local/bin")
            )

    def test_auth_status_and_login_argv_match_each_vendor(self) -> None:
        home = Path("/tmp/bootstrap-home")
        expected_status = {
            "cursor-agent": ["status"],
            "claude": ["auth", "status"],
            "codex": ["login", "status"],
        }
        expected_login = {
            "cursor-agent": ["login"],
            "claude": ["auth", "login"],
            "codex": ["login"],
        }
        for tool in preflight.tools(home):
            executable = home / ".local/bin" / tool.binary
            with self.subTest(tool=tool.key):
                with mock.patch.object(
                    preflight.subprocess,
                    "run",
                    return_value=preflight.subprocess.CompletedProcess([], 0),
                ) as status:
                    self.assertTrue(preflight.auth_ready(tool, executable, home))
                    self.assertEqual(
                        status.call_args.args[0],
                        [str(executable), *expected_status[tool.key]],
                    )
                with mock.patch.object(
                    preflight,
                    "_run_bounded",
                    return_value=preflight.subprocess.CompletedProcess([], 0),
                ) as login:
                    preflight._login(
                        tool, executable, stream=io.StringIO(), home=home
                    )
                    self.assertEqual(
                        login.call_args.args[0],
                        [str(executable), *expected_login[tool.key]],
                    )

    def test_login_timeout_is_bounded_and_actionable(self) -> None:
        with mock.patch.object(
            preflight,
            "_run_bounded",
            side_effect=preflight.subprocess.TimeoutExpired(["demo", "login"], 300),
        ):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                preflight._run_login(["demo", "login"])

    def test_bounded_command_kills_entire_process_group_on_timeout(self) -> None:
        process = mock.Mock(pid=4242)
        process.wait.side_effect = [
            preflight.subprocess.TimeoutExpired(["demo"], 1),
            137,
        ]
        with (
            mock.patch.object(preflight.subprocess, "Popen", return_value=process),
            mock.patch.object(preflight.os, "killpg") as killpg,
        ):
            with self.assertRaises(preflight.subprocess.TimeoutExpired):
                preflight._run_bounded(["demo"], timeout=1)
        killpg.assert_called_once_with(4242, preflight.signal.SIGKILL)
        self.assertEqual(process.wait.call_count, 2)

    def test_vendor_installer_timeout_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            home = Path(value)
            tool = preflight.tools(home)[1]

            def download(_url: str, destination: Path) -> None:
                destination.write_bytes(b"x" * 100)

            with (
                mock.patch.object(
                    preflight, "_download_installer", side_effect=download
                ),
                mock.patch.object(
                    preflight,
                    "_run_bounded",
                    side_effect=preflight.subprocess.TimeoutExpired(
                        ["/bin/bash", "installer"], 300
                    ),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    preflight.install_tool(tool, home, stream=io.StringIO())

    def test_installs_and_logs_in_each_missing_cli(self) -> None:
        missing = preflight.tools(Path("/tmp/bootstrap-home"))
        installed = [
            (tool, Path(f"/tmp/bootstrap-home/.local/bin/{tool.binary}"))
            for tool in missing
        ]
        stream = io.StringIO()
        with (
            mock.patch.object(preflight, "_normalize_cursor_alias"),
            mock.patch.object(
                preflight, "missing_tools", side_effect=[missing, []]
            ),
            mock.patch.object(
                preflight, "install_tool", side_effect=[path for _tool, path in installed]
            ) as install,
            mock.patch.object(
                preflight,
                "auth_ready",
                side_effect=[False, True, False, True, False, True],
            ),
            mock.patch.object(preflight, "_login") as login,
            mock.patch.object(preflight, "ensure_logins", return_value=0),
        ):
            code = preflight.install_missing(
                Path("/tmp/bootstrap-home"), stream=stream
            )
        self.assertEqual(code, 0)
        self.assertEqual(install.call_count, 3)
        self.assertEqual(login.call_count, 3)
        self.assertIn("required CLIs are installed", stream.getvalue())

    def test_install_failure_is_bounded_and_actionable(self) -> None:
        cursor = preflight.tools(Path("/tmp/bootstrap-home"))[0]
        stream = io.StringIO()
        with (
            mock.patch.object(preflight, "_normalize_cursor_alias"),
            mock.patch.object(
                preflight, "missing_tools", return_value=[cursor]
            ),
            mock.patch.object(
                preflight, "install_tool", side_effect=RuntimeError("network down")
            ),
        ):
            code = preflight.install_missing(
                Path("/tmp/bootstrap-home"), stream=stream
            )
        self.assertEqual(code, preflight.EXIT_MISSING_TOOL)
        self.assertIn("network down", stream.getvalue())

    def test_existing_unauthenticated_cli_opens_login_and_rechecks(self) -> None:
        selected = preflight.tools(Path("/tmp/bootstrap-home"))[0]
        executable = Path("/tmp/bootstrap-home/.local/bin/cursor-agent")
        with (
            mock.patch.object(preflight, "tools", return_value=[selected]),
            mock.patch.object(
                preflight, "_resolved_tool", return_value=executable
            ),
            mock.patch.object(preflight, "auth_ready", side_effect=[False, True]),
            mock.patch.object(preflight, "_login") as login,
        ):
            code = preflight.ensure_logins(Path("/tmp/bootstrap-home"))
        self.assertEqual(code, 0)
        login.assert_called_once_with(
            selected,
            executable,
            sys.stderr,
            home=Path("/tmp/bootstrap-home"),
        )

    def test_login_that_does_not_establish_authentication_fails(self) -> None:
        selected = preflight.tools(Path("/tmp/bootstrap-home"))[0]
        executable = Path("/tmp/bootstrap-home/.local/bin/cursor-agent")
        stream = io.StringIO()
        with (
            mock.patch.object(preflight, "tools", return_value=[selected]),
            mock.patch.object(
                preflight, "_resolved_tool", return_value=executable
            ),
            mock.patch.object(preflight, "auth_ready", return_value=False),
            mock.patch.object(preflight, "_login"),
        ):
            code = preflight.ensure_logins(
                Path("/tmp/bootstrap-home"), stream=stream
            )
        self.assertEqual(code, preflight.EXIT_MISSING_TOOL)
        self.assertIn("still unavailable", stream.getvalue())

    def test_external_codex_provider_counts_as_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            home = Path(value)
            config = home / ".codex/config.toml"
            config.parent.mkdir()
            config.write_text(
                'model_provider = "Databricks"\n',
                encoding="utf-8",
            )
            self.assertTrue(preflight._codex_external_auth(home))

    def test_existing_agent_binary_gets_cursor_agent_alias(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            home = Path(value)
            agent = home / ".local/bin/agent"
            agent.parent.mkdir(parents=True)
            agent.write_text("#!/bin/sh\n", encoding="utf-8")
            agent.chmod(0o755)
            with mock.patch.object(preflight.shutil, "which", return_value=None):
                preflight._normalize_cursor_alias(home)
            alias = home / ".local/bin/cursor-agent"
            self.assertTrue(alias.is_symlink())
            self.assertEqual(alias.readlink(), agent)

    def test_unrelated_agent_on_path_is_not_aliased_as_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            home = base / "home"
            unrelated = base / "bin/agent"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text("#!/bin/sh\n", encoding="utf-8")
            unrelated.chmod(0o755)
            with mock.patch.object(
                preflight.shutil, "which", return_value=str(unrelated)
            ):
                preflight._normalize_cursor_alias(home)
            self.assertFalse((home / ".local/bin/cursor-agent").exists())

    def test_installer_environment_withholds_ambient_secrets(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "openai-secret",
                "CLAUDE_CODE_OAUTH_TOKEN": "claude-secret",
                "HTTPS_PROXY": "https://proxy.example",
            },
            clear=True,
        ):
            env = preflight._subprocess_env(Path("/tmp/private-home"))
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertEqual(env["HTTPS_PROXY"], "https://proxy.example")

    def test_main_install_flag_selects_automatic_setup(self) -> None:
        with mock.patch.object(preflight, "install_missing", return_value=17) as install:
            self.assertEqual(preflight.main(["--install"]), 17)
        install.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
