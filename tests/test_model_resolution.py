from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "model_resolution_launcher_under_test",
    ROOT / ".omnigent/launcher.py",
)
assert SPEC is not None and SPEC.loader is not None
launcher = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = launcher
SPEC.loader.exec_module(launcher)


JQ = shutil.which("jq")
# Claude Code's managed apiKeyHelper on 2026-10-02, character for character.
MANAGED_ISAAC_HELPER = "jq -r '.access_token' ~/.databricks/model-serving-token.json"
UG_ARGS = "auth-token --host https://example.test --profile managed-oauth"
# Stand-ins for the real login tools. Each logs its arguments and hands back
# whatever token the test staged in the temporary HOME.
FAKE_UG = (
    'echo "$*" >> "$HOME/ug-calls"\n'
    'case " $* " in *" --force-refresh "*) exec cat "$HOME/ug-fresh" ;; esac\n'
    'exec cat "$HOME/ug-token"\n'
)
FAKE_ISAAC = (
    'echo "$*" >> "$HOME/isaac-calls"\n'
    'mkdir -p "$HOME/.databricks"\n'
    'if [ -f "$HOME/isaac-fresh" ]; then\n'
    '  cp "$HOME/isaac-fresh" "$HOME/.databricks/model-serving-token.json"\n'
    "fi\n"
)


def _jwt(minutes: float) -> str:
    claims = json.dumps({"exp": time.time() + minutes * 60}).encode()
    body = base64.urlsafe_b64encode(claims).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{body}.signature"


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _token_file(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"access_token": token}), encoding="utf-8")


def _managed(home: Path, helper: object) -> tuple[Path, ...]:
    settings = home / "managed-settings.json"
    settings.write_text(json.dumps({"apiKeyHelper": helper}), encoding="utf-8")
    return (settings,)


def _host(home: Path) -> dict[str, str]:
    jq_dir = Path(JQ).parent if JQ else Path("/usr/bin")
    return {"HOME": str(home), "PATH": f"{home}/.local/bin:{jq_dir}:/usr/bin:/bin"}


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


