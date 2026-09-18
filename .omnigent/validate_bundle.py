"""Static and runtime validation for the triple-stamp launch contract."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import stat
import subprocess
import sys
import tempfile
from importlib.metadata import entry_points, version
from pathlib import Path

import yaml
from alembic.script import ScriptDirectory
from omnigent.claude_launcher import resolve_claude_launch
from omnigent.config import load_effective_config
from omnigent.db.utils import _build_alembic_config
from omnigent.harness_startup_config import resolve_harness_command
from omnigent.host.connect import _build_runner_env
from omnigent.runtime.workflow import _build_claude_sdk_spawn_env
from omnigent.spec.parser import parse
from omnigent.spec.types import ProviderAuth
from omnigent.spec.validator import validate

_MODEL_CONFIG = yaml.safe_load(
    Path(__file__).with_name("provider-models.yaml").read_text(encoding="utf-8")
)
_PROFILES = _MODEL_CONFIG["profiles"]
_NAMESPACES = _MODEL_CONFIG["namespaces"]
_CURSOR_RUNNER_ONLY_ENV = frozenset(
    {
        "OMNIGENT_CLAUDE_LAUNCHER",
        "OMNIGENT_CODEX_PATH",
        "OMNIGENT_REMOTE_AUTH_TOKEN",
        "ISAAC_BIN",
        "ISAAC_DEFAULT_UCODE",
        "ISAAC_DISABLE_MAC_MANAGED_SETTINGS_UPDATE",
        "ISAAC_LAUNCH_MODE",
        "ISAAC_OMNIGENT_BIN",
        "ISAAC_OMNI_PY",
        "TRIPLE_STAMP_CODEX_BIN",
        "TRIPLE_STAMP_CODEX_MODEL",
        "TRIPLE_STAMP_CLAUDE_NAMESPACE",
        "TRIPLE_STAMP_OPUS_MODEL",
        "TRIPLE_STAMP_PROVIDER",
        "TRIPLE_STAMP_SUPERVISOR_MODEL",
    }
)


def _max_cycles() -> int:
    raw = os.environ.get("TRIPLE_STAMP_MAX_CYCLES", "2")
    if raw not in {"2", "4"}:
        raise RuntimeError(
            "TRIPLE_STAMP_MAX_CYCLES must be exactly 2 or 4"
        )
    return int(raw)


def _route_call_cap(max_cycles: int | None = None) -> int:
    return (max_cycles if max_cycles is not None else _max_cycles()) * 19 * 2 + 8


def _provider(value: str | None = None) -> str:
    selected = (
        value
        if value is not None
        else os.environ.get("TRIPLE_STAMP_PROVIDER", "direct")
    )
    if selected not in _PROFILES:
        raise RuntimeError(f"unsupported TRIPLE_STAMP_PROVIDER: {selected!r}")
    return selected


def _runtime_models() -> dict[str, str]:
    names = {
        "supervisor": "TRIPLE_STAMP_SUPERVISOR_MODEL",
        "opus_auditor": "TRIPLE_STAMP_OPUS_MODEL",
        "codex_judge": "TRIPLE_STAMP_CODEX_MODEL",
    }
    models = {role: os.environ.get(name, "") for role, name in names.items()}
    if not all(models.values()):
        fail("active resolved model environment is incomplete")
    return models


def _source_models() -> dict[str, str]:
    public = _NAMESPACES["public_anthropic"]
    return {
        "supervisor": public["supervisor"],
        "opus_auditor": public["opus_auditor"],
        "codex_judge": _PROFILES["direct"]["codex_judge"],
    }


def _expected_models(
    models: dict[str, str] | str,
) -> dict[str, tuple[str, str | None, str]]:
    if isinstance(models, str):
        if models == "direct":
            models = _source_models()
        elif models == "databricks":
            profile = _PROFILES["databricks"]
            models = {
                "supervisor": profile["supervisor"],
                "opus_auditor": profile["opus_auditor"],
                "codex_judge": profile["codex_judge"],
            }
        else:
            raise RuntimeError(f"unsupported model profile: {models!r}")
    return {
        "triple-stamp": (models["supervisor"], "low", "claude-sdk"),
        "cursor_workhorse": (
            "cursor-grok-4.6-xhigh",
            None,
            "cursor-native",
        ),
        "opus_auditor": (models["opus_auditor"], "max", "claude-native"),
        "codex_judge": (models["codex_judge"], "ultra", "codex-native"),
    }


EXPECTED_EXECUTOR_CONFIGS = {
    "triple-stamp": {"harness": "claude-sdk", "permission_mode": "auto"},
    "cursor_workhorse": {"harness": "cursor-native", "yolo": "True"},
    "opus_auditor": {"harness": "claude-native", "permission_mode": "dontAsk"},
    "codex_judge": {"harness": "codex-native", "yolo": "True"},
}
def _voice_profile() -> Path | None:
    """Configured voice profile, or None when voice rendering is off."""

    configured = os.environ.get("TRIPLE_STAMP_VOICE_PROFILE", "").strip()
    return Path(configured).expanduser() if configured else None


def fail(message: str) -> None:
    raise SystemExit(f"triple-stamp validation: {message}")


def _option_after(args: list[str], option: str) -> str | None:
    try:
        return args[args.index(option) + 1]
    except (ValueError, IndexError):
        return None


def validate_text_files(root: Path) -> None:
    paths = [
        root / "README.md",
        root / "AGENTS.md",
        root / "run-with-isaac",
        root / "verify-provider-matrix",
        root / "config.yaml",
        root / ".omnigent/config.yaml",
        root / ".omnigent/cursor-via-login",
        root / ".omnigent/cursor_via_login.py",
        root / ".omnigent/codex-via-isaac",
        root / ".omnigent/codex-launch",
        root / ".omnigent/provider-models.yaml",
        root / ".omnigent/auth_preflight.py",
        root / ".omnigent/build_outer_seatbelt.py",
        root / ".omnigent/launcher.py",
        root / ".omnigent/runtime-python/sitecustomize.py",
        root / ".omnigent/runtime-python/triple_stamp_browser_runtime.py",
        root / ".omnigent/runtime-python/triple_stamp_cursor_lifecycle.py",
        root / ".omnigent/runtime-python/triple_stamp_supervisor_runtime.py",
        root / ".omnigent/validate_bundle.py",
        root / ".omnigent/isaac-launcher/pyproject.toml",
        root / ".omnigent/isaac-launcher/triple_stamp_isaac_launcher.py",
        root / ".omnigent/isaac-launcher/triple_stamp_runtime_state.py",
        root / "agents/cursor_workhorse/config.yaml",
        root / "agents/opus_auditor/config.yaml",
        root / "agents/codex_judge/config.yaml",
        root / "tests/test_auth_preflight.py",
        root / "tests/test_opus_launch.py",
        root / "tests/test_cursor_startup.py",
        root / "tests/test_model_resolution.py",
        root / "tests/test_orchestration_contract.py",
        root / "tests/test_browser_host_runtime.py",
        root / "tests/test_cursor_lifecycle.py",
        root / "tests/fixtures/run-gezq2bml-terminal.json",
        root / "tests/fixtures/run-80z0p3az-cursor-lifecycle.json",
        root / "tests/fixtures/run-u9demmy7-handoff.json",
        root / "tests/fixtures/run-00jfpj73-databricks-timeout.json",
        root / "tests/fixtures/run-4jnvjebo-direct-turn-guard.json",
        root / "tests/fixtures/run-04grs3i-dispatch-observer-regression.json",
    ]
    for path in paths:
        try:
            data = path.read_bytes()
            data.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            fail(f"{path.relative_to(root)} is missing or not UTF-8: {exc}")
        if b"\r" in data:
            fail(f"{path.relative_to(root)} contains CR/CRLF line endings")
        if data and not data.endswith(b"\n"):
            fail(f"{path.relative_to(root)} has no final LF")


def validate_runtime_guard(root: Path) -> None:
    """Probe the current Cursor and isolated-inbox runtime shim."""

    bootstrap = root / ".omnigent/runtime-python"
    if os.environ.get("PYTHONPATH") != str(bootstrap):
        fail("runtime PYTHONPATH does not point at the Cursor compatibility guard")
    if os.environ.get("TRIPLE_STAMP_CURSOR_APPROVAL_GUARD") != (
        "suppress-yolo-false-cards"
    ):
        fail("Cursor approval guard mode is not enabled")
    probe = r"""
