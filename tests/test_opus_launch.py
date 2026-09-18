from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / ".omnigent/isaac-launcher/triple_stamp_opus_mcp.py"


def _load_opus_module(provider: str):
    spec = importlib.util.spec_from_file_location(
        f"triple_stamp_opus_mcp_{provider}_under_test",
        MODULE_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    model = (
        "system.ai.claude-opus-5[1m]"
        if provider == "databricks"
        else "claude-opus-5"
    )
    with mock.patch.dict(
        os.environ,
        {
            "TRIPLE_STAMP_PROVIDER": provider,
            "TRIPLE_STAMP_OPUS_MODEL": model,
        },
        clear=False,
    ):
        spec.loader.exec_module(module)
    return module


mcp = _load_opus_module("direct")

LAUNCHER_PATH = (
    ROOT / ".omnigent/isaac-launcher/triple_stamp_isaac_launcher.py"
)


def _entry(name: str) -> dict[str, object]:
    return {
        "type": "stdio",
        "command": "dbexec",
        "args": ["repo", "run", "mcp", "start-single", name],
        "env": {"DBEXEC_NO_CERT_REFRESH": "1"},
    }


class OpusLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.run_dir = self.root / "run"
        (self.run_dir / "tmp").mkdir(parents=True)
        config = {
            "mcpServers": {
                name: _entry(name) for name in mcp.OPUS_MCP_NAMES
            }
        }
        (self.home / ".claude.json").write_text(
            json.dumps(config), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def import_launcher(self):
        spec = importlib.util.spec_from_file_location(
            "triple_stamp_isaac_launcher_under_test", LAUNCHER_PATH
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        with mock.patch.dict(
            sys.modules,
            {
                "triple_stamp_opus_mcp": mcp,
            },
        ):
            spec.loader.exec_module(module)
        return module

    def test_definitions_and_read_only_tools_remain_without_health_gate(self) -> None:
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                MODULE_PATH,
                LAUNCHER_PATH,
                ROOT / ".omnigent/launcher.py",
                ROOT / ".omnigent/auth_preflight.py",
            )
        )
        for removed in (
            "REQUIRED_HEALTH_SERVERS",
            "MCP_ATTEMPT_SECONDS",
            "ATTESTATION_TTL_SECONDS",
            "def validate_attestation(",
            "def probe_server(",
            "auth_probe",
            "run_opus_attestation_refresher",
            "_start_opus_attestation_refresher",
            "TRIPLE_STAMP_OPUS_PREFLIGHT_MODE",
            "opus-mcp-attestation.json",
            "opus-attestation.key",
            "probe_opus_terminal_readiness",
            "wait_for_opus_catalog_registration",
            "inspect_opus_mcp_catalog",
            "OPUS_READINESS_TIMEOUT_SECONDS",
            "OPUS_CATALOG_WAIT_SECONDS",
        ):
            self.assertNotIn(removed, source)
        selected = mcp.load_generated_mcp_config(self.home)
        self.assertEqual(
            set(selected["mcpServers"]), set(mcp.OPUS_MCP_NAMES)
        )
        self.assertEqual(
            mcp.READ_ONLY_ALLOWED_TOOLS,
            (
                "ToolSearch",
                "mcp__glean__glean_chat",
                "mcp__confluence__get_confluence_page_comments",
                "mcp__confluence__list_confluence_page_versions",
            ),
        )
        self.assertFalse(
            set(mcp.READ_ONLY_ALLOWED_TOOLS).intersection(
                mcp.WRITE_TOOLS_DENIED
            )
        )
        self.assertIn(
            "mcp__slack__slack_write_api_call",
            mcp.WRITE_TOOLS_DENIED,
        )
        self.assertIn(
            "mcp__confluence__reply_to_confluence_comment",
            mcp.WRITE_TOOLS_DENIED,
        )
        self.assertIn(
            "mcp__safe__safe_merge_api_call",
            mcp.WRITE_TOOLS_DENIED,
        )

    def test_startup_environment_is_explicit_for_both_profiles(self) -> None:
        direct = _load_opus_module("direct")
        databricks = _load_opus_module("databricks")
        self.assertEqual(direct.OPUS_MODEL, "claude-opus-5")
        self.assertEqual(
            databricks.OPUS_MODEL,
            "system.ai.claude-opus-5[1m]",
        )
        isaac_settings = {
            "ISAAC_DEFAULT_UCODE": "0",
            "ISAAC_DISABLE_MAC_MANAGED_SETTINGS_UPDATE": "1",
            "ISAAC_LAUNCH_MODE": "omni",
        }
        self.assertFalse(set(direct.OPUS_STARTUP_ENV) & set(isaac_settings))
        self.assertEqual(
            {
                name: databricks.OPUS_STARTUP_ENV[name]
                for name in isaac_settings
            },
            isaac_settings,
        )

    def test_materialization_is_local_and_immutable(self) -> None:
        selected = mcp.load_generated_mcp_config(self.home)
        path = mcp.materialize_run_config(self.run_dir, selected)
        self.assertEqual(path.name, "opus-mcp.json")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o400)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), selected)

    def test_absent_internal_family_becomes_audit_gap_not_launch_failure(
        self,
    ) -> None:
        config_path = self.home / ".claude.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        del config["mcpServers"]["safe"]
        config_path.write_text(json.dumps(config), encoding="utf-8")

        selected = mcp.load_generated_mcp_config(self.home)

        self.assertEqual(
            set(selected["mcpServers"]),
            set(mcp.OPUS_MCP_NAMES) - {"safe"},
        )

    def test_absent_mcp_catalog_is_an_empty_optional_catalog(self) -> None:
        config_path = self.home / ".claude.json"
        config_path.unlink()
        self.assertEqual(
            mcp.load_generated_mcp_config(self.home),
            {"mcpServers": {}},
        )
        config_path.write_text("{}", encoding="utf-8")
        self.assertEqual(
            mcp.load_generated_mcp_config(self.home),
            {"mcpServers": {}},
        )

    def test_malformed_mcp_catalog_still_fails_closed(self) -> None:
        (self.home / ".claude.json").write_text(
            json.dumps({"mcpServers": []}),
            encoding="utf-8",
        )
        with self.assertRaises(mcp.OpusConfigurationError):
            mcp.load_generated_mcp_config(self.home)

    def test_unavailable_mcp_server_does_not_block_opus_launch(self) -> None:
        launcher = self.import_launcher()
        selected = mcp.load_generated_mcp_config(self.home)
        config_path = mcp.materialize_run_config(self.run_dir, selected)
        env = {
            "TRIPLE_STAMP_RUN_ID": "test-run",
            "TRIPLE_STAMP_RUN_DIR": str(self.run_dir),
            "ISAAC_BIN": sys.executable,
        }
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(
                launcher,
                "prepare_opus_mcp_config",
                return_value=config_path,
            ),
            mock.patch.object(
                launcher.os,
                "access",
                return_value=True,
            ),
        ):
            command, args = launcher.IsaacClaudeLauncher().launch(
                "claude",
                [
                    "--model",
                    launcher._OPUS_MODEL,
                    "--effort",
                    "max",
                ],
            )
        self.assertEqual(command, sys.executable)
        self.assertEqual(args[args.index("--tools") + 1], "ToolSearch")
        self.assertNotEqual(args[args.index("--tools") + 1], "")
        self.assertEqual(args[args.index("--setting-sources") + 1], "")
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(
            args[args.index("--mcp-config") + 1], str(config_path)
        )
        self.assertEqual(
            args[args.index("--allowedTools") + 1],
            ",".join(launcher._OPUS_ALLOWED_TOOLS),
        )
        allowed = args[args.index("--allowedTools") + 1].split(",")
        self.assertIn("ToolSearch", allowed)
        denied = args[args.index("--disallowedTools") + 1]
        self.assertIn("mcp__jira__jira_write_api_call", denied)
        self.assertIn("mcp__confluence__create_confluence_page", denied)
        self.assertIn("mcp__safe__safe_write_api_call", denied)

    def test_current_failure_fixture_proves_stale_terminal_resume(self) -> None:
        fixture = json.loads(
            (
                ROOT
                / "tests/fixtures/current-browser-opus-failure-20260910.json"
            ).read_text(encoding="utf-8")
        )
        failure = fixture["failed_continuation"]
        self.assertTrue(failure["same_child_session_reused"])
        self.assertTrue(failure["same_terminal_resource_reused"])
        self.assertFalse(failure["message_delivered"])
        self.assertFalse(failure["new_transcript_user_message_created"])
        self.assertEqual(failure["polls"], 181)
        self.assertIn("stale lifecycle", failure["root_cause"])

    def test_supported_startup_suppression_remains(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            configured = mcp.configure_opus_startup_environment()
        self.assertEqual(configured["DISABLE_AUTOUPDATER"], "1")
        self.assertEqual(
            configured["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"], "1"
        )
        self.assertEqual(configured["DISABLE_TELEMETRY"], "1")
        self.assertEqual(configured["DBEXEC_NO_CERT_REFRESH"], "1")


if __name__ == "__main__":
    unittest.main()