class ManagedLoginHelperTests(unittest.TestCase):
    """The run's gateway token comes from whatever helper Claude Code is managed to run."""

    def setUp(self) -> None:
        isaac = mock.patch.object(launcher, "ISAAC", Path("/nonexistent/isaac"))
        isaac.start()
        self.addCleanup(isaac.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name) / "Home Folder"
        self.home.mkdir()

    def mint(self, helper: object, home: Path | None = None) -> str:
        home = home or self.home
        return launcher._managed_claude_bearer(_host(home), _managed(home, helper))

    def test_any_helper_command_mints_the_run_token(self) -> None:
        """2026-10-02: the managed helper changed shape and every launch stopped."""

        token = _jwt(600)
        _script(self.home / ".local/bin/ug", FAKE_UG)
        (self.home / "ug-token").write_text(f"{token}\n", encoding="utf-8")
        _token_file(self.home / ".databricks/model-serving-token.json", token)
        _script(self.home / "My Tools/print token", f"printf '%s' '{token}'\n")
        helpers = {
            "the Isaac token file, as managed today": MANAGED_ISAAC_HELPER,
            "ug by absolute path, as managed before": (
                f"'{self.home}/.local/bin/ug' {UG_ARGS}"
            ),
            "ug through ~ with its flags reordered": (
                "~/.local/bin/ug auth-token --profile managed-oauth "
                "--host https://example.test"
            ),
            "ug found on PATH": f"ug {UG_ARGS}",
            "a quoted script path with spaces": '"$HOME/My Tools/print token"',
            "a pipeline": "cat ~/ug-token | tr -d ' '",
        }
        for label, helper in helpers.items():
            with self.subTest(label), mock.patch.object(launcher, "_eprint") as printed:
                if helper == MANAGED_ISAAC_HELPER and not JQ:
                    self.skipTest("jq is not installed")
                self.assertEqual(self.mint(helper), token)
                self.assertFalse(printed.called)

    def test_without_a_helper_the_sandboxed_round_trip_decides(self) -> None:
        settings = self.home / "managed-settings.json"
        contents = {
            "no managed settings": None,
            "malformed JSON": "{",
            "a JSON list": "[]",
            "no helper": json.dumps({"env": {"CLAUDE_CODE_USE_GATEWAY": "1"}}),
            "a blank helper": json.dumps({"apiKeyHelper": "  "}),
            "a helper that is not text": json.dumps({"apiKeyHelper": ["ug"]}),
        }
        for label, text in contents.items():
            settings.unlink(missing_ok=True)
            if text is not None:
                settings.write_text(text, encoding="utf-8")
            with self.subTest(label), mock.patch.object(launcher, "_run_quietly") as run:
                self.assertEqual(
                    launcher._managed_claude_bearer(_host(self.home), (settings,)), ""
                )
                run.assert_not_called()

    def test_a_failing_helper_names_its_reason_and_the_sign_in(self) -> None:
        _script(self.home / ".local/bin/ug", FAKE_UG)  # no token staged, so it fails
        noisy = _script(
            self.home / "noisy",
            "echo 'first line' >&2\n"
            "echo 'login expired at https://example.test/x; Bearer abc.def.ghi' >&2\n"
            "exit 3\n",
        )
        cases = {
            "ug": (f"ug {UG_ARGS}", "it exited 1: cat: "),
            "a helper that explains": (
                f"'{noisy}'",
                "it exited 3: login expired at [REDACTED_URL] Bearer [REDACTED].",
            ),
            "a helper that prints no token": ("echo null", "it printed no token."),
        }
        for label, (helper, reason) in cases.items():
            with self.subTest(label):
                with self.assertRaises(launcher.LaunchError) as raised:
                    self.mint(helper)
                message = str(raised.exception)
                self.assertEqual(raised.exception.code, launcher.EXIT_AUTH)
                self.assertIn(reason, message)
                self.assertNotIn("abc.def.ghi", message)
                self.assertTrue(
                    message.endswith(
                        "\nSign in once by running: claude"
                        "\nThen run ./triple-stamp again."
                    )
                )
        # A failed ug is never forced to refresh, which may open a browser.
        self.assertEqual(_lines(self.home / "ug-calls"), [UG_ARGS])

    @unittest.skipUnless(JQ, "jq is not installed")
    def test_an_expired_isaac_login_is_refreshed_once_without_a_browser(self) -> None:
        token = _jwt(600)
        isaac = _script(self.home / "isaac", FAKE_ISAAC)
        _token_file(self.home / "isaac-fresh", token)
        with mock.patch.object(launcher, "ISAAC", isaac):
            self.assertEqual(self.mint(MANAGED_ISAAC_HELPER), token)
            (self.home / "isaac-fresh").unlink()
            (self.home / ".databricks/model-serving-token.json").unlink()
            with self.assertRaises(launcher.LaunchError) as raised:
                self.mint(MANAGED_ISAAC_HELPER)
        self.assertEqual(_lines(self.home / "isaac-calls"), ["auth refresh"] * 2)
        self.assertEqual(raised.exception.code, launcher.EXIT_AUTH)
        self.assertIn(
            f"\nSign in once by running: {isaac} --claude\n", str(raised.exception)
        )

    def test_a_hung_helper_is_not_run_again(self) -> None:
        """A sign-in that waits on a browser must not be started a second time."""

        self.assertIsNone(launcher._run_helper("sleep 5", _host(self.home), timeout=0.2))
        isaac = _script(self.home / "isaac", FAKE_ISAAC)
        for helper in (MANAGED_ISAAC_HELPER, f"ug {UG_ARGS}"):
            with (
                self.subTest(helper),
                mock.patch.object(launcher, "ISAAC", isaac),
                mock.patch.object(launcher, "_run_quietly", return_value=None) as run,
            ):
                with self.assertRaises(launcher.LaunchError) as raised:
                    self.mint(helper)
                run.assert_called_once()
                self.assertIn("it did not finish within 30 s.", str(raised.exception))

    def test_a_nearly_expired_gateway_token_is_renewed_at_startup(self) -> None:
        """2026-09-27: a run started on a cached token 16 minutes from expiry."""

        stale, fresh, ample = _jwt(16), _jwt(60), _jwt(55)
        ug = f"~/.local/bin/ug {UG_ARGS}"
        cases = {
            "ug renewed": (ug, stale, fresh, fresh),
            "ug could not renew": (ug, stale, None, stale),
            "ug had plenty left": (ug, ample, fresh, ample),
            "Isaac renewed": (MANAGED_ISAAC_HELPER, stale, fresh, fresh),
            "Isaac could not renew": (MANAGED_ISAAC_HELPER, stale, None, stale),
        }
        for label, (helper, first, renewed, expected) in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as value:
                if helper == MANAGED_ISAAC_HELPER and not JQ:
                    self.skipTest("jq is not installed")
                home = Path(value)
                isaac = _script(home / "isaac", FAKE_ISAAC)
                _script(home / ".local/bin/ug", FAKE_UG)
                (home / "ug-token").write_text(first, encoding="utf-8")
                _token_file(home / ".databricks/model-serving-token.json", first)
                if renewed:
                    (home / "ug-fresh").write_text(renewed, encoding="utf-8")
                    _token_file(home / "isaac-fresh", renewed)
                with (
                    mock.patch.object(launcher, "ISAAC", isaac),
                    mock.patch.object(launcher, "_eprint") as printed,
                ):
                    self.assertEqual(self.mint(helper, home), expected)
                self.assertEqual(printed.called, expected == stale)
                if helper == ug:
                    renewal = [] if expected == ample else [f"{UG_ARGS} --force-refresh"]
                    self.assertEqual(_lines(home / "ug-calls"), [UG_ARGS, *renewal])
                else:
                    self.assertEqual(_lines(home / "isaac-calls"), ["auth refresh"])