import asyncio
import os
import sitecustomize
import sys
from pathlib import Path
from omnigent import claude_launcher
from omnigent import chat
from omnigent import codex_native_app_server
from omnigent import cursor_native_permissions as permissions
from omnigent.inner import claude_sdk_executor
from omnigent.policies import function as policy_function
from omnigent.runner import tool_dispatch
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec.parser import parse
from omnigent.tools.manager import ToolManager
from triple_stamp_supervisor_runtime import (
    _HEADLESS_PIPELINE_EXTRA_TURN_LIMIT,
    _OMNIGENT_HEADLESS_EXTRA_TURN_LIMIT,
    _replace_code_int_constant,
)
from triple_stamp_runtime_state import (
    add_opus_effort_observation,
    append_collection,
    append_dispatch,
    append_supervisor_tool_call,
    attest_codex_stamp,
    record_terminal_failure,
    read_collections,
    read_dispatches,
)

guard = permissions._yolo_auto_accept
assert getattr(guard, "__triple_stamp_guard__", False)
assert getattr(
    tool_dispatch._execute_subagent_tool,
    "__triple_stamp_inbox_guard__",
    False,
)
assert getattr(
    tool_dispatch._execute_async_inbox_tool,
    "__triple_stamp_parent_inbox_probe__",
    False,
)
assert getattr(
    tool_dispatch._drain_inbox,
    "__triple_stamp_prompt_inbox__",
    False,
)
from omnigent.runner import app as runner_app
assert getattr(
    runner_app.create_runner_app,
    "__triple_stamp_parent_inbox_init__",
    False,
)
assert sitecustomize._is_runner_process(
    ["python", "-P", "-m", "omnigent.runner._zygote"]
)
assert getattr(ToolManager.__init__, "__triple_stamp_minimal_surface__", False)
assert getattr(
    claude_sdk_executor._ensure_sdk,
    "__triple_stamp_minimal_surface__",
    False,
)
assert getattr(
    PolicyEngine._context,
    "__triple_stamp_policy_identity_context__",
    False,
)
assert getattr(
    policy_function._build_event,
    "__triple_stamp_policy_identity_context__",
    False,
)
assert getattr(
    policy_function.FunctionPolicy.evaluate,
    "__triple_stamp_policy_identity_context__",
    False,
)
assert getattr(
    claude_sdk_executor.ClaudeSDKExecutor.run_turn,
    "__triple_stamp_continuation_guard__",
    False,
)
assert getattr(
    codex_native_app_server.build_codex_native_server,
    "__triple_stamp_voice_environment__",
    False,
)
assert getattr(
    codex_native_app_server.codex_terminal_env,
    "__triple_stamp_voice_environment__",
    False,
)
assert chat._LOOP_TIMEOUT_S is None
_, old_turn_guards = _replace_code_int_constant(
    chat._query_sessions_once.__code__,
    _OMNIGENT_HEADLESS_EXTRA_TURN_LIMIT,
    _OMNIGENT_HEADLESS_EXTRA_TURN_LIMIT,
)
_, replacement_turn_guards = _replace_code_int_constant(
    chat._query_sessions_once.__code__,
    _HEADLESS_PIPELINE_EXTRA_TURN_LIMIT,
    _HEADLESS_PIPELINE_EXTRA_TURN_LIMIT,
)
assert old_turn_guards == 0
assert replacement_turn_guards == 1
assert getattr(
    chat._query_sessions_once,
    "__triple_stamp_headless_wait__",
    False,
)
assert getattr(
    claude_launcher.resolve_claude_launch,
    "__triple_stamp_fail_closed__",
    False,
)
assert (
    getattr(
        tool_dispatch._drain_inbox,
        "__triple_stamp_inbox_time_contract_s__",
        420,
    )
    < 420
)
assert sitecustomize._CURSOR_STAGE_INACTIVITY_S == 5 * 60
assert sitecustomize._CURSOR_STAGE_ABSOLUTE_S == 15 * 60
assert callable(append_dispatch)
assert callable(read_dispatches)
assert callable(append_collection)
assert callable(read_collections)
assert callable(attest_codex_stamp)
assert callable(add_opus_effort_observation)
assert callable(append_supervisor_tool_call)
assert callable(record_terminal_failure)
spec = parse(Path(os.environ["TRIPLE_STAMP_BUNDLE"]))
manager = ToolManager(spec, workdir=Path(os.environ["TRIPLE_STAMP_BUNDLE"]))
assert set(manager.get_tool_names()) == {"sys_session_send", "sys_read_inbox"}
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
assert options.tools == ["ToolSearch"]
assert set(options.allowed_tools) == {
    "mcp__omnigent__sys_session_send",
    "mcp__omnigent__sys_read_inbox",
}
call = permissions.CursorPendingToolCall(
    tool_call_id="call_regression",
    tool_name="WebSearch",
    args={"search_term": "regression"},
)
attempts = {call.tool_call_id: (0.0, permissions._YOLO_ACCEPT_MAX_ATTEMPTS)}
outcome = asyncio.run(
    guard(
        call,
        bridge_dir=Path("/nonexistent"),
        session_id="session_regression",
        now=1.0,
        attempts_by_call=attempts,
        allow_send=False,
    )
)
assert outcome is permissions._YoloAccept.SKIP
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(bootstrap)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["TRIPLE_STAMP_CURSOR_APPROVAL_GUARD_PROBE"] = "1"
    result = subprocess.run(
        [os.environ["STABLE_OMNIGENT_PY"], "-P", "-c", probe],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        fail(
            "runtime guard regression probe failed: "
            + (result.stderr.strip() or result.stdout.strip() or "no output")
        )


def validate_voice_profile() -> None:
    """Validate the voice profile only when the operator configured one.

    Voice rendering is optional, so an unset `TRIPLE_STAMP_VOICE_PROFILE` is a
    valid configuration rather than a bundle defect. A configured profile is
    still pinned to the exact bytes the launcher preflighted, so it cannot be
    swapped between preflight and the judge reading it.
    """

    profile = _voice_profile()
    digest = os.environ.get("TRIPLE_STAMP_VOICE_PROFILE_SHA256", "")
    if profile is None:
        if digest:
            fail("voice profile digest was exported without a profile path")
        return
    try:
        data = profile.read_bytes()
        data.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        fail(f"voice profile is unreadable or not UTF-8: {exc}")
    if hashlib.sha256(data).hexdigest() != digest:
        fail("voice profile digest changed after launcher preflight")


def validate_spec(
    root: Path,
    runtime_bundle: Path,
    *,
    provider: str | None = None,
) -> None:
    selected_provider = _provider(provider)
    active = _runtime_models()
    active_expected_models = _expected_models(active)
    namespace = os.environ.get("TRIPLE_STAMP_CLAUDE_NAMESPACE", "")
    if namespace not in {"public_anthropic", "databricks_gateway"}:
        fail(f"active Claude namespace marker is invalid: {namespace!r}")
    if selected_provider == "databricks" and namespace != "databricks_gateway":
        fail("Databricks launcher profile lost its gateway namespace marker")
    source = parse(root)
    runtime = parse(runtime_bundle)
    for bundle, expected_sandbox, bundle_expected_models, bundle_provider in (
        (source, "darwin_seatbelt", _expected_models(_source_models()), "direct"),
        (runtime, "none", active_expected_models, selected_provider),
    ):
        agents = {bundle.name: bundle, **{agent.name: agent for agent in bundle.sub_agents}}
        if set(agents) != set(bundle_expected_models):
            fail(f"unexpected agent set: {sorted(agents)}")
        if bundle.local_tools:
            fail(f"supervisor has unexpected local tools: {[tool.name for tool in bundle.local_tools]}")
        if bundle.skills_filter != "none" or not bundle.async_enabled:
            fail("SDK supervisor must be skills:none with async completion enabled")
        if bundle_provider == "databricks":
            if not isinstance(bundle.executor.auth, ProviderAuth):
                fail("Databricks SDK supervisor does not use explicit provider auth")
            if bundle.executor.auth.name != "isaac-databricks-ai-gateway":
                fail(f"SDK supervisor provider drifted: {bundle.executor.auth.name!r}")
        elif bundle.executor.auth is not None:
            fail("direct SDK supervisor unexpectedly uses provider auth")

        cursor_sandbox = agents["cursor_workhorse"].os_env.sandbox
        cursor_passthrough = set(
            cursor_sandbox.env_passthrough if cursor_sandbox is not None else []
        )
        if leaked := sorted(cursor_passthrough & _CURSOR_RUNNER_ONLY_ENV):
            fail(
                "Cursor child receives runner-only provider/launcher variables: "
                + ", ".join(leaked)
            )

        for name, (model, effort, harness) in bundle_expected_models.items():
            agent = agents[name]
            if agent.executor.model != model:
                fail(f"{name} model drifted: {agent.executor.model!r}")
            if agent.executor.reasoning_effort != effort:
                fail(f"{name} effort drifted: {agent.executor.reasoning_effort!r}")
            if agent.executor.config.get("harness") != harness:
                fail(f"{name} harness drifted: {agent.executor.config.get('harness')!r}")
            if {
                key: str(value) if isinstance(value, bool) else value
                for key, value in agent.executor.config.items()
            } != EXPECTED_EXECUTOR_CONFIGS[name]:
                fail(f"{name} executor config drifted: {agent.executor.config!r}")
            sandbox = agent.os_env.sandbox
            if sandbox is None or sandbox.type != expected_sandbox:
                fail(
                    f"{name} sandbox is {getattr(sandbox, 'type', None)!r}, "
                    f"expected {expected_sandbox!r}"
                )
            if "TRIPLE_STAMP_OUTER_SANDBOX" not in set(sandbox.env_passthrough or []):
                fail(f"{name} does not preserve the outer-sandbox marker")
            checked = validate(agent)
            if not checked.valid:
                errors = "; ".join(
                    f"{error.path}: {error.message}" for error in checked.errors
                )
                fail(f"{name} failed validator: {errors}")

        if bundle.executor.context_window != 1_000_000:
            fail("supervisor context window is not pinned to 1,000,000")
        supervisor_prompt = bundle.instructions
        prompt_cycles = next(
            (
                cycles
                for cycles in (2, 4)
                if f"This run permits exactly {cycles} complete cycles"
                in supervisor_prompt
            ),
            0,
        )
        if prompt_cycles not in {2, 4}:
            fail("supervisor prompt lacks a valid generated cycle policy")
        for marker in (
            "routing-only supervisor",
            "Omnigent parks the parent turn",
            "runtime exposes only those two MCP tools",
            "routing authorization never parses or compares handoff prose",
            "cursor-cycle-N",
            "cursor-retry-N-1",
            "cursor-web-opus-N-H",
            "cursor-web-codex-N-H",
            "audit-cycle-N",
            "audit-cycle-N-web-H",
            "audit-retry-N-1",
            "FRESH `opus_auditor` child",
            "judge-cycle-N",
            "judge-format-repair-N",
            "judge-convergence-N",
            f"This run permits exactly {prompt_cycles} complete cycles",
            "every runtime-appended `opus_effort_observation`",
            "byte-for-byte value",
            _VOICE_PROFILE_TEXT,
        ):
            if marker not in supervisor_prompt:
                fail(f"supervisor contract is missing {marker!r}")
        for forbidden in (
            "WAITING_FOR_SUBAGENT",
            "await_cursor_startup",
            "sys_session_get_info",
            "sys_session_get_history",
            "scheduled prompt",
        ):
            if forbidden in supervisor_prompt:
                fail(f"supervisor still contains obsolete orchestration text {forbidden!r}")

        worker_rule = (root / "AGENTS.md").read_text(encoding="utf-8")
        for marker in (
            "apply only when this Cursor session is the Triple-stamp Stage 1",
            "`cursor_workhorse` runtime",
            "Human-directed repository analysis",
            "follow the user's requested model",
            "gpt-5.6-sol-xhigh",
            "Never invoke a Skill, workflow, Subagent, Task, nested agent",
            "Emit at most one tool call per assistant turn",
            "Return the complete evidence packet inline",
            "For this Stage 1 `cursor_workhorse` runtime only",
            "cursor-grok-4.6-xhigh",
        ):
            if marker not in worker_rule:
                fail(f"Cursor workspace rule is missing {marker!r}")

        codex_prompt = agents["codex_judge"].instructions
        for marker in (
            # The prompt must carry BOTH branches so one static prompt serves a
            # configured profile and voice rendering being off.
            "TRIPLE_STAMP_VOICE_PROFILE",
            "no voice profile is configured",
            "Never use an em dash.",
            "voice_profile_check",
            "SHA-256 from the bytes you read",
            "re-check every material statement",
            "never STAMP",
            "Do no research yourself",
            "opus_effort_observation",
            "Only observed non-max effort blocks STAMP",
            "You have no tool catalog",
            "SUBSTANCE VS FORM",
            "gap_materiality",
            "Never put",
        ):
            if marker not in codex_prompt:
                fail(f"Codex voice/factual contract is missing {marker!r}")
        for name in ("cursor_workhorse", "codex_judge"):
            if str(agents[name].executor.config.get("yolo", "")).lower() != "true":
                fail(f"{name} is not in unattended yolo mode")
        opus_prompt = agents["opus_auditor"].instructions
        for marker in (
            "Do not inspect or launch `cursor-agent`",
            "Never emit or simulate a Bash",
            "including any tool failure",
            "FORMAT REPAIR ONLY",
            "optional capabilities, not launch",
            "internal_sources_consulted",
            "internal_coverage",
            "Every audit must attempt all five systems",
            "exact Opus child",
            "select:mcp__slack__slack_read_api_call",
            'type:"conversation"',
        ):
            if marker not in opus_prompt:
                fail(f"Opus audit contract is missing {marker!r}")
        if "If Isaac lands you on the wrong model" in opus_prompt:
            fail("Opus audit prompt still induces local harness self-checks")

        policies = bundle.guardrails.policies if bundle.guardrails else []
        by_name = {policy.name: policy for policy in policies}
        if set(by_name) != {"budget", "cap_calls", "final_response_contract"}:
            fail(f"unexpected guardrail set: {sorted(by_name)}")
        budget = by_name["budget"].function
        if (
            budget.path != "triple_stamp_isaac_launcher.strict_cost_budget"
            or budget.arguments != {"max_cost_usd": 50.0}
        ):
            fail("strict $50 budget policy drifted")
        contract = by_name["final_response_contract"].function
        if (
            contract.path != "triple_stamp_isaac_launcher.supervisor_contract"
            or contract.arguments != {"enabled": True}
        ):
            fail("supervisor route/relay policy drifted")
        cap = by_name["cap_calls"].function
        if (
            cap.path
            != "triple_stamp_isaac_launcher.supervisor_route_call_limit"
            or cap.arguments
            != {
                "limit": _route_call_cap(prompt_cycles),
                "counted_tools": [
                    "sys_session_send",
                    "sys_read_inbox",
                    "ToolSearch",
                    "sys_agent_start",
                ],
            }
        ):
            fail(f"supervisor tool-call cap drifted: {cap.arguments!r}")


_VOICE_PROFILE_TEXT = "TRIPLE_STAMP_VOICE_PROFILE"


def validate_launchers(
    root: Path,
    runtime_bundle: Path,
    *,
    provider: str | None = None,
) -> None:
    selected_provider = _provider(provider)
    expected_models = _expected_models(_runtime_models())
    launcher_source = (root / ".omnigent/launcher.py").read_text(encoding="utf-8")
    opus_launch_source = (
        root / ".omnigent/isaac-launcher/triple_stamp_opus_mcp.py"
    ).read_text(encoding="utf-8")
    isaac_launcher_source = (
        root / ".omnigent/isaac-launcher/triple_stamp_isaac_launcher.py"
    ).read_text(encoding="utf-8")
    browser_runtime_source = (
        root / ".omnigent/runtime-python/triple_stamp_browser_runtime.py"
    ).read_text(encoding="utf-8")
    runtime_state_source = (
        root / ".omnigent/isaac-launcher/triple_stamp_runtime_state.py"
    ).read_text(encoding="utf-8")
    supervisor_runtime_source = (
        root / ".omnigent/runtime-python/triple_stamp_supervisor_runtime.py"
    ).read_text(encoding="utf-8")
    required_relay_markers = (
        "stamp-attestation.json",
        "evidence_packet_sha256",
        "_emit_attested_answer",
        "failure-attestation.json",
        "_emit_terminal_failure",
        "child-stdout.bin",
    )
    if any(marker not in launcher_source for marker in required_relay_markers):
        fail("deterministic STAMP relay validation drifted")
    removed_opus_gates = (
        "REQUIRED_HEALTH_SERVERS",
        "MCP_ATTEMPT_SECONDS",
        "ATTESTATION_TTL_SECONDS",
        "def validate_attestation(",
        "def probe_server(",
        "run_opus_attestation_refresher",
        "_start_opus_attestation_refresher",
        "TRIPLE_STAMP_OPUS_PREFLIGHT_MODE",
        "opus-mcp-attestation.json",
    )
    opus_boundary_sources = opus_launch_source + isaac_launcher_source
    if any(marker in opus_boundary_sources for marker in removed_opus_gates):
        fail("removed Opus MCP health or attestation gate returned")
    for marker in (
        "def wait_for_browser_readiness(",
        "def browser_send_enabled(",
        "def parent_inbox_probe_request(",
        "def parent_inbox_probe_succeeded(",
        "def announce_after_browser_preflight(",
        "def _forward_runner_passthrough_into_local_daemon(",
    ):
        if marker not in browser_runtime_source:
            fail(f"browser host preflight marker missing: {marker}")
    for marker in (
        "def attest_codex_stamp(",
        "def record_terminal_failure(",
        "def write_budget_state(",
        "def observe_opus_effort(",
        "def add_opus_effort_observation(",
        "def append_supervisor_continuation(",
        "def parse_dispatch_title(",
        "def record_tool_dispatch_exception(",
        'r"cursor-retry-([1-4])-(1)"',
        'r"audit-internal-([1-4])-([1-2])"',
        'r"audit-retry-([1-4])-(1)"',
    ):
        if marker not in runtime_state_source:
            fail(f"runtime attestation marker missing: {marker}")
    for marker in (
        "_HEADLESS_PIPELINE_TIMEOUT_S: float | None = None",
        "_HEADLESS_PIPELINE_EXTRA_TURN_LIMIT = 255",
        "def _replace_code_int_constant(",
        "def install_headless_pipeline_wait(",
        "__triple_stamp_headless_wait__",
        "def install_supervisor_continuation_guard(",
        "ordinary_response_suppressed",
        "continuation_exhausted_abstained",
        "dispatch_observer_failure_abstained",
        "ledger observer failure=",
    ):
        if marker not in supervisor_runtime_source:
            fail(f"supervisor continuation marker missing: {marker}")
    if version("omnigent") != "0.12.0":
        fail(f"runtime is Omnigent {version('omnigent')}, expected 0.12.0")
    if version("claude-agent-sdk") != "0.2.152":
        fail(
            f"runtime is claude-agent-sdk {version('claude-agent-sdk')}, "
            "expected 0.2.152"
        )
    heads = ScriptDirectory.from_config(
        _build_alembic_config("sqlite:////tmp/triple-stamp-validator.db")
    ).get_heads()
    if heads != ["ga1b2c3d4e5f"]:
        fail(f"unexpected Omnigent migration heads: {heads}")

    isaac = os.environ.get("ISAAC_BIN", "")
    cursor = str(root / ".omnigent/cursor-via-login")
    codex = str(
        root
        / (
            ".omnigent/codex-via-isaac"
            if selected_provider == "databricks"
            else ".omnigent/codex-launch"
        )
    )
    cursor_wrapper_source = (root / ".omnigent/cursor_via_login.py").read_text(
        encoding="utf-8"
    )
    runtime_guard_source = (
        root / ".omnigent/runtime-python/triple_stamp_cursor_lifecycle.py"
    ).read_text(encoding="utf-8")
    for marker in (
        "--triple-stamp-startup-preflight",
        "create-chat",
        "paid_generation",
        "CURSOR_DATA_DIR",
    ):
        if marker not in cursor_wrapper_source:
            fail(f"Cursor startup preflight marker missing: {marker}")
    for marker in (
        "_cursor_data_root",
        "forwarder-required-after-prior-dispatch",
        "turn_ended_success",
        "_CURSOR_STAGE_ABSOLUTE_S = 15 * 60",
        "def _cursor_retry_note(",
        "def _observe_dispatch_bookkeeping(",
        "def ensure_parent_inbox(",
        "def _install_runner_session_inbox_initialization(",
        "__triple_stamp_parent_inbox_probe__",
        "missing_work_entry",
        "recovered_assistant_output",
        "retry denied",
    ):
        if marker not in runtime_guard_source:
            fail(f"Cursor lifecycle marker missing: {marker}")
    cfg = load_effective_config()
    if resolve_harness_command("cursor-native", default="cursor-agent", cfg=cfg) != cursor:
        fail("Cursor launcher did not resolve to the project wrapper")
    if not os.access(cursor, os.X_OK):
        fail("Cursor project wrapper is not executable")
    if os.environ.get("OMNIGENT_CODEX_PATH") != codex or not os.access(codex, os.X_OK):
        fail("Codex launcher did not resolve to the project wrapper")
    codex_source = Path(codex).read_text(encoding="utf-8")
    for marker in (
        'PROBE="/tmp/triple-stamp-codex-seatbelt-probe.$$"',
        "exit 78",
        "'model_reasoning_effort=\"ultra\"'",
    ):
        if marker not in codex_source:
            fail(f"Codex wrapper is missing {marker!r}")
    if selected_provider == "direct" and (
        "ISAAC_" in codex_source or "/usr/local/bin/isaac" in codex_source
    ):
        fail("direct Codex wrapper still depends on Isaac")

    sdk_env = _build_claude_sdk_spawn_env(parse(runtime_bundle), cwd=root)
    expected_sdk = {
        "HARNESS_CLAUDE_SDK_MODEL": expected_models["triple-stamp"][0],
        "HARNESS_CLAUDE_SDK_PERMISSION_MODE": "auto",
        "HARNESS_CLAUDE_SDK_SKILLS_FILTER": '"none"',
    }
    for key, expected in expected_sdk.items():
        if sdk_env.get(key) != expected:
            fail(f"SDK supervisor launch env {key} drifted: {sdk_env.get(key)!r}")
    direct_opus_probe = [
        "--model",
        str(expected_models["opus_auditor"][0]),
        "--effort",
        "max",
        "--probe",
    ]
    if selected_provider == "databricks":
        if sdk_env.get("HARNESS_CLAUDE_SDK_GATEWAY") != "true":
            fail("Databricks SDK supervisor gateway flag drifted")
        if not sdk_env.get("HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND"):
            fail("SDK supervisor has no Isaac gateway token helper")
        if "/ai-gateway/anthropic" not in sdk_env.get(
            "HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL", ""
        ):
            fail("SDK supervisor did not resolve an Anthropic-compatible Isaac gateway")
        if resolve_claude_launch("claude", ["--probe"]) != (
            isaac,
            ["--", "--probe", "--permission-mode", "dontAsk"],
        ):
            fail("native Claude launch did not resolve through verified Isaac")
    elif resolve_claude_launch("claude", direct_opus_probe) != (
        "claude",
        direct_opus_probe,
    ):
        fail("direct native Claude launch was not preserved as plain argv")

    from triple_stamp_isaac_launcher import (
        _COST_CEILINGS,
        _OPUS_ALLOWED_TOOLS,
        _OPUS_AUDITOR_PROMPT_MARKER,
        _OPUS_DENIED_TOOLS,
        _OPUS_MCP_NAMES,
        _SUPERVISOR_ROUTE_COUNT_STATE_KEY,
        _SUPERVISOR_ROUTE_LIMIT,
        _model_rates,
        _next_route,
        _triple_stamp_claude_args,
        _valid_audit,
        strict_cost_budget,
        supervisor_contract,
        supervisor_route_call_limit,
    )
    from triple_stamp_opus_mcp import (
        OPUS_STARTUP_ENV,
        WRITE_TOOLS_DENIED,
        configure_opus_startup_environment,
    )
    from triple_stamp_runtime_state import parse_dispatch_title

    if parse_dispatch_title("audit-internal-1-1") != {
        "stage_id": "audit_internal",
        "cycle": 1,
        "hop": 1,
        "requester": "codex",
    }:
        fail("canonical audit-internal dispatch title parsing drifted")
    for model in (
        str(expected_models["triple-stamp"][0]),
        str(expected_models["opus_auditor"][0]),
        str(expected_models["codex_judge"][0]),
    ):
        if _model_rates(model) == _COST_CEILINGS["unknown"]:
            fail(f"active provider model has unknown cost ceiling: {model}")

    auditor_base_args = [
        "--model",
        str(expected_models["opus_auditor"][0]),
        "--effort",
        "max",
        "--append-system-prompt",
        _OPUS_AUDITOR_PROMPT_MARKER,
    ]
    if selected_provider == "direct":
        from triple_stamp_opus_mcp import prepare_opus_mcp_config

        configure_opus_startup_environment()
        auditor_args = _triple_stamp_claude_args(
            auditor_base_args,
            opus_mcp_config=str(prepare_opus_mcp_config()),
        )
    else:
        auditor_args = resolve_claude_launch("claude", auditor_base_args)[1]
    if _option_after(auditor_args, "--model") != str(
        expected_models["opus_auditor"][0]
    ):
        fail("Opus model selector was not preserved as one argv value")
    if _option_after(auditor_args, "--effort") != "max":
        fail("Opus static --effort max launch pin drifted")
    if _option_after(auditor_args, "--permission-mode") != "dontAsk":
        fail("Opus native launch is not mechanically unattended")
    if _option_after(auditor_args, "--tools") != "ToolSearch":
        fail("Opus native launch does not expose exactly ToolSearch")
    if _option_after(auditor_args, "--setting-sources") != "":
        fail("Opus native launch still loads user/project/local settings")
    if "--strict-mcp-config" not in auditor_args:
        fail("Opus native launch does not isolate MCP discovery")
    opus_mcp_path = Path(_option_after(auditor_args, "--mcp-config") or "")
    try:
        opus_mcp_bytes = opus_mcp_path.read_bytes()
        opus_mcp_config = json.loads(opus_mcp_bytes)
    except (OSError, ValueError, json.JSONDecodeError):
        fail("Opus strict MCP config is not a readable run-scoped JSON file")
    if stat.S_IMODE(opus_mcp_path.stat().st_mode) != 0o400:
        fail("Opus strict MCP config is not immutable mode 0400")
    configured_opus_servers = set(opus_mcp_config.get("mcpServers", {}))
    if not configured_opus_servers.issubset(set(_OPUS_MCP_NAMES)):
        fail("Opus strict MCP catalog contains an unrelated server")
    if any(os.environ.get(name) != value for name, value in OPUS_STARTUP_ENV.items()):
        fail("Opus exact-wrapper startup environment controls were stripped")
    argv_digest = hashlib.sha256(chr(0).join(auditor_args).encode()).hexdigest()
    print(
        "Opus launch contract: PASS "
        f"argv_sha256={argv_digest} "
        "mcp_health_gating=disabled readiness_probe=absent"
    )
    expected_read_allow = (
        "ToolSearch",
        "mcp__glean__glean_chat",
        "mcp__confluence__get_confluence_page_comments",
        "mcp__confluence__list_confluence_page_versions",
    )
    if _OPUS_ALLOWED_TOOLS != expected_read_allow:
        fail("Opus read-only pre-approval gaps drifted")
    if _option_after(auditor_args, "--allowedTools") != ",".join(_OPUS_ALLOWED_TOOLS):
        fail("Opus internal MCP allowlist drifted")
    if _option_after(auditor_args, "--disallowedTools") != ",".join(_OPUS_DENIED_TOOLS):
        fail("Opus restricted-tool denylist drifted")
    if any(tool.endswith("__*") for tool in _OPUS_ALLOWED_TOOLS):
        fail("Opus internal MCP allowlist contains a wildcard")
    if not set(WRITE_TOOLS_DENIED).issubset(_OPUS_DENIED_TOOLS):
        fail("Opus write-capable MCP tools are not explicitly denied")
    if set(WRITE_TOOLS_DENIED).intersection(_OPUS_ALLOWED_TOOLS):
        fail("Opus write-capable MCP tool entered the read allowlist")
    if any(
        tool not in _OPUS_DENIED_TOOLS
        for tool in ("Bash", "Shell", "Task", "Agent", "Skill", "WebSearch", "WebFetch")
    ):
        fail("Opus local/public/nested tool denial drifted")
    if _valid_audit(
        '{"verdict":"FAIL","needs_web":false,"web_queries":[],'
        '"attacks":["internal MCP failed"],"must_retest":[],'
        '"acceptable_as_is":false,"punch_list_for_cursor":[],'
        '"internal_sources_consulted":[],'
        '"internal_sources_not_required_reason":"tool unavailable"}'
    ) is None:
        fail("Opus tool-failure FAIL verdict is not accepted")
    if _valid_audit(
        '{"verdict":"FAIL","needs_web":true,"web_queries":[{"claim":"c",'
        '"query":"q","where":"docs","break_how":"compare",'
        '"kill_condition":"absent","prove_condition":"present"}],'
        '"internal_sources_consulted":[],'
        '"internal_sources_not_required_reason":"public-only claim"}'
    ) is not None:
        fail("contradictory FAIL with needs_web true was accepted")
    retained_needs_web = (
        '{"verdict":"NEEDS_WEB","needs_web":true,"summary":"retest",'
        '"gaps":["missing"],"web_queries":[{"claim":"c","query":"q",'
        '"where":"docs","break_how":"compare","kill_condition":"absent",'
        '"prove_condition":"present"}],'
        '"attacks":[],"must_retest":[],"acceptable_as_is":["date"],'
        '"punch_list_for_cursor":["fetch"],'
        '"internal_sources_consulted":[],'
        '"internal_sources_not_required_reason":"public-only claim"}'
    )
    route = _next_route(
        [
            {
                "agent": "cursor_workhorse",
                "title": "cursor-cycle-1",
                "status": "completed",
                "output": "cursor packet",
            },
            {
                "agent": "opus_auditor",
                "title": "audit-cycle-1",
                "status": "completed",
                "output": retained_needs_web,
                "child_session_id": "opus_child",
            },
        ]
    )
    if (route.agent, route.title) != (
        "cursor_workhorse",
        "cursor-web-opus-1-1",
    ):
        fail("retained Opus NEEDS_WEB route was not recognized")
    fresh_reaudit = _next_route(
        [
            {
                "agent": "cursor_workhorse",
                "title": "cursor-cycle-1",
                "status": "completed",
                "output": "cursor packet",
            },
            {
                "agent": "opus_auditor",
                "title": "audit-cycle-1",
                "status": "completed",
                "output": retained_needs_web,
                "child_session_id": "finished-opus-child",
            },
            {
                "agent": "cursor_workhorse",
                "title": "cursor-web-opus-1-1",
                "status": "completed",
                "output": "new web evidence",
            },
        ]
    )
    if (
        fresh_reaudit.agent,
        fresh_reaudit.title,
        fresh_reaudit.resume_child_session_id,
    ) != ("opus_auditor", "audit-cycle-1-web-1", ""):
        fail("Opus web re-audit did not resolve to a fresh native child")

    transient_retry = _next_route(
        [
            {
                "agent": "cursor_workhorse",
                "title": "cursor-cycle-1",
                "status": "completed",
                "output": "cursor packet",
            },
            {
                "agent": "opus_auditor",
                "title": "audit-cycle-1",
                "status": "failed",
                "output": (
                    "API Error: Server error mid-response. The response above "
                    "may be incomplete."
                ),
                "child_session_id": "dead-opus-child",
            },
        ]
    )
    if (transient_retry.status, transient_retry.agent, transient_retry.title) != (
        "dispatch",
        "opus_auditor",
        "audit-retry-1-1",
    ):
        fail("transient Opus stream death did not resolve to a fresh retry child")
    cursor_retry = _next_route(
        [
            {
                "agent": "cursor_workhorse",
                "title": "cursor-cycle-1",
                "status": "failed",
                "output": (
                    "CURSOR_WORKER_TIMEOUT: kind=inactivity "
                    "inactivity_s=301 inactivity_limit_s=300; assistant_chars=0"
                ),
            }
        ]
    )
    if (cursor_retry.status, cursor_retry.agent, cursor_retry.title) != (
        "dispatch",
        "cursor_workhorse",
        "cursor-retry-1-1",
    ):
        fail("zero-output Cursor inactivity did not resolve to one fresh retry")

    runner_env = _build_runner_env(
        os.environ,
        server_url="http://127.0.0.1:1",
        runner_id="runner_validation",
        binding_token="validation",
        workspace=str(root),
        parent_pid=os.getpid(),
    )
    runner_required = [
        "TRIPLE_STAMP_OUTER_SANDBOX",
        "TRIPLE_STAMP_RUN_ID",
        "TRIPLE_STAMP_RUN_DIR",
        "TRIPLE_STAMP_CURSOR_HOME",
        "CURSOR_CONFIG_DIR",
        "CURSOR_DATA_DIR",
        "AGENT_CLI_CREDENTIAL_STORE",
        "CURSOR_AGENT_BIN",
        "OMNIGENT_CURSOR_PATH",
        "OMNIGENT_CODEX_PATH",
        "TRIPLE_STAMP_VOICE_PROFILE",
        "TRIPLE_STAMP_VOICE_PROFILE_SHA256",
        "PYTHONPATH",
        "PYTHONDONTWRITEBYTECODE",
        "TRIPLE_STAMP_CURSOR_APPROVAL_GUARD",
        "TRIPLE_STAMP_ROOT",
        "STABLE_OMNIGENT_PY",
        "TRIPLE_STAMP_AUTH_PREFLIGHT",
        "TRIPLE_STAMP_PROVIDER",
        "TRIPLE_STAMP_MAX_CYCLES",
        "TRIPLE_STAMP_SUPERVISOR_MODEL",
        "TRIPLE_STAMP_OPUS_MODEL",
        "TRIPLE_STAMP_CODEX_MODEL",
        "TRIPLE_STAMP_CLAUDE_NAMESPACE",
        "TRIPLE_STAMP_CODEX_BIN",
        "DISABLE_AUTOUPDATER",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "DISABLE_TELEMETRY",
    ]
    if selected_provider == "databricks":
        runner_required.extend(
            [
                "OMNIGENT_CLAUDE_LAUNCHER",
                "ISAAC_BIN",
                "ISAAC_DEFAULT_UCODE",
                "ISAAC_LAUNCH_MODE",
                "ISAAC_DISABLE_MAC_MANAGED_SETTINGS_UPDATE",
            ]
        )
    for name in runner_required:
        if runner_env.get(name) != os.environ.get(name):
            fail(f"runner environment stripped required variable {name}")

    plugins = [
        entry
        for entry in entry_points(group="omnigent.claude_launcher")
        if entry.name == "isaac"
    ]
    if (
        len(plugins) != 1
        or plugins[0].value != "triple_stamp_isaac_launcher:IsaacClaudeLauncher"
        or version("triple-stamp-isaac-launcher") != "0.4.0"
    ):
        fail("project Isaac Claude launcher is not installed exactly once")
    try:
        launcher_class = plugins[0].load()
    except Exception as exc:  # noqa: BLE001 - entry-point load can raise anything
        fail(f"project Isaac Claude launcher cannot import: {type(exc).__name__}: {exc}")
    if launcher_class.__module__ != "triple_stamp_isaac_launcher":
        fail(f"project Isaac Claude launcher loaded from {launcher_class.__module__!r}")
    expected_launcher = (
        root / ".omnigent/isaac-launcher/triple_stamp_isaac_launcher.py"
    ).resolve()
    if Path(inspect.getfile(launcher_class)).resolve() != expected_launcher:
        fail("project Isaac Claude launcher does not resolve to this bundle")

    contract = supervisor_contract(enabled=True)
    arbitrary_calls = (
        {
            "type": "tool_call",
            "data": {
                "name": "sys_session_send",
                "arguments": {
                    "agent": "opus_auditor",
                    "title": "arbitrary-title",
                    "args": {
                        "input": (
                            "cursor-cycle-1 cursor-cycle-1 and deliberately "
                            "noncanonical handoff prose"
                        )
                    },
                },
            },
        },
        {
            "type": "tool_call",
            "data": {"name": "sys_read_inbox", "arguments": {"native": "validates"}},
        },
        {
            "type": "tool_call",
            "data": {"name": "WebSearch", "arguments": {"query": "not exposed"}},
        },
    )
    if any(contract(event).get("result") != "ALLOW" for event in arbitrary_calls):
        fail("final response contract still gates a routing/tool payload")
    forbidden_retry = {
        "type": "tool_call",
        "data": {
            "name": "sys_session_send",
            "arguments": {
                "agent": "opus_auditor",
                "title": "audit-retry-1-2",
                "args": {"input": "must not launch"},
            },
        },
    }
    if contract(forbidden_retry).get("result") != "DENY":
        fail("second paid Opus retry was not denied before dispatch")

    if _SUPERVISOR_ROUTE_LIMIT != _route_call_cap():
        fail("supervisor route-call formula drifted")
    route_cap = supervisor_route_call_limit()
    root_route = {
        "type": "tool_call",
        "data": {"name": "ToolSearch", "arguments": {}},
        "context": {
            "conversation_id": "root",
            "root_conversation_id": "root",
        },
        "session_state": {},
    }
    if not route_cap(root_route).get("state_updates"):
        fail("verified root supervisor ToolSearch was not counted")
    child_route = dict(root_route)
    child_route["context"] = {
        "conversation_id": "child",
        "root_conversation_id": "root",
    }
    if route_cap(child_route) != {"result": "ALLOW"}:
        fail("child ToolSearch still consumes the supervisor route cap")
    over_route = dict(root_route)
    over_route["session_state"] = {
        _SUPERVISOR_ROUTE_COUNT_STATE_KEY: _SUPERVISOR_ROUTE_LIMIT,
    }
    if route_cap(over_route).get("result") != "DENY":
        fail("verified over-limit supervisor route was not denied")

    prior_run_dir = os.environ.get("TRIPLE_STAMP_RUN_DIR")
    with tempfile.TemporaryDirectory(
        prefix="validator-budget-", dir=os.environ["TRIPLE_STAMP_RUN_DIR"]
    ) as budget_dir:
        os.environ["TRIPLE_STAMP_RUN_DIR"] = budget_dir
        try:
            budget = strict_cost_budget(max_cost_usd=50.0)
            if budget(
                {
                    "type": "request",
                    "context": {"usage": {"total_cost_usd": 50.0}, "model": "any"},
                    "session_state": {},
                }
            ).get("result") != "DENY":
                fail("strict budget did not hard-stop at $50")
        finally:
            if prior_run_dir is None:
                os.environ.pop("TRIPLE_STAMP_RUN_DIR", None)
            else:
                os.environ["TRIPLE_STAMP_RUN_DIR"] = prior_run_dir


def main() -> None:
    if len(sys.argv) != 3:
        fail("usage: validate_bundle.py ROOT RUNTIME_BUNDLE")
    root = Path(sys.argv[1]).resolve()
    runtime_bundle = Path(sys.argv[2]).resolve()
    provider = _provider()
    if Path(os.environ.get("TRIPLE_STAMP_ROOT", "")).resolve() != root:
        fail("runtime root marker does not match the bundle being validated")
    validate_text_files(root)
    validate_runtime_guard(root)
    validate_voice_profile()
    validate_spec(root, runtime_bundle, provider=provider)
    validate_launchers(root, runtime_bundle, provider=provider)
    print("triple-stamp bundle validation: PASS")


if __name__ == "__main__":
    main()
