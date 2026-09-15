from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "auth_preflight_under_test", ROOT / ".omnigent/auth_preflight.py"
)
assert SPEC is not None and SPEC.loader is not None
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


class AuthPreflightTests(unittest.TestCase):
    def _write_token(self, home: Path, *, expires_in: int) -> None:
        path = home / ".databricks/model-serving-token.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "access_token": "not-returned",
                    "refresh_token": "not-returned",
                    "expires_at": {
                        "__datetime__": (
                            datetime.now() + timedelta(seconds=expires_in)
                        ).isoformat()
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_missing_model_token_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ, {"HOME": value}, clear=False
        ):
            self.assertEqual(preflight._model_token_seconds(), 0)

    def test_expired_model_token_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ, {"HOME": value}, clear=False
        ):
            self._write_token(Path(value), expires_in=-60)
            self.assertLessEqual(preflight._model_token_seconds(), 0)

    def test_healthy_model_token_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ, {"HOME": value}, clear=False
        ):
            self._write_token(Path(value), expires_in=7200)
            self.assertGreater(preflight._model_token_seconds(), 7000)

    def test_cursor_missing_exact_model_has_one_remediation(self) -> None:
        ok = preflight.subprocess.CompletedProcess([], 0, "logged in", "")
        missing = preflight.subprocess.CompletedProcess([], 0, "auto - Auto", "")
        with mock.patch.object(preflight, "_run", side_effect=[ok, missing, ok, ok]):
            with self.assertRaises(preflight.PreflightError) as raised:
                preflight._preflight_cursor(ROOT)
        self.assertEqual(raised.exception.stage, "Cursor")
        self.assertEqual(
            raised.exception.remediation,
            # Must match the source the code uses. Path.home() inside the
            # Seatbelt is the per-run HOME, which is the whole reason
            # _real_home() exists.
            f"{preflight._real_home() / '.local/bin/cursor-agent'} login",
        )

    def test_direct_preflight_uses_plain_binary_round_trips(self) -> None:
        claude = preflight.subprocess.CompletedProcess(
            [], 0, "DIRECT_CLAUDE_OK\n", ""
        )
        codex = preflight.subprocess.CompletedProcess(
            [], 0, "DIRECT_CODEX_OK\n", ""
        )
        with (
            mock.patch.object(preflight.shutil, "which", return_value="/plain/claude"),
            mock.patch.object(preflight.os.path, "isfile", return_value=True),
            mock.patch.object(preflight.os, "access", return_value=True),
            mock.patch.object(preflight, "_run", side_effect=[claude, codex]) as run,
            mock.patch.dict(
                os.environ,
                {"OMNIGENT_CODEX_PATH": "/bundle/.omnigent/codex-launch"},
                clear=False,
            ),
        ):
            preflight._direct_round_trip(ROOT)
        claude_argv = run.call_args_list[0].args[0]
        codex_argv = run.call_args_list[1].args[0]
        self.assertEqual(claude_argv[0], "/plain/claude")
        self.assertNotEqual(claude_argv[0], "claude code")
        self.assertIn(preflight.EXPECTED_OPUS, claude_argv)
        self.assertEqual(
            claude_argv[claude_argv.index("--effort") + 1],
            "max",
        )
        self.assertEqual(codex_argv[0], "/bundle/.omnigent/codex-launch")
        self.assertIn("--skip-git-repo-check", codex_argv)
        self.assertIn(preflight.EXPECTED_CODEX, codex_argv)
        self.assertNotIn("isaac", " ".join(codex_argv).lower())

    def test_direct_codex_model_unavailable_fails_closed(self) -> None:
        claude = preflight.subprocess.CompletedProcess(
            [], 0, "DIRECT_CLAUDE_OK\n", ""
        )
        codex = preflight.subprocess.CompletedProcess(
            [], 1, "", "model gpt-5.6-sol unavailable"
        )
        with (
            mock.patch.object(preflight.shutil, "which", return_value="/plain/claude"),
            mock.patch.object(preflight.os.path, "isfile", return_value=True),
            mock.patch.object(preflight.os, "access", return_value=True),
            mock.patch.object(preflight, "_run", side_effect=[claude, codex]),
        ):
            with self.assertRaises(preflight.PreflightError) as raised:
                preflight._direct_round_trip(ROOT)
        self.assertEqual(raised.exception.stage, "Codex")
        self.assertIn("account-dependent", raised.exception.remediation)

    def test_main_selects_only_active_provider_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            run = Path(value)
            home = run / "home"
            home.mkdir()
            common_env = {
                "TRIPLE_STAMP_ROOT": str(ROOT),
                "TRIPLE_STAMP_RUN_DIR": str(run),
                "TRIPLE_STAMP_OUTER_SANDBOX": "1",
                "KUBECONFIG": str(run / "kube/config"),
                "ISAAC_BIN": "/usr/local/bin/isaac",
            }
            for provider in ("direct", "databricks"):
                with (
                    self.subTest(provider=provider),
                    mock.patch.dict(
                        os.environ,
                        {**common_env, "TRIPLE_STAMP_PROVIDER": provider},
                        clear=False,
                    ),
                    mock.patch.object(preflight.Path, "home", return_value=home),
                    mock.patch.object(preflight, "_preflight_cursor") as cursor,
                    mock.patch.object(preflight, "_direct_round_trip") as direct,
                    mock.patch.object(preflight, "_preflight_dbcert") as dbcert,
                    mock.patch.object(preflight, "_refresh_model_token") as refresh,
                    mock.patch.object(preflight, "_preflight_supervisor_sdk") as sdk,
                    mock.patch.object(preflight, "_preflight_opus") as opus,
                    mock.patch.object(preflight, "_preflight_codex") as codex,
                ):
                    self.assertEqual(preflight.main(), 0)
                    cursor.assert_called_once_with(ROOT)
                    if provider == "direct":
                        direct.assert_called_once_with(ROOT)
                        for databricks_call in (dbcert, refresh, sdk, opus, codex):
                            databricks_call.assert_not_called()
                    else:
                        direct.assert_not_called()
                        for databricks_call in (dbcert, refresh, sdk, opus, codex):
                            databricks_call.assert_called_once()

    def test_direct_profiles_never_take_ownership_of_databricks_token_refresh(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as value:
            run = Path(value)
            home = run / "home"
            home.mkdir()
            common_env = {
                "TRIPLE_STAMP_ROOT": str(ROOT),
                "TRIPLE_STAMP_RUN_DIR": str(run),
                "TRIPLE_STAMP_OUTER_SANDBOX": "1",
                "TRIPLE_STAMP_PROVIDER": "direct",
                "KUBECONFIG": str(run / "kube/config"),
            }
            for namespace in ("public_anthropic", "databricks_gateway"):
                with (
                    self.subTest(namespace=namespace),
                    mock.patch.dict(
                        os.environ,
                        {
                            **common_env,
                            "TRIPLE_STAMP_CLAUDE_NAMESPACE": namespace,
                        },
                        clear=False,
                    ),
                    mock.patch.object(preflight.Path, "home", return_value=home),
                    mock.patch.object(
                        preflight,
                        "_model_token_seconds",
                        return_value=0,
                    ),
                    mock.patch.object(preflight, "_preflight_cursor"),
                    mock.patch.object(preflight, "_direct_round_trip") as direct,
                    mock.patch.object(preflight, "_preflight_dbcert") as dbcert,
                    mock.patch.object(
                        preflight,
                        "_refresh_model_token",
                    ) as refresh,
                    mock.patch.object(preflight, "_preflight_supervisor_sdk") as sdk,
                    mock.patch.object(preflight, "_preflight_opus") as opus,
                    mock.patch.object(preflight, "_preflight_codex") as codex,
                ):
                    self.assertEqual(preflight.main(), 0)
                    direct.assert_called_once_with(ROOT)
                    for databricks_call in (dbcert, refresh, sdk, opus, codex):
                        databricks_call.assert_not_called()

    def test_cursor_actual_startup_failure_is_not_hidden_by_config_pass(self) -> None:
        ok = preflight.subprocess.CompletedProcess([], 0, "logged in", "")
        models = preflight.subprocess.CompletedProcess(
            [], 0, preflight.EXPECTED_CURSOR, ""
        )
        failed = preflight.subprocess.CompletedProcess(
            [], 77, "", "Cursor process launch failed: EMFILE"
        )
        with mock.patch.object(
            preflight, "_run", side_effect=[ok, models, ok, failed]
        ):
            with self.assertRaises(preflight.PreflightError) as raised:
                preflight._preflight_cursor(ROOT)
        self.assertEqual(raised.exception.stage, "Cursor")
        self.assertIn("EMFILE", raised.exception.detail)

    def test_failed_silent_refresh_is_detected_despite_zero_exit(self) -> None:
        result = preflight.subprocess.CompletedProcess([], 0, "", "")
        with (
            mock.patch.object(preflight, "_run", return_value=result),
            mock.patch.object(preflight, "_model_token_seconds", return_value=0),
        ):
            with self.assertRaises(preflight.PreflightError) as raised:
                preflight._refresh_model_token("/usr/local/bin/isaac")
        self.assertEqual(raised.exception.stage, "Opus")
        self.assertEqual(raised.exception.remediation, "/usr/local/bin/isaac --claude")

    def test_opus_selector_preflight_requires_supported_startup_controls(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {
                    "DISABLE_AUTOUPDATER": "",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                    "DISABLE_TELEMETRY": "1",
                },
                clear=False,
            ),
            self.assertRaises(preflight.PreflightError) as raised,
        ):
            preflight._preflight_opus("/usr/local/bin/isaac")
        self.assertEqual(raised.exception.stage, "Opus")
        self.assertIn("DISABLE_AUTOUPDATER", raised.exception.detail)

    def test_supervisor_sdk_requires_exact_isaac_gateway(self) -> None:
        incomplete = {
            "HARNESS_CLAUDE_SDK_GATEWAY": "true",
            "HARNESS_CLAUDE_SDK_MODEL": preflight.EXPECTED_SUPERVISOR,
        }
        with (
            mock.patch.dict(
                os.environ,
                {"TRIPLE_STAMP_BUNDLE": str(ROOT)},
                clear=False,
            ),
            mock.patch(
                "omnigent.runtime.workflow._build_claude_sdk_spawn_env",
                return_value=incomplete,
            ),
            mock.patch("omnigent.spec.parser.parse", return_value=object()),
        ):
            with self.assertRaises(preflight.PreflightError) as raised:
                preflight._preflight_supervisor_sdk()
        self.assertEqual(raised.exception.stage, "Supervisor")
        self.assertEqual(raised.exception.remediation, "/usr/local/bin/isaac --claude")

    def test_supervisor_sdk_executes_token_helper_without_printing_token(self) -> None:
        resolved = {
            "HARNESS_CLAUDE_SDK_GATEWAY": "true",
            "HARNESS_CLAUDE_SDK_MODEL": preflight.EXPECTED_SUPERVISOR,
            "HARNESS_CLAUDE_SDK_PERMISSION_MODE": "auto",
            "HARNESS_CLAUDE_SDK_SKILLS_FILTER": '"none"',
            "HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL": (
                "https://example.test/ai-gateway/anthropic"
            ),
            "HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND": "token-helper",
        }
        token = preflight.subprocess.CompletedProcess([], 0, "x" * 40, "")
        with (
            mock.patch.dict(
                os.environ,
                {"TRIPLE_STAMP_BUNDLE": str(ROOT)},
                clear=False,
            ),
            mock.patch(
                "omnigent.runtime.workflow._build_claude_sdk_spawn_env",
                return_value=resolved,
            ),
            mock.patch("omnigent.spec.parser.parse", return_value=object()),
            mock.patch.object(preflight, "_run", return_value=token) as run,
        ):
            preflight._preflight_supervisor_sdk()
        run.assert_called_once_with(["/bin/sh", "-c", "token-helper"], timeout=30)

    def test_missing_codex_provider_has_exact_remediation(self) -> None:
        with mock.patch.object(preflight, "_codex_config", return_value=([], "")):
            with self.assertRaises(preflight.PreflightError) as raised:
                preflight._preflight_codex(ROOT)
        self.assertEqual(raised.exception.stage, "Codex")
        self.assertEqual(
            raised.exception.remediation,
            "/usr/local/bin/isaac codex --no-omni",
        )

    def test_expired_dbcert_after_refresh_fails_closed(self) -> None:
        refreshed = preflight.subprocess.CompletedProcess([], 0, "", "")
        with (
            mock.patch.object(preflight, "_dbcert_status", side_effect=[0, 0]),
            mock.patch.object(preflight.subprocess, "run", return_value=refreshed),
        ):
            with self.assertRaises(preflight.PreflightError) as raised:
                preflight._preflight_dbcert()
        self.assertEqual(raised.exception.stage, "Opus")
        self.assertEqual(
            raised.exception.remediation,
            "/usr/local/bin/dbcert --force --update-kubeconfig=false",
        )

    def test_secret_sanitizer_handles_authorization_bearer(self) -> None:
        text = preflight._sanitize(
            "Authorization: Bearer abc123 access_token=def456 password: ghi789"
        )
        for secret in ("abc123", "def456", "ghi789"):
            self.assertNotIn(secret, text)

    def test_secret_sanitizer_redacts_anthropic_base_url(self) -> None:
        text = preflight._sanitize(
            "ANTHROPIC_BASE_URL=https://secret.example/ai-gateway/anthropic"
        )
        self.assertNotIn("secret.example", text)
        self.assertIn("ANTHROPIC_BASE_URL=[REDACTED]", text)

    def test_gateway_namespace_mismatch_does_not_recommend_reauthentication(
        self,
    ) -> None:
        with mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_CLAUDE_NAMESPACE": "databricks_gateway"},
            clear=False,
        ):
            error = preflight._claude_model_failure(
                stage="Claude",
                model="claude-opus-5",
                detail="model may not exist",
                default_remediation="authenticate Claude Code",
            )
        self.assertIn("attempted 'claude-opus-5'", error.detail)
        self.assertIn("databricks_gateway", error.detail)
        self.assertIn("system.ai.claude-opus-5[1m]", error.remediation)
        self.assertIn("reauthentication will not fix", error.remediation)

    def test_matching_gateway_namespace_retains_access_remediation(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_CLAUDE_NAMESPACE": "databricks_gateway"},
            clear=False,
        ):
            error = preflight._claude_model_failure(
                stage="Claude",
                model="system.ai.claude-opus-5[1m]",
                detail="access denied",
                default_remediation="verify gateway authentication and access",
            )
        self.assertEqual(
            error.remediation, "verify gateway authentication and access"
        )
        self.assertIn("system.ai.claude-opus-5[1m]", error.detail)


if __name__ == "__main__":
    unittest.main()
