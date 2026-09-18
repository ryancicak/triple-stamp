from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml
from omnigent.runner import app as runner_app
from omnigent.runner import tool_dispatch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PYTHON = ROOT / ".omnigent/runtime-python"
ISAAC_LAUNCHER = ROOT / ".omnigent/isaac-launcher"
for import_path in (RUNTIME_PYTHON, ISAAC_LAUNCHER):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))
runtime_state_spec = importlib.util.spec_from_file_location(
    "triple_stamp_runtime_state",
    ISAAC_LAUNCHER / "triple_stamp_runtime_state.py",
)
assert runtime_state_spec is not None and runtime_state_spec.loader is not None
runtime_state = importlib.util.module_from_spec(runtime_state_spec)
sys.modules["triple_stamp_runtime_state"] = runtime_state
runtime_state_spec.loader.exec_module(runtime_state)


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


with mock.patch.dict(
    os.environ,
    {
        "TRIPLE_STAMP_PROVIDER": "direct",
        "TRIPLE_STAMP_SUPERVISOR_MODEL": "claude-sonnet-4-6",
        "TRIPLE_STAMP_OPUS_MODEL": "claude-opus-5",
        "TRIPLE_STAMP_CODEX_MODEL": "gpt-5.6-sol",
    },
    clear=False,
):
    plugin = _module(
        "triple_stamp_launcher_under_test",
        ROOT / ".omnigent/isaac-launcher/triple_stamp_isaac_launcher.py",
    )
launcher = _module(
    "triple_stamp_outer_launcher_under_test",
    ROOT / ".omnigent/launcher.py",
)
runtime_guard = _module(
    "triple_stamp_runtime_guard_under_test",
    ROOT / ".omnigent/runtime-python/sitecustomize.py",
)
supervisor_runtime = _module(
    "triple_stamp_supervisor_runtime_under_test",
    ROOT / ".omnigent/runtime-python/triple_stamp_supervisor_runtime.py",
)
cursor_lifecycle = _module(
    "triple_stamp_cursor_lifecycle_under_test",
    ROOT / ".omnigent/runtime-python/triple_stamp_cursor_lifecycle.py",
)


def _record(
    agent: str,
    output: str,
    *,
    status: str = "completed",
    title: str = "cycle-1",
) -> dict[str, str]:
    return {
        "id": "child",
        "status": status,
        "agent": agent,
        "title": title,
        "output": output,
    }


def _web_hunt() -> dict[str, str]:
    return {
        "claim": "verify claim",
        "query": "fetch the current primary source",
        "where": "official documentation",
        "break_how": "compare the source with the claim",
        "kill_condition": "the source contradicts the claim",
        "prove_condition": "the source states the claim",
    }


def _audit(verdict: str = "PASS", *, attack: str = "source checked") -> str:
    coverage = {
        system: {
            "system": system,
            "status": "unavailable_after_retry",
            "routes": [],
            "tools_called": [],
            "queries": [
                f"select:mcp__{system}__read",
                f"select:mcp__{system}__read",
            ],
            "results_seen": 0,
            "evidence_refs": [],
            "note": "two genuine ToolSearch attempts were unavailable",
        }
        for system in ("glean", "jira", "slack", "confluence", "safe")
    }
    return json.dumps(
        {
            "verdict": verdict,
            "needs_web": verdict == "NEEDS_WEB",
            "web_queries": [_web_hunt()] if verdict == "NEEDS_WEB" else [],
            "attacks": [attack],
            "must_retest": [],
            "acceptable_as_is": verdict in {"PASS", "PASS_WITH_GAPS"},
            "punch_list_for_cursor": [],
            "internal_sources_consulted": [],
            "internal_sources_not_required_reason": "public-only test fixture",
            "internal_coverage": coverage,
        }
    )


def _judgment(verdict: str = "REWORK") -> str:
    payload: dict[str, object] = {
        "verdict": verdict,
        "needs_web": verdict == "NEEDS_WEB",
        "needs_internal": verdict == "NEEDS_INTERNAL",
        "why": "bounded test judgment",
        "best_supported_answer": (
            "The completed evidence supports this fixture's customer answer."
        ),
        "gap_materiality": "material",
        "limitations": [],
    }
    if verdict == "REWORK":
        payload["punch_list_for_cursor"] = [
            {
                "gap_type": "stage1_evidence",
                "claim": "fix evidence",
                "required_capability": "cursor_stage1",
                "required_source": "stage1_packet",
                "requested_proof": "supply checkable Stage 1 evidence",
            }
        ]
    elif verdict == "NEEDS_WEB":
        payload["web_queries"] = [{"claim": "verify claim"}]
    elif verdict == "NEEDS_INTERNAL":
        payload["internal_queries"] = [
            {
                "claim": "verify internal claim",
                "exact_query_or_tool_call": "select:mcp__glean__search",
                "which_system": "glean",
                "where_to_look": "Glean internal index",
                "how_to_break_it": "find a contradictory primary source",
                "kill_condition": "primary source contradicts the claim",
                "prove_condition": "two current primary sources agree",
            }
        ]
    return json.dumps(payload)


def _route_packet(
    agent: str,
    title: str,
    output: str,
    *,
    child: str = "",
) -> dict[str, str]:
    return {
        "agent": agent,
        "title": title,
        "status": "completed",
        "output": output,
        "child_session_id": child or f"child-{title}",
        "work_id": f"work-{title}",
    }


# Voice rendering is optional, so tests that exercise the enabled mode declare
# the profile explicitly. The example profile is checked in so the suite never
# depends on a personal absolute path.
_VOICE_PROFILE_PATH = str(ROOT / "tests/fixtures/example-voice-profile.md")


def _voice_env(digest: str) -> dict[str, str]:
    """Environment for a run with voice rendering enabled."""

    return {
        "TRIPLE_STAMP_VOICE_PROFILE": _VOICE_PROFILE_PATH,
        "TRIPLE_STAMP_VOICE_PROFILE": _VOICE_PROFILE_PATH,
        "TRIPLE_STAMP_VOICE_PROFILE_SHA256": digest,
    }


def _opus_effort_fixture(
    efforts: list[str | None],
    *,
    title: str = "audit-cycle-1",
    provider: str = "direct",
    row_session_id: str = "claude-opus-session",
    missing_file: bool = False,
    unreadable: bool = False,
    outside_transcript_root: bool = False,
) -> dict[str, object]:
    """Build one run-scoped raw Claude transcript and observe it."""

    child_session_id = "omnigent-opus-child"
    claude_session_id = "claude-opus-session"
    with tempfile.TemporaryDirectory() as value:
        run_dir = Path(value)
        transcript = (
            run_dir / "foreign-transcripts" / f"{claude_session_id}.jsonl"
            if outside_transcript_root
            else run_dir
            / "home/.claude/projects/-Users-unit-workspace"
            / f"{claude_session_id}.jsonl"
        )
        transcript.parent.mkdir(parents=True)
        if unreadable:
            transcript.mkdir()
        elif not missing_file:
            rows: list[dict[str, object]] = [
                {
                    "type": "user",
                    "sessionId": claude_session_id,
                    "message": {"role": "user", "content": "audit"},
                }
            ]
            for effort in efforts:
                row: dict[str, object] = {
                    "type": "assistant",
                    "session_id": row_session_id,
                    "message": {"role": "assistant", "content": []},
                }
                if effort is not None:
                    row["effort"] = effort
                rows.append(row)
            transcript.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n",
                encoding="utf-8",
            )
        with (
            mock.patch.dict(
                os.environ,
                {
                    "TRIPLE_STAMP_RUN_DIR": str(run_dir),
                    "TRIPLE_STAMP_PROVIDER": provider,
                },
                clear=False,
            ),
            mock.patch(
                "omnigent.claude_native_bridge.bridge_dir_for_conversation_id",
                return_value=run_dir / "bridge",
            ),
            mock.patch(
                "omnigent.claude_native_bridge.read_claude_session_id",
                return_value=claude_session_id,
            ),
            mock.patch(
                "omnigent.claude_native_bridge.read_transcript_path",
                return_value=transcript,
            ),
        ):
            return runtime_state.add_opus_effort_observation(
                {
                    "child_session_id": child_session_id,
                    "agent": "opus_auditor",
                    "title": title,
                    "status": "completed",
                    "output": _audit(),
                }
            )