class LoginRenewalTests(unittest.TestCase):
    """A live run's gateway and usage logins are renewed from the real home."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        self.home = base / "Home Folder"
        self.run = base / "run-test"
        self.run_copy = self.run / "home/.databricks/model-serving-token.json"
        self.real = self.home / ".databricks/model-serving-token.json"
        self.isaac = _script(self.home / "isaac", FAKE_ISAAC)
        for patcher in (
            mock.patch.object(launcher, "ISAAC", self.isaac),
            mock.patch.object(
                launcher, "_managed_api_key_helper", return_value=MANAGED_ISAAC_HELPER
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def renew(self) -> None:
        launcher._renew_gateway_login(self.run, self.home, _host(self.home))

    def run_token(self) -> str:
        return json.loads(self.run_copy.read_text(encoding="utf-8"))["access_token"]

    def test_a_fresher_real_login_replaces_the_runs_copy(self) -> None:
        stale, fresh = _jwt(20), _jwt(600)
        _token_file(self.run_copy, stale)
        _token_file(self.real, fresh)
        self.renew()
        self.assertEqual(self.run_token(), fresh)
        self.assertEqual(_mode(self.run_copy), 0o600)
        self.assertEqual(_lines(self.home / "isaac-calls"), [])
        self.assertIn(
            "model gateway login renewed",
            (self.run / "login-renewals.log").read_text(encoding="utf-8"),
        )

    def test_a_login_near_expiry_is_renewed_through_isaac(self) -> None:
        stale, fresh = _jwt(20), _jwt(600)
        _token_file(self.run_copy, stale)
        _token_file(self.real, stale)
        _token_file(self.home / "isaac-fresh", fresh)
        self.renew()
        self.assertEqual(_lines(self.home / "isaac-calls"), ["auth refresh"])
        self.assertEqual(self.run_token(), fresh)

    def test_a_login_with_time_left_or_no_isaac_helper_is_left_alone(self) -> None:
        ample = _jwt(600)
        _token_file(self.run_copy, ample)
        _token_file(self.real, ample)
        _token_file(self.home / "isaac-fresh", _jwt(1200))
        self.renew()
        self.assertEqual(self.run_token(), ample)
        self.assertEqual(_lines(self.home / "isaac-calls"), [])
        stale = _jwt(20)
        _token_file(self.run_copy, stale)
        _token_file(self.real, _jwt(600))
        with mock.patch.object(launcher, "_managed_api_key_helper", return_value=""):
            self.renew()
        self.assertEqual(self.run_token(), stale)

    def _usage(self, minutes_left: float) -> Path:
        usage = self.run / "usage.json"
        usage.parent.mkdir(parents=True, exist_ok=True)
        usage.write_text(
            json.dumps(
                {
                    "host": "https://usage.example.test",
                    "token": "old-usage-token-value",
                    "expires_at": time.time() + minutes_left * 60,
                    "addon": {"databricks_profile": "example-usage"},
                }
            ),
            encoding="utf-8",
        )
        _script(self.home / ".local/bin/databricks", "exit 0\n")
        return usage

    def test_the_usage_login_is_reminted_before_it_expires(self) -> None:
        usage = self._usage(5)
        fresh_expiry = time.time() + 3600
        with mock.patch.object(
            launcher,
            "_databricks_token",
            return_value=("new-usage-token-value", fresh_expiry),
        ) as mint:
            launcher._renew_usage_login(self.run, _host(self.home))
        self.assertEqual(mint.call_args.args[1], "example-usage")
        config = json.loads(usage.read_text(encoding="utf-8"))
        self.assertEqual(config["token"], "new-usage-token-value")
        self.assertEqual(config["expires_at"], fresh_expiry)
        self.assertEqual(config["host"], "https://usage.example.test")
        self.assertEqual(_mode(usage), 0o600)

    def test_a_usage_login_with_time_left_is_not_reminted(self) -> None:
        usage = self._usage(50)
        before = usage.read_bytes()
        with mock.patch.object(launcher, "_databricks_token") as mint:
            launcher._renew_usage_login(self.run, _host(self.home))
        mint.assert_not_called()
        self.assertEqual(usage.read_bytes(), before)

    def test_a_failing_renewal_never_stops_the_renewer(self) -> None:
        calls: list[str] = []

        def broken(*_args: object) -> None:
            calls.append("gateway")
            raise OSError("disk full")

        with (
            mock.patch.object(launcher, "_LOGIN_RENEW_INTERVAL_S", 0.01),
            mock.patch.object(launcher, "_renew_gateway_login", side_effect=broken),
            mock.patch.object(
                launcher,
                "_renew_usage_login",
                side_effect=lambda *_args: calls.append("usage"),
            ),
        ):
            renewer = launcher._start_login_renewer(self.run, self.home, _host(self.home))
            self.assertIsNotNone(renewer)
            deadline = time.time() + 5
            while calls.count("usage") < 2 and time.time() < deadline:
                time.sleep(0.01)
            renewer.stop()
        self.assertGreaterEqual(calls.count("gateway"), 2)
        self.assertGreaterEqual(calls.count("usage"), 2)


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


class ModelResolutionTests(unittest.TestCase):
    def test_explicit_environment_wins_for_all_three_models(self) -> None:
        environment = {
            "TRIPLE_STAMP_SUPERVISOR_MODEL": "explicit-supervisor",
            "TRIPLE_STAMP_OPUS_MODEL": "explicit-opus",
            "TRIPLE_STAMP_CODEX_MODEL": "explicit-codex",
            "CLAUDE_CODE_USE_GATEWAY": "1",
        }
        models, namespace, reason = launcher._resolve_models(
            ROOT,
            "databricks",
            environment=environment,
            managed_environment={},
        )
        self.assertEqual(
            models,
            {
                "supervisor": "explicit-supervisor",
                "opus_auditor": "explicit-opus",
                "codex_judge": "explicit-codex",
            },
        )
        self.assertEqual(namespace, "public_anthropic")
        self.assertEqual(reason, "explicit environment")

    def test_yaml_mapping_wins_over_detection(self) -> None:
        models, namespace, reason = launcher._resolve_models(
            ROOT,
            "databricks",
            environment={},
            managed_environment={},
        )
        self.assertEqual(
            models["supervisor"], "system.ai.claude-sonnet-4-6[1m]"
        )
        self.assertEqual(models["opus_auditor"], "system.ai.claude-opus-5-5[1m]")
        self.assertEqual(namespace, "databricks_gateway")
        self.assertEqual(reason, "configured mapping")

    def test_gateway_signals_select_gateway_defaults_for_plain_launchers(self) -> None:
        for environment, managed in (
            ({"CLAUDE_CODE_USE_GATEWAY": "1"}, {}),
            ({"ANTHROPIC_BASE_URL": "https://redacted/ai-gateway/anthropic"}, {}),
            ({}, {"CLAUDE_CODE_USE_GATEWAY": "1"}),
        ):
            with self.subTest(environment=environment, managed=managed):
                models, namespace, reason = launcher._resolve_models(
                    ROOT,
                    "direct",
                    environment=environment,
                    managed_environment=managed,
                )
                self.assertEqual(
                    models["supervisor"],
                    "system.ai.claude-sonnet-4-6[1m]",
                )
                self.assertEqual(
                    models["opus_auditor"], "system.ai.claude-opus-5-5[1m]"
                )
                self.assertEqual(namespace, "databricks_gateway")
                self.assertNotIn("redacted", reason)
                self.assertNotIn("ai-gateway", reason)

    def test_public_environment_selects_public_defaults(self) -> None:
        models, namespace, reason = launcher._resolve_models(
            ROOT,
            "direct",
            environment={},
            managed_environment={},
        )
        self.assertEqual(models["supervisor"], "claude-sonnet-4-6")
        self.assertEqual(models["opus_auditor"], "claude-opus-5-5")
        self.assertEqual(namespace, "public_anthropic")
        self.assertIn("no gateway routing signal", reason)

    def test_codex_has_no_namespace_detection(self) -> None:
        public = launcher._resolve_models(
            ROOT,
            "direct",
            environment={},
            managed_environment={},
        )[0]
        gateway = launcher._resolve_models(
            ROOT,
            "direct",
            environment={"CLAUDE_CODE_USE_GATEWAY": "1"},
            managed_environment={},
        )[0]
        self.assertEqual(public["codex_judge"], "gpt-5.6-sol")
        self.assertEqual(gateway["codex_judge"], "gpt-5.6-sol")

    def test_direct_launcher_accepts_gateway_namespace_models(self) -> None:
        models = launcher._resolve_models(
            ROOT,
            "direct",
            environment={"CLAUDE_CODE_USE_GATEWAY": "1"},
            managed_environment={},
        )[0]
        tools = launcher.Toolchain(
            isaac=None,
            dbcert=None,
            databricks=None,
            uv=Path("/uv"),
            omnigent=Path("/omnigent"),
            omnigent_python=Path("/python"),
            cursor_agent=Path("/cursor"),
            sandbox_exec=Path("/sandbox-exec"),
            security=Path("/security"),
            claude=Path("/claude"),
            codex=Path("/codex"),
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            runtime = launcher._runtime_env(
                root=ROOT,
                real_home=Path("/Users/unit"),
                run_dir=Path("/tmp/run"),
                bundle=Path("/tmp/run/bundle"),
                run_id="run",
                sandbox_token="sandbox",
                cursor_token="cursor",
                omnigent_token="",
                harness_tmp=Path("/tmp/h"),
                tools=tools,
                managed_python=Path("/python"),
                voice_profile_sha256="0" * 64,
                provider="direct",
                models=models,
            )
        self.assertNotIn("OMNIGENT_CLAUDE_LAUNCHER", runtime)
        self.assertEqual(
            runtime["TRIPLE_STAMP_OPUS_MODEL"],
            "system.ai.claude-opus-5-5[1m]",
        )
        self.assertEqual(
            runtime["TRIPLE_STAMP_CLAUDE_NAMESPACE"], "databricks_gateway"
        )
        explicit = {
            "TRIPLE_STAMP_SUPERVISOR_MODEL": "explicit-supervisor",
            "TRIPLE_STAMP_OPUS_MODEL": "explicit-opus",
            "TRIPLE_STAMP_CODEX_MODEL": "explicit-codex",
        }
        with mock.patch.dict(os.environ, explicit, clear=True):
            overridden = launcher._runtime_env(
                root=ROOT,
                real_home=Path("/Users/unit"),
                run_dir=Path("/tmp/run"),
                bundle=Path("/tmp/run/bundle"),
                run_id="run",
                sandbox_token="sandbox",
                cursor_token="cursor",
                omnigent_token="",
                harness_tmp=Path("/tmp/h"),
                tools=tools,
                managed_python=Path("/python"),
                voice_profile_sha256="0" * 64,
                provider="direct",
                models=models,
            )
        self.assertEqual(
            {
                name: overridden[name]
                for name in (
                    "TRIPLE_STAMP_SUPERVISOR_MODEL",
                    "TRIPLE_STAMP_OPUS_MODEL",
                    "TRIPLE_STAMP_CODEX_MODEL",
                )
            },
            explicit,
        )

    def test_direct_runtime_preserves_detected_gateway_routing_signals(
        self,
    ) -> None:
        tools = launcher.Toolchain(
            isaac=None,
            dbcert=None,
            databricks=None,
            uv=Path("/uv"),
            omnigent=Path("/omnigent"),
            omnigent_python=Path("/python"),
            cursor_agent=Path("/cursor"),
            sandbox_exec=Path("/sandbox-exec"),
            security=Path("/security"),
            claude=Path("/claude"),
            codex=Path("/codex"),
        )
        routing = {
            "ANTHROPIC_BASE_URL": "https://example/ai-gateway/anthropic",
            "CLAUDE_CODE_USE_GATEWAY": "1",
        }
        with (
            mock.patch.dict(os.environ, routing, clear=True),
            mock.patch.object(
                launcher,
                "_managed_claude_environment",
                return_value={},
            ),
        ):
            runtime = launcher._runtime_env(
                root=ROOT,
                real_home=Path("/Users/unit"),
                run_dir=Path("/tmp/run"),
                bundle=Path("/tmp/run/bundle"),
                run_id="run",
                sandbox_token="sandbox",
                cursor_token="cursor",
                omnigent_token="",
                harness_tmp=Path("/tmp/h"),
                tools=tools,
                managed_python=Path("/python"),
                voice_profile_sha256="",
                provider="direct",
            )

        self.assertEqual(runtime["ANTHROPIC_BASE_URL"], routing["ANTHROPIC_BASE_URL"])
        self.assertEqual(runtime["CLAUDE_CODE_USE_GATEWAY"], "1")
        passthrough = runtime["OMNIGENT_RUNNER_ENV_PASSTHROUGH"].split(",")
        self.assertIn("ANTHROPIC_BASE_URL", passthrough)
        self.assertIn("CLAUDE_CODE_USE_GATEWAY", passthrough)

    def test_provider_mapping_has_no_machine_local_override(self) -> None:
        payload = yaml.safe_load(
            (ROOT / ".omnigent/provider-models.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(set(payload), {"namespaces", "profiles"})
        self.assertEqual(
            set(payload["profiles"]["direct"]),
            {"codex_judge"},
        )
        self.assertEqual(
            payload["profiles"]["databricks"]["opus_auditor"],
            "system.ai.claude-opus-5-5[1m]",
        )
        text = (ROOT / ".omnigent/provider-models.yaml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("LOCAL OVERRIDE", text)
        self.assertNotIn("Revert these two lines", text)


if __name__ == "__main__":
    unittest.main()
