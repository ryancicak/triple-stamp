from __future__ import annotations

import importlib.util
import os
import sys
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
        self.assertEqual(models["opus_auditor"], "system.ai.claude-opus-5[1m]")
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
                    models["opus_auditor"], "system.ai.claude-opus-5[1m]"
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
        self.assertEqual(models["opus_auditor"], "claude-opus-5")
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
            "system.ai.claude-opus-5[1m]",
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
            "system.ai.claude-opus-5[1m]",
        )
        text = (ROOT / ".omnigent/provider-models.yaml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("LOCAL OVERRIDE", text)
        self.assertNotIn("Revert these two lines", text)


if __name__ == "__main__":
    unittest.main()