class OrchestrationContractTests(unittest.TestCase):
    @staticmethod
    def _plugin_status(
        *,
        ok: bool,
        owned: int,
        unrelated: int = 0,
        import_error: str | None = None,
    ) -> dict[str, object]:
        return {
            "ok": ok,
            "match_count": owned + unrelated,
            "owned_count": owned,
            "unrelated_count": unrelated,
            "entries": [],
            "module_file": "/expected/plugin.py" if ok else None,
            "import_error": import_error,
            "load_error": None,
        }

    @staticmethod
    def _fake_toolchain() -> object:
        path = Path("/nonexistent")
        return launcher.Toolchain(
            isaac=path,
            dbcert=path,
            databricks=path,
            uv=path,
            omnigent=path,
            omnigent_python=path,
            cursor_agent=path,
            sandbox_exec=path,
            security=path,
        )

    def test_profile_pins_models_rates_and_workspace_markers(self) -> None:
        validator = _module(
            "triple_stamp_validator_model_test",
            ROOT / ".omnigent/validate_bundle.py",
        )
        for provider, supervisor, opus in (
            ("direct", "claude-sonnet-4-6", "claude-opus-5"),
            (
                "databricks",
                "system.ai.claude-sonnet-4-6[1m]",
                "system.ai.claude-opus-5[1m]",
            ),
        ):
            with self.subTest(provider=provider):
                expected = validator._expected_models(provider)
                self.assertEqual(
                    expected["triple-stamp"],
                    (supervisor, "low", "claude-sdk"),
                )
                self.assertEqual(
                    expected["cursor_workhorse"],
                    ("cursor-grok-4.6-xhigh", None, "cursor-native"),
                )
                self.assertEqual(
                    expected["opus_auditor"],
                    (opus, "max", "claude-native"),
                )
                self.assertEqual(
                    expected["codex_judge"],
                    ("gpt-5.6-sol", "ultra", "codex-native"),
                )
        cursor_rate = plugin._model_rates("cursor-grok-4.6-xhigh")
        self.assertEqual(cursor_rate, plugin._COST_CEILINGS["cursor"])
        self.assertNotEqual(cursor_rate, plugin._COST_CEILINGS["unknown"])
        for model, ceiling in (
            ("gpt-5.6-sol", "gpt-5.6-sol"),
            ("system.ai.claude-opus-5[1m]", "opus"),
            ("unrecognized", "unknown"),
        ):
            self.assertEqual(
                plugin._model_rates(model),
                plugin._COST_CEILINGS[ceiling],
            )

        for relative in (
            ".omnigent/cursor_via_login.py",
            "agents/cursor_workhorse/config.yaml",
        ):
            self.assertNotIn(
                "gpt-5.6-sol-xhigh",
                (ROOT / relative).read_text(encoding="utf-8"),
            )
        worker_rule = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        for marker in (
            "apply only when this Cursor session is the Triple-stamp Stage 1",
            "`cursor_workhorse` runtime",
            "Human-directed repository analysis",
            "follow the user's requested model",
            "gpt-5.6-sol-xhigh",
            "GPT-5.6 Sol Extra High",
            "Never invoke a Skill, workflow, Subagent, Task, nested agent",
            "Emit at most one tool call per assistant turn",
            "Return the complete evidence packet inline",
            "For this Stage 1 `cursor_workhorse` runtime only",
            "cursor-grok-4.6-xhigh",
        ):
            self.assertIn(marker, worker_rule)
        cursor_config = yaml.safe_load(
            (ROOT / "agents/cursor_workhorse/config.yaml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            cursor_config["executor"]["model"],
            "cursor-grok-4.6-xhigh",
        )

    def test_plugin_clean_install_and_repeated_noop(self) -> None:
        bad = self._plugin_status(ok=False, owned=0)
        good = self._plugin_status(ok=True, owned=1)
        completed = subprocess.CompletedProcess([], 0, "", "")
        failed = subprocess.CompletedProcess([], 1, "", "")
        with tempfile.TemporaryDirectory() as value, mock.patch.object(
            launcher,
            "_plugin_probe",
            side_effect=[(bad, failed), (good, completed)],
        ), mock.patch.object(launcher, "_install_plugin") as install, mock.patch.object(
            launcher, "_eprint"
        ):
            launcher._ensure_plugin(
                Path(value), self._fake_toolchain(), {}
            )
            install.assert_called_once()
            self.assertFalse(install.call_args.kwargs["reinstall"])
        with tempfile.TemporaryDirectory() as value, mock.patch.object(
            launcher, "_plugin_probe", return_value=(good, completed)
        ), mock.patch.object(launcher, "_install_plugin") as install:
            launcher._ensure_plugin(Path(value), self._fake_toolchain(), {})
            install.assert_not_called()

    def test_plugin_repairs_interrupted_and_stale_install(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        failed = subprocess.CompletedProcess([], 1, "", "")
        good = self._plugin_status(ok=True, owned=1)
        for bad, reinstall in (
            (
                self._plugin_status(
                    ok=False,
                    owned=0,
                    import_error="ModuleNotFoundError: interrupted install",
                ),
                False,
            ),
            (self._plugin_status(ok=False, owned=1), True),
        ):
            with tempfile.TemporaryDirectory() as value, mock.patch.object(
                launcher,
                "_plugin_probe",
                side_effect=[(bad, failed), (good, completed)],
            ), mock.patch.object(
                launcher, "_install_plugin"
            ) as install, mock.patch.object(launcher, "_eprint"):
                launcher._ensure_plugin(Path(value), self._fake_toolchain(), {})
                self.assertEqual(install.call_args.kwargs["reinstall"], reinstall)

    def test_plugin_repairs_duplicate_owned_metadata_only(self) -> None:
        duplicate = self._plugin_status(ok=False, owned=2)
        good = self._plugin_status(ok=True, owned=1)
        completed = subprocess.CompletedProcess([], 0, "", "")
        failed = subprocess.CompletedProcess([], 1, "", "")
        with tempfile.TemporaryDirectory() as value, mock.patch.object(
            launcher,
            "_plugin_probe",
            side_effect=[
                (duplicate, failed),
                (duplicate, failed),
                (good, completed),
            ],
        ), mock.patch.object(launcher, "_install_plugin") as install, mock.patch.object(
            launcher, "_run", return_value=completed
        ) as run, mock.patch.object(launcher, "_eprint"):
            launcher._ensure_plugin(Path(value), self._fake_toolchain(), {})
            self.assertEqual(install.call_count, 2)
            uninstall_args = run.call_args.args[0]
            self.assertIn("uninstall", uninstall_args)
            self.assertEqual(uninstall_args[-1], launcher.PLUGIN_DISTRIBUTION)

    def test_plugin_preserves_unrelated_registration(self) -> None:
        conflict = self._plugin_status(ok=False, owned=0, unrelated=1)
        failed = subprocess.CompletedProcess([], 1, "", "")
        with tempfile.TemporaryDirectory() as value, mock.patch.object(
            launcher, "_plugin_probe", return_value=(conflict, failed)
        ), mock.patch.object(launcher, "_install_plugin") as install:
            with self.assertRaises(launcher.LaunchError) as error:
                launcher._ensure_plugin(Path(value), self._fake_toolchain(), {})
            self.assertEqual(error.exception.code, 127)
            self.assertIn("unrelated package", str(error.exception))
            install.assert_not_called()

    def test_plugin_install_lock_serializes_threads(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            active = 0
            maximum = 0
            state_lock = threading.Lock()

            def critical_section() -> None:
                nonlocal active, maximum
                with launcher._PluginInstallLock(root):
                    with state_lock:
                        active += 1
                        maximum = max(maximum, active)
                    time.sleep(0.02)
                    with state_lock:
                        active -= 1

            threads = [threading.Thread(target=critical_section) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(maximum, 1)

    def test_supervisor_is_sdk_with_one_shot_inbox(self) -> None:
        config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(config["executor"]["config"]["harness"], "claude-sdk")
        self.assertEqual(config["executor"]["config"]["permission_mode"], "auto")
        self.assertEqual(config["executor"]["model"], "claude-sonnet-4-6")
        self.assertNotIn("auth", config["executor"])
        self.assertEqual(config["skills"], "none")
        self.assertTrue(config["async"])
        prompt = config["prompt"]
        for obsolete in (
            "WAITING_FOR_SUBAGENT",
            "await_cursor_startup",
            "sys_session_get_info",
            "sys_session_get_history",
        ):
            self.assertNotIn(obsolete, prompt)
        self.assertIn("runtime exposes only those two MCP tools", prompt)
        self.assertIn(
            "routing authorization never parses or compares handoff prose",
            prompt,
        )
        self.assertIn("At most two web hops for each", prompt)

    def test_final_response_policy_gates_only_second_paid_retry(self) -> None:
        contract = plugin.supervisor_contract(enabled=True)
        calls = (
            {
                "type": "tool_call",
                "data": {
                    "name": "sys_session_send",
                    "arguments": {
                        "agent": "not-a-declared-agent",
                        "title": "anything",
                        "args": {"input": "arbitrary handoff"},
                    },
                },
            },
            {
                "type": "tool_call",
                "data": {"name": "sys_read_inbox", "arguments": {"unexpected": True}},
            },
            {
                "type": "tool_call",
                "data": {"name": "WebSearch", "arguments": {"query": "not exposed"}},
            },
        )
        for call in calls:
            with self.subTest(call=call["data"]["name"]):
                self.assertEqual(contract(call)["result"], "ALLOW")
        with mock.patch.object(
            plugin,
            "append_supervisor_tool_call",
            side_effect=OSError("ledger unavailable"),
        ):
            self.assertEqual(contract(calls[0])["result"], "ALLOW")
        first_retry = {
            "type": "tool_call",
            "data": {
                "name": "mcp__omnigent__sys_session_send",
                "arguments": {
                    "agent": "opus_auditor",
                    "title": "audit-retry-1-1",
                    "args": {"input": "fresh retry"},
                },
            },
        }
        second_retry = {
            **first_retry,
            "data": {
                **first_retry["data"],
                "arguments": {
                    **first_retry["data"]["arguments"],
                    "title": "audit-retry-1-2",
                },
            },
        }
        self.assertEqual(contract(first_retry)["result"], "ALLOW")
        denied = contract(second_retry)
        self.assertEqual(denied["result"], "DENY")
        self.assertIn("Only one paid transient Opus retry", denied["reason"])

    def test_voice_profile_digest_propagates_current_bytes(self) -> None:
        required = (
            "# Example Voice Profile\n"
            "Never use em dashes.\n"
            "Do not invent confidence.\n"
        )
        with tempfile.TemporaryDirectory() as value:
            profile = Path(value) / "voice.md"
            profile.write_text(required + "version one\n", encoding="utf-8")
            with mock.patch.object(launcher, "VOICE_PROFILE", profile):
                first = launcher._validate_voice_profile(profile)
                profile.write_text(required + "version two\n", encoding="utf-8")
                second = launcher._validate_voice_profile(profile)
            self.assertNotEqual(first, second)
            self.assertEqual(
                second, hashlib.sha256(profile.read_bytes()).hexdigest()
            )

    def test_codex_voice_access_failure_never_routes_cursor_fallback(self) -> None:
        voice_failure = json.dumps(
            {
                "verdict": "REWORK",
                "needs_web": False,
                "why": "voice profile unavailable",
                "punch_list_for_cursor": [
                    f"Rerun the judge with read access to {_VOICE_PROFILE_PATH}"
                ],
            }
        )
        records = [
            _route_packet("cursor_workhorse", "cursor-cycle-1", "evidence"),
            _route_packet("opus_auditor", "audit-cycle-1", _audit("PASS")),
            _route_packet("codex_judge", "judge-cycle-1", voice_failure),
        ]
        # With a profile configured, failing to read it is a launch capability
        # failure and must never be handed to Cursor as rework.
        with mock.patch.dict(
            os.environ, _voice_env("digest"), clear=False
        ):
            route = plugin._next_route(records)
        self.assertEqual(route.status, "infrastructure_failed")
        self.assertNotEqual(route.title, "cursor-cycle-2")

        # With voice rendering off there is no profile to fail to read, so the
        # same words are a stray punch item that must not end the run.
        with mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_VOICE_PROFILE": "", "TRIPLE_STAMP_VOICE_PROFILE_SHA256": ""},
            clear=False,
        ):
            disabled = plugin._next_route(records)
        self.assertNotEqual(disabled.status, "infrastructure_failed")

    def test_codex_argv_uses_outer_read_only_profile_boundary(self) -> None:
        wrapper = (ROOT / ".omnigent/codex-launch").read_text(encoding="utf-8")
        codex = yaml.safe_load(
            (ROOT / "agents/codex_judge/config.yaml").read_text(encoding="utf-8")
        )
        builder = (ROOT / ".omnigent/build_outer_seatbelt.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("'model_reasoning_effort=\"ultra\"'", wrapper)
        self.assertIn('PROBE="/tmp/triple-stamp-codex-seatbelt-probe.$$"', wrapper)
        self.assertIn("exit 78", wrapper)
        self.assertNotIn("ISAAC_", wrapper)
        self.assertNotIn("/usr/local/bin/isaac", wrapper)
        # Installed Codex documents --add-dir as an additional writable root,
        # so the voice profile must instead remain a read-only outer grant.
        self.assertNotIn("--add-dir", wrapper)
        self.assertIn("TRIPLE_STAMP_VOICE_PROFILE", codex["prompt"])
        self.assertIn("no voice profile is configured", codex["prompt"])
        self.assertIn("str(voice_profile)", builder)
        self.assertIn(
            "outer profile grants voice profile writes", builder
        )

    def test_provider_profiles_select_exact_child_launchers(self) -> None:
        common = dict(
            root=ROOT,
            real_home=Path("/Users/unit"),
            run_dir=Path("/tmp/provider-test"),
            bundle=Path("/tmp/provider-test/bundle"),
            run_id="unit-run",
            sandbox_token="sandbox",
            cursor_token="cursor",
            omnigent_token="omnigent",
            harness_tmp=Path("/tmp/provider-harness"),
            tools=self._fake_toolchain(),
            managed_python=Path("/managed/python"),
            voice_profile_sha256="0" * 64,
        )
        direct = launcher._runtime_env(
            **common,
            provider="direct",
            models=launcher._provider_models(ROOT, "direct"),
        )
        self.assertNotIn("OMNIGENT_CLAUDE_LAUNCHER", direct)
        self.assertEqual(
            direct["OMNIGENT_CODEX_PATH"],
            str(ROOT / ".omnigent/codex-launch"),
        )
        self.assertEqual(
            direct["OMNIGENT_CURSOR_PATH"],
            str(ROOT / ".omnigent/cursor-via-login"),
        )
        self.assertEqual(direct["TRIPLE_STAMP_PROVIDER"], "direct")

        databricks = launcher._runtime_env(
            **common,
            provider="databricks",
            models=launcher._provider_models(ROOT, "databricks"),
        )
        self.assertEqual(
            {
                "OMNIGENT_CLAUDE_LAUNCHER": databricks["OMNIGENT_CLAUDE_LAUNCHER"],
                "OMNIGENT_CODEX_PATH": databricks["OMNIGENT_CODEX_PATH"],
                "OMNIGENT_CURSOR_PATH": databricks["OMNIGENT_CURSOR_PATH"],
            },
            {
                "OMNIGENT_CLAUDE_LAUNCHER": "isaac",
                "OMNIGENT_CODEX_PATH": str(
                    ROOT / ".omnigent/codex-via-isaac"
                ),
                "OMNIGENT_CURSOR_PATH": str(
                    ROOT / ".omnigent/cursor-via-login"
                ),
            },
        )
        databricks_runner_passthrough = set(
            databricks["OMNIGENT_RUNNER_ENV_PASSTHROUGH"].split(",")
        )
        self.assertTrue(
            {
                "OMNIGENT_CLAUDE_LAUNCHER",
                "ISAAC_BIN",
                "ISAAC_DEFAULT_UCODE",
                "ISAAC_LAUNCH_MODE",
                "ISAAC_DISABLE_MAC_MANAGED_SETTINGS_UPDATE",
            }.issubset(databricks_runner_passthrough)
        )

    def test_provider_matrix_covers_outside_and_exact_seatbelt_runs(self) -> None:
        matrix = (ROOT / "verify-provider-matrix").read_text(encoding="utf-8")
        self.assertEqual(matrix.count("-m unittest discover"), 2)
        self.assertEqual(matrix.count("--self-test"), 1)
        self.assertEqual(matrix.count("env -u TRIPLE_STAMP_PROVIDER"), 1)
        self.assertEqual(matrix.count("TRIPLE_STAMP_PROVIDER=databricks"), 2)
        self.assertEqual(matrix.count("TRIPLE_STAMP_PROFILE_MATRIX=1"), 1)
        launcher_source = (ROOT / ".omnigent/launcher.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'for matrix_provider in ("direct", "databricks")',
            launcher_source,
        )

    def test_provider_model_mapping_materializes_without_drift(self) -> None:
        validator = _module(
            "triple_stamp_validator_profiles_test",
            ROOT / ".omnigent/validate_bundle.py",
        )
        for provider in ("direct", "databricks"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as value:
                bundle = Path(value)
                for relative in (
                    "config.yaml",
                    "agents/cursor_workhorse/config.yaml",
                    "agents/opus_auditor/config.yaml",
                    "agents/codex_judge/config.yaml",
                ):
                    target = bundle / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(ROOT / relative, target)
                for config_path in bundle.glob("**/config.yaml"):
                    document = yaml.safe_load(
                        config_path.read_text(encoding="utf-8")
                    )
                    document["os_env"]["cwd"] = str(ROOT)
                    document["os_env"]["sandbox"]["type"] = "none"
                    config_path.write_text(
                        yaml.safe_dump(document, sort_keys=False),
                        encoding="utf-8",
                    )
                models = launcher._provider_models(ROOT, provider)
                launcher._apply_provider_to_bundle(bundle, provider, models)
                supervisor = yaml.safe_load(
                    (bundle / "config.yaml").read_text(encoding="utf-8")
                )
                opus = yaml.safe_load(
                    (
                        bundle / "agents/opus_auditor/config.yaml"
                    ).read_text(encoding="utf-8")
                )
                cursor = yaml.safe_load(
                    (
                        bundle / "agents/cursor_workhorse/config.yaml"
                    ).read_text(encoding="utf-8")
                )
                codex = yaml.safe_load(
                    (
                        bundle / "agents/codex_judge/config.yaml"
                    ).read_text(encoding="utf-8")
                )
                self.assertEqual(supervisor["executor"]["model"], models["supervisor"])
                self.assertEqual(opus["executor"]["model"], models["opus_auditor"])
                self.assertEqual(codex["executor"]["model"], models["codex_judge"])
                cursor_passthrough = set(
                    cursor["os_env"]["sandbox"]["env_passthrough"]
                )
                self.assertFalse(
                    cursor_passthrough & validator._CURSOR_RUNNER_ONLY_ENV
                )
                if provider == "databricks":
                    self.assertEqual(
                        supervisor["executor"]["auth"]["name"],
                        "isaac-databricks-ai-gateway",
                    )
                else:
                    self.assertNotIn("auth", supervisor["executor"])
                for model in models.values():
                    self.assertNotEqual(
                        plugin._model_rates(model),
                        plugin._COST_CEILINGS["unknown"],
                    )
                with (
                    mock.patch.dict(
                        os.environ,
                        {
                            "TRIPLE_STAMP_PROVIDER": provider,
                            "TRIPLE_STAMP_SUPERVISOR_MODEL": models["supervisor"],
                            "TRIPLE_STAMP_OPUS_MODEL": models["opus_auditor"],
                            "TRIPLE_STAMP_CODEX_MODEL": models["codex_judge"],
                            "TRIPLE_STAMP_CLAUDE_NAMESPACE": (
                                launcher._resolved_claude_namespace(models)
                            ),
                        },
                        clear=False,
                    ),
                ):
                    validator.validate_spec(ROOT, bundle, provider=provider)

    def test_active_profile_validator_rejects_model_drift(self) -> None:
        validator = _module(
            "triple_stamp_validator_drift_test",
            ROOT / ".omnigent/validate_bundle.py",
        )
        for provider in ("direct", "databricks"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as value:
                bundle = Path(value)
                shutil.copytree(ROOT / "agents", bundle / "agents")
                shutil.copy2(ROOT / "config.yaml", bundle / "config.yaml")
                for config_path in bundle.glob("**/config.yaml"):
                    document = yaml.safe_load(
                        config_path.read_text(encoding="utf-8")
                    )
                    document["os_env"]["cwd"] = str(ROOT)
                    document["os_env"]["sandbox"]["type"] = "none"
                    config_path.write_text(
                        yaml.safe_dump(document, sort_keys=False),
                        encoding="utf-8",
                    )
                models = launcher._provider_models(ROOT, provider)
                launcher._apply_provider_to_bundle(bundle, provider, models)
                opus_path = bundle / "agents/opus_auditor/config.yaml"
                opus = yaml.safe_load(opus_path.read_text(encoding="utf-8"))
                opus["executor"]["model"] = "claude-opus-drifted"
                opus_path.write_text(
                    yaml.safe_dump(opus, sort_keys=False),
                    encoding="utf-8",
                )
                with (
                    mock.patch.dict(
                        os.environ,
                        {
                            "TRIPLE_STAMP_PROVIDER": provider,
                            "TRIPLE_STAMP_SUPERVISOR_MODEL": models["supervisor"],
                            "TRIPLE_STAMP_OPUS_MODEL": models["opus_auditor"],
                            "TRIPLE_STAMP_CODEX_MODEL": models["codex_judge"],
                            "TRIPLE_STAMP_CLAUDE_NAMESPACE": (
                                launcher._resolved_claude_namespace(models)
                            ),
                        },
                        clear=False,
                    ),
                    self.assertRaises(SystemExit) as raised,
                ):
                    validator.validate_spec(ROOT, bundle, provider=provider)
                self.assertIn(
                    "opus_auditor model drifted",
                    str(raised.exception),
                )

    def test_plain_claude_resolution_and_codex_app_server_non_git_proof(self) -> None:
        from omnigent.claude_launcher import resolve_claude_launch

        direct_model = launcher._provider_models(ROOT, "direct")["opus_auditor"]
        databricks_model = launcher._provider_models(
            ROOT, "databricks"
        )["opus_auditor"]
        direct_args = ["--model", direct_model, "--effort", "max"]
        with mock.patch.dict(
            os.environ,
            {"OMNIGENT_CLAUDE_LAUNCHER": ""},
            clear=False,
        ):
            direct_command, resolved_args = resolve_claude_launch(
                "claude",
                direct_args,
            )
        self.assertEqual(direct_command, "claude")
        self.assertNotEqual(direct_command, "claude code")
        self.assertEqual(resolved_args, direct_args)
        self.assertEqual(resolved_args[1], direct_model)
        rendered = shlex.join(
            ["claude", "--model", databricks_model, "--effort", "max"]
        )
        self.assertIn(f"'{databricks_model}'", rendered)
        self.assertNotIn(f"--model {databricks_model}", rendered)
        fixture = json.loads(
            (
                ROOT / "tests/fixtures/codex-app-server-nongit.json"
            ).read_text(encoding="utf-8")
        )
        self.assertFalse(fixture["working_directory_was_git_repository"])
        self.assertTrue(fixture["thread_start_has_result"])
        self.assertIsNone(fixture["error"])
        self.assertFalse(fixture["skip_git_repo_check_passed"])
        self.assertNotIn(
            "--skip-git-repo-check",
            (ROOT / ".omnigent/codex-launch").read_text(encoding="utf-8"),
        )

    def test_direct_checked_in_configs_have_no_databricks_launcher_dependency(self) -> None:
        supervisor = yaml.safe_load(
            (ROOT / "config.yaml").read_text(encoding="utf-8")
        )
        opus = yaml.safe_load(
            (ROOT / "agents/opus_auditor/config.yaml").read_text(encoding="utf-8")
        )
        codex = yaml.safe_load(
            (ROOT / "agents/codex_judge/config.yaml").read_text(encoding="utf-8")
        )
        self.assertNotIn("auth", supervisor["executor"])
        for document in (supervisor, opus, codex):
            passthrough = document["os_env"]["sandbox"]["env_passthrough"]
            self.assertNotIn("ISAAC_BIN", passthrough)
            self.assertNotIn("OMNIGENT_CLAUDE_LAUNCHER", passthrough)
        wrapper = (ROOT / ".omnigent/codex-launch").read_text(encoding="utf-8")
        self.assertNotIn("ISAAC_", wrapper)
        self.assertNotIn("/usr/local/bin/isaac", wrapper)

    def test_generated_bundle_runs_full_validator_without_launcher(self) -> None:
        if os.environ.get("TRIPLE_STAMP_OUTER_SANDBOX") == "1":
            run_dir = Path(os.environ["TRIPLE_STAMP_RUN_DIR"]).resolve()
            runtime_bundle = Path(os.environ["TRIPLE_STAMP_BUNDLE"]).resolve()
            self.assertTrue(runtime_bundle.is_relative_to(run_dir))
            validator_env = {
                **os.environ,
                "PYTHONPATH": str(ROOT / ".omnigent/runtime-python"),
            }
            result = subprocess.run(
                [
                    os.environ["STABLE_OMNIGENT_PY"],
                    "-I",
                    str(ROOT / ".omnigent/validate_bundle.py"),
                    str(ROOT),
                    str(runtime_bundle),
                ],
                env=validator_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertIn("triple-stamp bundle validation: PASS", result.stdout)
            return

        real_home = Path.home().resolve()
        tools = launcher._resolve_toolchain(real_home, "direct")
        provider: dict[str, object] = {}
        base = Path(
            tempfile.mkdtemp(
                prefix="validator-regression-",
                dir=os.environ.get("TMPDIR"),
            )
        )
        run_dir: Path | None = None
        try:
            # Voice rendering enabled, so the exported path and digest agree and
            # the paired-export check is exercised rather than skipped.
            with mock.patch.object(
                launcher, "VOICE_PROFILE", Path(_VOICE_PROFILE_PATH)
            ):
                run_dir, profile, runtime_env, _run_id = launcher._prepare_runtime(
                    ROOT,
                    real_home,
                    base,
                    tools,
                    tools.omnigent_python,
                    "validator-cursor-token",
                    "validator-omnigent-token",
                    time.time() + 3600,
                    provider,
                    hashlib.sha256(
                        Path(_VOICE_PROFILE_PATH).read_bytes()
                    ).hexdigest(),
                    "direct",
                    launcher._provider_models(ROOT, "direct"),
                )
            result = subprocess.run(
                [
                    str(tools.sandbox_exec),
                    "-f",
                    str(profile),
                    str(tools.omnigent_python),
                    "-I",
                    str(ROOT / ".omnigent/validate_bundle.py"),
                    str(ROOT),
                    str(run_dir / "bundle"),
                ],
                env=runtime_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
                check=False,
            )
            self.assertEqual(
                result.returncode,
                0,
                result.stderr or result.stdout,
            )
            self.assertIn("triple-stamp bundle validation: PASS", result.stdout)
            self.assertIn("Opus launch contract: PASS", result.stdout)
            self.assertIn("mcp_health_gating=disabled", result.stdout)
            self.assertIn("readiness_probe=absent", result.stdout)
        finally:
            if run_dir is not None:
                marker = run_dir / "harness-link"
                if marker.is_file():
                    link = Path(marker.read_text(encoding="utf-8").strip())
                    if link.is_symlink():
                        link.unlink(missing_ok=True)
            shutil.rmtree(base, ignore_errors=True)

    def test_outer_seatbelt_denies_global_tmp_and_allows_runtime_tmp(self) -> None:
        probe = r"""
import os
from pathlib import Path

allowed = Path(os.environ["TMPDIR"]) / "triple-stamp-allowed-temp-probe"
allowed.write_text("ok", encoding="utf-8")
allowed.unlink()

denied = Path("/tmp") / f"triple-stamp-denied-temp-probe-{os.getpid()}"
try:
    denied.write_text("must fail", encoding="utf-8")
except PermissionError:
    pass
else:
    denied.unlink(missing_ok=True)
    raise SystemExit("outer Seatbelt allowed arbitrary /tmp write")

host_token = Path("/tmp/cached_hcvault_token")
try:
    host_token.read_bytes()
except (FileNotFoundError, PermissionError):
    pass
else:
    raise SystemExit("outer Seatbelt exposed the host HCVault token")
print("exact temp boundary: PASS")
"""
        if os.environ.get("TRIPLE_STAMP_OUTER_SANDBOX") == "1":
            result = subprocess.run(
                [os.environ["STABLE_OMNIGENT_PY"], "-I", "-c", probe],
                env=dict(os.environ),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertIn("exact temp boundary: PASS", result.stdout)
            return

        real_home = Path.home().resolve()
        tools = launcher._resolve_toolchain(real_home, "direct")
        provider: dict[str, object] = {}
        base = Path(
            tempfile.mkdtemp(
                prefix="temp-boundary-regression-",
                dir=os.environ.get("TMPDIR"),
            )
        )
        run_dir: Path | None = None
        try:
            # Voice rendering enabled, so the exported path and digest agree and
            # the paired-export check is exercised rather than skipped.
            with mock.patch.object(
                launcher, "VOICE_PROFILE", Path(_VOICE_PROFILE_PATH)
            ):
                run_dir, profile, runtime_env, _run_id = launcher._prepare_runtime(
                    ROOT,
                    real_home,
                    base,
                    tools,
                    tools.omnigent_python,
                    "validator-cursor-token",
                    "validator-omnigent-token",
                    time.time() + 3600,
                    provider,
                    hashlib.sha256(
                        Path(_VOICE_PROFILE_PATH).read_bytes()
                    ).hexdigest(),
                    "direct",
                    launcher._provider_models(ROOT, "direct"),
                )
            result = subprocess.run(
                [
                    str(tools.sandbox_exec),
                    "-f",
                    str(profile),
                    str(tools.omnigent_python),
                    "-I",
                    "-c",
                    probe,
                ],
                env=runtime_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertIn("exact temp boundary: PASS", result.stdout)
        finally:
            if run_dir is not None:
                marker = run_dir / "harness-link"
                if marker.is_file():
                    link = Path(marker.read_text(encoding="utf-8").strip())
                    if link.is_symlink():
                        link.unlink(missing_ok=True)
            shutil.rmtree(base, ignore_errors=True)

    def test_supervisor_mechanically_exposes_only_routing_tools(self) -> None:
        from omnigent.inner import claude_sdk_executor
        from omnigent.spec.parser import parse
        from omnigent.tools.manager import ToolManager

        runtime_guard._install_minimal_supervisor_tool_surface()
        spec = parse(ROOT)
        self.assertFalse(spec.spawn)
        self.assertEqual(
            {agent.name for agent in spec.sub_agents},
            {"cursor_workhorse", "opus_auditor", "codex_judge"},
        )
        manager = ToolManager(spec, workdir=ROOT)
        try:
            self.assertEqual(
                set(manager.get_tool_names()),
                {"sys_session_send", "sys_read_inbox"},
            )
        finally:
            manager.shutdown()

        sdk = claude_sdk_executor._ensure_sdk()
        options = sdk.ClaudeAgentOptions(
            tools=["Skill", "ToolSearch", "Bash"],
            allowed_tools=[
                "mcp__omnigent__sys_session_send",
                "mcp__omnigent__sys_read_inbox",
                "mcp__omnigent__sys_os_shell",
            ],
        )
        self.assertEqual(options.tools, ["ToolSearch"])
        self.assertEqual(
            set(options.allowed_tools),
            {
                "mcp__omnigent__sys_session_send",
                "mcp__omnigent__sys_read_inbox",
            },
        )

    def test_sitecustomize_installs_in_cold_and_zygote_runners(self) -> None:
        self.assertTrue(
            runtime_guard._is_runner_process(
                ["python", "-m", "omnigent.runner._entry"]
            )
        )
        self.assertTrue(
            runtime_guard._is_runner_process(
                ["python", "-P", "-m", "omnigent.runner._zygote"]
            )
        )
        self.assertFalse(
            runtime_guard._is_runner_process(
                ["python", "-m", "omnigent.cli", "server"]
            )
        )

    def test_policy_identity_bridge_uses_engine_current_and_root_ids(self) -> None:
        from omnigent.policies.function import FunctionPolicy
        from omnigent.policies.types import EvaluationContext
        from omnigent.runtime.policies.engine import PolicyEngine
        from omnigent.spec.types import FunctionPolicySpec, Phase

        runtime_guard._install_policy_identity_context()
        engine = object.__new__(PolicyEngine)
        engine._labels = {}
        engine._session_state = {}
        engine._conversation_id = "child"
        engine._root_conversation_id = "root"
        context = engine._context()
        self.assertEqual(context["conversation_id"], "child")
        self.assertEqual(context["root_conversation_id"], "root")

        captured: dict[str, object] = {}

        def capture(event: dict[str, object]) -> dict[str, str]:
            captured.update(event)
            return {"result": "ALLOW"}

        policy = FunctionPolicy(
            FunctionPolicySpec(name="capture", on=None),
            capture,
        )
        result = asyncio.run(
            policy.evaluate(
                EvaluationContext(
                    phase=Phase.TOOL_CALL,
                    content={"name": "ToolSearch", "arguments": {}},
                    tool_name="ToolSearch",
                ),
                context,
            )
        )
        self.assertEqual(result.action.value, "allow")
        event_context = captured["context"]
        self.assertIsInstance(event_context, dict)
        self.assertEqual(event_context["conversation_id"], "child")
        self.assertEqual(event_context["root_conversation_id"], "root")

    def test_required_isaac_launcher_failure_cannot_fall_back_to_claude(self) -> None:
        from omnigent import claude_launcher

        original = claude_launcher.resolve_claude_launch
        alias_originals = {
            name: getattr(sys.modules.get(name), "resolve_claude_launch", None)
            for name in (
                "omnigent.claude_native",
                "omnigent.runner.native.orchestration",
            )
        }

        class FailingLauncher:
            def launch(self, _command: str, _args: list[str]):
                raise RuntimeError("deterministic readiness failure")

        try:
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "OMNIGENT_CLAUDE_LAUNCHER": "isaac",
                        "TRIPLE_STAMP_PROVIDER": "databricks",
                        "TRIPLE_STAMP_RUN_ID": "run-a",
                    },
                    clear=False,
                ),
                mock.patch.object(
                    claude_launcher, "_load_launcher", return_value=FailingLauncher()
                ),
            ):
                runtime_guard._install_fail_closed_claude_launcher()
                with self.assertRaisesRegex(
                    RuntimeError, "deterministic readiness failure"
                ):
                    claude_launcher.resolve_claude_launch("claude", ["--model", "opus"])
        finally:
            claude_launcher.resolve_claude_launch = original
            for name, value in alias_originals.items():
                module = sys.modules.get(name)
                if module is not None and value is not None:
                    module.resolve_claude_launch = value

    def test_supervisor_route_cap_matches_default_and_deep_formula(self) -> None:
        config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        function = config["guardrails"]["policies"]["cap_calls"]["function"]
        self.assertEqual(
            function["path"],
            "triple_stamp_isaac_launcher.supervisor_route_call_limit",
        )
        arguments = function["arguments"]
        self.assertEqual(arguments["limit"], 84)
        self.assertEqual(
            arguments["counted_tools"],
            [
                "sys_session_send",
                "sys_read_inbox",
                "ToolSearch",
                "sys_agent_start",
            ],
        )
        # Per cycle: straight Cursor/Opus/Codex (3 children), two Opus web
        # hops (4), two Codex web hops (4), two Codex internal hops (4), and
        # all reachable one-shot format repairs (4). Every child needs one
        # send and one isolated inbox read.
        self.assertEqual(plugin._ROUTE_CHILDREN_PER_CYCLE, 19)
        self.assertEqual(
            plugin._ROUTE_STATE_MACHINE_CALLS,
            plugin._MAX_CYCLES * 19 * 2,
        )
        self.assertEqual(
            plugin._ROUTE_STATE_MACHINE_CALLS
            + plugin._ROUTE_SCHEMA_DISCOVERY_CALLS
            + plugin._ROUTE_AGENT_START_CALLS
            + plugin._ROUTE_FINALIZATION_OVERHEAD_CALLS,
            plugin._SUPERVISOR_ROUTE_LIMIT,
        )
        for cycles, expected in ((2, 84), (4, 160)):
            with self.subTest(cycles=cycles):
                self.assertEqual(
                    plugin.supervisor_route_call_cap(cycles),
                    expected,
                )
                self.assertEqual(launcher._route_call_cap(cycles), expected)

    def test_generated_cycle_policy_is_table_driven_for_two_and_four(self) -> None:
        models = {
            "supervisor": "claude-sonnet-4-6",
            "opus_auditor": "claude-opus-5",
            "codex_judge": "gpt-5.6-sol",
        }
        for cycles, cap, denied_cycle in ((2, 84, 3), (4, 160, 5)):
            with self.subTest(cycles=cycles), tempfile.TemporaryDirectory() as value:
                bundle = Path(value)
                shutil.copy2(ROOT / "config.yaml", bundle / "config.yaml")
                shutil.copytree(ROOT / "agents", bundle / "agents")
                launcher._apply_provider_to_bundle(
                    bundle,
                    "direct",
                    models,
                    cycles,
                )
                generated = yaml.safe_load(
                    (bundle / "config.yaml").read_text(encoding="utf-8")
                )
                arguments = generated["guardrails"]["policies"]["cap_calls"][
                    "function"
                ]["arguments"]
                self.assertEqual(arguments["limit"], cap)
                self.assertIn(
                    f"This run permits exactly {cycles} complete cycles",
                    generated["prompt"],
                )
                self.assertIn(
                    f"Cycle {denied_cycle} is denied",
                    generated["prompt"],
                )
        for invalid in (1, 3, 5, "02", "4.0"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(launcher.LaunchError):
                    launcher._max_cycles(invalid)
                with self.assertRaises(ValueError):
                    plugin.configured_max_cycles(invalid)
        launcher_source = (
            ROOT / ".omnigent/launcher.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"cycle-policy.json"', launcher_source)
        self.assertIn('"formula": "cycles*19*2+8"', launcher_source)

    def test_route_cap_counts_only_verified_supervisor_tools(self) -> None:
        policy = plugin.supervisor_route_call_limit()
        state: dict[str, int] = {}

        def event(name: str, conversation: str, root: str) -> dict[str, object]:
            return {
                "type": "tool_call",
                "data": {"name": name, "arguments": {}},
                "context": {
                    "conversation_id": conversation,
                    "root_conversation_id": root,
                },
                "session_state": dict(state),
            }

        for name in (
            "sys_session_send",
            "sys_read_inbox",
            "ToolSearch",
            "mcp__omnigent__sys_agent_start",
        ):
            result = policy(event(name, "root", "root"))
            self.assertEqual(result["result"], "ALLOW")
            self.assertEqual(len(result["state_updates"]), 1)
            state[plugin._SUPERVISOR_ROUTE_COUNT_STATE_KEY] = (
                state.get(plugin._SUPERVISOR_ROUTE_COUNT_STATE_KEY, 0) + 1
            )
        self.assertEqual(state[plugin._SUPERVISOR_ROUTE_COUNT_STATE_KEY], 4)

        for name in (
            "ToolSearch",
            "mcp__glean__search",
            "mcp__jira__jira_read_api_call",
            "WebSearch",
        ):
            result = policy(event(name, "child", "root"))
            self.assertEqual(result, {"result": "ALLOW"})
        self.assertEqual(state[plugin._SUPERVISOR_ROUTE_COUNT_STATE_KEY], 4)

    def test_all_default_cycle_route_paths_fit_and_next_call_is_denied(self) -> None:
        policy = plugin.supervisor_route_call_limit()
        state: dict[str, int] = {}
        state_machine = [
            name
            for _cycle in range(plugin._MAX_CYCLES)
            for _child in range(plugin._ROUTE_CHILDREN_PER_CYCLE)
            for name in ("sys_session_send", "sys_read_inbox")
        ]
        overhead = (
            ["ToolSearch"] * plugin._ROUTE_SCHEMA_DISCOVERY_CALLS
            + ["sys_agent_start"] * plugin._ROUTE_AGENT_START_CALLS
            + ["sys_session_send", "sys_read_inbox"]
        )
        calls = state_machine + overhead
        self.assertEqual(len(calls), plugin._SUPERVISOR_ROUTE_LIMIT)
        for name in calls:
            result = policy(
                {
                    "type": "tool_call",
                    "data": {"name": name, "arguments": {}},
                    "context": {
                        "conversation_id": "root",
                        "root_conversation_id": "root",
                    },
                    "session_state": dict(state),
                }
            )
            self.assertEqual(result["result"], "ALLOW")
            state[plugin._SUPERVISOR_ROUTE_COUNT_STATE_KEY] = (
                state.get(plugin._SUPERVISOR_ROUTE_COUNT_STATE_KEY, 0) + 1
            )
        denied = policy(
            {
                "type": "tool_call",
                "data": {"name": "ToolSearch", "arguments": {}},
                "context": {
                    "conversation_id": "root",
                    "root_conversation_id": "root",
                },
                "session_state": dict(state),
            }
        )
        self.assertEqual(denied["result"], "DENY")
        self.assertIn(str(plugin._SUPERVISOR_ROUTE_LIMIT), denied["reason"])

    def test_route_cap_unavailable_identity_or_state_never_fabricates_count(
        self,
    ) -> None:
        policy = plugin.supervisor_route_call_limit()
        cases = (
            {},
            {"context": {}},
            {"context": {"conversation_id": "root"}},
            {
                "context": {
                    "conversation_id": "child",
                    "root_conversation_id": "root",
                },
                "session_state": {},
            },
            {
                "context": {
                    "conversation_id": "root",
                    "root_conversation_id": "root",
                },
            },
            {
                "context": {
                    "conversation_id": "root",
                    "root_conversation_id": "root",
                },
                "session_state": {
                    plugin._SUPERVISOR_ROUTE_COUNT_STATE_KEY: "unknown"
                },
            },
        )
        for additions in cases:
            with self.subTest(additions=additions):
                event = {
                    "type": "tool_call",
                    "data": {"name": "sys_session_send", "arguments": {}},
                    **additions,
                }
                self.assertEqual(policy(event), {"result": "ALLOW"})

    def test_route_cap_persists_across_restart_and_ignores_propagated_child(
        self,
    ) -> None:
        key = plugin._SUPERVISOR_ROUTE_COUNT_STATE_KEY
        state = {key: plugin._SUPERVISOR_ROUTE_LIMIT - 1}
        first = plugin.supervisor_route_call_limit()(
            {
                "type": "tool_call",
                "data": {"name": "sys_read_inbox", "arguments": {}},
                "context": {
                    "conversation_id": "root",
                    "root_conversation_id": "root",
                },
                "session_state": state,
            }
        )
        self.assertEqual(first["result"], "ALLOW")
        restarted_state = {key: plugin._SUPERVISOR_ROUTE_LIMIT}
        child = plugin.supervisor_route_call_limit()(
            {
                "type": "tool_call",
                "data": {"name": "ToolSearch", "arguments": {}},
                "context": {
                    "conversation_id": "child",
                    "root_conversation_id": "root",
                },
                "session_state": restarted_state,
            }
        )
        self.assertEqual(child, {"result": "ALLOW"})
        restarted_root = plugin.supervisor_route_call_limit()(
            {
                "type": "tool_call",
                "data": {"name": "sys_session_send", "arguments": {}},
                "context": {
                    "conversation_id": "root",
                    "root_conversation_id": "root",
                },
                "session_state": restarted_state,
            }
        )
        self.assertEqual(restarted_root["result"], "DENY")

    def test_route_cap_has_direct_and_databricks_profile_parity(self) -> None:
        outcomes = []
        for provider in ("direct", "databricks"):
            with mock.patch.dict(
                os.environ,
                {"TRIPLE_STAMP_PROVIDER": provider},
                clear=False,
            ):
                outcomes.append(
                    plugin.supervisor_route_call_limit()(
                        {
                            "type": "tool_call",
                            "data": {"name": "ToolSearch", "arguments": {}},
                            "context": {
                                "conversation_id": "root",
                                "root_conversation_id": "root",
                            },
                            "session_state": {},
                        }
                    )
                )
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(outcomes[0]["result"], "ALLOW")

    def test_retained_run_fixture_separates_route_and_child_calls(self) -> None:
        fixture = json.loads(
            (
                ROOT / "tests/fixtures/run-retained-route-call-scope.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(fixture["observed_tool_calls"], 174)
        self.assertEqual(fixture["supervisor_route_calls"]["total"], 26)
        self.assertEqual(fixture["child_work_calls"]["total"], 148)
        self.assertEqual(
            fixture["supervisor_route_calls"]["total"]
            + fixture["child_work_calls"]["total"],
            fixture["observed_tool_calls"],
        )
        child = fixture["child_work_calls"]
        self.assertEqual(
            child["opus_internal_mcp_calls"]
            + child["opus_toolsearch_calls"]
            + child["other_child_internal_or_related_calls"],
            child["total"],
        )

    def test_opus_prompt_and_argv_forbid_local_runtime_probes(self) -> None:
        prompt = yaml.safe_load(
            (ROOT / "agents/opus_auditor/config.yaml").read_text(encoding="utf-8")
        )["prompt"]
        self.assertIn("Do not inspect or launch `cursor-agent`", prompt)
        self.assertIn("Never emit or simulate a Bash", prompt)
        self.assertIn("including any tool failure", prompt)
        self.assertIn("FORMAT REPAIR ONLY", prompt)
        self.assertIn("optional capabilities, not launch", prompt)
        self.assertIn("internal_sources_consulted", prompt)
        self.assertIn("internal_coverage", prompt)
        self.assertIn("Every audit must attempt all five systems", prompt)
        self.assertIn("select:mcp__slack__slack_read_api_call", prompt)
        self.assertIn('type:"conversation"', prompt)
        self.assertIn("metadata.container", prompt)
        self.assertNotIn("do not mechanically call all five", prompt)
        self.assertNotIn("If Isaac lands you on the wrong model", prompt)
        codex_prompt = yaml.safe_load(
            (ROOT / "agents/codex_judge/config.yaml").read_text(encoding="utf-8")
        )["prompt"]
        self.assertIn("internal_sources_consulted", codex_prompt)
        self.assertIn("NEEDS_INTERNAL", codex_prompt)
        self.assertIn("You also cannot use internal MCPs", codex_prompt)
        self.assertIn("never STAMP", codex_prompt)
        self.assertIn("Do no research yourself", codex_prompt)

        with mock.patch.dict(os.environ, {"TRIPLE_STAMP_RUN_ID": "test-run"}):
            args = plugin._triple_stamp_claude_args(
                [
                    "--model",
                    plugin._OPUS_MODEL,
                    "--mcp-config",
                    '{"mcpServers":{"omnigent":{"type":"stdio"}}}',
                    "--tools",
                    "",
                    "--setting-sources=user,project,local",
                    "--settings",
                    "/tmp/run/explicit-settings.json",
                    "--append-system-prompt",
                    plugin._OPUS_AUDITOR_PROMPT_MARKER,
                ],
                opus_mcp_config="/tmp/run/opus-mcp.json",
            )
        self.assertEqual(args[args.index("--tools") + 1], "ToolSearch")
        self.assertNotEqual(args[args.index("--tools") + 1], "")
        self.assertEqual(args.count("--tools"), 1)
        self.assertNotIn("--tools=", args)
        self.assertEqual(args[args.index("--setting-sources") + 1], "")
        self.assertEqual(args.count("--setting-sources"), 1)
        self.assertEqual(
            args[args.index("--settings") + 1],
            "/tmp/run/explicit-settings.json",
        )
        denied = args[args.index("--disallowedTools") + 1].split(",")
        for tool in ("Bash", "Shell", "Task", "Agent", "Skill", "WebSearch", "WebFetch"):
            self.assertIn(tool, denied)
        self.assertEqual(
            args[args.index("--allowedTools") + 1],
            ",".join(plugin._OPUS_ALLOWED_TOOLS),
        )
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(
            args[args.index("--mcp-config") + 1],
            "/tmp/run/opus-mcp.json",
        )
        self.assertEqual(args.count("--mcp-config"), 1)
        self.assertNotIn('"omnigent"', " ".join(args))
        allowed = set(args[args.index("--allowedTools") + 1].split(","))
        self.assertIn("ToolSearch", allowed)
        self.assertIn("mcp__web-search", denied)
        self.assertNotIn("mcp__web-search__*", denied)
        for tool in (
            "mcp__glean__create_go_link",
            "mcp__slack__slack_write_api_call",
            "mcp__slack__slack_batch_write_api_call",
            "mcp__jira__jira_write_api_call",
            "mcp__confluence__create_confluence_page",
            "mcp__confluence__update_confluence_page",
            "mcp__confluence__reply_to_confluence_comment",
            "mcp__safe__safe_write_api_call",
            "mcp__safe__safe_merge_api_call",
        ):
            self.assertNotIn(tool, allowed)
            self.assertIn(tool, denied)

    def test_internal_coverage_receipt_and_consistency_contract_is_explicit(self) -> None:
        opus_config = yaml.safe_load(
            (ROOT / "agents/opus_auditor/config.yaml").read_text(encoding="utf-8")
        )
        codex_config = yaml.safe_load(
            (ROOT / "agents/codex_judge/config.yaml").read_text(encoding="utf-8")
        )
        opus_prompt = opus_config["prompt"]
        codex_prompt = codex_config["prompt"]
        for marker in (
            "`routes` contains `native`, `glean_facet`, or both",
            "`tools_called` contains exact `mcp__*` names that",
            "Only `unavailable_after_retry` may have no tool calls",
            "requires two genuine ToolSearch attempts",
        ):
            self.assertIn(marker, opus_prompt)
        for marker in (
            "Opus still must attempt all five families",
            "Glean `unavailable_after_retry` requires NEEDS_INTERNAL",
            "exact-child per-family counts",
            "You have no tool catalog",
            'For `routes: ["glean_facet"]`',
            "SUBSTANCE VS FORM",
            "gap_materiality",
        ):
            self.assertIn(marker, codex_prompt)
        self.assertNotIn("tools", codex_config)

        with (
            mock.patch.dict(
                os.environ,
                {
                    "TRIPLE_STAMP_RUN_ID": "test-run",
                    "ISAAC_BIN": sys.executable,
                },
            ),
            mock.patch.object(
                plugin,
                "prepare_opus_mcp_config",
                return_value=Path("/tmp/run/opus-mcp.json"),
            ) as materialize,
            mock.patch.object(
                plugin,
                "configure_opus_startup_environment",
                wraps=plugin.configure_opus_startup_environment,
            ) as configure,
        ):
            command, runtime_args = plugin.IsaacClaudeLauncher().launch(
                "claude",
                [
                    "--model",
                    plugin._OPUS_MODEL,
                    "--append-system-prompt",
                    "arbitrary handoff text with no routing title",
                ],
            )
        self.assertEqual(command, sys.executable)
        self.assertEqual(
            runtime_args[runtime_args.index("--mcp-config") + 1],
            "/tmp/run/opus-mcp.json",
        )
        self.assertIn("--strict-mcp-config", runtime_args)
        materialize.assert_called_once_with()
        configure.assert_called_once_with()

    def test_runtime_opus_launch_performs_no_health_or_readiness_gate(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {
                    "TRIPLE_STAMP_RUN_ID": "test-run",
                    "ISAAC_BIN": sys.executable,
                },
            ),
            mock.patch.object(
                plugin,
                "prepare_opus_mcp_config",
                return_value=Path("/tmp/run/opus-mcp.json"),
            ) as materialize,
        ):
            command, runtime_args = plugin.IsaacClaudeLauncher().launch(
                "claude",
                ["--model", plugin._OPUS_MODEL, "--effort", "max"],
            )
        self.assertEqual(command, sys.executable)
        materialize.assert_called_once_with()
        self.assertIn("--strict-mcp-config", runtime_args)

    def test_completed_opus_handoff_includes_observed_internal_mcp_coverage(
        self,
    ) -> None:
        fixture = json.loads(
            (ROOT / "tests/fixtures/run-23agmzty-regressions.json").read_text(
                encoding="utf-8"
            )
        )

        class Response:
            def __init__(self, data: list[dict[str, object]]) -> None:
                self.data = data

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {"data": self.data}

        class Client:
            def __init__(self, data: list[dict[str, object]]) -> None:
                self.data = data
                self.paths: list[str] = []

            async def get(self, path: str, **_kwargs: object) -> Response:
                self.paths.append(path)
                return Response(self.data)

        for cycle in fixture["opus_cycles"]:
            items = [
                {
                    "id": f"ts-{index}",
                    "type": "function_call",
                    "call_id": f"ts-{index}",
                    "name": "ToolSearch",
                }
                for index in range(cycle["toolsearch_count"])
            ]
            for call_id, name in cycle["calls"]:
                items.extend(
                    [
                        {
                            "id": f"item-{call_id}",
                            "type": "function_call",
                            "call_id": call_id,
                            "name": name,
                        },
                        {
                            "id": f"result-{call_id}",
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": "sanitized retained result",
                        },
                    ]
                )
            # Copied stores can expose a duplicate event across page boundaries.
            items.append(dict(items[cycle["toolsearch_count"]]))
            client = Client(items)
            observed = asyncio.run(
                runtime_state.observe_opus_internal_mcp_calls(
                    client,
                    cycle["child_session_id"],
                    title=cycle["title"],
                )
            )
            self.assertTrue(observed["available"])
            self.assertEqual(observed["status"], "observed")
            self.assertEqual(observed["count"], len(cycle["calls"]))
            self.assertEqual(observed["by_system"], cycle["expected_by_system"])
            self.assertEqual(observed["toolsearch_count"], cycle["toolsearch_count"])
            self.assertTrue(all(call["result_observed"] for call in observed["calls"]))
            self.assertEqual(
                client.paths,
                [f"/v1/sessions/{cycle['child_session_id']}/items"],
            )

    def test_opus_observer_not_observed_never_claims_zero_calls(self) -> None:
        class RaisingClient:
            async def get(self, *_args: object, **_kwargs: object) -> object:
                raise RuntimeError("sanitized API outage")

        payload = {
            "child_session_id": "opus-child",
            "agent": "opus_auditor",
            "title": "audit-cycle-1",
            "status": "completed",
            "output": _audit(),
        }
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ, {"TRIPLE_STAMP_RUN_DIR": value}, clear=False
        ):
            observed = asyncio.run(
                runtime_state.add_opus_internal_mcp_observation(
                    payload,
                    server_client=RaisingClient(),
                )
            )
            error_rows = (
                Path(value) / "observer-errors.jsonl"
            ).read_text(encoding="utf-8")
        observation = observed["internal_mcp_observation"]
        self.assertFalse(observation["available"])
        self.assertEqual(observation["status"], "not_observed")
        self.assertIsNone(observation["count"])
        self.assertIn("not_observed", observed["output"])
        self.assertNotIn("calls=0", observed["output"])
        self.assertIn("RuntimeError", error_rows)

    def test_attested_stamp_answer_relays_without_byte_exact_supervisor_echo(
        self,
    ) -> None:
        """run-n0zgj9tp stamped and then failed because of this.

        Codex STAMPed, the answer was durably attested, and the supervisor was
        still required to retype 7351 bytes verbatim. It missed twice and the run
        ended in PIPELINE_INFRASTRUCTURE_ERROR with a valid answer on disk. The
        runtime must be able to relay the attested bytes itself.
        """

        digest = "a" * 64
        answer = "Ship this exact answer.\n\nWith a second paragraph."
        stamp = json.dumps(
            {
                "verdict": "STAMP",
                "needs_web": False,
                "needs_internal": False,
                "gap_materiality": "none",
                "limitations": [],
                "why": "fixture evidence holds",
                "citations_that_hold": ["official source"],
                "voice_profile_check": {
                    "source_path": _VOICE_PROFILE_PATH,
                    "sha256": digest,
                },
                "shippable_answer": answer,
            }
        )
        records = [
            _route_packet("cursor_workhorse", "cursor-cycle-1", "evidence"),
            _route_packet("opus_auditor", "audit-cycle-1", _audit("PASS")),
            _route_packet("codex_judge", "judge-cycle-1", stamp),
        ]
        with mock.patch.dict(
            os.environ,
            _voice_env(digest),
            clear=False,
        ):
            route = plugin._next_route(records)
            self.assertEqual(route.status, "success")

            # The bytes are recoverable under exactly the gates the equality
            # check applies, so a near-miss echo no longer has to be fatal.
            self.assertEqual(
                supervisor_runtime._attested_stamp_answer(route, records), answer
            )
            self.assertTrue(
                supervisor_runtime._terminal_response_allowed(
                    route, answer, records
                )
            )
            self.assertFalse(
                supervisor_runtime._terminal_response_allowed(
                    route, answer + " ", records
                )
            )

            # An incomplete stage chain must still yield nothing to relay.
            self.assertIsNone(
                supervisor_runtime._attested_stamp_answer(route, records[-1:])
            )

            # A non-success route must never relay an answer.
            rework = [
                *records[:-1],
                _route_packet(
                    "codex_judge", "judge-cycle-1", _judgment("REWORK")
                ),
            ]
            self.assertIsNone(
                supervisor_runtime._attested_stamp_answer(
                    plugin._next_route(rework), rework
                )
            )

    def test_format_repair_title_mirrors_router_suffix_rule(self) -> None:
        for title, expected in (
            ("audit-cycle-1", "audit-format-repair-1"),
            ("audit-cycle-3-web-2", "audit-format-repair-3-web-2"),
            ("audit-internal-2-1", "audit-format-repair-2"),
            # A repair that is still invalid is terminal, not another repair.
            ("audit-format-repair-1", ""),
            ("audit-format-repair-1-web-1", ""),
            ("judge-cycle-1", ""),
            ("", ""),
        ):
            with self.subTest(title=title):
                self.assertEqual(
                    runtime_state._format_repair_title(title), expected
                )

    def test_mechanically_invalid_audit_names_its_required_repair_dispatch(
        self,
    ) -> None:
        """run-r477r1_k deadlocked because the supervisor could not see this.

        The router required `audit-format-repair-1`, the supervisor read
        `"verdict": "FAIL"` and dispatched `judge-cycle-1`, and the resulting
        Codex STAMP was discarded for want of a valid chain.
        """

        class RaisingClient:
            async def get(self, *_args: object, **_kwargs: object) -> object:
                raise RuntimeError("sanitized API outage")

        contradictory = json.loads(_audit("FAIL"))
        contradictory["needs_web"] = True
        contradictory["web_queries"] = [_web_hunt()]
        marker = "[System-required next dispatch:"

        def observe(output: str, title: str = "audit-cycle-1") -> str:
            payload = {
                "child_session_id": "opus-child",
                "agent": "opus_auditor",
                "title": title,
                "status": "completed",
                "output": output,
            }
            with tempfile.TemporaryDirectory() as value, mock.patch.dict(
                os.environ, {"TRIPLE_STAMP_RUN_DIR": value}, clear=False
            ):
                return asyncio.run(
                    runtime_state.add_opus_internal_mcp_observation(
                        payload, server_client=RaisingClient()
                    )
                )["output"]

        # The invalid audit still parses to verdict FAIL for a prose reader, so
        # the routing instruction has to be explicit rather than inferable.
        invalid = observe(json.dumps(contradictory))
        self.assertIsNone(plugin._valid_audit(json.dumps(contradictory)))
        self.assertIn(marker, invalid)
        self.assertIn("audit-format-repair-1", invalid)
        self.assertIn("FORMAT REPAIR ONLY", invalid)
        self.assertIn("Do not dispatch codex_judge", invalid)

        # A valid audit must never be routed to a repair, or every cycle would
        # detour through a repair round.
        valid = observe(_audit())
        self.assertNotIn("audit-format-repair-1", valid)
        self.assertNotIn("FORMAT REPAIR ONLY", valid)

        # A repair that is still invalid is terminal, so it must not ask for
        # another repair.
        self.assertNotIn(
            marker,
            observe(json.dumps(contradictory), "audit-format-repair-1"),
        )

    def test_valid_audit_names_judge_over_its_own_cursor_punch_list(self) -> None:
        """run-4gqa_fil died 19 minutes in because this line was absent.

        The audit was `mechanically_validated` with 16 observed internal calls
        and read `"verdict": "FAIL"` beside a 12-item `punch_list_for_cursor`.
        The durable route was `judge-cycle-1`; the supervisor dispatched
        `cursor-cycle-2`, and the guard could only end that as a terminal
        infrastructure error. FAIL is Opus stating an opinion, not a route.
        """

        class RaisingClient:
            async def get(self, *_args: object, **_kwargs: object) -> object:
                raise RuntimeError("sanitized API outage")

        def observe(output: str, title: str = "audit-cycle-1") -> str:
            payload = {
                "child_session_id": "opus-child",
                "agent": "opus_auditor",
                "title": title,
                "status": "completed",
                "output": output,
            }
            with tempfile.TemporaryDirectory() as value, mock.patch.dict(
                os.environ, {"TRIPLE_STAMP_RUN_DIR": value}, clear=False
            ):
                return asyncio.run(
                    runtime_state.add_opus_internal_mcp_observation(
                        payload, server_client=RaisingClient()
                    )
                )["output"]

        baited = json.loads(_audit("FAIL"))
        baited["punch_list_for_cursor"] = [
            {
                "gap_type": "public_web",
                "claim": f"claim {index}",
                "required_capability": "cursor_public_web",
                "required_source": "official_docs",
                "requested_proof": "url",
            }
            for index in range(12)
        ]
        self.assertIsNotNone(plugin._valid_audit(json.dumps(baited)))

        for verdict in ("PASS", "PASS_WITH_GAPS", "FAIL"):
            with self.subTest(verdict=verdict):
                baited["verdict"] = verdict
                output = observe(json.dumps(baited))
                self.assertIn("[System-required next dispatch:", output)
                self.assertIn("codex_judge with title judge-cycle-1", output)
                self.assertIn("punch_list_for_cursor", output.rsplit("[", 1)[-1])
                self.assertNotIn("cursor-cycle-2", output)

        # The one audit verdict that genuinely routes away from Codex must name
        # the router's own web-hunt title, never the next ordinary cycle.
        hunting = json.loads(_audit("NEEDS_WEB"))
        hunting["needs_web"] = True
        hunting["web_queries"] = [_web_hunt()]
        hunted = observe(json.dumps(hunting))
        self.assertIn("cursor_workhorse with title cursor-web-opus-1-1", hunted)
        self.assertNotIn("cursor-cycle-2", hunted)

    def test_note_target_is_read_back_from_the_router(self) -> None:
        """The note must quote `_next_route`, not re-derive the rule."""

        record = {
            "agent": "opus_auditor",
            "title": "audit-cycle-1",
            "status": "completed",
            "output": _audit(),
        }
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ, {"TRIPLE_STAMP_RUN_DIR": value}, clear=False
        ):
            route = plugin._next_route([record])
            note = runtime_state._audit_next_dispatch_note(
                "audit-cycle-1", record, audit_unusable=False
            )
        self.assertEqual(route.status, "dispatch")
        self.assertIn(f"{route.agent} with title {route.title}", note)

    def test_retained_run_observer_identity_and_exact_family_counts(self) -> None:
        fixture = json.loads(
            (
                ROOT
                / "tests/fixtures/run-o6khrsg4-six-fixes.json"
            ).read_text(encoding="utf-8")
        )

        class Response:
            def __init__(self, data: list[dict[str, object]]) -> None:
                self._data = data

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {"data": self._data}

        class Client:
            def __init__(self, data: list[dict[str, object]]) -> None:
                self.data = data

            async def get(self, *_args: object, **_kwargs: object) -> Response:
                return Response(self.data)

        aggregate = {system: 0 for system in fixture["expected_aggregate_by_system"]}
        for child in fixture["opus_children"]:
            dispatch = {
                "agent": "opus_auditor",
                "title": child["title"],
                "child_session_id": child["child_session_id"],
                "work_id": child["work_id"],
            }
            enriched = cursor_lifecycle._enrich_packet(
                {
                    "type": "sub_agent",
                    "status": "completed",
                    "work_id": child["work_id"],
                    "output": _audit("PASS"),
                },
                [dispatch],
            )
            restored = cursor_lifecycle._restore_evaluated_packet_identity(
                {
                    "type": "sub_agent",
                    "status": "completed",
                    "output": _audit("PASS"),
                },
                enriched,
            )
            self.assertEqual(
                restored["conversation_id"],
                child["child_session_id"],
            )

            items: list[dict[str, object]] = []
            for system, count in child["by_system"].items():
                tools = fixture["real_tool_names"][system]
                for index in range(count):
                    call_id = f"{child['child_session_id']}-{system}-{index}"
                    items.extend(
                        [
                            {
                                "id": f"item-{call_id}",
                                "type": "function_call",
                                "call_id": call_id,
                                "name": tools[index % len(tools)],
                            },
                            {
                                "id": f"result-{call_id}",
                                "type": "function_call_output",
                                "call_id": call_id,
                                "output": "sanitized retained result",
                            },
                        ]
                    )
            if items:
                items.append(dict(items[0]))
            observation = asyncio.run(
                runtime_state.observe_opus_internal_mcp_calls(
                    Client(items),
                    str(restored["conversation_id"]),
                    title=child["title"],
                )
            )
            self.assertEqual(observation["status"], "observed")
            self.assertEqual(
                observation["child_session_id"],
                child["child_session_id"],
            )
            self.assertNotEqual(observation["status"], "not_observed")
            self.assertEqual(observation["by_system"], child["by_system"])
            self.assertIn(
                child["audit_status"],
                {"mechanically_validated", "form_imperfect"},
            )
            for system, count in observation["by_system"].items():
                aggregate[system] += count

            effort_observation = {
                "child_session_id": child["child_session_id"],
                "title": child["title"],
                "expected": "max",
                "status": "observed",
                "outcome": "all_max",
                "compliant": True,
                "assistant_rows": child["assistant_rows"],
                "values": ["max"],
                "claude_session_id": "retained-claude",
                "transcript_ref": "sanitized.jsonl",
                "reason": "",
            }
            with mock.patch.object(
                runtime_state,
                "observe_opus_effort",
                return_value=effort_observation,
            ) as effort:
                effort_packet = runtime_state.add_opus_effort_observation(
                    restored
                )
            effort.assert_called_once_with(
                child["child_session_id"],
                title=child["title"],
            )
            self.assertEqual(
                effort_packet["opus_effort_observation"]["status"],
                "observed",
            )

        self.assertEqual(aggregate, fixture["expected_aggregate_by_system"])
        self.assertEqual(
            fixture["reported_aggregate_by_system"],
            {
                "glean": 34,
                "safe": 16,
                "confluence": 13,
                "jira": 12,
                "slack": 7,
            },
        )
        self.assertEqual(fixture["audit_tools_called_entries"], 53)
        self.assertEqual(
            (
                fixture["retained_generated_route_call_cap"],
                fixture["pre_fix_checked_in_route_call_cap"],
                fixture["post_fix_default_route_call_cap"],
                fixture["post_fix_deep_route_call_cap"],
            ),
            (188, 160, 84, 160),
        )
        self.assertEqual(fixture["fabricated_names_present_in_audits"], [])
        self.assertTrue(fixture["substantive_evidence"]["direct_go_links_read"])
        self.assertTrue(
            fixture["substantive_evidence"]["faq_contradiction_found_by_opus"]
        )
        missing_identity = cursor_lifecycle._restore_evaluated_packet_identity(
            {
                "agent": "opus_auditor",
                "title": "audit-cycle-1",
                "status": "completed",
                "output": _audit("PASS"),
            },
            {
                "agent": "opus_auditor",
                "title": "audit-cycle-1",
                "status": "completed",
            },
        )
        self.assertEqual(missing_identity["status"], "failed")
        self.assertIn(
            "lacked the child identity",
            missing_identity["output"],
        )

    def test_audit_tool_names_are_mechanically_validated_or_advisory(self) -> None:
        coverage = json.loads(_audit("PASS"))
        for system, tool in (
            ("glean", "mcp__glean__search"),
            ("jira", "mcp__jira__jira_read_api_call"),
            ("slack", "mcp__slack__slack_read_api_call"),
            ("confluence", "mcp__confluence__search_confluence_pages"),
            ("safe", "mcp__safe__safe_read_api_call"),
        ):
            coverage["internal_coverage"][system].update(
                status="evidence_found",
                routes=["native"],
                tools_called=[tool],
                results_seen=1,
                note="observed exact-child call",
            )
        calls = [
            {
                "call_id": f"call-{system}",
                "name": entry["tools_called"][0],
                "system": system,
                "result_observed": True,
            }
            for system, entry in coverage["internal_coverage"].items()
        ]
        observation = {
            "available": True,
            "status": "observed",
            "calls": calls,
            "by_system": {system: 1 for system in coverage["internal_coverage"]},
        }
        valid = plugin._valid_audit(json.dumps(coverage), observation)
        self.assertEqual(
            valid["audit_validation"]["status"],
            "mechanically_validated",
        )

        fabricated = (
            ("slack", "mcp__slack__search_messages"),
            ("confluence", "mcp__confluence__search"),
            ("safe", "mcp__safe__list_flags"),
        )
        for system, tool in fabricated:
            with self.subTest(system=system, tool=tool):
                candidate = json.loads(json.dumps(coverage))
                candidate["internal_coverage"][system]["tools_called"] = [tool]
                invalid_payload, invalid_validation = (
                    plugin._audit_payload_and_validation(
                        json.dumps(candidate),
                        observation,
                    )
                )
                self.assertIsNotNone(invalid_payload)
                self.assertEqual(
                    invalid_validation["status"],
                    "invalid_tool_claims",
                )
                self.assertIsNone(
                    plugin._valid_audit(
                        json.dumps(candidate),
                        observation,
                    )
                )
        rejected_route = plugin._next_route(
            [
                _route_packet(
                    "cursor_workhorse",
                    "cursor-cycle-1",
                    "CURSOR",
                ),
                {
                    **_route_packet(
                        "opus_auditor",
                        "audit-cycle-1",
                        json.dumps(candidate),
                    ),
                    "internal_mcp_observation": observation,
                },
            ]
        )
        self.assertEqual(
            (rejected_route.agent, rejected_route.title),
            ("opus_auditor", "audit-internal-1-1"),
        )

        advisory_candidate = json.loads(json.dumps(coverage))
        advisory_candidate["internal_coverage"]["slack"]["tools_called"] = [
            "mcp__slack__search_messages"
        ]
        advisory = plugin._valid_audit(
            json.dumps(advisory_candidate),
            None,
        )
        self.assertIsNotNone(advisory)
        self.assertFalse(
            advisory["audit_validation"]["objective_observation_available"]
        )
        self.assertFalse(advisory["audit_validation"]["tool_claims_validated"])

    def test_opus_effort_observation_all_max_mixed_and_low(self) -> None:
        cases = (
            (["max", "max"], "all_max", True, ["max"]),
            (["max", "low", "max"], "non_max", False, ["max", "low"]),
            (["low"], "non_max", False, ["low"]),
        )
        for efforts, outcome, compliant, values in cases:
            with self.subTest(efforts=efforts):
                packet = _opus_effort_fixture(efforts)
                observation = packet["opus_effort_observation"]
                self.assertEqual(observation["status"], "observed")
                self.assertEqual(observation["outcome"], outcome)
                self.assertIs(observation["compliant"], compliant)
                self.assertEqual(observation["expected"], "max")
                self.assertEqual(observation["values"], values)
                self.assertEqual(observation["assistant_rows"], len(efforts))
                self.assertIn(
                    f"compliant={'true' if compliant else 'false'}",
                    packet["output"],
                )
                self.assertEqual(
                    plugin._valid_audit(packet["output"])["verdict"],
                    "PASS",
                )
                if compliant:
                    self.assertNotIn("must not STAMP", packet["output"])
                else:
                    self.assertIn("must not STAMP", packet["output"])

    def test_opus_effort_missing_unreadable_and_wrong_child_are_advisory(
        self,
    ) -> None:
        cases = (
            ("missing-effort", {"efforts": [None]}),
            ("missing-transcript", {"efforts": ["max"], "missing_file": True}),
            ("unreadable-transcript", {"efforts": ["max"], "unreadable": True}),
            (
                "wrong-child",
                {"efforts": ["low"], "row_session_id": "different-child"},
            ),
            (
                "outside-current-run-root",
                {"efforts": ["low"], "outside_transcript_root": True},
            ),
        )
        for name, kwargs in cases:
            with self.subTest(case=name):
                packet = _opus_effort_fixture(**kwargs)
                observation = packet["opus_effort_observation"]
                self.assertEqual(observation["status"], "not_observed")
                self.assertEqual(observation["outcome"], "not_observed")
                self.assertIsNone(observation["compliant"])
                self.assertEqual(observation["values"], [])
                self.assertTrue(observation["reason"])
                self.assertIn("advisory only", packet["output"])
                self.assertIn("never be treated as low", packet["output"])
                self.assertNotIn("must not STAMP", packet["output"])

    def test_opus_effort_observes_every_stage_for_both_profiles(self) -> None:
        titles = (
            "audit-cycle-1",
            "audit-cycle-1-web-1",
            "audit-internal-1-1",
            "audit-format-repair-1",
            "audit-format-repair-1-web-1",
        )
        for provider in ("direct", "databricks"):
            for title in titles:
                with self.subTest(provider=provider, title=title):
                    packet = _opus_effort_fixture(
                        ["max"],
                        title=title,
                        provider=provider,
                    )
                    observation = packet["opus_effort_observation"]
                    self.assertEqual(observation["title"], title)
                    self.assertEqual(observation["outcome"], "all_max")
                    self.assertTrue(observation["compliant"])

    def test_codex_refuses_only_observed_nonmax_opus_effort(self) -> None:
        prompt = yaml.safe_load(
            (ROOT / "agents/codex_judge/config.yaml").read_text(encoding="utf-8")
        )["prompt"]
        self.assertIn("opus_effort_observation", prompt)
        self.assertIn("Only observed non-max effort blocks STAMP", prompt)
        self.assertIn("missing or unreadable effort evidence must never", prompt)
        supervisor_prompt = yaml.safe_load(
            (ROOT / "config.yaml").read_text(encoding="utf-8")
        )["prompt"]
        self.assertIn(
            "every runtime-appended `opus_effort_observation`",
            supervisor_prompt,
        )

        cursor = _route_packet(
            "cursor_workhorse",
            "cursor-cycle-1",
            "evidence",
        )
        outcomes = (
            (
                {
                    "status": "observed",
                    "outcome": "all_max",
                    "compliant": True,
                },
                True,
            ),
            (
                {
                    "status": "not_observed",
                    "outcome": "not_observed",
                    "compliant": None,
                },
                True,
            ),
            (
                {
                    "status": "observed",
                    "outcome": "non_max",
                    "compliant": False,
                },
                False,
            ),
        )
        for observation, chain_allowed in outcomes:
            with self.subTest(observation=observation):
                audit = {
                    **_route_packet(
                        "opus_auditor",
                        "audit-cycle-1",
                        _audit(),
                    ),
                    "opus_effort_observation": observation,
                }
                self.assertIs(
                    plugin._has_required_stage_chain([cursor, audit], 1),
                    chain_allowed,
                )

        stamp = {
            "agent": "codex_judge",
            "child_session_id": "codex-child",
            "work_id": "codex-work",
            "title": "judge-cycle-1",
            "output": json.dumps(
                {
                    "verdict": "STAMP",
                    "needs_web": False,
                    "needs_internal": False,
                    "gap_materiality": "none",
                    "limitations": [],
                    "citations_that_hold": ["file:1"],
                    "voice_profile_check": {
                        "source_path": _VOICE_PROFILE_PATH,
                        "sha256": "profile-digest",
                    },
                    "shippable_answer": "answer",
                }
            ),
        }
        low_audit = {
            **_route_packet("opus_auditor", "audit-cycle-1", _audit()),
            "opus_effort_observation": {
                "status": "observed",
                "outcome": "non_max",
                "compliant": False,
            },
        }
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {
                "TRIPLE_STAMP_RUN_DIR": value,
                "TRIPLE_STAMP_VOICE_PROFILE": _VOICE_PROFILE_PATH,
                "TRIPLE_STAMP_VOICE_PROFILE_SHA256": "profile-digest",
            },
            clear=False,
        ):
            runtime_state.append_collection(cursor)
            runtime_state.append_collection(low_audit)
            self.assertFalse(runtime_state.attest_codex_stamp(stamp))
            self.assertFalse((Path(value) / "stamp-attestation.json").exists())

    def test_opus_observer_has_no_runner_local_conversation_store(self) -> None:
        source = (
            ISAAC_LAUNCHER / "triple_stamp_runtime_state.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("get_conversation_store", source)
        lifecycle_source = (
            RUNTIME_PYTHON / "triple_stamp_cursor_lifecycle.py"
        ).read_text(encoding="utf-8")
        self.assertIn("else add_opus_effort_observation(", lifecycle_source)
        self.assertIn(
            'if "opus_effort_observation" in evaluated_payload:',
            lifecycle_source,
        )

    def test_tool_failure_can_be_a_valid_fail_audit(self) -> None:
        output = _audit("FAIL", attack="internal MCP call failed: unavailable")
        self.assertEqual(plugin._valid_audit(output)["verdict"], "FAIL")

    def test_audit_requires_internal_source_records_or_explicit_reason(self) -> None:
        missing_contract = json.loads(_audit())
        missing_contract.pop("internal_sources_consulted")
        missing_contract.pop("internal_sources_not_required_reason")
        missing = plugin._valid_audit(json.dumps(missing_contract))
        self.assertEqual(
            missing["audit_validation"]["status"],
            "form_imperfect",
        )

        empty_without_reason = json.loads(_audit())
        empty_without_reason["internal_sources_not_required_reason"] = ""
        empty = plugin._valid_audit(json.dumps(empty_without_reason))
        self.assertEqual(
            empty["audit_validation"]["status"],
            "form_imperfect",
        )

        sourced = json.loads(_audit())
        sourced["internal_sources_consulted"] = [
            {
                "system": "glean",
                "url_or_record_id": "https://glean.example/doc/1",
                "exact_quote_or_concise_evidence": "Roadmap status is preview.",
                "retrieval_timestamp": "2026-09-10T22:00:00Z",
            }
        ]
        sourced["internal_sources_not_required_reason"] = ""
        self.assertEqual(plugin._valid_audit(json.dumps(sourced))["verdict"], "PASS")

    def test_retained_opus_transcript_requires_one_format_repair(self) -> None:
        malformed = (
            "I'll audit this rather than take the packet's word for anything. "
            "First, let me look for ground truth I can actually reach without "
            "the public web.\n\n`\u00ad`\n\n**Tool Use: Bash**\n```json\n{\n"
            '  "command": "which -a cursor-agent cursor 2>/dev/null; echo '
            '\\"---VERSION---\\"; cursor-agent --version 2>&1 | head -20; echo '
            '\\"---LSDIRS---\\"; ls -la ~/.local/bin 2>/dev/null | head -30; '
            'ls -la ~/.cursor 2>/dev/null | head -30",\n'
            '  "description": "Check for locally installed cursor-agent CLI"\n'
            "}\n```\n\n**Tool Result:**\n```\n---VERSION---\n"
            "zsh:1: command not found: cursor-agent\n---LSDIRS---\n```"
        )
        self.assertIsNone(plugin._valid_audit(malformed))
        parent = "parent_audit_repair"

        def dispatch(agent: str, title: str, handoff: str) -> str:
            return plugin.supervisor_contract(enabled=True)(
                {
                    "type": "tool_call",
                    "data": {
                        "name": "sys_session_send",
                        "arguments": {"agent": agent, "title": title, "args": handoff},
                    },
                }
            )["result"]

        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value},
            clear=False,
        ):
            cursor = {
                "parent_session_id": parent,
                "child_session_id": "cursor-child",
                "work_id": "cursor-work",
                "agent": "cursor_workhorse",
                "title": "cursor-cycle-1",
                "status": "completed",
                "output": "CURSOR PACKET",
            }
            audit = {
                "parent_session_id": parent,
                "child_session_id": "audit-child",
                "work_id": "audit-work",
                "agent": "opus_auditor",
                "title": "audit-cycle-1",
                "status": "completed",
                "output": malformed,
            }
            runtime_state.append_dispatch(cursor)
            runtime_state.append_collection(cursor)
            runtime_state.append_dispatch(audit)
            runtime_state.append_collection(audit)
            self.assertEqual(
                dispatch(
                    "opus_auditor",
                    "audit-format-repair-1",
                    f"FORMAT REPAIR ONLY\n{malformed}",
                ),
                "ALLOW",
            )
            repaired = {
                "parent_session_id": parent,
                "child_session_id": "repair-child",
                "work_id": "repair-work",
                "agent": "opus_auditor",
                "title": "audit-format-repair-1",
                "status": "completed",
                "output": _audit("FAIL", attack="malformed audit normalized"),
            }
            runtime_state.append_dispatch(repaired)
            runtime_state.append_collection(repaired)
            self.assertEqual(
                dispatch(
                    "codex_judge",
                    "judge-cycle-1",
                    "CURSOR PACKET\n" + repaired["output"],
                ),
                "ALLOW",
            )
            self.assertEqual(
                dispatch(
                    "opus_auditor",
                    "audit-format-repair-1",
                    repaired["output"],
                ),
                "ALLOW",
            )

    def test_invalid_format_repair_is_terminal_infrastructure_failure(self) -> None:
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value},
            clear=False,
        ), mock.patch.object(plugin, "_completion_records", return_value=[]):
            repair = {
                "parent_session_id": "parent",
                "child_session_id": "repair-child",
                "work_id": "repair-work",
                "agent": "opus_auditor",
                "title": "audit-format-repair-1",
                "status": "completed",
                "output": "still not a verdict",
            }
            runtime_state.append_collection(repair)
            decision = plugin.supervisor_contract(enabled=True)(
                {
                    "type": "response",
                    "context": {
                        "conversation_id": "parent",
                        "root_conversation_id": "parent",
                    },
                    "data": (
                        "PIPELINE_INFRASTRUCTURE_ERROR: "
                        "audit-format-repair-1 returned an invalid contract"
                    ),
                }
            )
            self.assertEqual(decision["result"], "ALLOW")

    def test_durable_route_survives_policy_reloads_and_bounds_rework(self) -> None:
        def decision(agent: str, title: str, handoff: str) -> str:
            # Model each SDK turn rebuilding the policy callable.
            return plugin.supervisor_contract(enabled=True)(
                {
                    "type": "tool_call",
                    "data": {
                        "name": "sys_session_send",
                        "arguments": {
                            "agent": agent,
                            "title": title,
                            "args": handoff,
                        },
                    },
                }
            )["result"]

        def consume(agent: str, title: str, work: str, output: str) -> None:
            record = {
                "parent_session_id": "parent_route",
                "child_session_id": f"child_{work}",
                "work_id": work,
                "agent": agent,
                "title": title,
                "status": "completed",
                "output": output,
            }
            runtime_state.append_dispatch(record)
            runtime_state.append_collection(record)

        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {
                "TRIPLE_STAMP_RUN_DIR": value,
                "TRIPLE_STAMP_SANDBOX_TOKEN": "unit-test-secret",
            },
            clear=False,
        ):
            self.assertEqual(
                decision("cursor_workhorse", "cursor-cycle-1", "request"), "ALLOW"
            )
            consume("cursor_workhorse", "cursor-cycle-1", "c1", "CURSOR-1")
            self.assertEqual(
                decision("opus_auditor", "audit-cycle-1", "CURSOR-1"), "ALLOW"
            )
            opus_1 = _audit()
            consume("opus_auditor", "audit-cycle-1", "a1", opus_1)
            self.assertEqual(
                decision(
                    "codex_judge",
                    "judge-cycle-1",
                    f"CURSOR-1\n{opus_1}",
                ),
                "ALLOW",
            )
            rework_1 = _judgment("REWORK")
            consume("codex_judge", "judge-cycle-1", "j1", rework_1)
            self.assertEqual(
                decision("cursor_workhorse", "cursor-cycle-2", rework_1),
                "ALLOW",
            )
            consume("cursor_workhorse", "cursor-cycle-2", "c2", "CURSOR-2")
            self.assertEqual(
                decision("opus_auditor", "audit-cycle-2", "CURSOR-2"), "ALLOW"
            )
            needs_web = _audit("NEEDS_WEB")
            consume("opus_auditor", "audit-cycle-2", "a2", needs_web)
            self.assertEqual(
                decision(
                    "cursor_workhorse",
                    "cursor-web-opus-2-1",
                    f"CURSOR-2\n{needs_web}",
                ),
                "ALLOW",
            )
            consume(
                "cursor_workhorse",
                "cursor-web-opus-2-1",
                "c2web",
                "WEB-PACKET",
            )
            self.assertEqual(
                decision(
                    "opus_auditor",
                    "audit-cycle-2-web-1",
                    f"ORIGINAL\nCURSOR-2\n{needs_web}\nWEB-PACKET",
                ),
                "ALLOW",
            )
            opus_2 = _audit("PASS_WITH_GAPS")
            consume(
                "opus_auditor",
                "audit-cycle-2-web-1",
                "a2fresh",
                opus_2,
            )
            self.assertEqual(
                decision("cursor_workhorse", "cursor-cycle-2", opus_2), "ALLOW"
            )

    def test_unpriced_usage_gets_conservative_noninteractive_budget(self) -> None:
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value},
            clear=False,
        ):
            budget = plugin.strict_cost_budget(max_cost_usd=50.0)
            under = budget(
                {
                    "type": "tool_call",
                    "context": {
                        "usage": {
                            "input_tokens": 100_000,
                            "output_tokens": 1_000,
                            "total_tokens": 101_000,
                        }
                    },
                }
            )
            self.assertEqual(under["result"], "ALLOW")
            self.assertNotEqual(under["result"], "ASK")
            over = budget(
                {
                    "type": "tool_call",
                    "context": {
                        "usage": {
                            "by_model": {
                                "system.ai.claude-opus-5[1m]": {
                                    "input_tokens": 2_000_000,
                                    "output_tokens": 0,
                                }
                            }
                        }
                    }
                }
            )
            self.assertEqual(over["result"], "DENY")

    def test_budget_boundary_at_fifty_dollars(self) -> None:
        for cost, expected in (
            (49.999999, "ALLOW"),
            (50.0, "DENY"),
            (50.000001, "DENY"),
        ):
            with self.subTest(cost=cost), tempfile.TemporaryDirectory() as value, mock.patch.dict(
                os.environ,
                {"TRIPLE_STAMP_RUN_DIR": value},
                clear=False,
            ):
                budget = plugin.strict_cost_budget(max_cost_usd=50.0)
                decision = budget(
                    {
                        "type": "tool_call",
                        "context": {"usage": {"total_cost_usd": cost}},
                    }
                )
                self.assertEqual(decision["result"], expected)

    def test_run_qcani_fixture_proves_synthetic_cap_and_real_spend(self) -> None:
        from types import SimpleNamespace

        fixture = json.loads(
            (ROOT / "tests/fixtures/run-qcani6mk-cost.json").read_text(
                encoding="utf-8"
            )
        )
        conversations = [
            SimpleNamespace(
                id=row["id"],
                sub_agent_name=row["agent"],
                session_usage=row["session_usage"],
                model_override=None,
            )
            for row in fixture["database_sessions"]
        ]
        snapshot = plugin._cost_snapshot_from_conversations(conversations)
        self.assertAlmostEqual(snapshot["reported_usd"], 0.0890496)
        self.assertEqual(snapshot["estimated_unpriced_usd"], 0.0)
        self.assertTrue(fixture["terminal_artifact"]["created_before_supervisor"])
        self.assertEqual(fixture["terminal_artifact"]["attested_cost_usd"], 50.0)
        self.assertFalse(
            fixture["budget_state_after_real_policy_evaluation"]["denied"]
        )
        raw = fixture["late_cursor_usage"]
        estimate = plugin._estimate_unpriced_bucket(
            raw["model"],
            {
                "input_tokens": raw["noncache_input_tokens"],
                "output_tokens": raw["output_tokens"],
                "cache_read_input_tokens": raw["cache_read_tokens"],
                "cache_creation_input_tokens": raw["cache_write_tokens"],
            },
        )
        self.assertAlmostEqual(estimate, raw["conservative_estimated_usd"])

    def test_canonical_cost_dedupes_child_and_ignores_propagated_policy_cost(
        self,
    ) -> None:
        from types import SimpleNamespace

        root = SimpleNamespace(
            id="root",
            sub_agent_name=None,
            model_override=None,
            session_usage={
                "total_cost_usd": 1.0,
                "policy_cost_usd": 3.0,
                "by_model": {"sonnet": {"total_cost_usd": 1.0}},
            },
        )
        child = SimpleNamespace(
            id="child",
            sub_agent_name="opus_auditor",
            model_override=None,
            session_usage={
                "total_cost_usd": 2.0,
                "by_model": {"opus": {"total_cost_usd": 2.0}},
            },
        )
        snapshot = plugin._cost_snapshot_from_conversations([root, child, child])
        self.assertEqual(snapshot["reported_usd"], 3.0)
        self.assertEqual(snapshot["estimated_unpriced_usd"], 0.0)
        self.assertEqual(len(snapshot["sessions"]), 2)
        self.assertEqual(snapshot["sessions"][0]["policy_cost_usd"], 3.0)

    def test_unpriced_codex_tokens_receive_cache_aware_estimate(self) -> None:
        from types import SimpleNamespace

        codex = SimpleNamespace(
            id="codex",
            sub_agent_name="codex_judge",
            model_override="gpt-5.6-sol",
            session_usage={
                "by_model": {
                    "gpt-5.6-sol": {
                        "input_tokens": 100_000,
                        "output_tokens": 10_000,
                        "cache_read_input_tokens": 50_000,
                    }
                }
            },
        )
        snapshot = plugin._cost_snapshot_from_conversations([codex])
        self.assertEqual(snapshot["reported_usd"], 0.0)
        self.assertAlmostEqual(snapshot["estimated_unpriced_usd"], 4.95)

    def test_predispatch_reserve_denies_before_doomed_stage(self) -> None:
        event = {
            "type": "tool_call",
            "data": {
                "name": "sys_session_send",
                "arguments": {
                    "agent": "cursor_workhorse",
                    "title": "cursor-cycle-1",
                    "args": {"input": "bounded request"},
                },
            },
        }
        snapshot = {
            "reported_usd": 48.0,
            "estimated_unpriced_usd": 0.0,
            "total_usd": 48.0,
            "sessions": [],
            "source_error": "",
        }
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value},
            clear=False,
        ), mock.patch.object(plugin, "_stored_cost_snapshot", return_value=snapshot):
            decision = plugin.strict_cost_budget(50.0)(event)
            self.assertEqual(decision["result"], "DENY")
            self.assertIn("denied before cursor-cycle-1", decision["reason"])
            state = json.loads(
                (Path(value) / "budget-state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["reported_usd"], 48.0)
            self.assertGreater(state["projected_reserve_usd"], 2.0)
            self.assertEqual(state["remaining_usd"], 2.0)

    def test_runner_budget_unavailable_abstains_without_false_infinity(self) -> None:
        source = (
            ISAAC_LAUNCHER / "triple_stamp_isaac_launcher.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn('float("inf")', source)
        unavailable = {
            "available": False,
            "source": "unavailable",
            "source_error": "RuntimeError",
            "reported_usd": 0.0,
            "estimated_unpriced_usd": 0.0,
            "total_usd": 0.0,
            "sessions": [],
        }
        event = {
            "type": "tool_call",
            "data": {
                "name": "sys_session_send",
                "arguments": {
                    "agent": "cursor_workhorse",
                    "title": "cursor-cycle-1",
                },
            },
        }
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ, {"TRIPLE_STAMP_RUN_DIR": value}, clear=False
        ), mock.patch.object(
            plugin, "_stored_cost_snapshot", return_value=unavailable
        ):
            decision = plugin.strict_cost_budget(50.0)(event)
            self.assertEqual(decision, {"result": "ALLOW"})
            self.assertEqual(list(Path(value).iterdir()), [])

    def test_terminal_failure_recovers_partial_cost_without_fabricating_total(
        self,
    ) -> None:
        import sqlite3

        from omnigent.db.compression import encode

        with tempfile.TemporaryDirectory() as value:
            run = Path(value)
            database = run / "tmp/ap-chat-data-fixture/chat.db"
            database.parent.mkdir(parents=True)
            connection = sqlite3.connect(database)
            try:
                connection.executescript(
                    """
                    CREATE TABLE conversations (
                        id BLOB NOT NULL,
                        workspace_id INTEGER NOT NULL,
                        created_at INTEGER NOT NULL,
                        title TEXT NOT NULL,
                        session_overrides TEXT
                    );
                    CREATE TABLE omnigent_conversation_metadata (
                        id BLOB NOT NULL,
                        workspace_id INTEGER NOT NULL,
                        sub_agent_name TEXT,
                        session_usage BLOB
                    );
                    """
                )
                rows = [
                    (
                        bytes.fromhex("01" * 16),
                        0,
                        1,
                        "parent",
                        "{}",
                        None,
                        {
                            "total_cost_usd": 1.0,
                            "by_model": {
                                "claude-sonnet-4-6": {
                                    "total_cost_usd": 1.0
                                }
                            },
                        },
                    ),
                    (
                        bytes.fromhex("02" * 16),
                        0,
                        2,
                        "cursor-cycle-1",
                        '{"reported_model":"grok-4.6"}',
                        "cursor_workhorse",
                        {
                            "by_model": {
                                "cursor-grok-4.6-xhigh": {
                                    "input_tokens": 1_000_000
                                }
                            }
                        },
                    ),
                    (
                        bytes.fromhex("03" * 16),
                        0,
                        3,
                        "cursor-cycle-2",
                        '{"reported_model":"grok-4.6"}',
                        "cursor_workhorse",
                        None,
                    ),
                ]
                for (
                    session_id,
                    workspace_id,
                    created_at,
                    title,
                    overrides,
                    agent,
                    usage,
                ) in rows:
                    connection.execute(
                        "INSERT INTO conversations VALUES (?, ?, ?, ?, ?)",
                        (
                            session_id,
                            workspace_id,
                            created_at,
                            title,
                            overrides,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO omnigent_conversation_metadata VALUES (?, ?, ?, ?)",
                        (
                            session_id,
                            workspace_id,
                            agent,
                            encode(json.dumps(usage)) if usage is not None else None,
                        ),
                    )
                connection.commit()
            finally:
                connection.close()
            recovered = launcher._recover_budget_observation(run)
            self.assertEqual(recovered["cost_observation_status"], "partial")
            self.assertEqual(
                recovered["unavailable_cost_sessions"],
                ["cursor-cycle-2"],
            )
            self.assertEqual(recovered["reported_usd"], 1.0)
            self.assertEqual(recovered["estimated_unpriced_usd"], 37.5)
            self.assertEqual(recovered["cost_usd"], 38.5)

    def test_terminal_failure_reports_pending_dispatch_not_last_collection(
        self,
    ) -> None:
        fixture = json.loads(
            (
                ROOT
                / "tests/fixtures/run-00jfpj73-databricks-timeout.json"
            ).read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as value:
            run = Path(value)
            judge = next(
                row
                for row in fixture["timeline"]
                if row["title"] == "judge-cycle-2"
            )
            cursor = next(
                row
                for row in fixture["timeline"]
                if row["title"] == "cursor-cycle-3"
            )
            (run / "routing-collections.jsonl").write_text(
                json.dumps(
                    {
                        "agent": judge["agent"],
                        "title": judge["title"],
                        "status": "completed",
                        "output": _judgment("REWORK"),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (run / "routing-dispatches.jsonl").write_text(
                json.dumps(
                    {
                        "agent": cursor["agent"],
                        "title": cursor["title"],
                        "child_session_id": cursor["child_session_id"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(
                launcher,
                "_recover_budget_observation",
                return_value={},
            ):
                failure = launcher._ensure_terminal_failure(run, 0)
            self.assertIn("stage cursor-cycle-3, cycle 3", failure)
            self.assertIn(
                "cursor-cycle-3 in flight after collecting judge-cycle-2",
                failure,
            )

    def test_static_budget_validation_cannot_poison_live_terminal_artifacts(self) -> None:
        synthetic = {
            "available": True,
            "source": "event_usage",
            "source_error": "",
            "reported_usd": 60.0,
            "estimated_unpriced_usd": 0.0,
            "total_usd": 60.0,
            "sessions": [{"session_id": "static-policy-check"}],
        }
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ, {"TRIPLE_STAMP_RUN_DIR": value}, clear=False
        ), mock.patch.object(
            plugin, "_stored_cost_snapshot", return_value=synthetic
        ):
            decision = plugin.strict_cost_budget(50.0)({"type": "request"})
            self.assertEqual(decision["result"], "DENY")
            for name in (
                "budget-state.json",
                "terminal-failure.txt",
                "failure-attestation.json",
            ):
                self.assertFalse((Path(value) / name).exists())

    def test_cursor_home_excludes_cross_provider_skill_trees(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            run_dir = Path(value)
            (run_dir / "home/.claude/skills").mkdir(parents=True)
            (run_dir / "home/.codex/skills").mkdir(parents=True)
            harness_tmp = run_dir / "harness"
            harness_tmp.mkdir()
            cursor_home = launcher._seed_cursor_home(run_dir, harness_tmp)
            self.assertEqual(cursor_home, run_dir / "cursor-home")
            self.assertTrue((cursor_home / ".cursor").is_dir())
            self.assertFalse((cursor_home / ".claude").exists())
            self.assertFalse((cursor_home / ".codex").exists())
            self.assertTrue((harness_tmp / "c").is_symlink())
            self.assertEqual((harness_tmp / "c").resolve(), cursor_home.resolve())

    def test_cursor_runtime_uses_short_run_local_data_alias(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            run_dir = Path(value)
            harness_tmp = Path("/tmp") / "ots-502-unit"
            env = launcher._runtime_env(
                root=ROOT,
                real_home=Path("/Users/unit"),
                run_dir=run_dir,
                bundle=run_dir / "bundle",
                run_id="unit-run",
                sandbox_token="sandbox",
                cursor_token="cursor",
                omnigent_token="omnigent",
                harness_tmp=harness_tmp,
                tools=self._fake_toolchain(),
                managed_python=Path("/managed/python"),
                voice_profile_sha256="0" * 64,
                provider="direct",
                models=launcher._provider_models(ROOT, "direct"),
            )
            self.assertEqual(
                env["TRIPLE_STAMP_CURSOR_HOME"],
                str(harness_tmp / "c"),
            )
            self.assertEqual(
                env["CURSOR_DATA_DIR"],
                str(harness_tmp / "c/.cursor"),
            )
            self.assertLessEqual(
                len(str(Path(env["CURSOR_DATA_DIR"]) / "projects")),
                84,
            )
            self.assertIn("CURSOR_DATA_DIR", env["OMNIGENT_RUNNER_ENV_PASSTHROUGH"])
            passthrough = set(env["OMNIGENT_RUNNER_ENV_PASSTHROUGH"].split(","))
            for name in (
                "DISABLE_AUTOUPDATER",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
                "DISABLE_TELEMETRY",
                "DBEXEC_NO_CERT_REFRESH",
            ):
                self.assertIn(name, passthrough)
            for name in (
                "OMNIGENT_CLAUDE_LAUNCHER",
                "ISAAC_BIN",
                "ISAAC_DEFAULT_UCODE",
                "ISAAC_LAUNCH_MODE",
                "ISAAC_DISABLE_MAC_MANAGED_SETTINGS_UPDATE",
            ):
                self.assertNotIn(name, passthrough)
                self.assertNotIn(name, env)
            self.assertEqual(env["TRIPLE_STAMP_PROVIDER"], "direct")
            self.assertEqual(env["DISABLE_AUTOUPDATER"], "1")
            self.assertEqual(
                env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"], "1"
            )
            self.assertEqual(env["DISABLE_TELEMETRY"], "1")

    def test_pipeline_output_reset_removes_validator_carryover(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            run_dir = Path(value)
            for name in launcher._PIPELINE_OUTPUTS:
                (run_dir / name).write_text("validator", encoding="utf-8")
            (run_dir / "packets").mkdir()
            (run_dir / "packets/stale.packet").write_text(
                "validator", encoding="utf-8"
            )
            (run_dir / "cursor-lifecycle").mkdir()
            (run_dir / "cursor-lifecycle/stale.json").write_text(
                "validator", encoding="utf-8"
            )
            (run_dir / "run-id").write_text("keep", encoding="utf-8")
            (run_dir / "opus-mcp.json").write_text("config", encoding="utf-8")
            launcher._reset_pipeline_outputs(run_dir)
            self.assertFalse(
                any((run_dir / name).exists() for name in launcher._PIPELINE_OUTPUTS)
            )
            self.assertFalse((run_dir / "packets").exists())
            self.assertFalse((run_dir / "cursor-lifecycle").exists())
            self.assertEqual(
                (run_dir / "run-id").read_text(encoding="utf-8"), "keep"
            )
            self.assertEqual(
                (run_dir / "opus-mcp.json").read_text(encoding="utf-8"),
                "config",
            )

    def test_context_fixture_and_packet_artifact_track_growth(self) -> None:
        fixture = json.loads(
            (ROOT / "tests/fixtures/run-qcani6mk-cost.json").read_text(
                encoding="utf-8"
            )
        )
        context = fixture["context_material"]
        self.assertGreater(context["cursor_input_tokens"], 400 * context["cursor_handoff_bytes"])
        self.assertGreater(
            context["isolated_home"]["claude_skill_bytes"],
            50_000_000,
        )
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value},
            clear=False,
        ):
            runtime_state.append_collection(
                {
                    "agent": "cursor_workhorse",
                    "title": "cursor-cycle-1",
                    "work_id": "fixture-work",
                    "output": "complete evidence",
                }
            )
            record = runtime_state.read_collections()[0]
            artifact = Path(value) / record["packet_ref"]
            self.assertEqual(artifact.read_text(encoding="utf-8"), "complete evidence")
            self.assertEqual(record["output_bytes"], len(b"complete evidence"))
            self.assertEqual(
                record["output_sha256"],
                hashlib.sha256(b"complete evidence").hexdigest(),
            )

    def test_subagent_completion_is_in_inbox_before_wake(self) -> None:
        parent = "parent_completion_regression"
        child = "child_completion_regression"
        inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        runner_app._session_inboxes_ref[parent] = inbox
        runner_app.register_subagent_work(
            parent_session_id=parent,
            child_session_id=child,
            agent="cursor_workhorse",
            title="cursor-cycle-1",
        )
        try:
            acknowledgement = runner_app.mark_subagent_work_terminal(
                child,
                status="completed",
                output="complete packet",
            )
            self.assertTrue(acknowledgement.delivered_now)
            self.assertEqual(inbox.qsize(), 1)
            self.assertEqual(inbox.get_nowait()["output"], "complete packet")
        finally:
            runner_app.unregister_subagent_work(child)
            runner_app._session_inboxes_ref.pop(parent, None)

    def test_missing_parent_inbox_fails_without_guard(self) -> None:
        parent = "parent_missing_inbox_regression"
        runner_app._session_inboxes_ref.pop(parent, None)
        original = getattr(
            tool_dispatch._execute_subagent_tool,
            "__triple_stamp_original__",
            tool_dispatch._execute_subagent_tool,
        )
        result = asyncio.run(
            original(
                {"agent": "cursor_workhorse", "args": "work", "title": "cursor-cycle-1"},
                server_client=object(),
                conversation_id=parent,
                agent_spec=None,
            )
        )
        self.assertIn("requires parent session inbox", result)

    def test_inbox_read_is_unconditional_after_toolsearch(self) -> None:
        contract = plugin.supervisor_contract(enabled=True)
        read = {
            "type": "tool_call",
            "data": {"name": "sys_read_inbox", "arguments": {}},
        }
        self.assertEqual(contract(read)["result"], "ALLOW")
        self.assertEqual(
            contract(
                {
                    "type": "tool_call",
                    "data": {
                        "name": "ToolSearch",
                        "arguments": {"query": "select:sys_read_inbox"},
                    },
                }
            )["result"],
            "ALLOW",
        )
        self.assertEqual(contract(read)["result"], "ALLOW")

    def test_missing_inbox_failure_is_terminal_and_not_lazy(self) -> None:
        parent = "parent_guarded_inbox_regression"
        runner_app._session_inboxes_ref.pop(parent, None)
        prior_send = tool_dispatch._execute_subagent_tool
        prior_drain = tool_dispatch._drain_inbox
        try:
            # Restore both unwrapped functions so this test proves one clean
            # installation without leaving nested process-global guards.
            tool_dispatch._execute_subagent_tool = getattr(
                prior_send, "__triple_stamp_original__", prior_send
            )
            tool_dispatch._drain_inbox = getattr(
                prior_drain, "__triple_stamp_original__", prior_drain
            )
            runtime_guard._install_parent_inbox_guard()
            guarded = tool_dispatch._execute_subagent_tool
            self.assertTrue(getattr(guarded, "__triple_stamp_inbox_guard__", False))
            result = asyncio.run(
                guarded(
                    {
                        "agent": "cursor_workhorse",
                        "args": "work",
                        "title": "cursor-cycle-1",
                    },
                    server_client=object(),
                    conversation_id=parent,
                    agent_spec=None,
                )
            )
            self.assertIn("PIPELINE_INFRASTRUCTURE_ERROR", result)
            self.assertIn("retry denied", result)
            self.assertNotIn(parent, runner_app._session_inboxes_ref)
            runtime_guard._lifecycle.ensure_parent_inbox(parent)
            retry = asyncio.run(
                guarded(
                    {
                        "agent": "cursor_workhorse",
                        "args": "work",
                        "title": "cursor-cycle-1",
                    },
                    server_client=object(),
                    conversation_id=parent,
                    agent_spec=None,
                )
            )
            self.assertEqual(retry, result)
        finally:
            runtime_guard._lifecycle._parent_inbox_failures.pop(parent, None)
            runner_app._session_inboxes_ref.pop(parent, None)
            tool_dispatch._execute_subagent_tool = prior_send
            tool_dispatch._drain_inbox = prior_drain

    def test_terminal_and_one_shot_parent_inbox_parity(self) -> None:
        for parent in ("parent-terminal", "parent-one-shot"):
            with self.subTest(parent=parent):
                runner_app._session_inboxes_ref.pop(parent, None)
                inbox = runtime_guard._lifecycle.ensure_parent_inbox(parent)
                self.assertIs(runner_app._session_inboxes_ref[parent], inbox)
                result = asyncio.run(
                    tool_dispatch._execute_subagent_tool(
                        {
                            "agent": "cursor_workhorse",
                            "args": "work",
                            "title": "cursor-cycle-1",
                        },
                        server_client=object(),
                        conversation_id=parent,
                        agent_spec=None,
                        session_inbox=inbox,
                    )
                )
                self.assertNotIn("requires parent session inbox", result)
                runner_app._session_inboxes_ref.pop(parent, None)

    def test_empty_inbox_does_not_change_send_policy(self) -> None:
        parent = "parent_one_shot"
        send = {
            "type": "tool_call",
            "data": {
                "name": "sys_session_send",
                "arguments": {
                    "agent": "cursor_workhorse",
                    "title": "cursor-cycle-1",
                    "args": "complete handoff",
                },
            },
        }
        read = {
            "type": "tool_call",
            "data": {"name": "sys_read_inbox", "arguments": {}},
        }
        record = {
            "parent_session_id": parent,
            "child_session_id": "child_one_shot",
            "work_id": "work_one_shot",
            "agent": "cursor_workhorse",
            "title": "cursor-cycle-1",
        }
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {
                "TRIPLE_STAMP_RUN_DIR": value,
                "TRIPLE_STAMP_SANDBOX_TOKEN": "unit-test-secret",
            },
            clear=False,
        ):
            contract = plugin.supervisor_contract(enabled=True)
            runtime_state.append_dispatch(record)
            self.assertEqual(contract(read)["result"], "ALLOW")
            empty: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            result = asyncio.run(
                tool_dispatch._drain_inbox(
                    empty,
                    server_client=None,
                    conversation_id=parent,
                )
            )
            self.assertIn("Inbox is empty", result)
            self.assertEqual(contract(send)["result"], "ALLOW")

    def test_collected_packet_does_not_gate_handoff_content(self) -> None:
        parent = "parent_stage_order"
        cursor_record = {
            "parent_session_id": parent,
            "child_session_id": "child_cursor",
            "work_id": "work_cursor",
            "agent": "cursor_workhorse",
            "title": "cursor-cycle-1",
            "status": "completed",
            "output": "COLLECTED CURSOR PACKET",
        }
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {
                "TRIPLE_STAMP_RUN_DIR": value,
                "TRIPLE_STAMP_SANDBOX_TOKEN": "unit-test-secret",
            },
            clear=False,
        ):
            contract = plugin.supervisor_contract(enabled=True)
            self.assertEqual(
                contract(
                    {
                        "type": "tool_call",
                        "data": {
                            "name": "sys_session_send",
                            "arguments": {
                                "agent": "cursor_workhorse",
                                "title": "cursor-cycle-1",
                                "args": "initial request",
                            },
                        },
                    }
                )["result"],
                "ALLOW",
            )
            runtime_state.append_dispatch(cursor_record)
            self.assertEqual(
                contract(
                    {
                        "type": "tool_call",
                        "data": {"name": "sys_read_inbox", "arguments": {}},
                    }
                )["result"],
                "ALLOW",
            )
            runtime_state.append_collection(cursor_record)
            allowed = contract(
                {
                    "type": "tool_call",
                    "data": {
                        "name": "sys_session_send",
                        "arguments": {
                            "agent": "opus_auditor",
                            "title": "audit-cycle-1",
                            "args": (
                                "ORIGINAL REQUEST\n"
                                "COLLECTED CURSOR PACKET"
                            ),
                        },
                    },
                }
            )
            self.assertEqual(allowed["result"], "ALLOW")

    def _historical_native_empty_output_case(self) -> None:
        runtime_guard._install_parent_inbox_guard()
        parent = "parent_native_empty"
        expected = {
            "parent_session_id": parent,
            "child_session_id": "child_expected",
            "work_id": "work_expected",
            "agent": "cursor_workhorse",
            "title": "cursor-cycle-1",
            "status": "completed",
        }
        # Exact native shape retained from run-80_2v6to. The terminal edge
        # carried output="" even though Cursor persisted a complete answer.
        native_payload = {
            "type": "sub_agent",
            "work_id": "work_expected",
            "task_id": "child_expected",
            "handle_id": "child_expected",
            "conversation_id": "child_expected",
            "tool_name": "cursor_workhorse",
            "agent": "cursor_workhorse",
            "title": "cursor-cycle-1",
            "status": "completed",
            "output": "",
        }
        inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        inbox.put_nowait(native_payload)

        class AllowResponse:
            status_code = 200
            text = ""

            @staticmethod
            def json() -> dict[str, str]:
                return {"result": "POLICY_ACTION_ALLOW"}

        class AllowClient:
            @staticmethod
            async def post(*args: object, **kwargs: object) -> AllowResponse:
                return AllowResponse()

        recovered = (
            "1. **Official URL:** https://cursor.com/docs/cli/using#non-interactive-mode\n"
            "2. **Exact quote:** “Use `-p` or `--print` to run Agent in "
            "non-interactive mode. This will print the response to the console.”"
        )

        class Item:
            @staticmethod
            def to_api_dict() -> dict[str, object]:
                return {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": recovered}],
                }

        class Page:
            data = [Item()]
            has_more = False
            last_id = None

        class Store:
            @staticmethod
            def list_items(*args: object, **kwargs: object) -> Page:
                return Page()

        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {
                "TRIPLE_STAMP_RUN_DIR": value,
                "TRIPLE_STAMP_SANDBOX_TOKEN": "unit-test-secret",
            },
            clear=False,
        ), mock.patch(
            "omnigent.runtime.get_conversation_store",
            return_value=Store(),
        ), mock.patch.object(
            runtime_guard, "_CURSOR_COMPLETION_STABLE_S", 0.005
        ), mock.patch.object(
            runtime_guard, "_CURSOR_COMPLETION_STABLE_S", 0.01
        ):
            bridge = (
                Path(value)
                / "tmp"
                / f"omnigent-{os.getuid()}"
                / "cursor-native"
                / hashlib.sha256(b"child_expected").hexdigest()[:32]
            )
            bridge.mkdir(parents=True)
            (bridge / "triple-stamp-startup.json").write_text(
                json.dumps(
                    {
                        "state": "exited",
                        "reason": (
                            "Cursor exited after startup acknowledgement (exit 0)"
                        ),
                    }
                ),
                encoding="utf-8",
            )
            runtime_state.append_dispatch(expected)
            output = asyncio.run(
                tool_dispatch._drain_inbox(
                    inbox,
                    server_client=AllowClient(),
                    conversation_id=parent,
                )
            )
            collected = runtime_state.read_collections()
            self.assertEqual(len(collected), 1)
            self.assertEqual(collected[0]["work_id"], "work_expected")
            self.assertEqual(collected[0]["output"], recovered)
            self.assertEqual(collected[0]["status"], "completed")
        self.assertIn(recovered, output)
        self.assertTrue(inbox.empty())

    def _historical_web_hop_case(self) -> None:
        runtime_guard._install_parent_inbox_guard()
        parent, child = "parent_web", "ecad120199a2426c9391a9ffccc0354c"
        expected = {
            "parent_session_id": parent,
            "child_session_id": child,
            "work_id": "subagent_f9f32d4a9a1f",
            "agent": "cursor_workhorse",
            "title": "cursor-web-opus-1-1",
        }
        native = {
            "type": "sub_agent",
            "work_id": expected["work_id"],
            "conversation_id": child,
            "agent": "cursor_workhorse",
            "title": "cursor-web-opus-1-1",
            "status": "completed",
            "output": "",
        }

        class Page:
            data: list[object] = []
            has_more = False
            last_id = None

        class Store:
            @staticmethod
            def list_items(*args: object, **kwargs: object) -> Page:
                return Page()

        class AllowResponse:
            status_code = 200
            text = ""

            @staticmethod
            def json() -> dict[str, str]:
                return {"result": "POLICY_ACTION_ALLOW"}

        class AllowClient:
            @staticmethod
            async def post(*args: object, **kwargs: object) -> AllowResponse:
                return AllowResponse()

        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value},
            clear=False,
        ), mock.patch("omnigent.runtime.get_conversation_store", return_value=Store()):
            run = Path(value)
            cursor_id = "736dcb5c-35a9-4cac-a242-1ecdf6ee00e9"
            bridge = (
                run
                / "tmp"
                / f"omnigent-{os.getuid()}"
                / "cursor-native"
                / hashlib.sha256(child.encode()).hexdigest()[:32]
            )
            bridge.mkdir(parents=True)
            store = run / "home/.cursor/chats/chat" / cursor_id / "store.db"
            store.parent.mkdir(parents=True)
            (bridge / "cursor_forwarder.json").write_text(
                json.dumps({"store_path": str(store)}), encoding="utf-8"
            )
            transcript = (
                run
                / "home/.cursor/projects/project/agent-transcripts"
                / cursor_id
                / f"{cursor_id}.jsonl"
            )
            transcript.parent.mkdir(parents=True)
            answer = "## Finding\n\nThe documented flag is `--force`."
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "role": "user",
                                "message": {"content": [{"type": "text", "text": "hunt"}]},
                            }
                        ),
                        json.dumps(
                            {
                                "role": "assistant",
                                "message": {
                                    "content": [{"type": "text", "text": answer}]
                                },
                            }
                        ),
                        json.dumps({"type": "turn_ended", "status": "success"}),
                    ]
                ),
                encoding="utf-8",
            )
            runtime_state.append_dispatch(expected)
            inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            inbox.put_nowait(native)
            output = asyncio.run(
                tool_dispatch._drain_inbox(
                    inbox, server_client=AllowClient(), conversation_id=parent
                )
            )
            self.assertIn(answer, output)
            self.assertEqual(runtime_state.read_collections()[0]["output"], answer)

            # Cursor's stop hook can fire between tool dispatches. The empty
            # native result must stay behind the transcript-flush barrier while
            # the same child is still appending work, then recover its eventual
            # prose-only final without consuming a continuation generation.
            late_child = "late_cursor_child"
            late = {
                **expected,
                "child_session_id": late_child,
                "work_id": "late_cursor_work",
                "title": "cursor-web-opus-1-2",
            }
            late_bridge = (
                run
                / "tmp"
                / f"omnigent-{os.getuid()}"
                / "cursor-native"
                / hashlib.sha256(late_child.encode()).hexdigest()[:32]
            )
            late_bridge.mkdir(parents=True)
            late_cursor_id = "late-cursor-session"
            late_store = run / "home/.cursor/chats/chat" / late_cursor_id / "store.db"
            late_store.parent.mkdir(parents=True, exist_ok=True)
            (late_bridge / "cursor_forwarder.json").write_text(
                json.dumps({"store_path": str(late_store)}), encoding="utf-8"
            )
            late_transcript = (
                run
                / "home/.cursor/projects/project/agent-transcripts"
                / late_cursor_id
                / f"{late_cursor_id}.jsonl"
            )
            late_transcript.parent.mkdir(parents=True)
            user_line = json.dumps(
                {
                    "role": "user",
                    "message": {"content": [{"type": "text", "text": "hunt"}]},
                }
            )
            tool_line = json.dumps(
                {
                    "role": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "checking one more source"},
                            {"type": "tool_use", "name": "WebFetch"},
                        ]
                    },
                }
            )
            late_answer = "## Final evidence\n\nThe supported flag is `--force`."
            final_line = json.dumps(
                {
                    "role": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": late_answer}]
                    },
                }
            )
            turn_end_line = json.dumps({"type": "turn_ended", "status": "success"})
            late_transcript.write_text(
                "\n".join((user_line, tool_line)) + "\n", encoding="utf-8"
            )
            runtime_state.append_dispatch(late)
            late_inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            late_inbox.put_nowait(
                {
                    **native,
                    "work_id": late["work_id"],
                    "conversation_id": late_child,
                    "title": late["title"],
                }
            )

            def append_late_final() -> None:
                late_transcript.write_text(
                    "\n".join((user_line, tool_line, final_line, turn_end_line)) + "\n",
                    encoding="utf-8",
                )

            timer = threading.Timer(0.04, append_late_final)
            timer.start()
            try:
                with mock.patch.object(
                    runtime_guard, "_CURSOR_COMPLETION_STABLE_S", 0.01
                ), mock.patch.object(
                    runtime_guard, "_CURSOR_COMPLETION_STABLE_S", 0.2
                ), mock.patch.object(
                    runtime_guard, "_CURSOR_STAGE_INACTIVITY_S", 1.0
                ):
                    late_output = asyncio.run(
                        tool_dispatch._drain_inbox(
                            late_inbox,
                            server_client=AllowClient(),
                            conversation_id=parent,
                        )
                    )
            finally:
                timer.join()
            self.assertIn(late_answer, late_output)
            self.assertEqual(
                runtime_state.read_collections()[-1]["output"], late_answer
            )

            tool_only_child = "tool_only_child"
            tool_only = {
                **expected,
                "child_session_id": tool_only_child,
                "work_id": "tool_only_work",
                "title": "cursor-web-codex-1-1",
            }
            tool_bridge = (
                run
                / "tmp"
                / f"omnigent-{os.getuid()}"
                / "cursor-native"
                / hashlib.sha256(tool_only_child.encode()).hexdigest()[:32]
            )
            tool_bridge.mkdir(parents=True)
            tool_cursor_id = "tool-only-cursor-session"
            tool_store = run / "home/.cursor/chats/chat" / tool_cursor_id / "store.db"
            tool_store.parent.mkdir(parents=True, exist_ok=True)
            (tool_bridge / "cursor_forwarder.json").write_text(
                json.dumps({"store_path": str(tool_store)}), encoding="utf-8"
            )
            (tool_bridge / "triple-stamp-startup.json").write_text(
                json.dumps({"state": "exited"}), encoding="utf-8"
            )
            tool_transcript = (
                run
                / "home/.cursor/projects/project/agent-transcripts"
                / tool_cursor_id
                / f"{tool_cursor_id}.jsonl"
            )
            tool_transcript.parent.mkdir(parents=True)
            tool_transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "role": "user",
                                "message": {"content": [{"type": "text", "text": "hunt"}]},
                            }
                        ),
                        json.dumps(
                            {
                                "role": "assistant",
                                "message": {
                                    "content": [
                                        {"type": "text", "text": "tool preamble"},
                                        {"type": "tool_use", "name": "WebFetch"},
                                    ]
                                },
                            }
                        ),
                        json.dumps({"type": "turn_ended", "status": "success"}),
                    ]
                ),
                encoding="utf-8",
            )
            runtime_state.append_dispatch(tool_only)
            tool_inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            tool_inbox.put_nowait(
                {
                    **native,
                    "work_id": "tool_only_work",
                    "conversation_id": tool_only_child,
                    "title": "cursor-web-codex-1-1",
                }
            )
            with mock.patch.object(
                runtime_guard, "_CURSOR_COMPLETION_STABLE_S", 0.01
            ), mock.patch.object(
                runtime_guard, "_CURSOR_COMPLETION_STABLE_S", 0.02
            ), mock.patch.object(
                runtime_guard, "_CURSOR_STAGE_INACTIVITY_S", 0.2
            ):
                asyncio.run(
                    tool_dispatch._drain_inbox(
                        tool_inbox,
                        server_client=AllowClient(),
                        conversation_id=parent,
                    )
                )
            self.assertTrue(
                runtime_state.read_collections()[-1]["output"].startswith(
                    "CURSOR_FINALIZATION_REQUIRED:"
                )
            )

    def test_run_7zcykg11_recovers_from_isolated_cursor_home(self) -> None:
        fixture = json.loads(
            (ROOT / "tests/fixtures/run-7zcykg11-cursor-home.json").read_text(
                encoding="utf-8"
            )
        )
        runtime_guard._install_parent_inbox_guard()
        transcript_reader = getattr(
            tool_dispatch._drain_inbox,
            "__triple_stamp_cursor_transcript__",
        )
        child = fixture["child_session_id"]
        cursor_id = fixture["cursor_session_id"]
        with tempfile.TemporaryDirectory() as value:
            run = Path(value)
            cursor_data = run / "cursor-home/.cursor"
            bridge = (
                run
                / "tmp"
                / f"omnigent-{os.getuid()}"
                / "cursor-native"
                / hashlib.sha256(child.encode()).hexdigest()[:32]
            )
            bridge.mkdir(parents=True)
            (bridge / "triple-stamp-startup.json").write_text(
                json.dumps({"state": "ready"}),
                encoding="utf-8",
            )
            transcript = (
                cursor_data
                / "projects/project/agent-transcripts"
                / cursor_id
                / f"{cursor_id}.jsonl"
            )
            transcript.parent.mkdir(parents=True)
            answer = "## Complete evidence packet\n\nRecovered from Cursor-only HOME."
            transcript.write_text(
                "\n".join(
                    (
                        json.dumps(
                            {
                                "role": "user",
                                "message": {
                                    "content": [{"type": "text", "text": "research"}]
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "role": "assistant",
                                "message": {
                                    "content": [{"type": "text", "text": answer}]
                                },
                            }
                        ),
                        json.dumps({"type": "turn_ended", "status": "success"}),
                    )
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {
                    "TRIPLE_STAMP_RUN_DIR": str(run),
                    "TRIPLE_STAMP_CURSOR_HOME": str(run / "cursor-home"),
                    "CURSOR_DATA_DIR": str(cursor_data),
                },
                clear=False,
            ):
                output, diagnostic, seen, active_tool, complete = transcript_reader(
                    child
                )
                from omnigent import cursor_native_forwarder

                self.assertEqual(
                    cursor_native_forwarder._cursor_chats_root(),
                    cursor_data / "chats",
                )
            self.assertEqual(output, answer)
            self.assertTrue(seen)
            self.assertFalse(active_tool)
            self.assertTrue(complete)
            self.assertIn("metadata_source=single-isolated-transcript", diagnostic)
            self.assertIn("forwarder_present=False", diagnostic)

    def _historical_partial_prose_case(self) -> None:
        runtime_guard._install_parent_inbox_guard()
        fixture = json.loads(
            (ROOT / "tests/fixtures/run-gezq2bml-terminal.json").read_text(
                encoding="utf-8"
            )
        )
        parent = "retained-run-gezq2bml-parent"
        child = fixture["last_collection"]["child_session_id"]

        class AllowResponse:
            status_code = 200
            text = ""

            @staticmethod
            def json() -> dict[str, str]:
                return {"result": "POLICY_ACTION_ALLOW"}

        class AllowClient:
            @staticmethod
            async def post(*args: object, **kwargs: object) -> AllowResponse:
                return AllowResponse()

        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ, {"TRIPLE_STAMP_RUN_DIR": value}, clear=False
        ):
            run = Path(value)
            bridge_id = hashlib.sha256(child.encode()).hexdigest()[:32]
            bridge = (
                run
                / "tmp"
                / f"omnigent-{os.getuid()}"
                / "cursor-native"
                / bridge_id
            )
            bridge.mkdir(parents=True)
            cursor_id = "c88f31d4-3097-4671-84b1-2daadab28840"
            store = run / "home/.cursor/chats/chat" / cursor_id / "store.db"
            store.parent.mkdir(parents=True)
            (bridge / "cursor_forwarder.json").write_text(
                json.dumps({"store_path": str(store)}), encoding="utf-8"
            )
            (bridge / "triple-stamp-startup.json").write_text(
                json.dumps({"state": "ready"}), encoding="utf-8"
            )
            transcript = (
                run
                / "home/.cursor/projects/project/agent-transcripts"
                / cursor_id
                / f"{cursor_id}.jsonl"
            )
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                "\n".join(
                    [
                        # A finalization continuation reuses one Cursor
                        # transcript. This older successful turn must not
                        # authorize collection of cycle 3's partial prose.
                        json.dumps(
                            {
                                "role": "user",
                                "message": {
                                    "content": [{"type": "text", "text": "older turn"}]
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "role": "assistant",
                                "message": {
                                    "content": [
                                        {"type": "text", "text": "older final answer"}
                                    ]
                                },
                            }
                        ),
                        json.dumps({"type": "turn_ended", "status": "success"}),
                        json.dumps(
                            {
                                "role": "user",
                                "message": {
                                    "content": [
                                        {"type": "text", "text": "cycle 3 rework"}
                                    ]
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "role": "assistant",
                                "message": {
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": "checking official documentation",
                                        },
                                        {
                                            "type": "tool_use",
                                            "name": "WebSearch",
                                            "input": {"search_term": "site:cursor.com/docs"},
                                        },
                                    ]
                                },
                            }
                        ),
                        json.dumps({"type": "status", "status": "idle"}),
                        json.dumps({"type": "hook", "name": "stop"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            runtime_state.append_dispatch(
                {
                    "parent_session_id": parent,
                    "child_session_id": child,
                    "work_id": fixture["last_collection"]["work_id"],
                    "agent": "cursor_workhorse",
                    "title": fixture["last_collection"]["title"],
                }
            )
            inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            inbox.put_nowait(
                {
                    "type": "sub_agent",
                    "work_id": fixture["last_collection"]["work_id"],
                    "conversation_id": child,
                    "agent": "cursor_workhorse",
                    "title": fixture["last_collection"]["title"],
                    "status": "completed",
                    "output": "",
                }
            )
            partial = json.dumps(
                {
                    "role": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "final-looking but still incomplete",
                            },
                            {
                                "type": "thinking",
                                "thinking": "reasoning continues after the prose",
                            },
                        ]
                    },
                }
            )
            final = json.dumps(
                {
                    "role": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "## Final evidence\n\n`--force` allows commands.",
                            }
                        ]
                    },
                }
            )

            def append_partial_prose() -> None:
                with transcript.open("a", encoding="utf-8") as handle:
                    handle.write(partial + "\n")

            def append_final_prose() -> None:
                with transcript.open("a", encoding="utf-8") as handle:
                    handle.write(final + "\n")

            def append_turn_end() -> None:
                with transcript.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps({"type": "turn_ended", "status": "success"})
                        + "\n"
                    )

            partial_timer = threading.Timer(0.04, append_partial_prose)
            final_timer = threading.Timer(0.09, append_final_prose)
            turn_timer = threading.Timer(0.15, append_turn_end)
            partial_timer.start()
            final_timer.start()
            turn_timer.start()
            try:
                started = time.monotonic()
                with mock.patch.object(
                    runtime_guard, "_CURSOR_COMPLETION_STABLE_S", 0.01
                ), mock.patch.object(
                    runtime_guard, "_CURSOR_COMPLETION_STABLE_S", 0.02
                ), mock.patch.object(
                    runtime_guard, "_CURSOR_STAGE_INACTIVITY_S", 0.08
                ), mock.patch.object(
                    runtime_guard, "_CURSOR_STAGE_ABSOLUTE_S", 0.3
                ):
                    output = asyncio.run(
                        tool_dispatch._drain_inbox(
                            inbox,
                            server_client=AllowClient(),
                            conversation_id=parent,
                        )
                    )
                elapsed = time.monotonic() - started
            finally:
                partial_timer.join()
                final_timer.join()
                turn_timer.join()
            self.assertIn("`--force` allows commands.", output)
            self.assertNotIn("final-looking but still incomplete", output)
            self.assertGreaterEqual(elapsed, 0.14)
            collected = runtime_state.read_collections()
            self.assertEqual(len(collected), 1)
            self.assertFalse(
                collected[0]["output"].startswith("CURSOR_FINALIZATION_REQUIRED:")
            )
            observation = fixture["cycle_3_cursor_observation"]
            self.assertTrue(observation["assistant_prose_before_turn_ended"])
            self.assertTrue(observation["active_reasoning_after_prose"])
            self.assertFalse(observation["turn_ended_success_at_collection"])
            self.assertEqual(observation["resident_process_state"], "ready")

    def _historical_progress_case(self) -> None:
        self.assertEqual(runtime_guard._CURSOR_STAGE_INACTIVITY_S, 5 * 60)
        self.assertEqual(
            runtime_guard._CURSOR_STAGE_ABSOLUTE_S,
            15 * 60,
        )
        runtime_guard._install_parent_inbox_guard()
        parent = "9522f82bb0cc4b14bfe8f8e6ee0ea527"
        child = "4f3cb7fe25544a91ba281ff42da91328"

        class AllowResponse:
            status_code = 200
            text = ""

            @staticmethod
            def json() -> dict[str, str]:
                return {"result": "POLICY_ACTION_ALLOW"}

        class AllowClient:
            @staticmethod
            async def post(*args: object, **kwargs: object) -> AllowResponse:
                return AllowResponse()

        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ, {"TRIPLE_STAMP_RUN_DIR": value}, clear=False
        ):
            run = Path(value)
            bridge = (
                run
                / "tmp"
                / f"omnigent-{os.getuid()}"
                / "cursor-native"
                / hashlib.sha256(child.encode()).hexdigest()[:32]
            )
            bridge.mkdir(parents=True)
            cursor_id = "38571a61-e6a1-4b59-b7ba-7a9976606678"
            store = run / "home/.cursor/chats/chat" / cursor_id / "store.db"
            store.parent.mkdir(parents=True)
            (bridge / "cursor_forwarder.json").write_text(
                json.dumps({"store_path": str(store)}), encoding="utf-8"
            )
            # "ready" is the normal resident interactive process state. Turn
            # completion must come from the transcript, not process exit.
            (bridge / "triple-stamp-startup.json").write_text(
                json.dumps({"state": "ready"}), encoding="utf-8"
            )
            transcript = (
                run
                / "home/.cursor/projects/project/agent-transcripts"
                / cursor_id
                / f"{cursor_id}.jsonl"
            )
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "role": "user",
                                "message": {"content": [{"type": "text", "text": "Lakebase"}]},
                            }
                        ),
                        json.dumps(
                            {
                                "role": "assistant",
                                "message": {
                                    "content": [
                                        {"type": "tool_use", "name": "WebSearch"}
                                    ]
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            runtime_state.append_dispatch(
                {
                    "parent_session_id": parent,
                    "child_session_id": child,
                    "work_id": "subagent_d2c4794f07f8",
                    "agent": "cursor_workhorse",
                    "title": "cursor-cycle-1",
                }
            )
            inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            inbox.put_nowait(
                {
                    "type": "sub_agent",
                    "status": "completed",
                    "output": "",
                    "work_id": "subagent_d2c4794f07f8",
                }
            )

            def append_progress() -> None:
                with transcript.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "role": "assistant",
                                "message": {
                                    "content": [
                                        {"type": "tool_use", "name": "WebFetch"}
                                    ]
                                },
                            }
                        )
                        + "\n"
                    )

            answer = "## Simple answer\n\nLakebase branches do not merge rows."

            def append_prose() -> None:
                with transcript.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "role": "assistant",
                                "message": {
                                    "content": [{"type": "text", "text": answer}]
                                },
                            }
                        )
                        + "\n"
                    )

            def append_turn_end() -> None:
                with transcript.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps({"type": "turn_ended", "status": "success"})
                        + "\n"
                    )

            progress = threading.Timer(0.04, append_progress)
            prose = threading.Timer(0.09, append_prose)
            final = threading.Timer(0.15, append_turn_end)
            progress.start()
            prose.start()
            final.start()
            try:
                started = time.monotonic()
                with mock.patch.object(
                    runtime_guard, "_CURSOR_COMPLETION_STABLE_S", 0.005
                ), mock.patch.object(
                    runtime_guard, "_CURSOR_COMPLETION_STABLE_S", 0.015
                ), mock.patch.object(
                    runtime_guard, "_CURSOR_STAGE_INACTIVITY_S", 0.08
                ), mock.patch.object(
                    runtime_guard, "_CURSOR_STAGE_ABSOLUTE_S", 0.3
                ):
                    output = asyncio.run(
                        tool_dispatch._drain_inbox(
                            inbox,
                            server_client=AllowClient(),
                            conversation_id=parent,
                        )
                    )
                elapsed = time.monotonic() - started
            finally:
                progress.join()
                prose.join()
                final.join()
            self.assertIn(answer, output)
            self.assertGreaterEqual(elapsed, 0.14)
            collected = runtime_state.read_collections()[-1]
            self.assertEqual(collected["output"], answer)
            self.assertNotIn("CURSOR_FINALIZATION_REQUIRED", collected["output"])

    def _historical_in_request_timeout_case(self) -> None:
        runtime_guard._install_parent_inbox_guard()
        recover = getattr(
            tool_dispatch._drain_inbox,
            "__triple_stamp_cursor_transcript__",
        )
        for kind, inactivity_limit, absolute_limit in (
            ("inactivity", 0.03, 0.2),
            ("absolute", 1.0, 0.03),
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as value:
                run = Path(value)
                child = f"timeout-{kind}-child"
                bridge = (
                    run
                    / "tmp"
                    / f"omnigent-{os.getuid()}"
                    / "cursor-native"
                    / hashlib.sha256(child.encode()).hexdigest()[:32]
                )
                bridge.mkdir(parents=True)
                cursor_id = f"timeout-{kind}-cursor"
                store = run / "home/.cursor/chats/chat" / cursor_id / "store.db"
                store.parent.mkdir(parents=True)
                (bridge / "cursor_forwarder.json").write_text(
                    json.dumps({"store_path": str(store)}),
                    encoding="utf-8",
                )
                (bridge / "triple-stamp-startup.json").write_text(
                    json.dumps({"state": "ready"}),
                    encoding="utf-8",
                )
                transcript = (
                    run
                    / "home/.cursor/projects/project/agent-transcripts"
                    / cursor_id
                    / f"{cursor_id}.jsonl"
                )
                transcript.parent.mkdir(parents=True)
                transcript.write_text(
                    "\n".join(
                        (
                            json.dumps(
                                {
                                    "role": "user",
                                    "message": {
                                        "content": [
                                            {"type": "text", "text": "keep working"}
                                        ]
                                    },
                                }
                            ),
                            json.dumps(
                                {
                                    "role": "assistant",
                                    "message": {
                                        "content": [
                                            {
                                                "type": "text",
                                                "text": "partial prose only",
                                            }
                                        ]
                                    },
                                }
                            ),
                        )
                    )
                    + "\n",
                    encoding="utf-8",
                )
                with mock.patch.dict(
                    os.environ,
                    {"TRIPLE_STAMP_RUN_DIR": value},
                    clear=False,
                ), mock.patch.object(
                    runtime_guard, "_CURSOR_COMPLETION_STABLE_S", 0.005
                ), mock.patch.object(
                    runtime_guard,
                    "_CURSOR_STAGE_INACTIVITY_S",
                    inactivity_limit,
                ), mock.patch.object(
                    runtime_guard,
                    "_CURSOR_STAGE_ABSOLUTE_S",
                    absolute_limit,
                ):
                    output, diagnostic, finalizable = asyncio.run(recover(child))
                self.assertEqual(output, "")
                self.assertFalse(finalizable)
                self.assertIn(
                    f"cursor_recovery_timeout kind={kind}",
                    diagnostic,
                )
                self.assertIn(
                    f"inactivity_limit_s={inactivity_limit:.3f}",
                    diagnostic,
                )
                self.assertIn(
                    f"absolute_limit_s={absolute_limit:.3f}",
                    diagnostic,
                )
                self.assertIn("turn_ended=False", diagnostic)
                self.assertIn("process_state=ready", diagnostic)

    def _obsolete_finalization_does_not_advance_web_hop_counter(self) -> None:
        marker = _route_packet(
            "cursor_workhorse",
            "cursor-web-opus-1-1",
            "CURSOR_FINALIZATION_REQUIRED: run-u76 tool-only edge",
            child="same-web-child",
        )
        finalization = plugin._next_route([marker])
        self.assertEqual(
            (
                finalization.title,
                finalization.hop,
                finalization.requester,
                finalization.resume_child_session_id,
            ),
            ("cursor-web-opus-1-1", 1, "opus", "same-web-child"),
        )
        completed = {**marker, "output": "complete web evidence"}
        resumed_audit = plugin._next_route([marker, completed])
        self.assertEqual(
            (
                resumed_audit.title,
                resumed_audit.hop,
                resumed_audit.requester,
                resumed_audit.resume_child_session_id,
            ),
            ("audit-cycle-1-web-1", 1, "opus", ""),
        )
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ, {"TRIPLE_STAMP_RUN_DIR": value}, clear=False
        ):
            runtime_state.append_dispatch(
                {
                    **marker,
                    "parent_session_id": "parent-finalize",
                    "work_id": "first-work",
                }
            )
            runtime_state.append_collection({**marker, "work_id": "first-work"})
            contract = plugin.supervisor_contract(enabled=True)
            allowed_send = contract(
                {
                    "type": "tool_call",
                    "data": {
                        "name": "sys_session_send",
                        "arguments": {
                            "agent": "cursor_workhorse",
                            "title": "cursor-web-opus-1-1",
                            "args": "FINALIZE ONLY: emit the existing evidence packet.",
                        },
                    },
                }
            )
            self.assertEqual(allowed_send["result"], "ALLOW")
            runtime_state.append_dispatch(
                {
                    **marker,
                    "parent_session_id": "parent-finalize",
                    "work_id": "finalization-work",
                }
            )
            allowed_launch_result = contract(
                {
                    "type": "tool_result",
                    "data": {"result": '{"status":"launching"}'},
                }
            )
            self.assertEqual(allowed_launch_result["result"], "ALLOW")

    def _obsolete_every_cursor_stage_gets_one_same_session_finalization(self) -> None:
        for title in (
            "cursor-cycle-1",
            "cursor-cycle-2",
            "cursor-web-opus-1-1",
            "cursor-web-codex-1-1",
        ):
            with self.subTest(title=title):
                empty = _route_packet(
                    "cursor_workhorse",
                    title,
                    "CURSOR_FINALIZATION_REQUIRED: tool-only transcript",
                    child=f"{title}-child",
                )
                route = plugin._next_route([empty])
                self.assertEqual(
                    (route.agent, route.title, route.resume_child_session_id),
                    ("cursor_workhorse", title, f"{title}-child"),
                )
                exhausted = plugin._next_route([empty, dict(empty)])
                self.assertEqual(exhausted.status, "infrastructure_failed")

    def test_tool_result_accepts_sole_nonempty_result_without_native_identity(self) -> None:
        parent = "parent_native_identity"
        dispatch = {
            "parent_session_id": parent,
            "child_session_id": "expected_child",
            "work_id": "work_expected",
            "agent": "cursor_workhorse",
            "title": "cursor-cycle-1",
        }
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {
                "TRIPLE_STAMP_RUN_DIR": value,
                "TRIPLE_STAMP_SANDBOX_TOKEN": "unit-test-secret",
            },
            clear=False,
        ):
            runtime_state.append_dispatch(dispatch)
            event = {
                "type": "tool_result",
                "data": {"result": "packet output"},
                "request_data": {
                    "name": "sys_session_send",
                    "args": {},
                },
            }
            contract = plugin.supervisor_contract(enabled=True)
            self.assertEqual(contract(event)["result"], "ALLOW")

    def test_multiple_native_results_pass_through_without_selection_gate(self) -> None:
        runtime_guard._install_parent_inbox_guard()
        parent = "parent_multiple_native"
        dispatch = {
            "parent_session_id": parent,
            "child_session_id": "expected_child",
            "work_id": "work_expected",
            "agent": "cursor_workhorse",
            "title": "cursor-cycle-1",
        }
        inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        for suffix, secret in (("a", "SECRET-A"), ("b", "SECRET-B")):
            inbox.put_nowait(
                {
                    "type": "sub_agent",
                    "conversation_id": f"child-{suffix}",
                    "status": "completed",
                    "output": secret,
                }
            )

        class AllowResponse:
            status_code = 200
            text = ""

            @staticmethod
            def json() -> dict[str, str]:
                return {"result": "POLICY_ACTION_ALLOW"}

        class AllowClient:
            @staticmethod
            async def post(*args: object, **kwargs: object) -> AllowResponse:
                return AllowResponse()

        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value},
            clear=False,
        ):
            runtime_state.append_dispatch(dispatch)
            output = asyncio.run(
                tool_dispatch._drain_inbox(
                    inbox,
                    server_client=AllowClient(),
                    conversation_id=parent,
                )
            )
            collected = runtime_state.read_collections()
            self.assertEqual(len(collected), 2)
            self.assertEqual(
                {record["output"] for record in collected},
                {"SECRET-A", "SECRET-B"},
            )
        self.assertIn("SECRET-A", output)
        self.assertIn("SECRET-B", output)

    def test_direct_or_premature_response_fails_closed(self) -> None:
        contract = plugin.supervisor_contract(enabled=True)
        with mock.patch.object(plugin, "_completion_records", return_value=[]):
            decision = contract({"type": "response", "data": "answer from training"})
        self.assertEqual(decision["result"], "DENY")

    def test_exact_stamp_relay_writes_byte_audit(self) -> None:
        answer = "Verified answer.\n\nSource: https://example.test/\n"
        digest = "ab" * 32
        payload = {
            "verdict": "STAMP",
            "needs_web": False,
            "needs_internal": False,
            "gap_materiality": "none",
            "limitations": [],
            "why": "evidence holds",
            "citations_that_hold": ["official source"],
            "voice_profile_check": {
                "source": _VOICE_PROFILE_PATH,
                "sha256": digest,
            },
            "shippable_answer": answer,
            "evidence_appendix": [],
        }
        records = [_record("codex_judge", json.dumps(payload), title="judge-cycle-1")]
        contract = plugin.supervisor_contract(enabled=True)
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {
                "TRIPLE_STAMP_RUN_DIR": value,
                "TRIPLE_STAMP_VOICE_PROFILE": _VOICE_PROFILE_PATH,
                "TRIPLE_STAMP_VOICE_PROFILE_SHA256": digest,
            },
            clear=False,
        ), mock.patch.object(plugin, "_completion_records", return_value=records):
            runtime_state.append_collection(
                _route_packet("cursor_workhorse", "cursor-cycle-1", "evidence")
            )
            runtime_state.append_collection(
                _route_packet("opus_auditor", "audit-cycle-1", _audit("PASS"))
            )
            decision = contract({"type": "response", "data": answer})
            self.assertEqual(decision["result"], "ALLOW")
            data = (Path(value) / "stamped-answer.bin").read_bytes()
            self.assertEqual(data, answer.encode("utf-8"))
            self.assertEqual(
                (Path(value) / "stamped-answer.sha256").read_text().strip(),
                hashlib.sha256(data).hexdigest(),
            )
            self.assertEqual((Path(value) / "pipeline-terminal").read_text().strip(), "STAMP")

            altered = contract({"type": "response", "data": answer + "\n"})
            self.assertEqual(altered["result"], "DENY")

    def test_stamp_without_live_voice_digest_is_denied(self) -> None:
        payload = {
            "verdict": "STAMP",
            "needs_web": False,
            "needs_internal": False,
            "gap_materiality": "none",
            "limitations": [],
            "citations_that_hold": ["source"],
            "voice_profile_check": {
                "source": _VOICE_PROFILE_PATH,
                "sha256": "wrong",
            },
            "shippable_answer": "answer",
        }
        records = [_record("codex_judge", json.dumps(payload))]
        contract = plugin.supervisor_contract(enabled=True)
        with mock.patch.dict(
            os.environ,
            _voice_env("expected"),
            clear=False,
        ), mock.patch.object(plugin, "_completion_records", return_value=records):
            decision = contract({"type": "response", "data": "answer"})
        self.assertEqual(decision["result"], "DENY")

    def test_voice_rendering_is_optional_end_to_end(self) -> None:
        """With no profile configured, a stamp must validate and attest.

        This is the configuration anyone who clones the repository gets by
        default, so it has to reach a stamp without a voice profile existing
        anywhere on the machine.
        """

        answer = "A plain answer with no styling applied."
        payload = {
            "verdict": "STAMP",
            "needs_web": False,
            "needs_internal": False,
            "gap_materiality": "none",
            "limitations": [],
            "citations_that_hold": ["https://example.com/doc"],
            "voice_profile_check": {
                "source_path": "",
                "sha256": "",
                "constraints_applied": "none; voice rendering disabled",
            },
            "shippable_answer": answer,
        }
        packet = _route_packet("codex_judge", "judge-cycle-1", json.dumps(payload))
        off = {
            "TRIPLE_STAMP_VOICE_PROFILE": "",
            "TRIPLE_STAMP_VOICE_PROFILE_SHA256": "",
        }

        # The stamp validator accepts it.
        with mock.patch.dict(os.environ, off, clear=False):
            self.assertEqual(plugin._valid_stamp(payload), answer)

        # A stamp claiming a profile that the run never configured is still
        # rejected, so this is a real branch rather than a blanket bypass.
        lying = {**payload, "voice_profile_check": {"source_path": "/nope"}}
        with mock.patch.dict(
            os.environ, _voice_env("expected"), clear=False
        ):
            self.assertIsNone(plugin._valid_stamp(lying))

        # The attestation records the disabled mode and the launcher's exit
        # validator agrees with it.
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ, {**off, "TRIPLE_STAMP_RUN_DIR": value}, clear=False
        ):
            run = Path(value)
            (run / "routing-collections.jsonl").write_text(
                "\n".join(
                    json.dumps(record)
                    for record in (
                        _route_packet("cursor_workhorse", "cursor-cycle-1", "evidence"),
                        _route_packet("opus_auditor", "audit-cycle-1", _audit("PASS")),
                        packet,
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            self.assertTrue(runtime_state.attest_codex_stamp(packet))
            attestation = json.loads(
                (run / "stamp-attestation.json").read_text(encoding="utf-8")
            )
            self.assertEqual(attestation["voice_profile_sha256"], "")
            # attest_codex_stamp already wrote stamped-answer.bin read-only.
            self.assertEqual(
                (run / "stamped-answer.bin").read_bytes(), answer.encode("utf-8")
            )
            with mock.patch.object(launcher, "_eprint"):
                self.assertEqual(
                    launcher._validated_pipeline_exit(run, 0, ["-p", "prompt"]), 0
                )

    def test_stamp_attestation_requires_cursor_and_opus_chain(self) -> None:
        digest = "d" * 64
        payload = _route_packet(
            "codex_judge",
            "judge-cycle-1",
            json.dumps(
                {
                    "verdict": "STAMP",
                    "needs_web": False,
                    "needs_internal": False,
                    "gap_materiality": "none",
                    "limitations": [],
                    "citations_that_hold": ["official source"],
                    "voice_profile_check": {
                        "source_path": _VOICE_PROFILE_PATH,
                        "sha256": digest,
                    },
                    "shippable_answer": "answer",
                }
            ),
        )
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {
                "TRIPLE_STAMP_RUN_DIR": value,
                "TRIPLE_STAMP_VOICE_PROFILE": _VOICE_PROFILE_PATH,
                "TRIPLE_STAMP_VOICE_PROFILE_SHA256": digest,
            },
            clear=False,
        ):
            runtime_state.append_collection(payload)
            self.assertFalse(runtime_state.attest_codex_stamp(payload))
            self.assertFalse((Path(value) / "stamp-attestation.json").exists())

    def test_malformed_audit_cannot_fabricate_stamp_or_best_effort(self) -> None:
        """Exact POC: completed audit title plus `not json` is not evidence."""

        parent = "malformed-audit-parent"
        answer = "Fabricated approval despite malformed audit."
        judge = {
            **_route_packet(
                "codex_judge",
                "judge-cycle-1",
                json.dumps(
                    {
                        "verdict": "STAMP",
                        "needs_web": False,
                        "needs_internal": False,
                        "gap_materiality": "none",
                        "limitations": [],
                        "citations_that_hold": ["invalid audit title only"],
                        "voice_profile_check": {
                            "source_path": "",
                            "sha256": "",
                        },
                        "shippable_answer": answer,
                    }
                ),
            ),
            "parent_session_id": parent,
        }
        records = [
            {
                **_route_packet(
                    "cursor_workhorse",
                    "cursor-cycle-1",
                    "2 + 2 = 4",
                ),
                "parent_session_id": parent,
            },
            {
                **_route_packet(
                    "opus_auditor",
                    "audit-cycle-1",
                    "not json",
                ),
                "parent_session_id": parent,
            },
            judge,
        ]
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {
                "TRIPLE_STAMP_RUN_DIR": value,
                "TRIPLE_STAMP_VOICE_PROFILE": "",
                "TRIPLE_STAMP_VOICE_PROFILE_SHA256": "",
            },
            clear=False,
        ):
            runtime_state.activate_parent_attempt(parent, "request")
            for record in records:
                runtime_state.append_collection(record)
            self.assertFalse(plugin._has_required_stage_chain(records, 1))
            self.assertFalse(runtime_state.attest_codex_stamp(judge))
            self.assertEqual(
                runtime_state.record_best_effort_answer(
                    answer,
                    records,
                    cycle=1,
                    reason="must not attest malformed audit",
                    parent_session_id=parent,
                ),
                "",
            )
            decision = plugin.supervisor_contract(enabled=True)(
                {
                    "type": "response",
                    "data": answer,
                    "context": {
                        "conversation_id": parent,
                        "root_conversation_id": parent,
                    },
                }
            )
            self.assertEqual(decision["result"], "DENY")
            attempt = (
                Path(value)
                / ".triple-stamp-attempts"
                / hashlib.sha256(parent.encode()).hexdigest()[:16]
                / "current"
                / "terminal"
            )
            self.assertFalse(attempt.exists())
            for artifact in (
                "stamped-answer.bin",
                "stamp-attestation.json",
                "best-effort-answer.bin",
                "best-effort-attestation.json",
                "pipeline-terminal",
            ):
                self.assertFalse(
                    runtime_state._parent_artifact_path(
                        Path(value),
                        artifact,
                        parent,
                    ).exists()
                )

    def test_infrastructure_failure_needs_worker_provenance(self) -> None:
        text = "PIPELINE_INFRASTRUCTURE_ERROR: Cursor failed"
        with tempfile.TemporaryDirectory() as empty_value, mock.patch.dict(
            os.environ, {"TRIPLE_STAMP_RUN_DIR": empty_value}, clear=False
        ), mock.patch.object(plugin, "_completion_records", return_value=[]):
            contract = plugin.supervisor_contract(enabled=True)
            self.assertEqual(
                contract({"type": "response", "data": text})["result"],
                "DENY",
            )
        records = [_record("cursor_workhorse", "startup timeout", status="failed")]
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ, {"TRIPLE_STAMP_RUN_DIR": value}, clear=False
        ), mock.patch.object(plugin, "_completion_records", return_value=records):
            contract = plugin.supervisor_contract(enabled=True)
            self.assertEqual(
                contract({"type": "response", "data": text})["result"],
                "ALLOW",
            )
            self.assertEqual(
                (Path(value) / "pipeline-terminal").read_text().strip(),
                "PIPELINE_INFRASTRUCTURE_ERROR",
            )

    def test_validation_failure_needs_configured_codex_rejections(self) -> None:
        contract = plugin.supervisor_contract(enabled=True)
        rework = _judgment("REWORK")
        before_limit = [
            _record("codex_judge", rework, title=f"judge-cycle-{cycle}")
            for cycle in range(1, plugin._MAX_CYCLES)
        ]
        at_limit = [
            *before_limit,
            _record(
                "codex_judge",
                rework,
                title=f"judge-cycle-{plugin._MAX_CYCLES}",
            ),
        ]
        text = "PIPELINE_VALIDATION_FAILED: unresolved citation"
        with mock.patch.object(
            plugin,
            "_completion_records",
            return_value=before_limit,
        ):
            self.assertEqual(
                contract({"type": "response", "data": text})["result"],
                "DENY",
            )
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ, {"TRIPLE_STAMP_RUN_DIR": value}, clear=False
        ), mock.patch.object(
            plugin,
            "_completion_records",
            return_value=at_limit,
        ):
            self.assertEqual(
                contract({"type": "response", "data": text})["result"],
                "ALLOW",
            )

    def test_auth_preflight_precedes_lock_and_model_launch(self) -> None:
        source = (ROOT / ".omnigent/launcher.py").read_text(encoding="utf-8")
        outer = source[source.index("def _outer_main") :]
        self.assertLess(outer.index("_sandboxed_auth_preflight("), outer.index("with _RunLock"))
        self.assertLess(outer.index("with _RunLock"), outer.index("_spawn_sandboxed("))

    def test_concurrent_launch_lock_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            lock_path = Path(value) / "run.lock"
            with launcher._RunLock(lock_path):
                with self.assertRaises(launcher.LaunchError) as raised:
                    with launcher._RunLock(lock_path):
                        pass
            self.assertEqual(raised.exception.code, launcher.EXIT_BUSY)

    def test_stale_runtime_cleanup_removes_temp_and_harness_link(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            run = base / "run-stale"
            run.mkdir()
            (run / "owner-pid").write_text("99999999\n", encoding="utf-8")
            target = run / "h"
            target.mkdir()
            link = base / "harness-link"
            link.symlink_to(target, target_is_directory=True)
            (run / "harness-link").write_text(str(link) + "\n", encoding="utf-8")
            with mock.patch.object(launcher, "_reap_marked_processes", return_value=[]):
                launcher._cleanup_stale_runs(base)
            self.assertFalse(run.exists())
            self.assertFalse(link.exists())

    def test_launcher_exit_requires_valid_stamp_audit(self) -> None:
        # Exercised with voice rendering enabled, so the attestation's
        # voice digest is a real value the exit validator has to match.
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            _voice_env(
                hashlib.sha256(Path(_VOICE_PROFILE_PATH).read_bytes()).hexdigest()
            ),
            clear=False,
        ):
            run = Path(value)
            with mock.patch.object(launcher, "_eprint"):
                self.assertEqual(
                    launcher._validated_pipeline_exit(run, 0, ["-p", "prompt"]),
                    launcher.EXIT_PIPELINE,
                )
            answer = b"byte-exact answer\n"
            (run / "stamped-answer.bin").write_bytes(answer)
            profile_digest = hashlib.sha256(Path(_VOICE_PROFILE_PATH).read_bytes()).hexdigest()
            evidence = '{"verdict":"STAMP"}'
            (run / "routing-collections.jsonl").write_text(
                "\n".join(
                    json.dumps(record)
                    for record in (
                        {
                            "agent": "cursor_workhorse",
                            "title": "cursor-cycle-1",
                            "status": "completed",
                            "output": "evidence",
                        },
                        {
                            "agent": "opus_auditor",
                            "title": "audit-cycle-1",
                            "status": "completed",
                            "output": _audit("PASS"),
                        },
                        {
                        "agent": "codex_judge",
                        "child_session_id": "codex-child",
                        "title": "judge-cycle-1",
                        "status": "completed",
                        "output": evidence,
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            (run / "stamp-attestation.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "verdict": "STAMP",
                        "cycle": 1,
                        "child_session_id": "codex-child",
                        "title": "judge-cycle-1",
                        "answer_sha256": hashlib.sha256(answer).hexdigest(),
                        "answer_length": len(answer),
                        "voice_profile_sha256": profile_digest,
                        "evidence_packet_sha256": hashlib.sha256(
                            evidence.encode()
                        ).hexdigest(),
                        "evidence_packet_length": len(evidence.encode()),
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                launcher._validated_pipeline_exit(run, 0, ["-p", "prompt"]),
                0,
            )
            (run / "stamped-answer.bin").write_bytes(answer + b"tampered")
            with mock.patch.object(launcher, "_eprint"):
                self.assertEqual(
                    launcher._validated_pipeline_exit(run, 0, ["-p", "prompt"]),
                    launcher.EXIT_PIPELINE,
                )

    def test_launcher_exit_accepts_retry_stamp_and_best_effort(self) -> None:
        """Both terminal validators accept the one valid retry audit title."""

        off = {
            "TRIPLE_STAMP_VOICE_PROFILE": "",
            "TRIPLE_STAMP_VOICE_PROFILE_SHA256": "",
        }
        stamp_answer = "Retry-backed stamped answer."
        stamp = json.dumps(
            {
                "verdict": "STAMP",
                "needs_web": False,
                "needs_internal": False,
                "gap_materiality": "none",
                "limitations": [],
                "citations_that_hold": ["retry audit evidence"],
                "voice_profile_check": {
                    "source_path": "",
                    "sha256": "",
                },
                "shippable_answer": stamp_answer,
            }
        )
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {**off, "TRIPLE_STAMP_RUN_DIR": value},
            clear=False,
        ):
            run = Path(value)
            records = [
                _route_packet(
                    "cursor_workhorse",
                    "cursor-cycle-1",
                    "cursor evidence",
                ),
                self._dead_opus("audit-cycle-1"),
                _route_packet(
                    "opus_auditor",
                    "audit-retry-1-1",
                    _audit("PASS"),
                ),
            ]
            judge = _route_packet(
                "codex_judge",
                "judge-cycle-1",
                stamp,
            )
            for record in (*records, judge):
                runtime_state.append_collection(record)
            self.assertTrue(runtime_state.attest_codex_stamp(judge))
            with mock.patch.object(launcher, "_eprint"):
                self.assertEqual(
                    launcher._validated_pipeline_exit(
                        run,
                        0,
                        ["-p", "prompt"],
                    ),
                    0,
                )

        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {**off, "TRIPLE_STAMP_RUN_DIR": value},
            clear=False,
        ):
            run = Path(value)
            records = [
                _route_packet(
                    "cursor_workhorse",
                    "cursor-cycle-1",
                    "bounded customer answer",
                ),
                self._dead_opus("audit-cycle-1"),
                _route_packet(
                    "opus_auditor",
                    "audit-retry-1-1",
                    _audit("PASS_WITH_GAPS"),
                ),
                _route_packet(
                    "codex_judge",
                    "judge-cycle-1",
                    _judgment("REWORK"),
                ),
            ]
            for record in records:
                runtime_state.append_collection(record)
            self.assertEqual(
                runtime_state.record_best_effort_answer(
                    "bounded customer answer",
                    records,
                    cycle=1,
                    reason="bounded retry-backed answer",
                ),
                "bounded customer answer",
            )
            with mock.patch.object(launcher, "_eprint"):
                self.assertEqual(
                    launcher._validated_pipeline_exit(
                        run,
                        0,
                        ["-p", "prompt"],
                    ),
                    0,
                )

    def test_attested_answer_emission_preserves_exact_bytes_and_framing(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            run = Path(value)
            answer = "first line\n\nlast line"
            payload = {
                "agent": "codex_judge",
                "child_session_id": "retained-kl5-child",
                "work_id": "retained-kl5-work",
                "title": "judge-cycle-1",
                "output": json.dumps(
                    {
                        "verdict": "STAMP",
                        "needs_web": False,
                        "needs_internal": False,
                        "gap_materiality": "none",
                        "limitations": [],
                        "citations_that_hold": ["https://cursor.com/docs/cli/using"],
                        "voice_profile_check": {
                            "source_path": _VOICE_PROFILE_PATH,
                            "sha256": "profile-digest",
                        },
                        "shippable_answer": answer,
                    }
                ),
            }
            with mock.patch.dict(
                os.environ,
                {
                    "TRIPLE_STAMP_RUN_DIR": value,
                    "TRIPLE_STAMP_VOICE_PROFILE": _VOICE_PROFILE_PATH,
                    "TRIPLE_STAMP_VOICE_PROFILE_SHA256": "profile-digest",
                },
                clear=False,
            ):
                runtime_state.append_collection(
                    _route_packet("cursor_workhorse", "cursor-cycle-1", "evidence")
                )
                runtime_state.append_collection(
                    _route_packet("opus_auditor", "audit-cycle-1", _audit("PASS"))
                )
                runtime_state.append_collection(payload)
                self.assertTrue(runtime_state.attest_codex_stamp(payload))
            stream = io.BytesIO()

            class Stdout:
                buffer = stream

            with mock.patch.object(sys, "stdout", Stdout()):
                launcher._emit_attested_answer(run, ["-p", "prompt"])
            self.assertEqual(stream.getvalue(), answer.encode("utf-8"))
            stream.seek(0)
            stream.truncate()
            with mock.patch.object(sys, "stdout", Stdout()):
                launcher._emit_attested_answer(run, [])
            self.assertEqual(stream.getvalue(), b"")

    def test_retained_run_kl5_stamp_attests_and_validates(self) -> None:
        # The retained packet carries a placeholder rather than the absolute
        # profile path it was captured with, so the fixture stays portable.
        fixture = json.loads(
            (ROOT / "tests/fixtures/run-kl5-stamp.json")
            .read_text(encoding="utf-8")
            .replace("__VOICE_PROFILE__", _VOICE_PROFILE_PATH)
        )
        with tempfile.TemporaryDirectory() as value:
            run = Path(value)
            profile_digest = hashlib.sha256(Path(_VOICE_PROFILE_PATH).read_bytes()).hexdigest()
            self.assertEqual(
                profile_digest,
                hashlib.sha256(
                    Path(_VOICE_PROFILE_PATH).read_bytes()
                ).hexdigest(),
            )
            (run / "routing-collections.jsonl").write_text(
                "\n".join(
                    json.dumps(record)
                    for record in (
                        _route_packet(
                            "cursor_workhorse", "cursor-cycle-1", "retained evidence"
                        ),
                        _route_packet(
                            "opus_auditor", "audit-cycle-1", _audit("PASS")
                        ),
                        fixture,
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {
                    "TRIPLE_STAMP_RUN_DIR": value,
                    "TRIPLE_STAMP_VOICE_PROFILE": _VOICE_PROFILE_PATH,
                    "TRIPLE_STAMP_VOICE_PROFILE_SHA256": profile_digest,
                },
                clear=False,
            ):
                self.assertTrue(runtime_state.attest_codex_stamp(fixture))
                # Still inside the patch: the exit validator resolves the active
                # profile from the environment the run was launched with.
                self.assertEqual(
                    launcher._validated_pipeline_exit(run, 0, ["-p", "prompt"]), 0
                )

    def test_launcher_preserves_child_failure_and_self_test_success(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            run = Path(value)
            self.assertEqual(
                launcher._validated_pipeline_exit(run, 23, ["-p", "prompt"]),
                23,
            )
            self.assertEqual(
                launcher._validated_pipeline_exit(run, 0, ["--self-test"]),
                0,
            )

    def test_retained_run_gezq2bml_gets_exact_failure_attestation(self) -> None:
        fixture = json.loads(
            (ROOT / "tests/fixtures/run-gezq2bml-terminal.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as value:
            run = Path(value)
            (run / "routing-collections.jsonl").write_text(
                json.dumps(fixture["last_collection"]) + "\n",
                encoding="utf-8",
            )
            launcher._record_external_signal(
                run,
                fixture["external_signal"]["signal_number"],
            )
            self.assertEqual(
                launcher._validated_pipeline_exit(
                    run, fixture["child_code"], ["-p", "prompt"]
                ),
                launcher.EXIT_PIPELINE,
            )
            self.assertEqual(
                (run / "terminal-failure.txt").read_text(encoding="utf-8"),
                fixture["expected_result"],
            )
            attestation = json.loads(
                (run / "failure-attestation.json").read_text(encoding="utf-8")
            )
            self.assertEqual(attestation["stage"], "cursor-cycle-3")
            self.assertEqual(attestation["cycle"], 3)
            self.assertEqual(fixture["observed_terminal_signal"], "SIGTERM")
            self.assertLess(
                fixture["observed_sigterm_after_shell_start_seconds"],
                fixture["observed_shell_elapsed_seconds"],
            )
            self.assertLess(
                fixture["observed_runner_runtime_seconds"],
                fixture["observed_sigterm_after_shell_start_seconds"],
            )
            self.assertNotIn("budget", attestation["reason"].lower())
            self.assertNotIn("cycle cap", attestation["reason"].lower())
            stream = io.BytesIO()

            class Stdout:
                buffer = stream

            with mock.patch.object(sys, "stdout", Stdout()):
                launcher._emit_terminal_failure(run, ["-p", "prompt"])
            self.assertEqual(
                stream.getvalue(), fixture["expected_result"].encode("utf-8")
            )

    def test_runtime_cleanup_retains_failures_only(self) -> None:
        self.assertTrue(
            launcher._retain_runtime_diagnostics(
                models_started=True,
                keep_runtime=False,
                return_code=launcher.EXIT_PIPELINE,
            )
        )
        self.assertFalse(
            launcher._retain_runtime_diagnostics(
                models_started=True,
                keep_runtime=False,
                return_code=0,
            )
        )
        self.assertFalse(
            launcher._retain_runtime_diagnostics(
                models_started=False,
                keep_runtime=True,
                return_code=launcher.EXIT_PIPELINE,
            )
        )

    def test_browser_sessions_route_from_only_their_own_ledger(self) -> None:
        session_a = "browser-session-a"
        session_b = "browser-session-b"
        records = [
            {
                **_route_packet(
                    "cursor_workhorse",
                    "cursor-cycle-1",
                    "CURSOR-A",
                ),
                "parent_session_id": session_a,
            },
            {
                **_route_packet(
                    "opus_auditor",
                    "audit-cycle-1",
                    _audit("PASS"),
                ),
                "parent_session_id": session_a,
            },
            {
                **_route_packet(
                    "codex_judge",
                    "judge-cycle-1",
                    json.dumps(
                        {
                            "verdict": "STAMP",
                            "needs_web": False,
                            "needs_internal": False,
                            "why": "all claims hold",
                            "gap_materiality": "none",
                            "limitations": [],
                            "shippable_answer": "answer-a",
                            "citations_that_hold": ["source-a"],
                        }
                    ),
                ),
                "parent_session_id": session_a,
            },
        ]

        route_a = plugin._next_route(records, parent_session_id=session_a)
        route_b = plugin._next_route(records, parent_session_id=session_b)

        self.assertEqual(route_a.status, "success")
        self.assertEqual(route_b.title, "cursor-cycle-1")

    def test_route_table_covers_straight_web_rework_and_repairs(self) -> None:
        cursor_1 = _route_packet(
            "cursor_workhorse", "cursor-cycle-1", "CURSOR-1"
        )
        audit_pass = _route_packet(
            "opus_auditor", "audit-cycle-1", _audit("PASS"), child="opus-1"
        )
        audit_web = _route_packet(
            "opus_auditor", "audit-cycle-1", _audit("NEEDS_WEB"), child="opus-1"
        )
        audit_web_1 = _route_packet(
            "opus_auditor",
            "audit-cycle-1-web-1",
            _audit("NEEDS_WEB"),
            child="opus-web-1",
        )
        audit_web_2 = _route_packet(
            "opus_auditor",
            "audit-cycle-1-web-2",
            _audit("NEEDS_WEB"),
            child="opus-web-2",
        )
        audit_web_pass = _route_packet(
            "opus_auditor",
            "audit-cycle-1-web-1",
            _audit("PASS_WITH_GAPS"),
            child="opus-web-pass",
        )
        cursor_web_1 = _route_packet(
            "cursor_workhorse", "cursor-web-opus-1-1", "OPUS-WEB-1"
        )
        cursor_web_2 = _route_packet(
            "cursor_workhorse", "cursor-web-opus-1-2", "OPUS-WEB-2"
        )
        judge_web = _route_packet(
            "codex_judge", "judge-cycle-1", _judgment("NEEDS_WEB"), child="judge-1"
        )
        codex_web_1 = _route_packet(
            "cursor_workhorse", "cursor-web-codex-1-1", "CODEX-WEB-1"
        )
        malformed_audit = _route_packet(
            "opus_auditor", "audit-cycle-1", "not json"
        )
        repaired_audit = _route_packet(
            "opus_auditor", "audit-format-repair-1", _audit("FAIL")
        )
        malformed_audit_web = _route_packet(
            "opus_auditor", "audit-cycle-1-web-1", "not json"
        )
        malformed_judge = _route_packet(
            "codex_judge", "judge-cycle-1", "not json"
        )
        repaired_judge = _route_packet(
            "codex_judge", "judge-format-repair-1", _judgment("REWORK")
        )
        cases = [
            ([], ("dispatch", "cursor_workhorse", "cursor-cycle-1", "")),
            ([cursor_1], ("dispatch", "opus_auditor", "audit-cycle-1", "")),
            (
                [cursor_1, audit_pass],
                ("dispatch", "codex_judge", "judge-cycle-1", ""),
            ),
            (
                [cursor_1, audit_web],
                ("dispatch", "cursor_workhorse", "cursor-web-opus-1-1", ""),
            ),
            (
                [cursor_1, audit_web, cursor_web_1],
                ("dispatch", "opus_auditor", "audit-cycle-1-web-1", ""),
            ),
            (
                [cursor_1, audit_web, cursor_web_1, audit_web_pass],
                ("dispatch", "codex_judge", "judge-cycle-1", ""),
            ),
            (
                [cursor_1, audit_web, cursor_web_1, audit_web_1],
                ("dispatch", "cursor_workhorse", "cursor-web-opus-1-2", ""),
            ),
            (
                [
                    cursor_1,
                    audit_web,
                    cursor_web_1,
                    audit_web_1,
                    cursor_web_2,
                ],
                ("dispatch", "opus_auditor", "audit-cycle-1-web-2", ""),
            ),
            (
                [
                    cursor_1,
                    audit_web,
                    cursor_web_1,
                    audit_web_1,
                    cursor_web_2,
                    audit_web_2,
                ],
                ("best_effort", "", "", ""),
            ),
            (
                [cursor_1, audit_pass, judge_web],
                ("dispatch", "cursor_workhorse", "cursor-web-codex-1-1", ""),
            ),
            (
                [cursor_1, audit_pass, judge_web, codex_web_1],
                ("dispatch", "codex_judge", "judge-cycle-1", "judge-1"),
            ),
            (
                [cursor_1, malformed_audit],
                ("dispatch", "opus_auditor", "audit-format-repair-1", ""),
            ),
            (
                [cursor_1, malformed_audit, repaired_audit],
                ("dispatch", "codex_judge", "judge-cycle-1", ""),
            ),
            (
                [cursor_1, audit_web, cursor_web_1, malformed_audit_web],
                (
                    "dispatch",
                    "opus_auditor",
                    "audit-format-repair-1-web-1",
                    "",
                ),
            ),
            (
                [cursor_1, audit_pass, malformed_judge],
                ("dispatch", "codex_judge", "judge-format-repair-1", ""),
            ),
            (
                [cursor_1, audit_pass, malformed_judge, repaired_judge],
                ("dispatch", "cursor_workhorse", "cursor-cycle-2", ""),
            ),
        ]
        for records, expected in cases:
            with self.subTest(expected=expected):
                route = plugin._next_route(records)
                self.assertEqual(
                    (
                        route.status,
                        route.agent,
                        route.title,
                        route.resume_child_session_id,
                    ),
                    expected,
                )

    def test_opus_need_web_reaudit_is_fresh_and_accepted(self) -> None:
        records = [
            _route_packet(
                "cursor_workhorse", "cursor-cycle-1", "ORIGINAL CURSOR"
            ),
            _route_packet(
                "opus_auditor",
                "audit-cycle-1",
                _audit("NEEDS_WEB"),
                child="finished-opus",
            ),
            _route_packet(
                "cursor_workhorse",
                "cursor-web-opus-1-1",
                "NEW WEB EVIDENCE",
            ),
        ]
        fresh = plugin._next_route(records)
        self.assertEqual(fresh.agent, "opus_auditor")
        self.assertEqual(fresh.title, "audit-cycle-1-web-1")
        self.assertEqual(fresh.resume_child_session_id, "")

        records.append(
            _route_packet(
                "opus_auditor",
                fresh.title,
                _audit("PASS_WITH_GAPS"),
                child="fresh-opus",
            )
        )
        accepted = plugin._next_route(records)
        self.assertEqual(
            (accepted.agent, accepted.title),
            ("codex_judge", "judge-cycle-1"),
        )
        self.assertTrue(plugin._has_required_stage_chain(records, 1))
        failed_fresh = dict(records[-1], status="failed", output="native timeout")
        terminal = plugin._next_route([*records[:-1], failed_fresh])
        self.assertEqual(terminal.status, "infrastructure_failed")
        self.assertEqual(terminal.agent, "")

    def _dead_opus(self, title: str, *, child: str = "") -> dict[str, str]:
        """A transient mid-stream Opus death packet for the given title."""

        return dict(
            _route_packet("opus_auditor", title, "", child=child or f"opus-{title}"),
            status="failed",
            output=(
                "API Error: Server error mid-response. The response above may "
                "be incomplete."
            ),
        )

    def test_transient_opus_stream_death_recovers_with_fresh_retry(self) -> None:
        """A transient mid-stream Opus death routes to a fresh unique-title child."""

        lead = [
            _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR-1"),
            _route_packet(
                "opus_auditor", "audit-cycle-1", _audit("PASS"), child="opus-1"
            ),
            _route_packet(
                "codex_judge",
                "judge-cycle-1",
                _judgment("REWORK"),
                child="judge-1",
            ),
            _route_packet("cursor_workhorse", "cursor-cycle-2", "CURSOR-2"),
        ]
        route = plugin._next_route([*lead, self._dead_opus("audit-cycle-2")])
        self.assertEqual(
            (route.status, route.agent, route.title, route.requester),
            ("dispatch", "opus_auditor", "audit-retry-2-1", "opus"),
        )
        # Recovery is not a model-quality cycle and never resumes the dead
        # terminal: the cycle counter is unchanged and there is no resume id.
        self.assertEqual(route.cycle, 2)
        self.assertEqual(route.resume_child_session_id, "")

        # A genuine, non-transient worker failure (native timeout) is still
        # terminal — the retry path must not swallow real failures.
        genuine = dict(
            _route_packet("opus_auditor", "audit-cycle-2", "", child="opus-2"),
            status="failed",
            output="native timeout",
        )
        terminal = plugin._next_route([*lead, genuine])
        self.assertEqual(terminal.status, "infrastructure_failed")
        self.assertEqual(terminal.agent, "")

    def test_transient_opus_retry_is_bounded_then_terminal(self) -> None:
        """One fresh retry per cycle; a second stream death is terminal."""

        self.assertEqual(plugin._OPUS_TRANSIENT_RETRY_CAP, 1)
        base = [_route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR-1")]
        first = plugin._next_route([*base, self._dead_opus("audit-cycle-1")])
        self.assertEqual((first.status, first.title), ("dispatch", "audit-retry-1-1"))
        # The single retry also died mid-stream -> terminal, not another retry.
        second = plugin._next_route(
            [
                *base,
                self._dead_opus("audit-cycle-1"),
                self._dead_opus("audit-retry-1-1"),
            ]
        )
        self.assertEqual(second.status, "infrastructure_failed")
        self.assertEqual(second.agent, "")
        self.assertIn("retries exhausted", second.reason)

    def test_unknown_opus_completion_never_retries_to_avoid_duplicate_launch(
        self,
    ) -> None:
        """A timed-out/unknown completion stays terminal; a fresh paid retry is unsafe."""

        base = [_route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR-1")]
        unknown = dict(
            _route_packet("opus_auditor", "audit-cycle-1", "", child="opus-1"),
            status="failed",
            output=(
                "PIPELINE_INFRASTRUCTURE_ERROR: Opus native dispatch timed out "
                "after child creation; completion is unknown and duplicate paid "
                "launch is forbidden: ReadTimeout; child=opus-1; "
                "task=running,busy=True. Retry is latched until `/quit` reaps "
                "the run."
            ),
        )
        route = plugin._next_route([*base, unknown])
        self.assertEqual(route.status, "infrastructure_failed")
        self.assertEqual(route.agent, "")
        self.assertFalse(plugin._is_transient_opus_stream_error(unknown))

    def test_incomplete_opus_output_is_never_audit_evidence(self) -> None:
        """A failed stream cannot turn a valid-looking body into evidence."""

        truncated = _audit("PASS")
        records = [
            _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR-1"),
            dict(
                _route_packet(
                    "opus_auditor",
                    "audit-cycle-1",
                    truncated,
                    child="opus-1",
                ),
                status="failed",
                stream_error=(
                    "API Error: Server error mid-response. The response above "
                    "may be incomplete."
                ),
            ),
        ]
        route = plugin._next_route(records)
        self.assertEqual(
            (route.status, route.agent, route.title),
            ("dispatch", "opus_auditor", "audit-retry-1-1"),
        )
        self.assertFalse(plugin._has_required_stage_chain(records, 1))

    def test_valid_audit_prose_about_http_500_is_not_stream_truncation(self) -> None:
        """Provider-error analysis inside a valid audit is ordinary evidence."""

        audit = _audit(
            "PASS_WITH_GAPS",
            attack=(
                "The earlier HTTP 500 internal server error was transient; "
                "the completed response is valid."
            ),
        )
        record = _route_packet(
            "opus_auditor",
            "audit-cycle-1",
            audit,
            child="opus-valid-http-analysis",
        )
        self.assertIsNotNone(plugin._valid_audit(audit))
        self.assertFalse(plugin._is_transient_opus_stream_error(record))
        route = plugin._next_route(
            [
                _route_packet(
                    "cursor_workhorse",
                    "cursor-cycle-1",
                    "CURSOR-1",
                ),
                record,
            ]
        )
        self.assertEqual(
            (route.status, route.agent, route.title),
            ("dispatch", "codex_judge", "judge-cycle-1"),
        )

    def test_completed_retry_audit_routes_like_normal_audit(self) -> None:
        """A completed retry audit judges normally and completes the stage chain."""

        records = [
            _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR-1"),
            self._dead_opus("audit-cycle-1"),
            _route_packet(
                "opus_auditor",
                "audit-retry-1-1",
                _audit("PASS"),
                child="opus-retry-1",
            ),
        ]
        route = plugin._next_route(records)
        self.assertEqual(
            (route.agent, route.title), ("codex_judge", "judge-cycle-1")
        )
        self.assertTrue(plugin._has_required_stage_chain(records, 1))
        self.assertIsNone(
            runtime_state.parse_dispatch_title("audit-retry-1-2")
        )
        self.assertIsNone(plugin._stage("audit-retry-1-2"))

    def test_transient_opus_failure_is_recoverable_worker_state(self) -> None:
        """A transient Opus death is terminal only after its retry budget is spent."""

        recoverable = [
            _record("cursor_workhorse", "CURSOR-1", title="cursor-cycle-1"),
            _record(
                "opus_auditor",
                "API Error: Server error mid-response. The response above may "
                "be incomplete.",
                status="failed",
                title="audit-cycle-1",
            ),
        ]
        with mock.patch.object(
            plugin, "_completion_records", return_value=recoverable
        ), mock.patch.object(plugin, "read_collections", return_value=recoverable):
            self.assertFalse(plugin._has_terminal_worker_failure())

        exhausted = [
            *recoverable,
            _record(
                "opus_auditor",
                "Server error mid-response; the response above may be incomplete",
                status="failed",
                title="audit-retry-1-1",
            ),
        ]
        with mock.patch.object(
            plugin, "_completion_records", return_value=exhausted
        ), mock.patch.object(plugin, "read_collections", return_value=exhausted):
            self.assertTrue(plugin._has_terminal_worker_failure())

    def test_run_00jfpj73_nonterminal_judge_stays_live_and_bounds_terminal_routes(
        self,
    ) -> None:
        fixture = json.loads(
            (
                ROOT
                / "tests/fixtures/run-00jfpj73-databricks-timeout.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(fixture["provider"], "databricks")
        self.assertEqual(
            fixture["judge_cycle_2"]["verdict"],
            "REWORK",
        )
        records = [
            _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR-1"),
            _route_packet("opus_auditor", "audit-cycle-1", _audit("PASS_WITH_GAPS")),
            _route_packet("codex_judge", "judge-cycle-1", _judgment("REWORK")),
            _route_packet("cursor_workhorse", "cursor-cycle-2", "CURSOR-2"),
            _route_packet("opus_auditor", "audit-cycle-2", _audit("PASS_WITH_GAPS")),
            _route_packet("codex_judge", "judge-cycle-2", _judgment("REWORK")),
        ]
        route = plugin._next_route(records)
        expected = fixture["judge_cycle_2"]["corrected_expected_next_route"]
        self.assertEqual(
            (route.status, route.cycle, route.agent, route.title),
            (
                expected["status"],
                expected["cycle"],
                expected["agent"],
                expected["title"],
            ),
        )
        dispatches = [
            {
                "agent": stage["agent"],
                "title": stage["title"],
            }
            for stage in fixture["timeline"]
            if stage.get("dispatched_at")
        ]
        dispatches.append(
            {
                "agent": expected["agent"],
                "title": expected["title"],
            }
        )
        self.assertTrue(plugin._route_dispatch_pending(route, records, dispatches))
        self.assertIsNone(supervisor_runtime._HEADLESS_PIPELINE_TIMEOUT_S)
        self.assertFalse(fixture["judge_cycle_2"]["stamp_attestation_should_exist"])

        digest = "a" * 64
        stamp = json.dumps(
            {
                "verdict": "STAMP",
                "needs_web": False,
                "needs_internal": False,
                "gap_materiality": "none",
                "limitations": [],
                "why": "fixture evidence holds",
                "citations_that_hold": ["official source"],
                "voice_profile_check": {
                    "source_path": _VOICE_PROFILE_PATH,
                    "sha256": digest,
                },
                "shippable_answer": "answer",
            }
        )
        with mock.patch.dict(
            os.environ,
            _voice_env(digest),
            clear=False,
        ):
            terminal = plugin._next_route(
                [
                    _route_packet(
                        "cursor_workhorse", "cursor-cycle-1", "CURSOR"
                    ),
                    _route_packet(
                        "opus_auditor", "audit-cycle-1", _audit("PASS")
                    ),
                    _route_packet(
                        "codex_judge", "judge-cycle-1", stamp
                    ),
                ]
            )
        self.assertEqual(terminal.status, "success")

        exhausted: list[dict[str, str]] = []
        for cycle in range(1, 5):
            exhausted.extend(
                [
                    _route_packet(
                        "cursor_workhorse",
                        f"cursor-cycle-{cycle}",
                        f"CURSOR-{cycle}",
                    ),
                    _route_packet(
                        "opus_auditor",
                        f"audit-cycle-{cycle}",
                        _audit("PASS_WITH_GAPS"),
                    ),
                    _route_packet(
                        "codex_judge",
                        f"judge-cycle-{cycle}",
                        _judgment("REWORK"),
                    ),
                ]
            )
        bounded = plugin._next_route(exhausted)
        self.assertEqual(
            bounded.status,
            "dispatch" if plugin._MAX_CYCLES == 4 else "validation_failed",
        )
        self.assertEqual(bounded.cycle, 4)
        if plugin._MAX_CYCLES == 4:
            self.assertEqual(bounded.title, "judge-convergence-4")

    def test_run_00jfpj73_model_effort_mcp_and_cost_receipts(self) -> None:
        fixture = json.loads(
            (
                ROOT
                / "tests/fixtures/run-00jfpj73-databricks-timeout.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            set(fixture["cursor_receipts"]["startup_models"]),
            {"cursor-grok-4.6-xhigh"},
        )
        self.assertEqual(
            fixture["models"]["opus_auditor"],
            "system.ai.claude-opus-5[1m]",
        )
        for observation in fixture["opus_observations"]:
            self.assertEqual(observation["effort_values"], ["max"])
            self.assertEqual(
                observation["assistant_rows"],
                sum(observation["stop_reasons"].values()),
            )
            self.assertEqual(
                set(observation["mcp_counts"]),
                {"glean", "jira", "slack", "confluence", "safe"},
            )
            self.assertTrue(
                all(count > 0 for count in observation["mcp_counts"].values())
            )
            self.assertEqual(
                set(observation["receipt_statuses"]),
                {"glean", "jira", "slack", "confluence", "safe"},
            )
        self.assertEqual(
            [row["verdict"] for row in fixture["codex_verdicts"]],
            ["REWORK", "REWORK"],
        )
        cost = fixture["cost_observation"]
        self.assertAlmostEqual(
            cost["provider_reported_usd"]
            + cost["conservative_unpriced_estimate_usd"],
            cost["observed_lower_bound_usd"],
        )
        self.assertFalse(cost["live_budget_state_present"])

    def test_run_4jnvjebo_reconstructs_direct_gateway_failure(self) -> None:
        fixture = json.loads(
            (
                ROOT
                / "tests/fixtures/run-4jnvjebo-direct-turn-guard.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(fixture["provider"], "direct")
        self.assertEqual(fixture["claude_namespace"], "databricks_gateway")
        self.assertEqual(
            [stage["title"] for stage in fixture["timeline"]],
            [
                "cursor-cycle-1",
                "audit-cycle-1",
                "judge-cycle-1",
                "cursor-cycle-2",
                "audit-cycle-2",
                "judge-cycle-2",
                "cursor-cycle-3",
                "audit-cycle-3",
                "judge-cycle-3",
                "cursor-web-codex-3-1",
            ],
        )
        self.assertEqual(
            [
                stage["verdict"]
                for stage in fixture["timeline"]
                if stage["agent"] == "codex_judge"
            ],
            ["REWORK", "REWORK", "NEEDS_WEB"],
        )
        for observation in fixture["opus_observations"]:
            self.assertEqual(observation["effort_values"], ["max"])
            self.assertEqual(
                set(observation["mcp_counts"]),
                {"glean", "jira", "slack", "confluence", "safe"},
            )
            self.assertTrue(
                all(count > 0 for count in observation["mcp_counts"].values())
            )

    def test_run_4jnvjebo_token_was_healthy_and_direct_stayed_isaac_free(
        self,
    ) -> None:
        fixture = json.loads(
            (
                ROOT
                / "tests/fixtures/run-4jnvjebo-direct-turn-guard.json"
            ).read_text(encoding="utf-8")
        )
        auth = fixture["authentication"]
        self.assertGreater(
            auth["seconds_remaining_at_guard_if_raw_is_utc"],
            4 * 15 * 60,
        )
        self.assertEqual(auth["model_gateway_401_or_403_count"], 0)
        self.assertFalse(auth["refresh_attempted_by_direct_profile"])
        self.assertFalse(fixture["processes"]["isaac_process_observed"])
        self.assertEqual(fixture["processes"]["marked_processes_after_cleanup"], 0)

    def test_run_4jnvjebo_reports_in_flight_stage_without_false_budget_cause(
        self,
    ) -> None:
        fixture = json.loads(
            (
                ROOT
                / "tests/fixtures/run-4jnvjebo-direct-turn-guard.json"
            ).read_text(encoding="utf-8")
        )
        pending = fixture["timeline"][-1]
        cost = fixture["cost_observation"]
        deadlines = fixture["deadline_checks"]
        termination = fixture["termination"]
        self.assertEqual(pending["title"], "cursor-web-codex-3-1")
        self.assertFalse(pending["collected"])
        self.assertFalse(pending["terminal_result_recoverable"])
        self.assertTrue(pending["partial_tool_evidence_recoverable"])
        self.assertIsNone(deadlines["omnigent_loop_timeout_seconds"])
        self.assertLess(
            deadlines["cursor_stage_elapsed_at_guard_seconds"],
            deadlines["cursor_stage_absolute_limit_seconds"],
        )
        self.assertLess(
            deadlines["cursor_stage_inactivity_at_guard_seconds"],
            deadlines["cursor_stage_inactivity_limit_seconds"],
        )
        self.assertFalse(deadlines["host_lease_present"])
        self.assertFalse(deadlines["tmux_session_present"])
        self.assertFalse(deadlines["provider_max_duration_observed"])
        self.assertEqual(termination["inner_cli_return_code"], 0)
        self.assertEqual(termination["outer_launcher_return_code"], 70)
        self.assertEqual(termination["runner_signal"], "SIGTERM")
        self.assertEqual(termination["in_flight_cursor_signal"], "SIGHUP")
        self.assertGreater(cost["remaining_usd"], 0)
        self.assertFalse(cost["budget_caused_termination"])

    def test_unstamped_zero_exit_recovers_in_flight_stage_not_budget_failure(
        self,
    ) -> None:
        fixture = json.loads(
            (
                ROOT
                / "tests/fixtures/run-4jnvjebo-direct-turn-guard.json"
            ).read_text(encoding="utf-8")
        )
        timeline = fixture["timeline"]
        cost = fixture["cost_observation"]
        with tempfile.TemporaryDirectory() as value:
            run = Path(value)
            dispatches = [
                {
                    "agent": stage["agent"],
                    "title": stage["title"],
                    "child_session_id": stage["child_session_id"],
                }
                for stage in timeline
            ]
            collections = [
                {
                    "agent": stage["agent"],
                    "title": stage["title"],
                    "child_session_id": stage["child_session_id"],
                    "status": "completed",
                    "output": stage.get("verdict", "packet"),
                }
                for stage in timeline[:-1]
            ]
            (run / "routing-dispatches.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in dispatches),
                encoding="utf-8",
            )
            (run / "routing-collections.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in collections),
                encoding="utf-8",
            )
            (run / "supervisor-tool-calls.jsonl").write_text(
                "{}\n" * cost["failure_attestation_calls"],
                encoding="utf-8",
            )
            recovered = {
                "cost_usd": cost["observed_partial_lower_bound_usd"],
                "reported_usd": cost["provider_reported_usd"],
                "estimated_unpriced_usd": (
                    cost["conservative_unpriced_estimate_usd"]
                ),
                "remaining_usd": cost["remaining_usd"],
                "max_cost_usd": cost["max_cost_usd"],
                "cost_observation_status": "partial",
                "unavailable_cost_sessions": cost["unavailable_sessions"],
                "cost_source": "retained_conversation_store",
            }
            with mock.patch.object(
                launcher,
                "_recover_budget_observation",
                return_value=recovered,
            ):
                failure = launcher._ensure_terminal_failure(run, 0)
            self.assertIn(
                "pipeline process exited with cursor-web-codex-3-1 in flight "
                "after collecting judge-cycle-3",
                failure,
            )
            self.assertIn("remaining $12.19 of $50.00", failure)
            self.assertNotIn("budget exceeded", failure.lower())
            attestation = json.loads(
                (run / "failure-attestation.json").read_text(encoding="utf-8")
            )
            self.assertEqual(attestation["stage"], "cursor-web-codex-3-1")
            self.assertEqual(attestation["cycle"], 3)
            self.assertEqual(
                attestation["calls"],
                cost["failure_attestation_calls"],
            )

    def test_headless_pipeline_wait_removes_only_omnigent_transport_guards(
        self,
    ) -> None:
        from omnigent import chat

        query_once = chat._query_sessions_once
        original_code = query_once.__code__
        original_timeout = chat._LOOP_TIMEOUT_S
        original_marker = getattr(
            query_once,
            "__triple_stamp_headless_wait__",
            None,
        )
        query_once.__code__, _ = supervisor_runtime._replace_code_int_constant(
            original_code,
            supervisor_runtime._HEADLESS_PIPELINE_EXTRA_TURN_LIMIT,
            supervisor_runtime._OMNIGENT_HEADLESS_EXTRA_TURN_LIMIT,
        )
        with contextlib.suppress(AttributeError):
            del query_once.__triple_stamp_headless_wait__
        try:
            with mock.patch.dict(
                os.environ,
                {"TRIPLE_STAMP_RUN_ID": "long-direct-gateway-fixture"},
                clear=False,
            ):
                supervisor_runtime.install_headless_pipeline_wait()
                first_code = query_once.__code__
                supervisor_runtime.install_headless_pipeline_wait()
            self.assertIs(chat._LOOP_TIMEOUT_S, None)
            self.assertIs(query_once.__code__, first_code)
            _, old_matches = supervisor_runtime._replace_code_int_constant(
                query_once.__code__,
                supervisor_runtime._OMNIGENT_HEADLESS_EXTRA_TURN_LIMIT,
                supervisor_runtime._OMNIGENT_HEADLESS_EXTRA_TURN_LIMIT,
            )
            _, new_matches = supervisor_runtime._replace_code_int_constant(
                query_once.__code__,
                supervisor_runtime._HEADLESS_PIPELINE_EXTRA_TURN_LIMIT,
                supervisor_runtime._HEADLESS_PIPELINE_EXTRA_TURN_LIMIT,
            )
            self.assertEqual(old_matches, 0)
            self.assertEqual(new_matches, 1)
            self.assertTrue(
                getattr(query_once, "__triple_stamp_headless_wait__", False)
            )
        finally:
            query_once.__code__ = original_code
            chat._LOOP_TIMEOUT_S = original_timeout
            with contextlib.suppress(AttributeError):
                del query_once.__triple_stamp_headless_wait__
            if original_marker is not None:
                query_once.__triple_stamp_headless_wait__ = original_marker

    def test_headless_pipeline_wait_fails_closed_on_omnigent_drift(self) -> None:
        from omnigent import chat

        query_once = chat._query_sessions_once
        original_code = query_once.__code__
        original_timeout = chat._LOOP_TIMEOUT_S
        original_marker = getattr(
            query_once,
            "__triple_stamp_headless_wait__",
            None,
        )
        changed, matches = supervisor_runtime._replace_code_int_constant(
            original_code,
            supervisor_runtime._OMNIGENT_HEADLESS_EXTRA_TURN_LIMIT,
            31,
        )
        if matches == 0:
            changed, matches = supervisor_runtime._replace_code_int_constant(
                original_code,
                supervisor_runtime._HEADLESS_PIPELINE_EXTRA_TURN_LIMIT,
                31,
            )
        self.assertEqual(matches, 1)
        query_once.__code__ = changed
        with contextlib.suppress(AttributeError):
            del query_once.__triple_stamp_headless_wait__
        try:
            with mock.patch.dict(
                os.environ,
                {"TRIPLE_STAMP_RUN_ID": "drift-fixture"},
                clear=False,
            ), self.assertRaises(RuntimeError):
                supervisor_runtime.install_headless_pipeline_wait()
        finally:
            query_once.__code__ = original_code
            chat._LOOP_TIMEOUT_S = original_timeout
            with contextlib.suppress(AttributeError):
                del query_once.__triple_stamp_headless_wait__
            if original_marker is not None:
                query_once.__triple_stamp_headless_wait__ = original_marker

    def test_system_wake_uses_authoritative_parent_session_context(self) -> None:
        from omnigent.runtime import telemetry

        with mock.patch.object(
            telemetry,
            "current_session_id",
            return_value="browser-parent",
        ):
            self.assertEqual(
                supervisor_runtime._session_id(
                    [{"role": "user", "content": "child completed"}]
                ),
                "browser-parent",
            )

    def test_internal_send_result_resolves_exact_concurrent_parent(self) -> None:
        child_ids = supervisor_runtime._tool_child_session_ids(
            '{"task_id":"child-a","handle_id":"child-a"}'
        )
        completed_child_ids = supervisor_runtime._tool_child_session_ids(
            "[System: sub-agent task "
            "540f937b5b7c4d0ea313cb8a637ffc65 completed]"
        )
        inbox_parents = supervisor_runtime._tool_parent_session_ids(
            {
                "content": [
                    {
                        "text": '{"parent_session_id":"browser-a"}',
                    }
                ]
            }
        )
        dispatches = [
            {
                "title": "audit-cycle-1",
                "child_session_id": "child-b",
                "parent_session_id": "browser-b",
            },
            {
                "title": "audit-cycle-1",
                "child_session_id": "child-a",
                "parent_session_id": "browser-a",
            },
        ]

        parent = supervisor_runtime._dispatch_parent_session_id(
            dispatches,
            child_ids,
        )
        records = [
            {"parent_session_id": "browser-a", "output": "A"},
            {"parent_session_id": "browser-b", "output": "B"},
        ]

        self.assertEqual(parent, "browser-a")
        self.assertEqual(
            supervisor_runtime._dispatch_parent_session_id(
                dispatches,
                {"child-a", "child-b"},
            ),
            "",
        )
        self.assertEqual(
            completed_child_ids,
            {"540f937b5b7c4d0ea313cb8a637ffc65"},
        )
        self.assertEqual(inbox_parents, {"browser-a"})
        self.assertEqual(
            supervisor_runtime._records_for_session(records, parent),
            [records[0]],
        )

    def test_supervisor_refuses_unscoped_multi_parent_reduction(self) -> None:
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value},
            clear=False,
        ):
            for parent in ("parent-a", "parent-b"):
                runtime_state.append_collection(
                    {
                        **_route_packet(
                            "cursor_workhorse",
                            "cursor-cycle-1",
                            parent,
                        ),
                        "parent_session_id": parent,
                    }
                )
            contract = plugin.supervisor_contract(enabled=True)
            denied = contract(
                {"type": "response", "data": "union answer"}
            )
            self.assertEqual(denied["result"], "DENY")
            self.assertIn("cross-parent", denied["reason"])
            self.assertEqual(
                contract({"type": "response", "data": ""}),
                {"result": "ALLOW"},
            )

    def test_nonterminal_runtime_suppresses_status_after_pending_dispatch(
        self,
    ) -> None:
        from omnigent.inner import claude_sdk_executor
        from omnigent.inner.executor import TextChunk, ToolCallRequest, TurnComplete

        original = claude_sdk_executor.ClaudeSDKExecutor.run_turn
        calls = 0

        async def scripted(
            _self: object,
            _messages: object,
            _tools: object,
            _system_prompt: object,
            _config: object = None,
        ):
            nonlocal calls
            calls += 1
            if calls == 1:
                yield TextChunk("ordinary final response")
                yield TurnComplete(response="ordinary final response")
                return
            yield ToolCallRequest(
                name="mcp__omnigent__sys_session_send",
                args={
                    "agent": "codex_judge",
                    "title": "judge-convergence-2",
                },
            )
            runtime_state.append_dispatch(
                {
                    "agent": "codex_judge",
                    "title": "judge-convergence-2",
                    "child_session_id": "convergence-child",
                    "work_id": "convergence-work",
                    "parent_session_id": "parent",
                }
            )
            yield TextChunk("status prose after dispatch")
            yield TurnComplete(response="status prose after dispatch")

        class Supervisor:
            _agent_name = "triple-stamp"

        async def collect() -> list[object]:
            return [
                event
                async for event in claude_sdk_executor.ClaudeSDKExecutor.run_turn(
                    Supervisor(),
                    [{"role": "user", "content": "wake", "session_id": "parent"}],
                    [],
                    "route",
                    None,
                )
            ]

        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value, "TRIPLE_STAMP_RUN_ID": "fixture"},
            clear=False,
        ):
            for record in (
                _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR-1"),
                _route_packet(
                    "opus_auditor", "audit-cycle-1", _audit("PASS_WITH_GAPS")
                ),
                _route_packet(
                    "codex_judge", "judge-cycle-1", _judgment("REWORK")
                ),
                _route_packet("cursor_workhorse", "cursor-cycle-2", "CURSOR-2"),
                _route_packet(
                    "opus_auditor", "audit-cycle-2", _audit("PASS_WITH_GAPS")
                ),
                _route_packet(
                    "codex_judge", "judge-cycle-2", _judgment("REWORK")
                ),
            ):
                runtime_state.append_collection(
                    {**record, "parent_session_id": "parent"}
                )
            claude_sdk_executor.ClaudeSDKExecutor.run_turn = scripted
            try:
                supervisor_runtime.install_supervisor_continuation_guard()
                events = asyncio.run(collect())
            finally:
                claude_sdk_executor.ClaudeSDKExecutor.run_turn = original
            self.assertEqual(calls, 2)
            self.assertTrue(
                any(
                    isinstance(event, ToolCallRequest)
                    and event.name.endswith("sys_session_send")
                    for event in events
                )
            )
            # The invariant is that the model's prose never reaches the reader,
            # not that no text does: the runtime narrates the stage itself.
            streamed = "".join(
                event.text for event in events if isinstance(event, TextChunk)
            )
            self.assertNotIn("status prose after dispatch", streamed)
            self.assertNotIn("ordinary final response", streamed)
            self.assertIn("judge-convergence-2", streamed)
            self.assertIn("final judgment", streamed)
            completions = [
                event for event in events if isinstance(event, TurnComplete)
            ]
            self.assertEqual(len(completions), 1)
            self.assertEqual(completions[0].response, "")
            self.assertEqual(
                plugin.supervisor_contract(enabled=True)(
                    {"type": "response", "data": ""}
                )["result"],
                "ALLOW",
            )
            continuation = runtime_state.read_supervisor_continuations()
            self.assertEqual(
                [row["action"] for row in continuation],
                ["continuation_enqueued", "ordinary_response_suppressed"],
            )
            self.assertIn(
                (value, "parent", 1),
                supervisor_runtime._NARRATED_STAGES,
            )

    def test_two_empty_supervisor_turns_still_dispatch_pending_cursor(
        self,
    ) -> None:
        """Recoverable empty routing turns never become a user-visible failure."""

        from omnigent.inner import claude_sdk_executor
        from omnigent.inner.executor import ToolCallRequest, TurnComplete

        original = claude_sdk_executor.ClaudeSDKExecutor.run_turn
        calls = 0
        parent = "empty-turn-parent"

        async def scripted(
            _self: object,
            _messages: object,
            _tools: object,
            _system_prompt: object,
            _config: object = None,
        ):
            nonlocal calls
            calls += 1
            if calls <= 2:
                yield TurnComplete(response="")
                return
            yield ToolCallRequest(
                name="mcp__omnigent__sys_session_send",
                args={
                    "agent": "cursor_workhorse",
                    "title": "cursor-cycle-1",
                },
            )
            runtime_state.append_dispatch(
                {
                    "agent": "cursor_workhorse",
                    "title": "cursor-cycle-1",
                    "child_session_id": "cursor-child",
                    "work_id": "cursor-work",
                    "parent_session_id": parent,
                }
            )
            yield TurnComplete(response="")

        class Supervisor:
            _agent_name = "triple-stamp"

        async def collect() -> list[object]:
            return [
                event
                async for event in claude_sdk_executor.ClaudeSDKExecutor.run_turn(
                    Supervisor(),
                    [
                        {
                            "role": "user",
                            "content": "2 + 2 = ?",
                            "session_id": parent,
                        }
                    ],
                    [],
                    "route",
                    None,
                )
            ]

        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {
                "TRIPLE_STAMP_RUN_DIR": value,
                "TRIPLE_STAMP_RUN_ID": "empty-turn-fixture",
            },
            clear=False,
        ):
            claude_sdk_executor.ClaudeSDKExecutor.run_turn = scripted
            try:
                supervisor_runtime.install_supervisor_continuation_guard()
                events = asyncio.run(collect())
            finally:
                claude_sdk_executor.ClaudeSDKExecutor.run_turn = original
            self.assertEqual(calls, 3)
            self.assertTrue(
                any(
                    isinstance(event, ToolCallRequest)
                    and event.args.get("title") == "cursor-cycle-1"
                    for event in events
                )
            )
            self.assertEqual(runtime_state.read_terminal_failure(parent), "")
            self.assertEqual(
                [
                    row["action"]
                    for row in runtime_state.read_supervisor_continuations(parent)
                ],
                [
                    "continuation_enqueued",
                    "continuation_enqueued",
                    "ordinary_response_suppressed",
                ],
            )
            self.assertNotIn(
                "continuation denied",
                " ".join(
                    str(getattr(event, "response", ""))
                    for event in events
                ).lower(),
            )

    def test_later_cursor_cycle_cannot_claim_prior_single_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {
                "TRIPLE_STAMP_RUN_DIR": value,
                "CURSOR_DATA_DIR": str(Path(value) / "cursor-home/.cursor"),
            },
            clear=False,
        ):
            run = Path(value)
            old_child = "cursor-cycle-1-child"
            new_child = "cursor-cycle-2-child"
            runtime_state.append_dispatch(
                {
                    "agent": "cursor_workhorse",
                    "title": "cursor-cycle-1",
                    "child_session_id": old_child,
                    "work_id": "old-work",
                }
            )
            runtime_state.append_dispatch(
                {
                    "agent": "cursor_workhorse",
                    "title": "cursor-cycle-2",
                    "child_session_id": new_child,
                    "work_id": "new-work",
                }
            )
            transcript = (
                run
                / "cursor-home/.cursor/projects/project/agent-transcripts"
                / "old-cursor-session"
                / "old-cursor-session.jsonl"
            )
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "role": "user",
                                "message": {
                                    "content": [{"type": "text", "text": "cycle 1"}]
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "role": "assistant",
                                "message": {
                                    "content": [
                                        {"type": "text", "text": "OLD PACKET"}
                                    ]
                                },
                            }
                        ),
                        json.dumps({"type": "turn_ended", "status": "success"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            new_bridge = (
                run
                / "tmp"
                / f"omnigent-{os.getuid()}"
                / "cursor-native"
                / hashlib.sha256(new_child.encode()).hexdigest()[:32]
            )
            new_bridge.mkdir(parents=True)
            snapshot = cursor_lifecycle._cursor_transcript_snapshot(
                new_child,
                new_bridge,
            )
            self.assertFalse(snapshot["seen"])
            self.assertFalse(snapshot["complete"])
            self.assertEqual(snapshot["output"], "")
            self.assertIn(
                "metadata_source=forwarder-required-after-prior-dispatch",
                snapshot["diagnostic"],
            )

    def test_rework_obeys_two_and_four_cycle_limits(self) -> None:
        for maximum in (2, 4):
            with self.subTest(maximum=maximum), mock.patch.object(
                plugin,
                "_MAX_CYCLES",
                maximum,
            ):
                records: list[dict[str, str]] = []
                for cycle in range(1, maximum + 1):
                    judgment = json.loads(_judgment("REWORK"))
                    judgment["punch_list_for_cursor"][0]["claim"] = (
                        f"distinct material gap for cycle {cycle}"
                    )
                    records.extend(
                        [
                            _route_packet(
                                "cursor_workhorse",
                                f"cursor-cycle-{cycle}",
                                f"CURSOR-{cycle}",
                            ),
                            _route_packet(
                                "opus_auditor",
                                f"audit-cycle-{cycle}",
                                _audit("FAIL"),
                            ),
                            _route_packet(
                                "codex_judge",
                                f"judge-cycle-{cycle}",
                                json.dumps(judgment),
                            ),
                        ]
                    )
                    route = plugin._next_route(records)
                    if cycle < maximum:
                        self.assertEqual(
                            route.title,
                            f"cursor-cycle-{cycle + 1}",
                        )
                    else:
                        self.assertEqual(route.status, "best_effort")

        contract = plugin.supervisor_contract(enabled=True)
        denied = contract(
            {
                "type": "tool_call",
                "data": {
                    "name": "sys_session_send",
                    "arguments": {
                        "agent": "cursor_workhorse",
                        "title": "cursor-cycle-5",
                        "args": "must not dispatch",
                    },
                },
            }
        )
        self.assertEqual(denied["result"], "ALLOW")

    def test_internal_gap_table_never_routes_to_cursor(self) -> None:
        cases = (
            ("internal", "internal_receipts", "audit_packet"),
            ("internal", "glean", "glean"),
            ("internal", "jira", "jira"),
            ("internal", "slack", "slack"),
            ("internal", "confluence", "confluence"),
            ("internal", "safe", "safe"),
            ("internal", "roadmap", "roadmap"),
            ("internal", "customer_history", "customer_history"),
        )
        for gap_type, capability, source in cases:
            with self.subTest(capability=capability):
                judgment = json.loads(_judgment("REWORK"))
                judgment["punch_list_for_cursor"] = [
                    {
                        "gap_type": gap_type,
                        "claim": f"verify {capability}",
                        "required_capability": capability,
                        "required_source": source,
                        "requested_proof": f"query {source} for primary evidence",
                    }
                ]
                parsed = plugin._valid_judgment(json.dumps(judgment))
                self.assertEqual(parsed["verdict"], "NEEDS_INTERNAL")
                self.assertEqual(
                    parsed["mechanically_rerouted_from"],
                    "REWORK",
                )
                route = plugin._next_route(
                    [
                        _route_packet(
                            "cursor_workhorse",
                            "cursor-cycle-1",
                            "CURSOR",
                        ),
                        _route_packet(
                            "opus_auditor",
                            "audit-cycle-1",
                            _audit("PASS_WITH_GAPS"),
                        ),
                        _route_packet(
                            "codex_judge",
                            "judge-cycle-1",
                            json.dumps(judgment),
                        ),
                    ]
                )
                self.assertEqual(
                    (route.agent, route.title),
                    ("opus_auditor", "audit-internal-1-1"),
                )

        public = json.loads(_judgment("REWORK"))
        public_route = plugin._next_route(
            [
                _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR"),
                _route_packet(
                    "opus_auditor",
                    "audit-cycle-1",
                    _audit("PASS_WITH_GAPS"),
                ),
                _route_packet(
                    "codex_judge",
                    "judge-cycle-1",
                    json.dumps(public),
                ),
            ]
        )
        self.assertEqual(
            (public_route.agent, public_route.title),
            ("cursor_workhorse", "cursor-cycle-2"),
        )

    def test_materiality_schema_and_form_only_adjudication(self) -> None:
        digest = "a" * 64
        stamp = {
            "verdict": "STAMP",
            "needs_web": False,
            "needs_internal": False,
            "why": "material claims hold",
            "gap_materiality": "nonmaterial",
            "limitations": [
                "The exact-child observer was unavailable for one receipt."
            ],
            "citations_that_hold": ["official source"],
            "voice_profile_check": {
                "source_path": _VOICE_PROFILE_PATH,
                "sha256": digest,
            },
            "shippable_answer": "Supported answer with a disclosed limitation.",
        }
        with mock.patch.dict(
            os.environ,
            _voice_env(digest),
            clear=False,
        ):
            self.assertEqual(
                plugin._valid_judgment(json.dumps(stamp))["verdict"],
                "STAMP",
            )
            missing_limit = dict(stamp, limitations=[])
            self.assertIsNone(
                plugin._valid_judgment(json.dumps(missing_limit))
            )

        nonmaterial_rework = json.loads(_judgment("REWORK"))
        nonmaterial_rework["gap_materiality"] = "nonmaterial"
        nonmaterial_rework["limitations"] = ["form only"]
        self.assertIsNone(
            plugin._valid_judgment(json.dumps(nonmaterial_rework))
        )

        form_rework = json.loads(_judgment("REWORK"))
        form_rework["punch_list_for_cursor"] = [
            {
                "gap_type": "form",
                "claim": "receipt schema needs normalization",
                "required_capability": "audit_receipt",
                "required_source": "observer",
                "requested_proof": "state the observer limitation",
            }
        ]
        form_route = plugin._next_route(
            [
                _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR"),
                _route_packet(
                    "opus_auditor",
                    "audit-cycle-1",
                    _audit("PASS_WITH_GAPS"),
                ),
                _route_packet(
                    "codex_judge",
                    "judge-cycle-1",
                    json.dumps(form_rework),
                ),
            ]
        )
        self.assertEqual(
            (form_route.agent, form_route.title),
            ("codex_judge", "judge-convergence-1"),
        )

    def test_repeated_fabricated_tool_failure_stops_after_cycle_two(self) -> None:
        fixture = json.loads(
            (
                ROOT
                / "tests/fixtures/run-o6khrsg4-six-fixes.json"
            ).read_text(encoding="utf-8")
        )
        stable_gap = {
            "gap_type": "stage1_evidence",
            "claim": fixture["repeated_judgment_gap"]["claim"],
            "required_capability": "cursor_stage1",
            "required_source": "stage1_packet",
            "requested_proof": fixture["repeated_judgment_gap"][
                "requested_proof"
            ],
        }

        def judgment(cycle: int) -> str:
            payload = json.loads(_judgment("REWORK"))
            payload["why"] = (
                f"cycle {cycle} repeats the assertion with different prose"
            )
            payload["punch_list_for_cursor"] = [stable_gap]
            return json.dumps(payload)

        four_retained_failures = [judgment(cycle) for cycle in range(1, 5)]
        self.assertEqual(
            len(four_retained_failures),
            fixture["cycles_completed"],
        )
        records: list[dict[str, str]] = []
        for cycle in (1, 2):
            records.extend(
                [
                    _route_packet(
                        "cursor_workhorse",
                        f"cursor-cycle-{cycle}",
                        f"CURSOR-{cycle}",
                    ),
                    _route_packet(
                        "opus_auditor",
                        f"audit-cycle-{cycle}",
                        _audit("PASS_WITH_GAPS"),
                    ),
                    _route_packet(
                        "codex_judge",
                        f"judge-cycle-{cycle}",
                        four_retained_failures[cycle - 1],
                    ),
                ]
            )
        with mock.patch.object(plugin, "_MAX_CYCLES", 4):
            route = plugin._next_route(records)
        self.assertEqual(
            (route.agent, route.title, route.cycle),
            ("codex_judge", "judge-convergence-2", 2),
        )
        self.assertFalse(route.title.startswith("cursor-cycle-3"))

        terminal = plugin._next_route(
            [
                *records,
                _route_packet(
                    "codex_judge",
                    "judge-convergence-2",
                    judgment(3),
                ),
            ]
        )
        self.assertEqual(terminal.status, "best_effort")
        self.assertNotIn("cursor-cycle-3", terminal.title)

        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value},
            clear=False,
        ):
            runtime_state.append_collection(records[2])
            runtime_state.append_collection(records[5])
            persisted = [
                json.loads(line)
                for line in (
                    Path(value) / "codex-punch-lists.jsonl"
                ).read_text(encoding="utf-8").splitlines()
            ]
        self.assertEqual(len(persisted), 2)
        self.assertEqual(
            persisted[0]["punch_list_signature"],
            persisted[1]["punch_list_signature"],
        )
        self.assertEqual(
            persisted[0]["normalized_punch_list"][0]["canonical_id"],
            persisted[1]["normalized_punch_list"][0]["canonical_id"],
        )

    def test_codex_two_hops_resume_same_judge_then_exhaust(self) -> None:
        records = [
            _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR"),
            _route_packet("opus_auditor", "audit-cycle-1", _audit("PASS")),
            _route_packet(
                "codex_judge",
                "judge-cycle-1",
                _judgment("NEEDS_WEB"),
                child="same-judge",
            ),
        ]
        self.assertEqual(
            plugin._next_route(records).title, "cursor-web-codex-1-1"
        )
        records.extend(
            [
                _route_packet(
                    "cursor_workhorse", "cursor-web-codex-1-1", "WEB-1"
                ),
                _route_packet(
                    "codex_judge",
                    "judge-cycle-1",
                    _judgment("NEEDS_WEB"),
                    child="same-judge",
                ),
            ]
        )
        self.assertEqual(
            plugin._next_route(records).title, "cursor-web-codex-1-2"
        )
        records.extend(
            [
                _route_packet(
                    "cursor_workhorse", "cursor-web-codex-1-2", "WEB-2"
                ),
                _route_packet(
                    "codex_judge",
                    "judge-cycle-1",
                    _judgment("NEEDS_WEB"),
                    child="same-judge",
                ),
            ]
        )
        route = plugin._next_route(records)
        self.assertEqual(route.status, "best_effort")
        self.assertIn("codex web-hop cap exhausted", route.reason)

    def test_codex_needs_internal_skips_cursor_uses_fresh_opus_and_exhausts(self) -> None:
        malformed = json.loads(_judgment("NEEDS_INTERNAL"))
        malformed["internal_queries"][0].pop("kill_condition")
        self.assertIsNone(plugin._valid_judgment(json.dumps(malformed)))

        records = [
            _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR"),
            _route_packet("opus_auditor", "audit-cycle-1", _audit("PASS")),
            _route_packet(
                "codex_judge",
                "judge-cycle-1",
                _judgment("NEEDS_INTERNAL"),
                child="judge-child",
            ),
        ]
        first = plugin._next_route(records)
        self.assertEqual(
            (
                first.status,
                first.agent,
                first.title,
                first.hop,
                first.resume_child_session_id,
            ),
            ("dispatch", "opus_auditor", "audit-internal-1-1", 1, ""),
        )
        records.extend(
            [
                _route_packet(
                    "opus_auditor",
                    first.title,
                    _audit("PASS"),
                    child="fresh-opus-internal-1",
                ),
                _route_packet(
                    "codex_judge",
                    "judge-cycle-1",
                    _judgment("NEEDS_INTERNAL"),
                    child="judge-child",
                ),
            ]
        )
        second = plugin._next_route(records)
        self.assertEqual(
            (second.agent, second.title, second.hop, second.resume_child_session_id),
            ("opus_auditor", "audit-internal-1-2", 2, ""),
        )
        records.extend(
            [
                _route_packet(
                    "opus_auditor",
                    second.title,
                    _audit("PASS"),
                    child="fresh-opus-internal-2",
                ),
                _route_packet(
                    "codex_judge",
                    "judge-cycle-1",
                    _judgment("NEEDS_INTERNAL"),
                    child="judge-child",
                ),
            ]
        )
        exhausted = plugin._next_route(records)
        self.assertEqual(exhausted.status, "best_effort")
        self.assertIn("internal-hop cap exhausted", exhausted.reason)
        self.assertFalse(
            any(
                str(record["title"]).startswith("cursor-web")
                for record in records
            )
        )

    def test_mixed_opus_and_codex_web_paths_remain_independent(self) -> None:
        records = [
            _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR"),
            _route_packet("opus_auditor", "audit-cycle-1", _audit("NEEDS_WEB")),
            _route_packet("cursor_workhorse", "cursor-web-opus-1-1", "OWEB"),
            _route_packet(
                "opus_auditor",
                "audit-cycle-1-web-1",
                _audit("PASS"),
            ),
            _route_packet(
                "codex_judge", "judge-cycle-1", _judgment("NEEDS_WEB")
            ),
        ]
        route = plugin._next_route(records)
        self.assertEqual(route.title, "cursor-web-codex-1-1")
        self.assertEqual(route.hop, 1)

    def test_straight_chain_reaches_stamp(self) -> None:
        digest = "a" * 64
        stamp = json.dumps(
            {
                "verdict": "STAMP",
                "needs_web": False,
                "needs_internal": False,
                "gap_materiality": "none",
                "limitations": [],
                "why": "all claims hold",
                "citations_that_hold": ["official source"],
                "voice_profile_check": {
                    "source": _VOICE_PROFILE_PATH,
                    "sha256": digest,
                },
                "shippable_answer": "answer",
                "evidence_appendix": [],
            }
        )
        records = [
            _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR"),
            _route_packet("opus_auditor", "audit-cycle-1", _audit("PASS")),
            _route_packet("codex_judge", "judge-cycle-1", stamp),
        ]
        with mock.patch.dict(
            os.environ,
            _voice_env(digest),
            clear=False,
        ):
            self.assertEqual(plugin._next_route(records).status, "success")

    def test_retained_needs_web_shape_drives_canonical_opus_web_stage(self) -> None:
        retained = json.dumps(
            {
                "verdict": "NEEDS_WEB",
                "needs_web": True,
                "summary": "scope mismatch",
                "gaps": ["missing full page"],
                "web_queries": [
                    {
                        **_web_hunt(),
                        "claim": "flag semantics",
                    }
                ],
                "attacks": [{"attack": "scope", "result": "HIT"}],
                "must_retest": [{"claim": "flag"}],
                "acceptable_as_is": ["retrieval date"],
                "punch_list_for_cursor": ["fetch full page"],
                "internal_sources_consulted": [],
                "internal_sources_not_required_reason": "public-only claim",
            }
        )
        records = [
            _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR"),
            _route_packet("opus_auditor", "audit-cycle-1", retained),
        ]
        route = plugin._next_route(records)
        self.assertEqual(
            (route.agent, route.title),
            ("cursor_workhorse", "cursor-web-opus-1-1"),
        )

    def test_audit_parser_accepts_supported_wrappers_and_rejects_missing_hunts(
        self,
    ) -> None:
        payload = {
            "verdict": "NEEDS_WEB",
            "needs_web": True,
            "web_queries": [
                {
                    "claim": "c",
                    "query": "q",
                    "where": "docs",
                    "break_how": "compare",
                    "kill_condition": "absent",
                    "prove_condition": "present",
                }
            ],
            "internal_sources_consulted": [],
            "internal_sources_not_required_reason": "public-only claim",
        }
        plain = json.dumps(payload)
        for output in (
            plain,
            f"```json\n{plain}\n```",
            "Audit preface that is not part of JSON.\n\n" + plain,
            (
                "verdict: NEEDS_WEB\nclaim: c\nquery: q\nwhere: docs\n"
                "break_how: compare\nkill_condition: absent\n"
                "prove_condition: present\ninternal_sources_consulted: []\n"
                "internal_sources_not_required_reason: public-only claim\n"
            ),
        ):
            with self.subTest(output=output[:30]):
                parsed = plugin._valid_audit(output)
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed["verdict"], "NEEDS_WEB")
        self.assertIsNone(plugin._valid_audit("verdict: NEEDS_WEB"))
        self.assertIsNone(plugin._valid_audit("analysis without a verdict"))

    def test_exact_deadlock_shape_routes_to_web_without_literal_handoff(self) -> None:
        retained_shape = (
            "All internal corroboration tools are unavailable. This prose preceded "
            "the object in the retained Opus transcript.\n\n"
            + json.dumps(
                {
                    "verdict": "NEEDS_WEB",
                    "summary": "Cannot stamp.",
                    "needs_web": True,
                    "web_queries": [
                        {
                            "claim": "`-f, --force` is documented.",
                            "query": "WebFetch the parameters page.",
                            "where": "Cursor docs",
                            "break_how": "diff the row",
                            "kill_condition": "flag absent",
                            "prove_condition": "row present",
                        }
                    ],
                    "internal_sources_consulted": [],
                    "internal_sources_not_required_reason": "tools unavailable",
                },
                indent=2,
            )
        )
        route = plugin._next_route(
            [
                _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR"),
                _route_packet("opus_auditor", "audit-cycle-1", retained_shape),
            ]
        )
        self.assertEqual(route.title, "cursor-web-opus-1-1")
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ, {"TRIPLE_STAMP_RUN_DIR": value}, clear=False
        ):
            for record in (
                _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR"),
                _route_packet("opus_auditor", "audit-cycle-1", retained_shape),
            ):
                runtime_state.append_dispatch(record)
                runtime_state.append_collection(record)
            result = plugin.supervisor_contract(enabled=True)(
                {
                    "type": "tool_call",
                    "data": {
                        "name": "sys_session_send",
                        "arguments": {
                            "agent": "cursor_workhorse",
                            "title": "cursor-web-opus-1-1",
                            "args": "canonical hunt reference",
                        },
                    },
                }
            )
            self.assertEqual(result["result"], "ALLOW")

    def test_noncanonical_transitions_are_not_project_policy_denied(self) -> None:
        records = [
            _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR"),
            _route_packet("opus_auditor", "audit-cycle-1", _audit("NEEDS_WEB")),
        ]
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ, {"TRIPLE_STAMP_RUN_DIR": value}, clear=False
        ):
            for record in records:
                runtime_state.append_dispatch(record)
                runtime_state.append_collection(record)
            contract = plugin.supervisor_contract(enabled=True)
            candidates = [
                ("cursor_workhorse", "cursor-cycle-1"),
                ("cursor_workhorse", "cursor-cycle-2"),
                ("opus_auditor", "audit-cycle-1"),
                ("codex_judge", "judge-cycle-1"),
                ("cursor_workhorse", "cursor-web-opus-1-2"),
                ("cursor_workhorse", "cursor-web-codex-1-1"),
            ]
            for agent, title in candidates:
                result = contract(
                    {
                        "type": "tool_call",
                        "data": {
                            "name": "sys_session_send",
                            "arguments": {
                                "agent": agent,
                                "title": title,
                                "args": "\n".join(r["output"] for r in records),
                            },
                        },
                    }
                )
                self.assertEqual(result["result"], "ALLOW", (agent, title))
            self.assertEqual(
                contract(
                    {
                        "type": "tool_call",
                        "data": {
                            "name": "sys_session_send",
                            "arguments": {
                                "agent": "cursor_workhorse",
                                "title": "cursor-web-opus-1-1",
                                "args": "canonical after denial",
                            },
                        },
                    }
                )["result"],
                "ALLOW",
            )

    def test_run_u9_real_handoff_and_repeated_title_are_allowed(self) -> None:
        fixture = json.loads(
            (ROOT / "tests/fixtures/run-u9demmy7-handoff.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            fixture["parent_session_id"],
            "f03fa18beaf4449fa7f4a0a6c71fbaeb",
        )
        self.assertEqual(fixture["recorded_input"]["cursor_cycle_1_literal_count"], 0)
        self.assertFalse(fixture["recorded_input"]["canonical_inbox_wrapper_prefix"])
        self.assertTrue(fixture["recorded_input"]["two_dispatch_inputs_were_identical"])
        probe = fixture["policy_probe_call"]
        self.assertEqual(
            probe["data"]["arguments"]["args"]["input"].count("cursor-cycle-1"),
            2,
        )
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value},
            clear=False,
        ):
            contract = plugin.supervisor_contract(enabled=True)
            self.assertEqual(contract(probe)["result"], "ALLOW")
            self.assertEqual(contract(probe)["result"], "ALLOW")

    def test_actual_send_schema_and_named_continuation_contract(self) -> None:
        from types import SimpleNamespace

        from omnigent.tools.builtins.spawn import _build_sys_session_send_schema

        spec = SimpleNamespace(
            description="auditor",
            executor=SimpleNamespace(config={}),
        )
        schema = _build_sys_session_send_schema({"opus_auditor": spec})
        parameters = schema["function"]["parameters"]
        self.assertEqual(parameters["required"], ["args"])
        self.assertIn("session_id", parameters["properties"])
        self.assertEqual(
            parameters["properties"]["agent"]["enum"], ["opus_auditor"]
        )
        description = schema["function"]["description"]
        self.assertIn("later calls continue it", description)
        self.assertIn("Reusing a title continues the same session", description)

    def test_dispatch_title_table_parses_every_canonical_form_and_boundaries(
        self,
    ) -> None:
        forms = (
            ("cursor-cycle-{cycle}", "cursor_grunt", "", False),
            ("audit-cycle-{cycle}", "audit", "opus", False),
            ("audit-cycle-{cycle}-web-{hop}", "audit_web", "opus", True),
            ("audit-retry-{cycle}-{hop}", "audit_retry", "opus", True),
            (
                "audit-internal-{cycle}-{hop}",
                "audit_internal",
                "codex",
                True,
            ),
            ("judge-cycle-{cycle}", "judge", "codex", False),
            (
                "judge-convergence-{cycle}",
                "judge_convergence",
                "codex",
                False,
            ),
            (
                "audit-format-repair-{cycle}",
                "audit_repair",
                "opus",
                False,
            ),
            (
                "audit-format-repair-{cycle}-web-{hop}",
                "audit_repair_web",
                "opus",
                True,
            ),
            (
                "judge-format-repair-{cycle}",
                "judge_repair",
                "codex",
                False,
            ),
            (
                "cursor-web-opus-{cycle}-{hop}",
                "cursor_web",
                "opus",
                True,
            ),
            (
                "cursor-web-codex-{cycle}-{hop}",
                "cursor_web",
                "codex",
                True,
            ),
        )
        expected_groups = {
            stage_id: 2 if has_hop else 1
            for _form, stage_id, _requester, has_hop in forms
        }
        self.assertEqual(len(runtime_state._DISPATCH_TITLE_PATTERNS), len(forms))
        for pattern, stage_id, _requester in (
            runtime_state._DISPATCH_TITLE_PATTERNS
        ):
            self.assertEqual(re.compile(pattern).groups, expected_groups[stage_id])

        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value},
            clear=False,
        ):
            for form, stage_id, requester, has_hop in forms:
                boundaries = (
                    ((1, 1), (4, 1))
                    if stage_id == "audit_retry"
                    else ((1, 1), (4, 2))
                )
                for cycle, hop in boundaries:
                    title = form.format(cycle=cycle, hop=hop)
                    with self.subTest(title=title):
                        runtime_state.append_dispatch(
                            {
                                "agent": "fixture",
                                "title": title,
                                "child_session_id": f"child-{title}",
                                "work_id": f"work-{title}",
                            }
                        )
                        parsed = runtime_state.read_dispatches()[-1]
                        self.assertEqual(parsed["stage_id"], stage_id)
                        self.assertEqual(parsed["cycle"], cycle)
                        self.assertEqual(parsed["hop"], hop if has_hop else 0)
                        self.assertEqual(parsed["requester"], requester)
                        self.assertEqual(
                            plugin._stage(title),
                            plugin._Stage(
                                stage_id,
                                cycle,
                                requester=requester,
                                hop=hop if has_hop else 0,
                            ),
                        )

            malformed: set[str] = set()
            for form, _stage_id, _requester, has_hop in forms:
                canonical = form.format(cycle=1, hop=1)
                malformed.update(
                    {
                        "prefix-" + canonical,
                        canonical + "-suffix",
                        form.format(cycle=0, hop=1),
                        form.format(cycle=5, hop=1),
                    }
                )
                if has_hop:
                    malformed.update(
                        {
                            form.format(cycle=1, hop=0),
                            form.format(cycle=1, hop=3),
                        }
                    )
                    if _stage_id == "audit_retry":
                        malformed.add(form.format(cycle=1, hop=2))
            for title in sorted(malformed):
                with self.subTest(malformed=title):
                    self.assertIsNone(runtime_state.parse_dispatch_title(title))
                    self.assertIsNone(plugin._stage(title))
                    runtime_state.append_dispatch(
                        {
                            "agent": "fixture",
                            "title": title,
                            "child_session_id": f"child-{title}",
                            "work_id": f"work-{title}",
                        }
                    )
                    record = runtime_state.read_dispatches()[-1]
                    for field in ("stage_id", "cycle", "hop", "requester"):
                        self.assertNotIn(field, record)

    def test_dispatch_observer_failure_is_diagnostic_and_fail_open(self) -> None:
        class Entry:
            work_id = "native-work"

        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value},
            clear=False,
        ), self.assertLogs(cursor_lifecycle.__name__, level="WARNING") as logs:
            native_result = json.dumps(
                {
                    "status": "launching",
                    "conversation_id": "native-child",
                }
            )

            def failed_append(_payload: object) -> None:
                raise IndexError("sensitive implementation detail")

            observed = cursor_lifecycle._observe_dispatch_bookkeeping(
                native_result,
                args={"args": {"input": "secret handoff"}},
                conversation_id="parent-turn",
                agent="opus_auditor",
                title="audit-internal-1-1",
                get_subagent_work=lambda _child: Entry(),
                append_dispatch=failed_append,
                record_tool_dispatch_exception=(
                    runtime_state.record_tool_dispatch_exception
                ),
            )
            self.assertEqual(observed, native_result)
            diagnostic = runtime_state.read_last_tool_dispatch_exception(
                parent_session_id="parent-turn",
                titles=["audit-internal-1-1"],
            )
            self.assertIsNotNone(diagnostic)
            assert diagnostic is not None
            self.assertEqual(diagnostic["phase"], "ledger_observer")
            self.assertEqual(diagnostic["native_send_status"], "launched")
            self.assertEqual(diagnostic["reason"], "IndexError")
            self.assertNotIn("sensitive implementation detail", json.dumps(diagnostic))
            self.assertTrue(
                (Path(value) / "last-tool-dispatch-exception.json").is_file()
            )
            self.assertIn("native_send_status=launched", "\n".join(logs.output))

    def test_missing_dispatch_diagnostic_distinguishes_native_and_observer(
        self,
    ) -> None:
        from types import SimpleNamespace

        route = SimpleNamespace(title="audit-internal-1-1", status="dispatch")
        observer = supervisor_runtime._missing_dispatch_reason(
            route,
            sent_titles=["audit-internal-1-1"],
            diagnostic={
                "phase": "ledger_observer",
                "native_send_status": "launched",
                "reason": "IndexError",
                "title": "audit-internal-1-1",
            },
            abstained=True,
        )
        self.assertIn("native send status=launched", observer)
        self.assertIn("ledger observer failure=IndexError", observer)
        self.assertNotIn("native send failure=IndexError", observer)

        native = supervisor_runtime._missing_dispatch_reason(
            route,
            sent_titles=["audit-internal-1-1"],
            diagnostic={
                "phase": "native_send",
                "native_send_status": "failed",
                "reason": "ReadTimeout",
                "title": "audit-internal-1-1",
            },
        )
        self.assertIn("native send failure=ReadTimeout", native)
        self.assertIn("ledger observer failure=none recorded", native)
        self.assertNotIn("stage-order", native)
        self.assertNotIn("routing condition", native)

    def test_misrouted_dispatch_spends_continuation_before_failing(self) -> None:
        """run-4gqa_fil proved a wrong title must not be fatal on sight.

        Cursor and a mechanically validated 16-call Opus audit were both
        collected, the durable route was `judge-cycle-1`, the supervisor sent
        `cursor-cycle-2`, and this branch had no outcome except
        PIPELINE_INFRASTRUCTURE_ERROR 19 minutes into a healthy run.
        """

        from omnigent.inner import claude_sdk_executor
        from omnigent.inner.executor import (
            ExecutorError,
            TextChunk,
            ToolCallRequest,
            TurnComplete,
        )

        original = claude_sdk_executor.ClaudeSDKExecutor.run_turn
        prompts: list[str] = []

        async def scripted(
            _self: object,
            messages: list[dict[str, object]],
            _tools: object,
            _system_prompt: object,
            _config: object = None,
        ):
            prompts.append(str(messages[-1].get("content") or ""))
            # Turn one takes the audit's own punch list as a routing order.
            # Turn two obeys the deterministic continuation prompt.
            wrong = len(prompts) == 1
            agent = "cursor_workhorse" if wrong else "codex_judge"
            title = "cursor-cycle-2" if wrong else "judge-cycle-1"
            yield ToolCallRequest(
                name="mcp__omnigent__sys_session_send",
                args={"agent": agent, "title": title},
            )
            if not wrong:
                runtime_state.append_dispatch(
                    {
                        "agent": agent,
                        "title": title,
                        "child_session_id": "judge-child",
                        "parent_session_id": "misroute-parent",
                        "work_id": "work-judge-cycle-1",
                    }
                )
            yield TextChunk("status prose must be suppressed")
            yield TurnComplete(response="status prose must be suppressed")

        class Supervisor:
            _agent_name = "triple-stamp"

        async def collect() -> list[object]:
            return [
                event
                async for event in claude_sdk_executor.ClaudeSDKExecutor.run_turn(
                    Supervisor(),
                    [
                        {
                            "role": "user",
                            "content": "route",
                            "session_id": "misroute-parent",
                        }
                    ],
                    [],
                    "route",
                    None,
                )
            ]

        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value, "TRIPLE_STAMP_RUN_ID": "fixture"},
            clear=False,
        ), self.assertLogs(supervisor_runtime.__name__, level="WARNING"):
            for record in (
                _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR"),
                _route_packet("opus_auditor", "audit-cycle-1", _audit("FAIL")),
            ):
                runtime_state.append_collection(
                    {**record, "parent_session_id": "misroute-parent"}
                )
            claude_sdk_executor.ClaudeSDKExecutor.run_turn = scripted
            try:
                supervisor_runtime.install_supervisor_continuation_guard()
                events = asyncio.run(collect())
            finally:
                claude_sdk_executor.ClaudeSDKExecutor.run_turn = original
            actions = [
                row["action"]
                for row in runtime_state.read_supervisor_continuations()
            ]

        # The misroute cost one bounded re-prompt, not the run.
        self.assertFalse(any(isinstance(event, ExecutorError) for event in events))
        self.assertEqual(len(prompts), 2)
        self.assertIn("misrouted_dispatch_continuation", actions)
        self.assertNotIn("continuation_failed", actions)
        self.assertIn("judge-cycle-1", prompts[1])
        self.assertIn("codex_judge", prompts[1])
        # Model prose stays suppressed; runtime narration is what the reader gets.
        streamed = "".join(
            event.text for event in events if isinstance(event, TextChunk)
        )
        self.assertNotIn("status prose must be suppressed", streamed)
        self.assertIn("cursor-cycle-2", streamed)
        self.assertIn("judge-cycle-1", streamed)
        self.assertEqual(
            [
                event.response
                for event in events
                if isinstance(event, TurnComplete)
            ],
            [""],
        )

    def test_exhausted_misroute_abstains_instead_of_killing_the_run(self) -> None:
        """run-4gqa_fil settled this empirically.

        The guard declared a misroute terminal at 20:08:27, the pipeline ignored
        the death notice, and cycle 2 STAMPed a clean answer at 20:28 with
        `gap_materiality: nonmaterial`. Recording a terminal failure here would
        have destroyed a good answer, and would also make the stop check at the
        top of the turn loop fire on a recoverable condition.
        """

        from omnigent.inner import claude_sdk_executor
        from omnigent.inner.executor import (
            ExecutorError,
            ToolCallRequest,
            TurnComplete,
        )

        original = claude_sdk_executor.ClaudeSDKExecutor.run_turn

        async def always_misroutes(
            _self: object,
            _messages: object,
            _tools: object,
            _system_prompt: object,
            _config: object = None,
        ):
            yield ToolCallRequest(
                name="mcp__omnigent__sys_session_send",
                args={"agent": "cursor_workhorse", "title": "cursor-cycle-2"},
            )
            yield TurnComplete(response="status prose")

        class Supervisor:
            _agent_name = "triple-stamp"

        async def collect() -> list[object]:
            return [
                event
                async for event in claude_sdk_executor.ClaudeSDKExecutor.run_turn(
                    Supervisor(),
                    [
                        {
                            "role": "user",
                            "content": "route",
                            "session_id": "abstain-parent",
                        }
                    ],
                    [],
                    "route",
                    None,
                )
            ]

        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value, "TRIPLE_STAMP_RUN_ID": "fixture"},
            clear=False,
        ), self.assertLogs(supervisor_runtime.__name__, level="WARNING"):
            for record in (
                _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR"),
                _route_packet("opus_auditor", "audit-cycle-1", _audit("FAIL")),
            ):
                runtime_state.append_collection(
                    {**record, "parent_session_id": "abstain-parent"}
                )
            claude_sdk_executor.ClaudeSDKExecutor.run_turn = always_misroutes
            try:
                supervisor_runtime.install_supervisor_continuation_guard()
                events = asyncio.run(collect())
            finally:
                claude_sdk_executor.ClaudeSDKExecutor.run_turn = original
            actions = [
                row["action"]
                for row in runtime_state.read_supervisor_continuations()
            ]
            self.assertEqual(runtime_state.read_terminal_failure(), "")

        self.assertFalse(any(isinstance(event, ExecutorError) for event in events))
        self.assertIn("misrouted_dispatch_continuation", actions)
        self.assertIn("misrouted_dispatch_abstained", actions)
        self.assertNotIn("continuation_failed", actions)
        # Abstaining ends the turn quietly and leaves the run alive.
        self.assertEqual(
            [event.response for event in events if isinstance(event, TurnComplete)],
            [""],
        )

    def test_empty_supervisor_exhaustion_abstains_without_terminal_failure(
        self,
    ) -> None:
        """Empty routing turns leave a recoverable route open and invisible."""

        from omnigent.inner import claude_sdk_executor
        from omnigent.inner.executor import (
            ExecutorError,
            TextChunk,
            ToolCallRequest,
            TurnComplete,
        )

        original = claude_sdk_executor.ClaudeSDKExecutor.run_turn
        turns = 0

        async def never_dispatches(
            _self: object,
            _messages: object,
            _tools: object,
            _system_prompt: object,
            _config: object = None,
        ):
            nonlocal turns
            turns += 1
            yield TextChunk("here is my own answer, which is never allowed")
            yield TurnComplete(response="here is my own answer, which is never allowed")

        class Supervisor:
            _agent_name = "triple-stamp"

        async def collect() -> list[object]:
            return [
                event
                async for event in claude_sdk_executor.ClaudeSDKExecutor.run_turn(
                    Supervisor(),
                    [
                        {
                            "role": "user",
                            "content": "route",
                            "session_id": "terminal-parent",
                        }
                    ],
                    [],
                    "route",
                    None,
                )
            ]

        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value, "TRIPLE_STAMP_RUN_ID": "fixture"},
            clear=False,
        ), self.assertLogs(supervisor_runtime.__name__, level="WARNING"):
            for record in (
                _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR"),
                _route_packet("opus_auditor", "audit-cycle-1", _audit("FAIL")),
            ):
                runtime_state.append_collection(
                    {**record, "parent_session_id": "terminal-parent"}
                )
            claude_sdk_executor.ClaudeSDKExecutor.run_turn = never_dispatches
            try:
                supervisor_runtime.install_supervisor_continuation_guard()
                failing = asyncio.run(collect())
                after = asyncio.run(collect())
            finally:
                claude_sdk_executor.ClaudeSDKExecutor.run_turn = original
            recorded = runtime_state.read_terminal_failure(
                parent_session_id="terminal-parent"
            )
            actions = [
                row["action"]
                for row in runtime_state.read_supervisor_continuations()
            ]
            dispatches_after = len(runtime_state.read_dispatches())

        # Neither the supervisor's prose nor a fabricated infrastructure error
        # reaches the reader. A later wake may retry the still-open route.
        self.assertFalse(any(isinstance(event, ExecutorError) for event in failing))
        streamed = "".join(
            event.text for event in failing if isinstance(event, TextChunk)
        )
        self.assertNotIn("here is my own answer", streamed)
        self.assertEqual(streamed, "")
        self.assertEqual(recorded, "")
        self.assertEqual(
            [
                event.response
                for event in failing
                if isinstance(event, TurnComplete)
            ],
            [""],
        )

        self.assertEqual(turns, 6)
        self.assertEqual(dispatches_after, 0)
        self.assertFalse(any(isinstance(event, TextChunk) for event in after))
        self.assertEqual(
            [event.response for event in after if isinstance(event, TurnComplete)],
            [""],
        )
        self.assertEqual(
            actions,
            [
                "continuation_enqueued",
                "continuation_enqueued",
                "continuation_exhausted_abstained",
                "continuation_enqueued",
                "continuation_enqueued",
                "continuation_exhausted_abstained",
            ],
        )

    def test_supervisor_abstains_after_recorded_dispatch_observer_failure(
        self,
    ) -> None:
        from omnigent.inner import claude_sdk_executor
        from omnigent.inner.executor import (
            ExecutorError,
            TextChunk,
            ToolCallRequest,
            TurnComplete,
        )

        original = claude_sdk_executor.ClaudeSDKExecutor.run_turn
        scripted_calls = 0

        async def scripted(
            _self: object,
            _messages: object,
            _tools: object,
            _system_prompt: object,
            _config: object = None,
        ):
            nonlocal scripted_calls
            scripted_calls += 1
            title = "audit-internal-1-1"
            if scripted_calls == 1:
                yield ToolCallRequest(
                    name="mcp__omnigent__sys_session_send",
                    args={"agent": "opus_auditor", "title": title},
                )
                runtime_state.record_tool_dispatch_exception(
                    parent_session_id="observer-parent",
                    title=title,
                    phase="ledger_observer",
                    native_send_status="launched",
                    exc=IndexError("must remain sanitized"),
                )
            yield TextChunk("status prose must be suppressed")
            yield TurnComplete(response="status prose must be suppressed")

        class Supervisor:
            _agent_name = "triple-stamp"

        async def collect() -> list[object]:
            return [
                event
                async for event in claude_sdk_executor.ClaudeSDKExecutor.run_turn(
                    Supervisor(),
                    [
                        {
                            "role": "user",
                            "content": "route",
                            "session_id": "observer-parent",
                        }
                    ],
                    [],
                    "route",
                    None,
                )
            ]

        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value, "TRIPLE_STAMP_RUN_ID": "fixture"},
            clear=False,
        ), self.assertLogs(supervisor_runtime.__name__, level="WARNING"):
            for record in (
                _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR"),
                _route_packet("opus_auditor", "audit-cycle-1", _audit("FAIL")),
                _route_packet(
                    "codex_judge",
                    "judge-cycle-1",
                    _judgment("NEEDS_INTERNAL"),
                ),
            ):
                runtime_state.append_collection(
                    {**record, "parent_session_id": "observer-parent"}
                )
            claude_sdk_executor.ClaudeSDKExecutor.run_turn = scripted
            try:
                supervisor_runtime.install_supervisor_continuation_guard()
                events = asyncio.run(collect())
                later_events = asyncio.run(collect())
            finally:
                claude_sdk_executor.ClaudeSDKExecutor.run_turn = original
            self.assertFalse(any(isinstance(event, ExecutorError) for event in events))
            self.assertFalse(
                any(isinstance(event, ExecutorError) for event in later_events)
            )
            # Model prose stays suppressed. The runtime narrates the stage on
            # the turn that dispatched it, and never repeats it on a later wake.
            first = "".join(
                event.text for event in events if isinstance(event, TextChunk)
            )
            self.assertNotIn("status prose must be suppressed", first)
            self.assertIn("audit-internal-1-1", first)
            self.assertFalse(
                any(isinstance(event, TextChunk) for event in later_events)
            )
            completions = [
                event for event in events if isinstance(event, TurnComplete)
            ]
            self.assertEqual([event.response for event in completions], [""])
            later_completions = [
                event for event in later_events if isinstance(event, TurnComplete)
            ]
            self.assertEqual(
                [event.response for event in later_completions],
                [""],
            )
            continuations = runtime_state.read_supervisor_continuations()
            continuation = continuations[-2]
            self.assertEqual(
                continuation["action"],
                "dispatch_observer_failure_abstained",
            )
            reason = continuation["reason"]
            self.assertIn(
                "actual title(s) sent in turn=['audit-internal-1-1']",
                reason,
            )
            self.assertIn("native send status=launched", reason)
            self.assertIn("ledger observer failure=IndexError", reason)
            self.assertIn("guard abstained", reason)
            self.assertEqual(
                continuations[-1]["action"],
                "continuation_abstained_observer_failure",
            )
            self.assertEqual(scripted_calls, 2)
            self.assertFalse((Path(value) / "failure-attestation.json").exists())

    def test_audit_verdict_and_needs_web_matrix_is_consistent(self) -> None:
        for verdict in ("PASS", "PASS_WITH_GAPS", "FAIL", "NEEDS_WEB"):
            for needs_web in (False, True):
                payload = json.loads(_audit(verdict))
                payload["needs_web"] = needs_web
                expected_valid = needs_web is (verdict == "NEEDS_WEB")
                with self.subTest(verdict=verdict, needs_web=needs_web):
                    parsed = plugin._valid_audit(json.dumps(payload))
                    self.assertEqual(parsed is not None, expected_valid)

        needs_web = json.loads(_audit("NEEDS_WEB"))
        for hunts in ([], [{"claim": "not a complete hunt"}], ["not a mapping"]):
            candidate = dict(needs_web, web_queries=hunts)
            with self.subTest(hunts=hunts):
                self.assertIsNone(plugin._valid_audit(json.dumps(candidate)))

        for verdict in ("PASS", "PASS_WITH_GAPS", "FAIL", "NEEDS_WEB"):
            candidate = json.loads(_audit(verdict))
            candidate.pop("needs_web")
            with self.subTest(missing_flag=verdict):
                self.assertIsNone(plugin._valid_audit(json.dumps(candidate)))

    def test_retained_fail_needs_web_contradiction_routes_to_one_repair(
        self,
    ) -> None:
        contradictory = json.loads(_audit("FAIL"))
        contradictory["needs_web"] = True
        contradictory["web_queries"] = [_web_hunt()]
        output = json.dumps(contradictory)
        self.assertIsNone(plugin._valid_audit(output))
        route = plugin._next_route(
            [
                _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR"),
                _route_packet("opus_auditor", "audit-cycle-1", output),
            ]
        )
        self.assertEqual(
            (route.status, route.agent, route.title),
            ("dispatch", "opus_auditor", "audit-format-repair-1"),
        )

    def test_retained_run_fixture_proves_routing_and_observers_were_correct(
        self,
    ) -> None:
        fixture = json.loads(
            (
                ROOT
                / "tests/fixtures/run-04grs3i-dispatch-observer-regression.json"
            ).read_text(encoding="utf-8")
        )
        observation = fixture["internal_mcp_observation"]
        self.assertTrue(observation["available"])
        self.assertEqual(
            observation["by_system"],
            {
                "glean": 8,
                "safe": 6,
                "confluence": 2,
                "jira": 2,
                "slack": 1,
            },
        )
        self.assertEqual(len(observation["call_ids"]), observation["count"])
        effort = fixture["opus_effort_observation"]
        self.assertEqual((effort["outcome"], effort["assistant_rows"]), ("all_max", 43))
        audit = fixture["audit"]
        self.assertEqual(audit["internal_sources_count"], 11)
        self.assertTrue(
            all(
                receipt["status"] == "evidence_found"
                for receipt in audit["internal_coverage"].values()
            )
        )
        for system in ("slack", "confluence"):
            self.assertEqual(
                audit["internal_coverage"][system]["routes"],
                ["native", "glean_facet"],
            )
        retained = fixture["retained_stage"]
        route = plugin._next_route(
            [
                _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR"),
                _route_packet("opus_auditor", "audit-cycle-1", _audit("FAIL")),
                _route_packet(
                    "codex_judge",
                    "judge-cycle-1",
                    _judgment(retained["judge_verdict"]),
                ),
            ]
        )
        self.assertEqual(
            (route.agent, route.title),
            (retained["expected_next_agent"], retained["expected_next_title"]),
        )
        failure = fixture["ledger_failure"]
        before = re.fullmatch(
            failure["pattern_before"],
            retained["expected_next_title"],
        )
        self.assertIsNotNone(before)
        assert before is not None
        self.assertEqual(before.groups(), ("1",))
        self.assertEqual(
            runtime_state.parse_dispatch_title(retained["expected_next_title"]),
            {
                "stage_id": "audit_internal",
                "cycle": 1,
                "hop": 1,
                "requester": "codex",
            },
        )

    def test_dispatch_ledger_never_marks_opus_for_native_resume(self) -> None:
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ, {"TRIPLE_STAMP_RUN_DIR": value}, clear=False
        ):
            first = _route_packet(
                "opus_auditor", "audit-cycle-1", "one", child="same-child"
            )
            runtime_state.append_dispatch(first)
            second = dict(first, work_id="work-continued", output="two")
            runtime_state.append_dispatch(second)
            records = runtime_state.read_dispatches()
            self.assertEqual(records[0]["stage_id"], "audit")
            self.assertEqual(records[0]["cycle"], 1)
            self.assertEqual(records[0]["requester"], "opus")
            self.assertNotIn("resume_child_session_id", records[1])

    def test_final_cycle_rework_returns_completed_answer_with_gaps(self) -> None:
        parent = "parent-final-cycle-rework"
        first = json.loads(_judgment("REWORK"))
        first["punch_list_for_cursor"][0]["claim"] = "cycle one gap"
        second = json.loads(_judgment("REWORK"))
        second["punch_list_for_cursor"][0]["claim"] = "cycle two gap"
        second["best_supported_answer"] = (
            "The supported customer conclusion is still available."
        )
        records = [
            {
                **packet,
                "parent_session_id": parent,
            }
            for packet in (
                _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR-1"),
                _route_packet("opus_auditor", "audit-cycle-1", _audit("FAIL")),
                _route_packet(
                    "codex_judge",
                    "judge-cycle-1",
                    json.dumps(first),
                ),
                _route_packet("cursor_workhorse", "cursor-cycle-2", "CURSOR-2"),
                _route_packet("opus_auditor", "audit-cycle-2", _audit("FAIL")),
                _route_packet(
                    "codex_judge",
                    "judge-cycle-2",
                    json.dumps(second),
                ),
            )
        ]
        route = plugin._next_route(records, parent_session_id=parent)
        self.assertEqual(route.status, "best_effort")
        answer = plugin._best_effort_answer(records, route)
        self.assertIsNotNone(answer)
        assert answer is not None
        self.assertTrue(answer.startswith(second["best_supported_answer"]))
        self.assertIn("Remaining evidence gaps:", answer)
        self.assertIn("cycle two gap", answer)
        self.assertFalse(answer.startswith("PIPELINE_"))

        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value},
            clear=False,
        ):
            for record in records:
                runtime_state.append_collection(record)
            contract = plugin.supervisor_contract(enabled=True)
            rejected = contract(
                {
                    "type": "response",
                    "context": {
                        "conversation_id": parent,
                        "root_conversation_id": parent,
                    },
                    "data": "PIPELINE_VALIDATION_FAILED: final cycle",
                }
            )
            self.assertEqual(rejected["result"], "DENY")
            accepted = contract(
                {
                    "type": "response",
                    "context": {
                        "conversation_id": parent,
                        "root_conversation_id": parent,
                    },
                    "data": answer,
                }
            )
            self.assertEqual(accepted["result"], "ALLOW")
            self.assertEqual(
                runtime_state.read_best_effort_answer(parent),
                answer,
            )
            self.assertEqual(runtime_state.read_terminal_failure(parent), "")
            self.assertEqual(
                launcher._validated_pipeline_exit(
                    Path(value),
                    70,
                    [],
                    parent_session_id=parent,
                ),
                0,
            )

    def test_final_cycle_internal_hop_exhaustion_returns_answer(self) -> None:
        judgment = _judgment("NEEDS_INTERNAL")
        records = [
            _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR-1"),
            _route_packet("opus_auditor", "audit-cycle-1", _audit("FAIL")),
            _route_packet("codex_judge", "judge-cycle-1", _judgment("REWORK")),
            _route_packet("cursor_workhorse", "cursor-cycle-2", "CURSOR-2"),
            _route_packet("opus_auditor", "audit-cycle-2", _audit("FAIL")),
            _route_packet("codex_judge", "judge-cycle-2", judgment),
            _route_packet(
                "opus_auditor",
                "audit-internal-2-1",
                _audit("PASS_WITH_GAPS"),
            ),
            _route_packet("codex_judge", "judge-cycle-2", judgment),
            _route_packet(
                "opus_auditor",
                "audit-internal-2-2",
                _audit("PASS_WITH_GAPS"),
            ),
            _route_packet("codex_judge", "judge-cycle-2", judgment),
        ]
        route = plugin._next_route(records)
        self.assertEqual(route.status, "best_effort")
        answer = plugin._best_effort_answer(records, route)
        self.assertIsNotNone(answer)
        assert answer is not None
        self.assertIn("verify internal claim", answer)
        self.assertFalse(answer.startswith("PIPELINE_"))

    def test_opus_web_hop_exhaustion_uses_completed_cursor_draft(self) -> None:
        cursor = (
            "question_restated: Verify the feature.\n"
            "suggested_customer_answer_draft: The feature is supported by the "
            "completed public evidence.\n"
            "weakest_part: One live source still needs confirmation."
        )
        records = [
            _route_packet("cursor_workhorse", "cursor-cycle-1", cursor),
            _route_packet("opus_auditor", "audit-cycle-1", _audit("NEEDS_WEB")),
            _route_packet(
                "cursor_workhorse",
                "cursor-web-opus-1-1",
                "WEB-1",
            ),
            _route_packet(
                "opus_auditor",
                "audit-cycle-1-web-1",
                _audit("NEEDS_WEB"),
            ),
            _route_packet(
                "cursor_workhorse",
                "cursor-web-opus-1-2",
                "WEB-2",
            ),
            _route_packet(
                "opus_auditor",
                "audit-cycle-1-web-2",
                _audit("NEEDS_WEB"),
            ),
        ]
        route = plugin._next_route(records)
        self.assertEqual(route.status, "best_effort")
        answer = plugin._best_effort_answer(records, route)
        self.assertIsNotNone(answer)
        assert answer is not None
        self.assertTrue(answer.startswith("The feature is supported"))
        self.assertIn("Remaining evidence gaps:", answer)
        self.assertFalse(answer.startswith("PIPELINE_"))

    def test_runtime_relays_bounded_answer_instead_of_pipeline_failure(self) -> None:
        from omnigent.inner import claude_sdk_executor
        from omnigent.inner.executor import TextChunk, TurnComplete

        parent = "parent-runtime-bounded-answer"
        second = json.loads(_judgment("REWORK"))
        second["punch_list_for_cursor"][0]["claim"] = "distinct final gap"
        second["best_supported_answer"] = "Here is the supported customer answer."
        records = [
            _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR-1"),
            _route_packet("opus_auditor", "audit-cycle-1", _audit("FAIL")),
            _route_packet("codex_judge", "judge-cycle-1", _judgment("REWORK")),
            _route_packet("cursor_workhorse", "cursor-cycle-2", "CURSOR-2"),
            _route_packet("opus_auditor", "audit-cycle-2", _audit("FAIL")),
            _route_packet(
                "codex_judge",
                "judge-cycle-2",
                json.dumps(second),
            ),
        ]
        original = claude_sdk_executor.ClaudeSDKExecutor.run_turn

        async def scripted(*_args: object, **_kwargs: object):
            yield TextChunk("PIPELINE_VALIDATION_FAILED: must be suppressed")
            yield TurnComplete(
                response="PIPELINE_VALIDATION_FAILED: must be suppressed"
            )

        class Supervisor:
            _agent_name = "triple-stamp"

        async def collect() -> list[object]:
            return [
                event
                async for event in claude_sdk_executor.ClaudeSDKExecutor.run_turn(
                    Supervisor(),
                    [
                        {
                            "role": "user",
                            "content": "finish",
                            "session_id": parent,
                        }
                    ],
                    [],
                    "route",
                    None,
                )
            ]

        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value, "TRIPLE_STAMP_RUN_ID": "fixture"},
            clear=False,
        ):
            for packet in records:
                runtime_state.append_collection(
                    {**packet, "parent_session_id": parent}
                )
            claude_sdk_executor.ClaudeSDKExecutor.run_turn = scripted
            try:
                supervisor_runtime.install_supervisor_continuation_guard()
                events = asyncio.run(collect())
            finally:
                claude_sdk_executor.ClaudeSDKExecutor.run_turn = original
            persisted = runtime_state.read_best_effort_answer(parent)

        rendered = "".join(
            event.text for event in events if isinstance(event, TextChunk)
        )
        self.assertEqual(rendered, persisted)
        self.assertTrue(rendered.startswith(second["best_supported_answer"]))
        self.assertNotIn("PIPELINE_VALIDATION_FAILED", rendered)
        self.assertEqual(
            [
                event.response
                for event in events
                if isinstance(event, TurnComplete)
            ],
            [persisted],
        )

    def test_prior_stamp_cannot_be_replaced_by_later_rework_or_sibling(self) -> None:
        parent = "parent-prior-stamp"
        sibling = "parent-sibling-failure"
        answer = "The cycle-one answer remains authoritative."
        stamp = json.dumps(
            {
                "verdict": "STAMP",
                "needs_web": False,
                "needs_internal": False,
                "why": "cycle one evidence is complete",
                "gap_materiality": "none",
                "limitations": [],
                "citations_that_hold": ["cycle one evidence"],
                "voice_profile_check": {
                    "source_path": "",
                    "sha256": "",
                    "constraints_applied": "none",
                },
                "shippable_answer": answer,
            }
        )
        cycle_one = [
            _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR-1"),
            _route_packet("opus_auditor", "audit-cycle-1", _audit("PASS")),
            _route_packet("codex_judge", "judge-cycle-1", stamp),
        ]
        cycle_two = [
            _route_packet("cursor_workhorse", "cursor-cycle-2", "CURSOR-2"),
            _route_packet("opus_auditor", "audit-cycle-2", _audit("FAIL")),
            _route_packet("codex_judge", "judge-cycle-2", _judgment("REWORK")),
        ]
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {
                "TRIPLE_STAMP_RUN_DIR": value,
                "TRIPLE_STAMP_VOICE_PROFILE": "",
                "TRIPLE_STAMP_VOICE_PROFILE_SHA256": "",
            },
            clear=False,
        ):
            for packet in cycle_one:
                record = {**packet, "parent_session_id": parent}
                runtime_state.append_collection(record)
                if packet["agent"] == "codex_judge":
                    self.assertTrue(runtime_state.attest_codex_stamp(record))
            for packet in cycle_two:
                runtime_state.append_collection(
                    {**packet, "parent_session_id": parent}
                )
            sibling_failure = runtime_state.record_terminal_failure(
                "PIPELINE_VALIDATION_FAILED",
                "sibling exhausted",
                cycle=2,
                parent_session_id=sibling,
            )
            contract = plugin.supervisor_contract(enabled=True)
            rejected = contract(
                {
                    "type": "response",
                    "context": {
                        "conversation_id": parent,
                        "root_conversation_id": parent,
                    },
                    "data": "PIPELINE_VALIDATION_FAILED: later rework",
                }
            )
            self.assertEqual(rejected["result"], "DENY")
            self.assertEqual(
                runtime_state.record_terminal_failure(
                    "PIPELINE_VALIDATION_FAILED",
                    "same parent later rework",
                    cycle=2,
                    parent_session_id=parent,
                ),
                answer,
            )
            self.assertEqual(runtime_state.read_attested_answer(parent), answer)
            self.assertEqual(runtime_state.read_terminal_failure(parent), "")
            self.assertEqual(
                runtime_state.read_terminal_failure(sibling),
                sibling_failure,
            )

    def test_same_cycle_nonstamp_retries_do_not_authorize_validation(self) -> None:
        parent = "parent-same-cycle-judgments"
        records = [
            _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR"),
            _route_packet("opus_auditor", "audit-cycle-1", _audit("FAIL")),
            _route_packet(
                "codex_judge",
                "judge-cycle-1",
                _judgment("NEEDS_INTERNAL"),
            ),
            _route_packet(
                "opus_auditor",
                "audit-internal-1-1",
                _audit("PASS_WITH_GAPS"),
            ),
            _route_packet(
                "codex_judge",
                "judge-cycle-1",
                _judgment("NEEDS_INTERNAL"),
            ),
        ]
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value},
            clear=False,
        ):
            for packet in records:
                runtime_state.append_collection(
                    {**packet, "parent_session_id": parent}
                )
            self.assertFalse(plugin._max_unstamped_judgments(parent))
            decision = plugin.supervisor_contract(enabled=True)(
                {
                    "type": "response",
                    "context": {
                        "conversation_id": parent,
                        "root_conversation_id": parent,
                    },
                    "data": "PIPELINE_VALIDATION_FAILED: duplicate judgments",
                }
            )
            self.assertEqual(decision["result"], "DENY")
            self.assertEqual(runtime_state.read_terminal_failure(parent), "")

    def test_incomplete_cursor_packet_is_not_a_fallback_answer(self) -> None:
        second = json.loads(_judgment("REWORK"))
        second["punch_list_for_cursor"][0]["claim"] = "distinct cycle two gap"
        records = [
            _route_packet("cursor_workhorse", "cursor-cycle-1", "CURSOR-1"),
            _route_packet("opus_auditor", "audit-cycle-1", _audit("FAIL")),
            _route_packet("codex_judge", "judge-cycle-1", _judgment("REWORK")),
            _route_packet(
                "cursor_workhorse",
                "cursor-cycle-2",
                "CURSOR_FINALIZATION_REQUIRED: partial output",
            ),
            _route_packet("opus_auditor", "audit-cycle-2", _audit("FAIL")),
            _route_packet(
                "codex_judge",
                "judge-cycle-2",
                json.dumps(second),
            ),
        ]
        route = plugin._next_route(records)
        self.assertEqual(route.status, "best_effort")
        self.assertIsNone(plugin._best_effort_answer(records, route))

    def test_parent_scoped_pending_and_resume_ignore_same_title_sibling(
        self,
    ) -> None:
        parent_a = "parent-math"
        parent_b = "parent-lakebase"
        route = plugin._Route(
            "dispatch",
            1,
            "cursor_workhorse",
            "cursor-cycle-1",
        )
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value},
            clear=False,
        ):
            runtime_state.append_dispatch(
                {
                    "parent_session_id": parent_a,
                    "child_session_id": "math-child",
                    "work_id": "math-work",
                    "agent": "cursor_workhorse",
                    "title": "cursor-cycle-1",
                }
            )
            runtime_state.append_dispatch(
                {
                    "parent_session_id": parent_b,
                    "child_session_id": "lakebase-child",
                    "work_id": "lakebase-work",
                    "agent": "cursor_workhorse",
                    "title": "cursor-cycle-1",
                }
            )
            dispatches = runtime_state.read_dispatches()
            self.assertNotIn(
                "resume_child_session_id",
                runtime_state.read_dispatches(
                    parent_session_id=parent_b
                )[-1],
            )
            self.assertTrue(
                plugin._route_dispatch_pending(
                    route,
                    [],
                    dispatches,
                    parent_session_id=parent_a,
                )
            )
            runtime_state.append_collection(
                {
                    "parent_session_id": parent_a,
                    "child_session_id": "math-child",
                    "work_id": "math-work",
                    "agent": "cursor_workhorse",
                    "title": "cursor-cycle-1",
                    "status": "completed",
                    "output": "4",
                }
            )
            self.assertFalse(
                plugin._route_dispatch_pending(
                    route,
                    runtime_state.read_collections(),
                    dispatches,
                    parent_session_id=parent_a,
                )
            )
            self.assertTrue(
                plugin._route_dispatch_pending(
                    route,
                    runtime_state.read_collections(),
                    dispatches,
                    parent_session_id=parent_b,
                )
            )

    def test_foreign_judge_resume_never_crosses_parent_merge_gate(self) -> None:
        foreign_targets = (
            "df571-judge-child",
            "b1f6-judge-child",
            "d0e2-judge-child",
            "bc89-judge-child",
        )
        for foreign_child in foreign_targets:
            with self.subTest(foreign_child=foreign_child), tempfile.TemporaryDirectory() as value, mock.patch.dict(
                os.environ,
                {
                    "TRIPLE_STAMP_RUN_DIR": value,
                    "TRIPLE_STAMP_VOICE_PROFILE": "",
                    "TRIPLE_STAMP_VOICE_PROFILE_SHA256": "",
                },
                clear=False,
            ):
                parent_math = f"parent-math-{foreign_child}"
                parent_lakebase = f"parent-lakebase-{foreign_child}"
                lakebase_child = f"lakebase-{foreign_child}"
                answer = f"Math answer isolated from {foreign_child}."
                stamp = json.dumps(
                    {
                        "verdict": "STAMP",
                        "needs_web": False,
                        "needs_internal": False,
                        "why": "math evidence is complete",
                        "gap_materiality": "none",
                        "limitations": [],
                        "citations_that_hold": ["math packet"],
                        "voice_profile_check": {
                            "source_path": "",
                            "sha256": "",
                            "constraints_applied": "none",
                        },
                        "shippable_answer": answer,
                    }
                )

                math_packets = (
                    _route_packet(
                        "cursor_workhorse",
                        "cursor-cycle-1",
                        "MATH-EVIDENCE",
                    ),
                    _route_packet(
                        "opus_auditor",
                        "audit-cycle-1",
                        _audit("PASS"),
                    ),
                    _route_packet(
                        "codex_judge",
                        "judge-cycle-1",
                        _judgment("NEEDS_WEB"),
                        child=foreign_child,
                    ),
                    _route_packet(
                        "cursor_workhorse",
                        "cursor-web-codex-1-1",
                        "MATH-WEB",
                    ),
                    _route_packet(
                        "codex_judge",
                        "judge-cycle-1",
                        stamp,
                        child=foreign_child,
                    ),
                )
                for packet in math_packets:
                    runtime_state.append_collection(
                        {**packet, "parent_session_id": parent_math}
                    )
                self.assertTrue(
                    runtime_state.attest_codex_stamp(
                        {
                            **math_packets[-1],
                            "parent_session_id": parent_math,
                        }
                    )
                )

                lakebase_packets = (
                    _route_packet(
                        "cursor_workhorse",
                        "cursor-cycle-1",
                        "LAKEBASE-EVIDENCE-LONGER",
                    ),
                    _route_packet(
                        "opus_auditor",
                        "audit-cycle-1",
                        _audit("PASS_WITH_GAPS"),
                    ),
                    _route_packet(
                        "codex_judge",
                        "judge-cycle-1",
                        _judgment("NEEDS_WEB"),
                        child=lakebase_child,
                    ),
                    _route_packet(
                        "cursor_workhorse",
                        "cursor-web-codex-1-1",
                        "LAKEBASE-WEB-EVIDENCE",
                    ),
                )
                for packet in lakebase_packets:
                    runtime_state.append_collection(
                        {**packet, "parent_session_id": parent_lakebase}
                    )

                math_records = runtime_state.read_collections(parent_math)
                lakebase_records = runtime_state.read_collections(
                    parent_lakebase
                )
                math_route = plugin._next_route(
                    math_records,
                    parent_session_id=parent_math,
                )
                lakebase_route = plugin._next_route(
                    lakebase_records,
                    parent_session_id=parent_lakebase,
                )
                self.assertEqual(math_route.status, "success")
                self.assertEqual(lakebase_route.title, "judge-cycle-1")
                self.assertEqual(
                    lakebase_route.resume_child_session_id,
                    lakebase_child,
                )
                self.assertNotEqual(
                    lakebase_route.resume_child_session_id,
                    foreign_child,
                )
                self.assertEqual(
                    {row["parent_session_id"] for row in math_records},
                    {parent_math},
                )
                self.assertEqual(
                    {
                        row["parent_session_id"]
                        for row in lakebase_records
                    },
                    {parent_lakebase},
                )
                self.assertIn('"verdict": "STAMP"', math_records[-1]["output"])
                self.assertIn(
                    '"verdict": "NEEDS_WEB"',
                    lakebase_records[-2]["output"],
                )
                self.assertNotIn(
                    '"verdict": "STAMP"',
                    json.dumps(lakebase_records),
                )

                math_note = supervisor_runtime._progress_note(
                    "cursor_workhorse",
                    "cursor-cycle-2",
                    set(),
                    parent_session_id=parent_math,
                )
                lakebase_note = supervisor_runtime._progress_note(
                    "codex_judge",
                    "judge-cycle-1",
                    set(),
                    parent_session_id=parent_lakebase,
                )
                self.assertIn(
                    f"judge-cycle-1, {len(stamp.encode('utf-8')):,} bytes",
                    math_note,
                )
                self.assertNotIn("cursor-web-codex-1-1", math_note)
                self.assertIn(
                    "cursor-web-codex-1-1, "
                    f"{len(b'LAKEBASE-WEB-EVIDENCE'):,} bytes",
                    lakebase_note,
                )
                self.assertNotIn(
                    f"{len(stamp.encode('utf-8')):,} bytes",
                    lakebase_note,
                )

                failure = runtime_state.record_terminal_failure(
                    "PIPELINE_VALIDATION_FAILED",
                    "Lakebase exhausted its bounded web route",
                    stage="cursor-web-codex-1-1",
                    cycle=1,
                    parent_session_id=parent_lakebase,
                )
                self.assertEqual(
                    runtime_state.read_attested_answer(parent_math),
                    answer,
                )
                self.assertEqual(
                    runtime_state.read_terminal_failure(parent_math),
                    "",
                )
                self.assertEqual(
                    runtime_state.read_attested_answer(parent_lakebase),
                    "",
                )
                self.assertEqual(
                    runtime_state.read_terminal_failure(parent_lakebase),
                    failure,
                )

    def test_parent_attempt_generation_resets_latches_budgets_and_retries(
        self,
    ) -> None:
        parent = "same-chat-parent"
        first_messages = [
            {
                "role": "user",
                "content": "first question",
                "session_id": parent,
            }
        ]
        second_messages = [
            *first_messages,
            {"role": "assistant", "content": "failed"},
            {
                "role": "user",
                "content": "please retry",
                "session_id": parent,
            },
        ]
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value},
            clear=False,
        ):
            first_identity = supervisor_runtime._attempt_request_identity(
                first_messages
            )
            second_identity = supervisor_runtime._attempt_request_identity(
                second_messages
            )
            self.assertEqual(
                runtime_state.activate_parent_attempt(
                    parent,
                    first_identity,
                ),
                1,
            )
            runtime_state.append_dispatch(
                {
                    "parent_session_id": parent,
                    "child_session_id": "first-child",
                    "work_id": "first-work",
                    "agent": "cursor_workhorse",
                    "title": "cursor-cycle-1",
                }
            )
            runtime_state.append_supervisor_tool_call(
                "sys_session_send",
                parent,
            )
            runtime_state.append_supervisor_continuation(
                {
                    "parent_session_id": parent,
                    "action": "continuation_exhausted",
                }
            )
            runtime_state.write_budget_state(
                49.0,
                50.0,
                False,
                reported_usd=49.0,
                parent_session_id=parent,
                observed_cost_usd=49.0,
                observed_reported_usd=49.0,
            )
            first_failure = runtime_state.record_terminal_failure(
                "PIPELINE_INFRASTRUCTURE_ERROR",
                "first attempt failed",
                stage="cursor-cycle-1",
                cycle=1,
                parent_session_id=parent,
            )
            first_failure_path = runtime_state._parent_artifact_path(
                Path(value),
                "terminal-failure.txt",
                parent,
            ).resolve()
            first_failure_bytes = first_failure_path.read_bytes()

            self.assertEqual(
                runtime_state.activate_parent_attempt(
                    parent,
                    first_identity,
                ),
                1,
            )
            self.assertEqual(
                runtime_state.read_terminal_failure(parent),
                first_failure,
            )

            self.assertEqual(
                runtime_state.activate_parent_attempt(
                    parent,
                    second_identity,
                ),
                2,
            )
            self.assertEqual(runtime_state.read_dispatches(parent), [])
            runtime_state.append_collection(
                {
                    "parent_session_id": parent,
                    "child_session_id": "first-child",
                    "work_id": "first-work",
                    "agent": "cursor_workhorse",
                    "title": "cursor-cycle-1",
                    "status": "completed",
                    "output": "late completion from attempt one",
                }
            )
            self.assertEqual(runtime_state.read_collections(parent), [])
            self.assertEqual(
                runtime_state.read_supervisor_continuations(parent),
                [],
            )
            self.assertEqual(
                runtime_state.read_supervisor_tool_calls(parent),
                [],
            )
            self.assertEqual(runtime_state.read_budget_state(parent), {})
            self.assertEqual(runtime_state.read_terminal_failure(parent), "")
            self.assertEqual(runtime_state.read_attested_answer(parent), "")
            self.assertEqual(
                first_failure_path.read_bytes(),
                first_failure_bytes,
            )

            snapshot = {
                "available": True,
                "source": "conversation_store",
                "total_usd": 51.0,
                "reported_usd": 51.0,
                "estimated_unpriced_usd": 0.0,
                "sessions": [],
            }
            with mock.patch.object(
                plugin,
                "_stored_cost_snapshot",
                return_value=snapshot,
            ):
                decision = plugin.strict_cost_budget(50.0)(
                    {
                        "type": "request",
                        "context": {
                            "conversation_id": parent,
                            "root_conversation_id": parent,
                        },
                    }
                )
            self.assertEqual(decision["result"], "ALLOW")
            self.assertEqual(
                runtime_state.read_budget_state(parent)["cost_usd"],
                2.0,
            )

            route_policy = plugin.supervisor_route_call_limit()
            route_decision = route_policy(
                {
                    "type": "tool_call",
                    "data": {
                        "name": "sys_session_send",
                        "arguments": {},
                    },
                    "context": {
                        "conversation_id": parent,
                        "root_conversation_id": parent,
                    },
                    "session_state": {
                        plugin._SUPERVISOR_ROUTE_COUNT_STATE_KEY: (
                            plugin._SUPERVISOR_ROUTE_LIMIT
                        )
                    },
                }
            )
            self.assertEqual(route_decision["result"], "ALLOW")
            self.assertEqual(
                route_decision["state_updates"][0]["key"],
                (
                    f"{plugin._SUPERVISOR_ROUTE_COUNT_STATE_KEY}"
                    ":attempt-2"
                ),
            )

            best_effort_parent = "same-chat-best-effort-parent"
            runtime_state.activate_parent_attempt(
                best_effort_parent,
                runtime_state.attempt_request_identity("first request"),
            )
            best_effort_records = [
                {
                    **_route_packet(
                        "cursor_workhorse",
                        "cursor-cycle-1",
                        "bounded customer answer",
                    ),
                    "parent_session_id": best_effort_parent,
                },
                {
                    **_route_packet(
                        "opus_auditor",
                        "audit-cycle-1",
                        _audit("PASS_WITH_GAPS"),
                    ),
                    "parent_session_id": best_effort_parent,
                },
            ]
            for record in best_effort_records:
                runtime_state.append_collection(record)
            self.assertEqual(
                runtime_state.record_best_effort_answer(
                    "bounded customer answer",
                    best_effort_records,
                    cycle=1,
                    reason="final cycle exhausted",
                    parent_session_id=best_effort_parent,
                ),
                "bounded customer answer",
            )
            best_effort_path = runtime_state._parent_artifact_path(
                Path(value),
                "best-effort-answer.bin",
                best_effort_parent,
            ).resolve()
            immutable_best_effort = best_effort_path.read_bytes()
            runtime_state.activate_parent_attempt(
                best_effort_parent,
                runtime_state.attempt_request_identity("second request"),
            )
            self.assertEqual(
                runtime_state.read_best_effort_answer(best_effort_parent),
                "",
            )
            self.assertEqual(
                best_effort_path.read_bytes(),
                immutable_best_effort,
            )

    def test_child_wakes_and_late_cursor_never_discard_inflight_audit(
        self,
    ) -> None:
        """Regression for run-o5qy5w05's 2+2 attempt-generation churn."""

        parent = "live-abort-regression-parent"
        zero_cost = {
            "available": True,
            "source": "fixture",
            "total_usd": 0.0,
            "reported_usd": 0.0,
            "estimated_unpriced_usd": 0.0,
            "sessions": [],
        }
        request = {
            "type": "request",
            "data": {
                "user_content": "2 + 2 = ?",
                "attachments": [],
            },
            "context": {
                "conversation_id": parent,
                "root_conversation_id": parent,
            },
        }
        wake = {
            **request,
            "data": {
                "user_content": (
                    "[System: sub-agent task child completed — "
                    "cursor_workhorse:cursor-cycle-1 returned: 4]"
                ),
                "attachments": [],
            },
        }
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value},
            clear=False,
        ), mock.patch.object(
            plugin,
            "_stored_cost_snapshot",
            return_value=zero_cost,
        ):
            budget = plugin.strict_cost_budget(50.0)
            self.assertEqual(budget(request)["result"], "ALLOW")
            self.assertEqual(
                runtime_state.current_attempt_generation(parent),
                1,
            )
            # The executor guard only ensures the request policy's generation;
            # it never derives a new identity from synthetic wake messages.
            self.assertEqual(
                runtime_state.activate_parent_attempt(parent, ""),
                1,
            )

            cursor = {
                **_route_packet(
                    "cursor_workhorse",
                    "cursor-cycle-1",
                    "4",
                ),
                "parent_session_id": parent,
            }
            runtime_state.append_dispatch(cursor)
            runtime_state.append_collection(cursor)
            audit = {
                **_route_packet(
                    "opus_auditor",
                    "audit-cycle-1",
                    _audit("PASS_WITH_GAPS"),
                ),
                "parent_session_id": parent,
            }
            runtime_state.append_dispatch(audit)

            self.assertEqual(budget(wake)["result"], "ALLOW")
            self.assertEqual(
                runtime_state.current_attempt_generation(parent),
                1,
            )
            self.assertEqual(
                supervisor_runtime._attempt_request_identity(
                    [
                        {
                            "role": "user",
                            "content": wake["data"]["user_content"],
                        }
                    ]
                ),
                "",
            )

            late_cursor = {
                **_route_packet(
                    "cursor_workhorse",
                    "cursor-cycle-1",
                    "",
                    child="late-cursor",
                ),
                "parent_session_id": parent,
                "status": "failed",
            }
            runtime_state.append_collection(late_cursor)
            records = runtime_state.read_collections(parent)
            self.assertEqual(len(records), 2)
            route = plugin._next_route(
                records,
                parent_session_id=parent,
            )
            self.assertEqual(
                (route.status, route.agent, route.title),
                ("dispatch", "opus_auditor", "audit-cycle-1"),
            )
            self.assertTrue(
                plugin._route_dispatch_pending(
                    route,
                    records,
                    runtime_state.read_dispatches(parent),
                    parent_session_id=parent,
                )
            )

            runtime_state.append_collection(audit)
            records = runtime_state.read_collections(parent)
            route = plugin._next_route(
                records,
                parent_session_id=parent,
            )
            self.assertEqual(
                (route.status, route.agent, route.title),
                ("dispatch", "codex_judge", "judge-cycle-1"),
            )
            self.assertFalse(
                runtime_state._parent_artifact_path(
                    Path(value),
                    "terminal-failure.txt",
                    parent,
                ).exists()
            )

    def test_same_chat_reask_invokes_model_before_old_failure_relay(
        self,
    ) -> None:
        from omnigent.inner import claude_sdk_executor
        from omnigent.inner.executor import TextChunk, ToolCallRequest, TurnComplete

        parent = "same-chat-runtime-parent"
        original = claude_sdk_executor.ClaudeSDKExecutor.run_turn
        model_turns = 0

        async def scripted(
            _self: object,
            _messages: object,
            _tools: object,
            _system_prompt: object,
            _config: object = None,
        ):
            nonlocal model_turns
            model_turns += 1
            yield ToolCallRequest(
                name="mcp__omnigent__sys_session_send",
                args={
                    "agent": "cursor_workhorse",
                    "title": "cursor-cycle-1",
                },
            )
            runtime_state.append_dispatch(
                {
                    "parent_session_id": parent,
                    "child_session_id": "retry-child",
                    "work_id": "retry-work",
                    "agent": "cursor_workhorse",
                    "title": "cursor-cycle-1",
                }
            )
            yield TurnComplete(response="model prose")

        class Supervisor:
            _agent_name = "triple-stamp"

        first_messages = [
            {
                "role": "user",
                "content": "question that failed",
                "session_id": parent,
            }
        ]
        retry_messages = [
            *first_messages,
            {
                "role": "assistant",
                "content": "PIPELINE_INFRASTRUCTURE_ERROR",
            },
            {
                "role": "user",
                "content": "try that again",
                "session_id": parent,
            },
        ]

        async def collect() -> list[object]:
            return [
                event
                async for event in claude_sdk_executor.ClaudeSDKExecutor.run_turn(
                    Supervisor(),
                    retry_messages,
                    [],
                    "route",
                    None,
                )
            ]

        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {
                "TRIPLE_STAMP_RUN_DIR": value,
                "TRIPLE_STAMP_RUN_ID": "fixture",
            },
            clear=False,
        ):
            runtime_state.activate_parent_attempt(
                parent,
                supervisor_runtime._attempt_request_identity(first_messages),
            )
            old_failure = runtime_state.record_terminal_failure(
                "PIPELINE_INFRASTRUCTURE_ERROR",
                "old terminal latch",
                stage="cursor-cycle-1",
                cycle=1,
                parent_session_id=parent,
            )
            claude_sdk_executor.ClaudeSDKExecutor.run_turn = scripted
            try:
                supervisor_runtime.install_supervisor_continuation_guard()
                events = asyncio.run(collect())
            finally:
                claude_sdk_executor.ClaudeSDKExecutor.run_turn = original

            self.assertEqual(model_turns, 1)
            self.assertEqual(
                runtime_state.current_attempt_generation(parent),
                2,
            )
            self.assertEqual(runtime_state.read_terminal_failure(parent), "")
            self.assertEqual(
                [row["title"] for row in runtime_state.read_dispatches(parent)],
                ["cursor-cycle-1"],
            )
            streamed = "".join(
                event.text
                for event in events
                if isinstance(event, TextChunk)
            )
            self.assertNotIn(old_failure, streamed)
            self.assertEqual(
                [
                    event.response
                    for event in events
                    if isinstance(event, TurnComplete)
                ],
                [""],
            )

    def test_request_policy_resets_same_question_before_old_budget_denial(
        self,
    ) -> None:
        parent = "same-chat-policy-parent"
        question = "repeat this exact question"
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value},
            clear=False,
        ):
            runtime_state.activate_parent_attempt(
                parent,
                runtime_state.attempt_request_identity(question),
            )
            runtime_state.write_budget_state(
                49.0,
                50.0,
                False,
                reported_usd=49.0,
                parent_session_id=parent,
                observed_cost_usd=49.0,
                observed_reported_usd=49.0,
            )
            runtime_state.record_terminal_failure(
                "PIPELINE_INFRASTRUCTURE_ERROR",
                "first attempt failed at the budget edge",
                stage="cursor-cycle-1",
                cycle=1,
                parent_session_id=parent,
            )
            snapshot = {
                "available": True,
                "source": "conversation_store",
                "total_usd": 51.0,
                "reported_usd": 51.0,
                "estimated_unpriced_usd": 0.0,
                "sessions": [],
            }
            with mock.patch.object(
                plugin,
                "_stored_cost_snapshot",
                return_value=snapshot,
            ):
                decision = plugin.strict_cost_budget(50.0)(
                    {
                        "type": "request",
                        "data": {
                            "user_content": question,
                            "attachments": [],
                        },
                        "context": {
                            "conversation_id": parent,
                            "root_conversation_id": parent,
                        },
                    }
                )

            self.assertEqual(decision["result"], "ALLOW")
            self.assertEqual(
                runtime_state.current_attempt_generation(parent),
                2,
            )
            self.assertEqual(runtime_state.read_terminal_failure(parent), "")
            self.assertEqual(
                runtime_state.read_budget_state(parent)["cost_usd"],
                2.0,
            )

    def test_stamp_generation_publish_is_atomic_and_never_rewritten(
        self,
    ) -> None:
        parent = "atomic-stamp-parent"
        answer = "immutable attested answer"
        stamp = json.dumps(
            {
                "verdict": "STAMP",
                "needs_web": False,
                "needs_internal": False,
                "why": "complete",
                "gap_materiality": "none",
                "limitations": [],
                "citations_that_hold": ["evidence"],
                "voice_profile_check": {
                    "source_path": "",
                    "sha256": "",
                    "constraints_applied": "none",
                },
                "shippable_answer": answer,
            }
        )
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {
                "TRIPLE_STAMP_RUN_DIR": value,
                "TRIPLE_STAMP_VOICE_PROFILE": "",
                "TRIPLE_STAMP_VOICE_PROFILE_SHA256": "",
            },
            clear=False,
        ):
            runtime_state.activate_parent_attempt(parent, "request-one")
            for packet in (
                _route_packet(
                    "cursor_workhorse",
                    "cursor-cycle-1",
                    "evidence",
                ),
                _route_packet(
                    "opus_auditor",
                    "audit-cycle-1",
                    _audit("PASS"),
                ),
            ):
                runtime_state.append_collection(
                    {**packet, "parent_session_id": parent}
                )
            judge = {
                **_route_packet(
                    "codex_judge",
                    "judge-cycle-1",
                    stamp,
                ),
                "parent_session_id": parent,
            }
            runtime_state.append_collection(judge)

            with mock.patch.object(
                runtime_state.os,
                "rename",
                side_effect=OSError("simulated death before publish"),
            ):
                self.assertFalse(runtime_state.attest_codex_stamp(judge))
            self.assertEqual(runtime_state.read_attested_answer(parent), "")
            answer_path = runtime_state._parent_artifact_path(
                Path(value),
                "stamped-answer.bin",
                parent,
            )
            attestation_path = runtime_state._parent_artifact_path(
                Path(value),
                "stamp-attestation.json",
                parent,
            )
            self.assertFalse(answer_path.exists())
            self.assertFalse(attestation_path.exists())

            self.assertTrue(runtime_state.attest_codex_stamp(judge))
            self.assertTrue(answer_path.exists())
            self.assertTrue(attestation_path.exists())
            before = answer_path.read_bytes()
            before_digest = hashlib.sha256(before).hexdigest()
            plugin._mark_terminal(
                "STAMP",
                answer,
                parent_session_id=parent,
            )
            plugin._mark_terminal(
                "STAMP",
                answer + " tampered",
                parent_session_id=parent,
            )
            self.assertEqual(answer_path.read_bytes(), before)
            self.assertEqual(
                hashlib.sha256(answer_path.read_bytes()).hexdigest(),
                before_digest,
            )

    def test_stamp_and_terminal_failure_are_owned_by_parent(self) -> None:
        parent_a = "parent-math"
        parent_b = "parent-lakebase"
        answer = "2 + 2 = 4."
        stamp = json.dumps(
            {
                "verdict": "STAMP",
                "needs_web": False,
                "needs_internal": False,
                "why": "integer arithmetic is complete",
                "gap_materiality": "none",
                "limitations": [],
                "citations_that_hold": ["Stage 1 arithmetic evidence"],
                "voice_profile_check": {
                    "source_path": "",
                    "sha256": "",
                    "constraints_applied": "none",
                },
                "shippable_answer": answer,
            }
        )
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {
                "TRIPLE_STAMP_RUN_DIR": value,
                "TRIPLE_STAMP_VOICE_PROFILE": "",
                "TRIPLE_STAMP_VOICE_PROFILE_SHA256": "",
            },
            clear=False,
        ):
            for packet in (
                _route_packet(
                    "cursor_workhorse",
                    "cursor-cycle-1",
                    "2 + 2 = 4",
                ),
                _route_packet(
                    "opus_auditor",
                    "audit-cycle-1",
                    _audit("PASS"),
                ),
            ):
                runtime_state.append_collection(
                    {**packet, "parent_session_id": parent_a}
                )
            judge = {
                **_route_packet(
                    "codex_judge",
                    "judge-cycle-1",
                    stamp,
                ),
                "parent_session_id": parent_a,
            }
            runtime_state.append_collection(judge)
            self.assertTrue(runtime_state.attest_codex_stamp(judge))

            # B has no Cursor/Opus chain, so A's completed chain cannot attest B.
            foreign_judge = {**judge, "parent_session_id": parent_b}
            runtime_state.append_collection(foreign_judge)
            self.assertFalse(runtime_state.attest_codex_stamp(foreign_judge))

            failure_b = runtime_state.record_terminal_failure(
                "PIPELINE_VALIDATION_FAILED",
                "Lakebase audit exhausted",
                stage="audit-internal-2-2",
                cycle=2,
                parent_session_id=parent_b,
            )
            self.assertEqual(
                runtime_state.read_attested_answer(parent_a),
                answer,
            )
            self.assertEqual(
                runtime_state.read_terminal_failure(parent_a),
                "",
            )
            self.assertEqual(
                runtime_state.read_terminal_failure(parent_b),
                failure_b,
            )
            self.assertEqual(
                launcher._validated_pipeline_exit(
                    Path(value),
                    0,
                    [],
                    parent_session_id=parent_a,
                ),
                0,
            )
            digest_a = hashlib.sha256(parent_a.encode()).hexdigest()[:16]
            digest_b = hashlib.sha256(parent_b.encode()).hexdigest()[:16]
            run = Path(value)
            self.assertTrue(
                (run / f"stamp-attestation-{digest_a}.json").is_file()
            )
            self.assertTrue(
                (run / f"terminal-failure-{digest_b}.txt").is_file()
            )
            self.assertFalse((run / "terminal-failure.txt").exists())

    def test_stamped_parent_relay_wins_over_sibling_failure(self) -> None:
        from omnigent.inner import claude_sdk_executor
        from omnigent.inner.executor import TextChunk, TurnComplete

        parent_a = "parent-math-relay"
        parent_b = "parent-lakebase-failure"
        answer = "2 + 2 = 4."
        stamp = json.dumps(
            {
                "verdict": "STAMP",
                "needs_web": False,
                "needs_internal": False,
                "why": "complete",
                "gap_materiality": "none",
                "limitations": [],
                "citations_that_hold": ["arithmetic evidence"],
                "voice_profile_check": {
                    "source_path": "",
                    "sha256": "",
                    "constraints_applied": "none",
                },
                "shippable_answer": answer,
            }
        )
        original = claude_sdk_executor.ClaudeSDKExecutor.run_turn
        model_turns = 0

        async def must_not_run(*_args: object, **_kwargs: object):
            nonlocal model_turns
            model_turns += 1
            yield TurnComplete(response="wrong")

        class Supervisor:
            _agent_name = "triple-stamp"

        async def collect() -> list[object]:
            return [
                event
                async for event in claude_sdk_executor.ClaudeSDKExecutor.run_turn(
                    Supervisor(),
                    [
                        {
                            "role": "user",
                            "content": "sibling wake",
                            "session_id": parent_a,
                        }
                    ],
                    [],
                    "route",
                    None,
                )
            ]

        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {
                "TRIPLE_STAMP_RUN_DIR": value,
                "TRIPLE_STAMP_RUN_ID": "fixture",
                "TRIPLE_STAMP_VOICE_PROFILE": "",
                "TRIPLE_STAMP_VOICE_PROFILE_SHA256": "",
            },
            clear=False,
        ):
            for packet in (
                _route_packet(
                    "cursor_workhorse",
                    "cursor-cycle-1",
                    "2 + 2 = 4",
                ),
                _route_packet(
                    "opus_auditor",
                    "audit-cycle-1",
                    _audit("PASS"),
                ),
            ):
                runtime_state.append_collection(
                    {**packet, "parent_session_id": parent_a}
                )
            judge = {
                **_route_packet(
                    "codex_judge",
                    "judge-cycle-1",
                    stamp,
                ),
                "parent_session_id": parent_a,
            }
            runtime_state.append_collection(judge)
            self.assertTrue(runtime_state.attest_codex_stamp(judge))
            runtime_state.record_terminal_failure(
                "PIPELINE_VALIDATION_FAILED",
                "sibling exhausted internal hops",
                stage="audit-internal-2-2",
                cycle=2,
                parent_session_id=parent_b,
            )
            claude_sdk_executor.ClaudeSDKExecutor.run_turn = must_not_run
            try:
                supervisor_runtime.install_supervisor_continuation_guard()
                events = asyncio.run(collect())
            finally:
                claude_sdk_executor.ClaudeSDKExecutor.run_turn = original

        self.assertEqual(model_turns, 0)
        self.assertEqual(
            "".join(
                event.text
                for event in events
                if isinstance(event, TextChunk)
            ),
            answer,
        )
        self.assertEqual(
            [
                event.response
                for event in events
                if isinstance(event, TurnComplete)
            ],
            [answer],
        )

    def test_budget_denial_is_parent_scoped(self) -> None:
        snapshot = {
            "available": True,
            "source": "conversation_store",
            "total_usd": 51.0,
            "reported_usd": 51.0,
            "estimated_unpriced_usd": 0.0,
            "sessions": [],
        }
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value},
            clear=False,
        ), mock.patch.object(
            plugin,
            "_stored_cost_snapshot",
            return_value=snapshot,
        ):
            decision = plugin.strict_cost_budget(50.0)(
                {
                    "type": "request",
                    "context": {
                        "conversation_id": "budget-parent-a",
                        "root_conversation_id": "budget-parent-a",
                    },
                }
            )
            self.assertEqual(decision["result"], "DENY")
            self.assertTrue(
                runtime_state.read_terminal_failure("budget-parent-a")
            )
            self.assertEqual(
                runtime_state.read_terminal_failure("budget-parent-b"),
                "",
            )
            self.assertFalse(
                (Path(value) / "terminal-failure.txt").exists()
            )


if __name__ == "__main__":
    unittest.main()
