"""Project-owned Isaac launcher and fail-closed supervisor policies."""

from __future__ import annotations

import hashlib
import json
import os
import re
import textwrap
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from omnigent.claude_launcher import ClaudeLauncher
from triple_stamp_opus_mcp import (
    OPUS_MCP_NAMES,
    READ_ONLY_ALLOWED_TOOLS,
    WRITE_TOOLS_DENIED,
    OpusConfigurationError,
    configure_opus_startup_environment,
    prepare_opus_mcp_config,
)
from triple_stamp_runtime_state import (
    append_supervisor_tool_call,
    parse_dispatch_title,
    read_budget_state,
    read_collections,
    read_dispatches,
    record_best_effort_answer,
    record_terminal_failure,
    write_budget_state,
)

_OPUS_AUDITOR_PROMPT_MARKER = "You are the auditor. You do not trust the workhorse."
_SUPERVISOR_MODEL = os.environ.get(
    "TRIPLE_STAMP_SUPERVISOR_MODEL", "claude-sonnet-4-6"
)
_OPUS_MODEL = os.environ.get("TRIPLE_STAMP_OPUS_MODEL", "claude-opus-5")
_CODEX_MODEL = os.environ.get("TRIPLE_STAMP_CODEX_MODEL", "gpt-5.6-sol")
_OPUS_ALLOWED_TOOLS = READ_ONLY_ALLOWED_TOOLS
_OPUS_MCP_NAMES = OPUS_MCP_NAMES
_OPUS_DENIED_TOOLS = (
    "Bash",
    "Shell",
    "Task",
    "Agent",
    "Skill",
    "WebSearch",
    "WebFetch",
    "mcp__web-search",
    "mcp__omnigent__sys_os_shell",
    "mcp__omnigent__sys_session_send",
    *WRITE_TOOLS_DENIED,
)
def configured_max_cycles(raw: object | None = None) -> int:
    """Return the fail-fast two-cycle default or explicit four-cycle depth."""

    value = os.environ.get("TRIPLE_STAMP_MAX_CYCLES", "2") if raw is None else raw
    text = str(value)
    if text not in {"2", "4"}:
        raise ValueError("TRIPLE_STAMP_MAX_CYCLES must be exactly 2 or 4")
    cycles = int(text)
    return cycles


_MAX_CYCLES = configured_max_cycles()
_SUPERVISOR_ROUTE_TOOLS = frozenset(
    {
        "ToolSearch",
        "sys_agent_start",
        "sys_read_inbox",
        "sys_session_send",
    }
)
_ROUTE_CALLS_PER_CHILD = 2
_ROUTE_BASE_CHILDREN_PER_CYCLE = 3
_ROUTE_OPUS_WEB_CHILDREN_PER_CYCLE = 4
_ROUTE_CODEX_WEB_CHILDREN_PER_CYCLE = 4
_ROUTE_CODEX_INTERNAL_CHILDREN_PER_CYCLE = 4
_ROUTE_FORMAT_REPAIR_CHILDREN_PER_CYCLE = 4
_ROUTE_CHILDREN_PER_CYCLE = (
    _ROUTE_BASE_CHILDREN_PER_CYCLE
    + _ROUTE_OPUS_WEB_CHILDREN_PER_CYCLE
    + _ROUTE_CODEX_WEB_CHILDREN_PER_CYCLE
    + _ROUTE_CODEX_INTERNAL_CHILDREN_PER_CYCLE
    + _ROUTE_FORMAT_REPAIR_CHILDREN_PER_CYCLE
)
_ROUTE_FIXED_OVERHEAD_CALLS = 8
_ROUTE_STATE_MACHINE_CALLS = (
    _MAX_CYCLES * _ROUTE_CHILDREN_PER_CYCLE * _ROUTE_CALLS_PER_CHILD
)
_ROUTE_SCHEMA_DISCOVERY_CALLS = 4
_ROUTE_AGENT_START_CALLS = 2
_ROUTE_FINALIZATION_OVERHEAD_CALLS = 2


def supervisor_route_call_cap(max_cycles: object | None = None) -> int:
    """Return the exact scoped root-route cap: cycles * 19 * 2 + 8."""

    cycles = _MAX_CYCLES if max_cycles is None else configured_max_cycles(max_cycles)
    return (
        cycles * _ROUTE_CHILDREN_PER_CYCLE * _ROUTE_CALLS_PER_CHILD
        + _ROUTE_FIXED_OVERHEAD_CALLS
    )


_SUPERVISOR_ROUTE_LIMIT = supervisor_route_call_cap()
_SUPERVISOR_ROUTE_COUNT_STATE_KEY = "_triple_stamp_supervisor_route_call_count"
_COMPLETION = re.compile(
    r"^\[System: sub-agent task (?P<id>\S+) "
    r"(?P<status>completed|failed|cancelled) — "
    r"(?P<agent>cursor_workhorse|opus_auditor|codex_judge)"
    r"(?::(?P<title>[^\s\]]+))?"
    r"(?:(?: returned: | error: |: )(?P<output>.*))?\]$",
    re.DOTALL,
)
_NO_OUTPUT = re.compile(
    r"^\[System: sub-agent task \S+ completed — "
    r"(?P<agent>cursor_workhorse|opus_auditor|codex_judge)"
    r"(?::[^\s\]]+)? produced no output\]$",
    re.DOTALL,
)
_INFRA_PREFIX = "PIPELINE_INFRASTRUCTURE_ERROR:"
_VALIDATION_PREFIX = "PIPELINE_VALIDATION_FAILED:"

# Every Opus audit-stage kind. A transient platform stream death on any of
# these can be recovered by a fresh child under a unique title, exactly the way
# NEEDS_WEB and NEEDS_INTERNAL already relaunch fresh Opus children.
_OPUS_AUDIT_KINDS = frozenset(
    {
        "audit",
        "audit_web",
        "audit_retry",
        "audit_internal",
        "audit_repair",
        "audit_repair_web",
    }
)
# Lower-cased substrings that mark a transient, platform-side Opus stream death:
# the native Claude process began (or completed) a turn whose response the model
# provider cut off mid-stream. These are distinct from a genuine worker failure
# (a native timeout, a pin/harness break, or empty output), which stays
# terminal. Detection is marker-based so a plain "native timeout" is never
# misclassified as recoverable.
_TRANSIENT_OPUS_STREAM_MARKERS = (
    "server error mid-response",
    "the response above may be incomplete",
    "response may be incomplete",
    "api error: server error",
    "api error: overloaded",
    "overloaded_error",
    "internal server error",
    "error streaming response",
    "stream disconnected",
    "connection reset by peer",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "http 529",
)
# One fresh retry per cycle. A transient death dispatches audit-retry-N-1; a
# second transient death (on that retry) is terminal infrastructure failure.
# The bound is deliberately conservative: every retry is a paid Opus launch, and
# a single fresh child recovers the overwhelmingly common one-off provider blip
# without risking repeated spend on a persistently failing provider. The title
# grammar still admits hop 2 so the cap can be raised by this one constant with
# no title or parser change.
_OPUS_TRANSIENT_RETRY_CAP = 1


def _has_incomplete_stream_marker(text: object) -> bool:
    """Return whether text bears a transient mid-stream truncation marker."""

    if not isinstance(text, str) or not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _TRANSIENT_OPUS_STREAM_MARKERS)


def _is_transient_opus_stream_error(record: dict[str, Any]) -> bool:
    """Classify one Opus record as a recoverable, transient stream death.

    True only when the record carries a concrete transient platform marker in
    its output. A failed record without such a marker (for example a native
    timeout, a model/harness pin break, or empty output) is a genuine worker
    failure and stays terminal.
    """

    return _has_incomplete_stream_marker(record.get("output"))


def _voice_profile_path() -> str:
    """The configured voice profile, or "" when voice rendering is off."""

    return os.environ.get("TRIPLE_STAMP_VOICE_PROFILE", "").strip()
_COST_CEILINGS = {
    # Cursor's persisted non-cache input also contains cache-write tokens.
    # Charge that whole bucket at the higher write ceiling.
    "cursor": (37.5, 180.0, 3.0, 37.5),
    "opus": (30.0, 150.0, 3.0, 37.5),
    "sonnet": (6.0, 30.0, 0.6, 7.5),
    "gpt-5.6-sol": (30.0, 180.0, 3.0, 37.5),
    "unknown": (50.0, 250.0, 50.0, 62.5),
}
_STAGE_PROJECTIONS = {
    "cursor_workhorse": ("cursor-grok-4.6-xhigh", 80_000, 20_000),
    "opus_auditor": (_OPUS_MODEL, 40_000, 10_000),
    "codex_judge": (_CODEX_MODEL, 80_000, 25_000),
}


def _has_option(args: list[str], *names: str) -> bool:
    return any(arg in names for arg in args)


def _is_opus_process(args: list[str]) -> bool:
    for index, arg in enumerate(args):
        if arg == "--model" and index + 1 < len(args):
            return args[index + 1] == _OPUS_MODEL
        if arg.startswith("--model="):
            return arg.removeprefix("--model=") == _OPUS_MODEL
    return False


def _without_claude_options(
    args: list[str],
    *,
    value_options: tuple[str, ...] = (),
    flag_options: tuple[str, ...] = (),
) -> list[str]:
    """Remove every spelling of selected Claude options and their values."""

    result: list[str] = []
    skip_value = False
    prefixes = tuple(f"{name}=" for name in value_options)
    for arg in args:
        if skip_value:
            skip_value = False
            continue
        if arg in value_options:
            skip_value = True
            continue
        if arg in flag_options or arg.startswith(prefixes):
            continue
        result.append(arg)
    return result


def _triple_stamp_claude_args(
    args: list[str],
    *,
    opus_mcp_config: str | None = None,
) -> list[str]:
    """Harden only the native Opus worker; the supervisor is SDK-based."""

    if not os.environ.get("TRIPLE_STAMP_RUN_ID"):
        return list(args)
    hardened = list(args)
    if _is_opus_process(hardened):
        if not opus_mcp_config:
            raise RuntimeError(
                "PIPELINE_INFRASTRUCTURE_ERROR: Opus launch did not "
                "provide its immutable run-scoped config"
            )
        # Omnigent adds its own bridge MCP JSON before the launcher plugin runs.
        # Strict mode would otherwise pin that bridge and silently discard the
        # run-scoped Glean/Slack/Jira config. Replace every caller value
        # rather than treating the first --mcp-config as authoritative.
        hardened = _without_claude_options(
            hardened,
            value_options=(
                "--permission-mode",
                "--tools",
                "--setting-sources",
                "--mcp-config",
                "--allowedTools",
                "--allowed-tools",
                "--disallowedTools",
                "--disallowed-tools",
            ),
            flag_options=("--strict-mcp-config", "--disable-slash-commands"),
        )
        hardened.extend(
            (
                "--permission-mode",
                "dontAsk",
                "--tools",
                "ToolSearch",
                "--setting-sources",
                "",
                "--strict-mcp-config",
                "--mcp-config",
                opus_mcp_config,
                "--allowedTools",
                ",".join(_OPUS_ALLOWED_TOOLS),
                "--disallowedTools",
                ",".join(_OPUS_DENIED_TOOLS),
                "--disable-slash-commands",
            )
        )
    elif not _has_option(hardened, "--permission-mode"):
        hardened.extend(("--permission-mode", "dontAsk"))
    return hardened


class IsaacClaudeLauncher(ClaudeLauncher):
    """Launch native workers through Isaac with a strict optional MCP catalog."""

    def launch(self, command: str, args: list[str]) -> tuple[str, list[str]]:
        del command
        isaac = os.environ.get("ISAAC_BIN", "")
        if not isaac or not os.path.isabs(isaac) or not os.access(isaac, os.X_OK):
            raise RuntimeError(
                "triple-stamp: ISAAC_BIN is not an absolute executable; "
                "refusing to fall back through PATH"
            )
        is_auditor = _is_opus_process(args)
        opus_mcp_config: str | None = None
        if is_auditor:
            configure_opus_startup_environment()
            try:
                config_path = prepare_opus_mcp_config()
                opus_mcp_config = str(config_path)
            except OpusConfigurationError as exc:
                raise RuntimeError(
                    "PIPELINE_INFRASTRUCTURE_ERROR: Opus strict MCP "
                    f"configuration failed: {exc.reason}"
                ) from exc
        runtime_args = [
            "--",
            *_triple_stamp_claude_args(
                args,
                opus_mcp_config=opus_mcp_config,
            ),
        ]
        return isaac, runtime_args


def _normalize_tool_name(value: object) -> str:
    name = value if isinstance(value, str) else ""
    if name.startswith("mcp__") and "__" in name[5:]:
        return name.split("__", 2)[-1]
    return name


def _iter_session_text() -> Iterable[str]:
    """Yield persisted message text for the current runner conversation."""

    try:
        from omnigent.debug_logging import runner_primary_session_id
        from omnigent.runtime import get_conversation_store

        session_id = runner_primary_session_id()
        if not session_id:
            return
        store = get_conversation_store()
        cursor: str | None = None
        while True:
            page = store.list_items(session_id, after=cursor)
            for item in page.data:
                payload = item.to_api_dict()
                if payload.get("type") != "message":
                    continue
                content = payload.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    text = block.get("text")
                    if isinstance(text, str):
                        yield text
            if not page.has_more or not page.last_id:
                break
            cursor = page.last_id
    except Exception:  # noqa: BLE001 - conversation-store observation is best effort
        return


def _completion_records() -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for text in _iter_session_text():
        match = _COMPLETION.fullmatch(text.strip())
        if match is not None:
            records.append(
                {
                    key: value or ""
                    for key, value in match.groupdict().items()
                }
            )
            continue
        empty = _NO_OUTPUT.fullmatch(text.strip())
        if empty is not None:
            records.append(
                {
                    "id": "",
                    "status": "completed",
                    "agent": empty.group("agent"),
                    "title": "",
                    "output": "",
                }
            )
    return records


def _mapping_candidates(text: str) -> Iterable[dict[str, Any]]:
    """Parse strict or embedded JSON/YAML mappings without requiring wrapper purity."""

    candidates = [text.strip()]
    candidates.extend(
        match.group(1).strip()
        for match in re.finditer(
            r"```(?:json|ya?ml)?\s*\n(.*?)\n```",
            text,
            re.DOTALL | re.IGNORECASE,
        )
    )
    for candidate in candidates:
        for loader in (json.loads, yaml.safe_load):
            try:
                value = loader(candidate)
            except (TypeError, ValueError, yaml.YAMLError):
                continue
            if isinstance(value, dict):
                yield value
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            yield value


_AUDIT_VERDICTS = frozenset({"PASS", "PASS_WITH_GAPS", "FAIL", "NEEDS_WEB"})
_INTERNAL_SYSTEMS = ("glean", "jira", "slack", "confluence", "safe")
_COVERAGE_STATUSES = frozenset(
    {"evidence_found", "searched_no_hits", "unavailable_after_retry"}
)
_COVERAGE_ROUTES = frozenset({"native", "glean_facet"})
_WEB_HUNT_FIELD_ALIASES = (
    ("claim",),
    ("query", "exact_query", "exact_query_or_command"),
    ("where", "where_to_look"),
    ("break_how", "how_to_break", "how_to_break_it"),
    ("kill_condition",),
    ("prove_condition",),
)


def _valid_web_hunt_spec(spec: object) -> bool:
    """Require every concrete public-web hunt dimension."""

    return isinstance(spec, dict) and all(
        any(
            isinstance(spec.get(name), str) and bool(spec[name].strip())
            for name in aliases
        )
        for aliases in _WEB_HUNT_FIELD_ALIASES
    )


def _valid_internal_source_contract(payload: dict[str, Any]) -> bool:
    """Require auditable internal sources or an explicit reason none were needed."""

    sources = payload.get("internal_sources_consulted")
    if not isinstance(sources, list):
        return False
    if not sources:
        reason = payload.get("internal_sources_not_required_reason")
        return isinstance(reason, str) and bool(reason.strip())
    required = {
        "system",
        "url_or_record_id",
        "exact_quote_or_concise_evidence",
        "retrieval_timestamp",
    }
    return all(
        isinstance(source, dict)
        and required.issubset(source)
        and all(
            isinstance(source[field], str) and source[field].strip()
            for field in required
        )
        for source in sources
    )


def _coverage_entries(
    payload: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Normalize list or keyed-map receipts without treating shape as substance."""

    raw = payload.get("internal_coverage")
    issues: list[str] = []
    rows: list[tuple[str, object]]
    if isinstance(raw, dict):
        rows = [(str(key), value) for key, value in raw.items()]
    elif isinstance(raw, list):
        rows = [
            (
                str(value.get("system") or "")
                if isinstance(value, dict)
                else "",
                value,
            )
            for value in raw
        ]
    else:
        return {}, ["internal_coverage is not a list or keyed mapping"]

    entries: dict[str, dict[str, Any]] = {}
    for key, value in rows:
        if not isinstance(value, dict):
            issues.append(f"{key or 'unknown'} receipt is not an object")
            continue
        system = str(value.get("system") or key).lower()
        if system not in _INTERNAL_SYSTEMS:
            issues.append(f"receipt has unknown system {system or 'empty'}")
            continue
        if system in entries:
            issues.append(f"duplicate {system} receipt")
            continue
        entry = dict(value)
        entry["system"] = system
        entries[system] = entry
    for system in _INTERNAL_SYSTEMS:
        if system not in entries:
            issues.append(f"missing {system} receipt")
    return entries, issues


def _audit_coverage_validation(
    payload: dict[str, Any],
    observation: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate receipts against exact-child calls while preserving tri-state."""

    entries, form_issues = _coverage_entries(payload)
    invalid_tool_claims: list[dict[str, str]] = []
    claimed_tools: dict[str, list[str]] = {}
    for system, entry in entries.items():
        status = entry.get("status")
        if status not in _COVERAGE_STATUSES:
            form_issues.append(f"{system} has invalid status")
        routes = entry.get("routes")
        if (
            not isinstance(routes, list)
            or any(route not in _COVERAGE_ROUTES for route in routes)
            or (not routes and status != "unavailable_after_retry")
        ):
            form_issues.append(f"{system} has invalid routes")
            routes = []
        queries = entry.get("queries")
        if not (
            isinstance(queries, list)
            and queries
            and all(isinstance(query, str) and query.strip() for query in queries)
        ):
            form_issues.append(f"{system} lacks verbatim queries")
        tools = entry.get("tools_called")
        if not isinstance(tools, list) or not all(
            isinstance(tool, str) and tool for tool in tools
        ):
            form_issues.append(f"{system} tools_called is not a string list")
            tools = []
        claimed_tools[system] = list(tools)
        if status == "unavailable_after_retry":
            if tools:
                form_issues.append(f"{system} unavailable receipt names tools")
            note = entry.get("note")
            if not isinstance(note, str) or "two" not in note.lower():
                form_issues.append(
                    f"{system} unavailable receipt lacks two-attempt note"
                )
        elif not tools:
            form_issues.append(f"{system} {status or 'unknown'} receipt has no tool")
        results = entry.get("results_seen")
        if status == "evidence_found" and (
            isinstance(results, (int, float)) and results <= 0
        ):
            form_issues.append(f"{system} evidence_found has no results")
        if status == "searched_no_hits" and results != 0:
            form_issues.append(f"{system} searched_no_hits is not zero")

        for tool in tools:
            match = re.fullmatch(r"mcp__([a-z0-9_]+)__[A-Za-z0-9_.-]+", tool)
            family = match.group(1) if match is not None else ""
            allowed_fallback = (
                family == "glean"
                and system in {"jira", "slack", "confluence"}
                and "glean_facet" in routes
            )
            if family != system and not allowed_fallback:
                invalid_tool_claims.append(
                    {
                        "system": system,
                        "tool": tool,
                        "reason": "tool family does not map to receipt",
                    }
                )

    observed = (
        isinstance(observation, dict)
        and observation.get("available") is True
        and observation.get("status") in {"observed", "observed_zero"}
    )
    observed_names: set[str] = set()
    observed_by_system: dict[str, int] = {}
    if observed and isinstance(observation, dict):
        calls = observation.get("calls")
        if isinstance(calls, list):
            observed_names = {
                str(call.get("name"))
                for call in calls
                if isinstance(call, dict)
                and isinstance(call.get("name"), str)
                and str(call["name"]).startswith("mcp__")
            }
        raw_counts = observation.get("by_system")
        if isinstance(raw_counts, dict):
            observed_by_system = {
                str(system): int(count)
                for system, count in raw_counts.items()
                if isinstance(count, int) and not isinstance(count, bool) and count >= 0
            }
        for system, tools in claimed_tools.items():
            for tool in tools:
                if tool not in observed_names:
                    invalid_tool_claims.append(
                        {
                            "system": system,
                            "tool": tool,
                            "reason": "tool was not observed for this Opus child",
                        }
                    )
            entry = entries.get(system, {})
            routes = entry.get("routes")
            relevant_count = observed_by_system.get(system, 0)
            if (
                isinstance(routes, list)
                and "glean_facet" in routes
                and system in {"jira", "slack", "confluence"}
            ):
                relevant_count += observed_by_system.get("glean", 0)
            status = entry.get("status")
            if status in {"evidence_found", "searched_no_hits"} and relevant_count == 0:
                form_issues.append(
                    f"{system} receipt claims a search but exact-child count is zero"
                )
            if status == "unavailable_after_retry" and relevant_count > 0:
                form_issues.append(
                    f"{system} unavailable receipt contradicts exact-child calls"
                )

    if not _valid_internal_source_contract(payload):
        form_issues.append("internal source receipt schema is incomplete")
    status = (
        "invalid_tool_claims"
        if invalid_tool_claims
        else "form_imperfect"
        if form_issues
        else "mechanically_validated"
        if observed
        else "advisory_unobserved"
    )
    return {
        "status": status,
        "objective_observation_available": observed,
        "tool_claims_validated": observed and not invalid_tool_claims,
        "invalid_tool_claims": invalid_tool_claims,
        "form_issues": list(dict.fromkeys(form_issues)),
        "claimed_tools": claimed_tools,
        "observed_by_system": observed_by_system,
        "observed_tools": sorted(observed_names),
    }


def _audit_payload_and_validation(
    text: object,
    observation: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Extract an audit and independently classify its receipt quality."""

    if not isinstance(text, str) or not text.strip():
        return None, None
    lowered = text.lower()
    if "tool use: bash" in lowered or "cursor-agent --version" in lowered:
        return None, None
    # An Opus turn the provider truncated mid-stream can leave a syntactically
    # parseable verdict object behind. Incomplete output must never be treated
    # as audit evidence; the router recovers it with a fresh retry child.
    if _has_incomplete_stream_marker(lowered):
        return None, None
    payloads = [
        candidate
        for candidate in _mapping_candidates(text)
        if candidate.get("verdict") in _AUDIT_VERDICTS
    ]
    verdicts = {str(payload["verdict"]) for payload in payloads}
    if not payloads or len(verdicts) != 1:
        return None, None
    payload = dict(payloads[0])
    validation = _audit_coverage_validation(payload, observation)
    payload["audit_validation"] = validation
    if payload["verdict"] != "NEEDS_WEB":
        return (
            (payload, validation)
            if payload.get("needs_web") is False
            else (None, validation)
        )
    hunts = payload.get("web_queries")
    if (
        payload.get("needs_web") is True
        and isinstance(hunts, list)
        and bool(hunts)
        and all(_valid_web_hunt_spec(hunt) for hunt in hunts)
    ):
        return payload, validation
    if hunts is not None or "needs_web" in payload:
        return None, validation
    # A YAML-style mapping may express each hunt field directly instead of
    # nesting it under web_queries; validate that structured form below.

    verdicts = {
        match.group(1).upper()
        for match in re.finditer(
            r"(?im)^\s*(?:[-*]\s*)?verdict\s*:\s*[\"']?"
            r"(PASS_WITH_GAPS|PASS|FAIL|NEEDS_WEB)\b",
            text,
        )
    }
    if len(verdicts) != 1:
        return None, validation
    verdict = verdicts.pop()
    if verdict != "NEEDS_WEB":
        return None, validation
    hunt_markers = {
        marker
        for marker in (
            "claim",
            "query",
            "exact_query",
            "exact_query_or_command",
            "where",
            "where_to_look",
            "break_how",
            "how_to_break",
            "how_to_break_it",
            "kill_condition",
            "prove_condition",
        )
        if re.search(rf"(?im)^\s*(?:[-*]\s*)?{marker}\s*:", text)
    }
    if (
        {"claim", "kill_condition", "prove_condition"}.issubset(
            hunt_markers
        )
        and (
            {"query", "exact_query", "exact_query_or_command"}
            & hunt_markers
        )
        and ({"where", "where_to_look"} & hunt_markers)
        and (
            {"break_how", "how_to_break", "how_to_break_it"}
            & hunt_markers
        )
    ):
        payload["needs_web"] = True
        payload["web_queries"] = [{"raw": text}]
        return payload, validation
    return None, validation


def _valid_audit(
    text: object,
    observation: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return a semantic audit unless exact-child evidence disproves its ledger."""

    payload, validation = _audit_payload_and_validation(text, observation)
    if payload is None or validation is None:
        return None
    if validation["status"] == "invalid_tool_claims":
        return None
    return payload


_JUDGE_VERDICTS = frozenset(
    {"STAMP", "REWORK", "NEEDS_WEB", "NEEDS_INTERNAL"}
)
_INTERNAL_LOOKUP_FIELDS = frozenset(
    {
        "claim",
        "exact_query_or_tool_call",
        "which_system",
        "where_to_look",
        "how_to_break_it",
        "kill_condition",
        "prove_condition",
    }
)
_GAP_STABLE_FIELDS = (
    "gap_type",
    "claim",
    "required_capability",
    "required_source",
    "requested_proof",
)
_INTERNAL_GAP_VALUES = frozenset(
    {
        "internal",
        "glean",
        "jira",
        "slack",
        "confluence",
        "safe",
        "internal_receipts",
        "roadmap",
        "customer_history",
    }
)
_CURSOR_GAP_TYPES = frozenset({"public_web", "live_test", "stage1_evidence"})
_CURSOR_CAPABILITIES = frozenset(
    {"cursor_public_web", "cursor_live_test", "cursor_stage1"}
)
_CURSOR_SOURCES = frozenset(
    {
        "public_web",
        "official_docs",
        "live_system",
        "test_environment",
        "stage1_packet",
    }
)
_FORM_GAP_TYPES = frozenset({"form", "schema", "observer"})
_FORM_CAPABILITIES = frozenset(
    {"audit_receipt", "schema", "observer", "format"}
)
_FORM_SOURCES = frozenset({"audit_packet", "observer", "schema"})


def _normalize_gap_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold())


def _normalized_codex_punch_list(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Return stable structured gap identities, independent of prose ordering."""

    raw = payload.get("punch_list_for_cursor")
    if not isinstance(raw, list):
        return []
    normalized: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(field), str) and item[field].strip()
            for field in _GAP_STABLE_FIELDS
        ):
            return []
        stable = {
            field: _normalize_gap_text(str(item[field]))
            for field in _GAP_STABLE_FIELDS
        }
        encoded = json.dumps(
            stable,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        stable["canonical_id"] = hashlib.sha256(encoded).hexdigest()
        normalized.append(stable)
    return sorted(normalized, key=lambda item: item["canonical_id"])


def _punch_list_signature(items: list[dict[str, str]]) -> str:
    """Use exact canonical-ID set equality as the convergence threshold."""

    ids = sorted({item["canonical_id"] for item in items})
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest() if ids else ""


def _structured_internal_gap(item: dict[str, str]) -> bool:
    return any(
        item[field] in _INTERNAL_GAP_VALUES
        for field in ("gap_type", "required_capability", "required_source")
    )


def _internal_query_from_gap(item: dict[str, str]) -> dict[str, str]:
    source = item["required_source"]
    capability = item["required_capability"]
    system = source if source in _INTERNAL_GAP_VALUES else capability
    return {
        "claim": item["claim"],
        "exact_query_or_tool_call": item["requested_proof"],
        "which_system": system,
        "where_to_look": source,
        "how_to_break_it": item["requested_proof"],
        "kill_condition": f"Evidence disproves: {item['claim']}",
        "prove_condition": item["requested_proof"],
    }


def _valid_materiality(payload: dict[str, Any], verdict: str) -> bool:
    materiality = payload.get("gap_materiality")
    limitations = payload.get("limitations")
    if not isinstance(limitations, list) or not all(
        isinstance(item, str) and item.strip() for item in limitations
    ):
        return False
    if verdict == "STAMP":
        return materiality in {"none", "nonmaterial"} and (
            materiality != "nonmaterial" or bool(limitations)
        )
    return materiality == "material"


def _valid_judgment(text: object) -> dict[str, Any] | None:
    """Return a complete Codex judgment object or ``None``."""

    if not isinstance(text, str) or not text.strip():
        return None
    payload = next(iter(_mapping_candidates(text)), None)
    if payload is None or payload.get("verdict") not in _JUDGE_VERDICTS:
        return None
    # Retained pre-NEEDS_INTERNAL judgments omitted this false-valued key.
    payload.setdefault("needs_internal", False)
    if (
        not isinstance(payload.get("needs_web"), bool)
        or not isinstance(payload.get("needs_internal"), bool)
        or not isinstance(payload.get("why"), str)
    ):
        return None
    verdict = payload["verdict"]
    if not _valid_materiality(payload, verdict):
        return None
    if verdict == "STAMP":
        return payload if _valid_stamp(payload) is not None else None
    field = {
        "NEEDS_WEB": "web_queries",
        "NEEDS_INTERNAL": "internal_queries",
        "REWORK": "punch_list_for_cursor",
    }[verdict]
    if not isinstance(payload.get(field), list) or not payload[field]:
        return None
    if payload["needs_web"] is not (verdict == "NEEDS_WEB"):
        return None
    if payload["needs_internal"] is not (verdict == "NEEDS_INTERNAL"):
        return None
    best_supported = payload.get("best_supported_answer")
    if best_supported is not None and (
        not isinstance(best_supported, str)
        or not best_supported.strip()
        or best_supported.startswith(
            ("PIPELINE_VALIDATION_FAILED:", "PIPELINE_INFRASTRUCTURE_ERROR:")
        )
    ):
        return None
    if verdict == "REWORK":
        normalized = _normalized_codex_punch_list(payload)
        if len(normalized) != len(payload[field]):
            return None
        internal = [item for item in normalized if _structured_internal_gap(item)]
        if internal:
            amended = dict(payload)
            amended.update(
                verdict="NEEDS_INTERNAL",
                needs_web=False,
                needs_internal=True,
                internal_queries=[
                    _internal_query_from_gap(item) for item in internal
                ],
                mechanically_rerouted_from="REWORK",
                normalized_punch_list=normalized,
                punch_list_signature=_punch_list_signature(normalized),
            )
            return amended
        for item in normalized:
            gap_type = item["gap_type"]
            capability = item["required_capability"]
            source = item["required_source"]
            if gap_type in _FORM_GAP_TYPES:
                if (
                    capability not in _FORM_CAPABILITIES
                    or source not in _FORM_SOURCES
                ):
                    return None
                continue
            if (
                gap_type not in _CURSOR_GAP_TYPES
                or capability not in _CURSOR_CAPABILITIES
                or source not in _CURSOR_SOURCES
            ):
                return None
        payload["normalized_punch_list"] = normalized
        payload["punch_list_signature"] = _punch_list_signature(normalized)
        payload["requires_convergence_adjudication"] = any(
            item["gap_type"] in _FORM_GAP_TYPES for item in normalized
        )
    if verdict == "NEEDS_INTERNAL" and not all(
        isinstance(spec, dict)
        and _INTERNAL_LOOKUP_FIELDS.issubset(spec)
        and all(
            isinstance(spec[name], str) and bool(spec[name].strip())
            for name in _INTERNAL_LOOKUP_FIELDS
        )
        for spec in payload[field]
    ):
        return None
    return payload


@dataclass(frozen=True)
class _Stage:
    """Canonical routing stage parsed from a stable title."""

    kind: str
    cycle: int
    requester: str = ""
    hop: int = 0


@dataclass(frozen=True)
class _Route:
    """The sole next action derived from the durable collection ledger."""

    status: str
    cycle: int
    agent: str = ""
    title: str = ""
    requester: str = ""
    hop: int = 0
    resume_child_session_id: str = ""
    reason: str = ""


def _stage(title: object) -> _Stage | None:
    parsed = parse_dispatch_title(title)
    if parsed is None:
        return None
    return _Stage(
        str(parsed["stage_id"]),
        int(parsed["cycle"]),
        requester=str(parsed["requester"]),
        hop=int(parsed["hop"]),
    )


def _resume_child(
    records: list[dict[str, Any]],
    agent: str,
    title: str,
    *,
    parent_session_id: str = "",
) -> str:
    for record in records:
        if (
            record.get("agent") == agent
            and record.get("title") == title
            and (
                not parent_session_id
                or record.get("parent_session_id") == parent_session_id
            )
        ):
            return str(record.get("child_session_id") or "")
    return ""


def _internal_reaudit_route(
    records: list[dict[str, Any]],
    cycle: int,
    *,
    reason: str = "",
) -> _Route:
    """Route exact internal receipt/capability gaps only to fresh Opus."""

    used = sum(
        1
        for record in records
        if (
            (parsed := _stage(record.get("title"))) is not None
            and parsed.kind == "audit_internal"
            and parsed.cycle == cycle
        )
    )
    if used >= 2:
        return _Route(
            "best_effort",
            cycle,
            requester="codex",
            hop=used,
            reason=(
                f"Codex internal-hop cap exhausted in cycle {cycle}"
                + (f": {reason}" if reason else "")
            ),
        )
    hop = used + 1
    return _Route(
        "dispatch",
        cycle,
        "opus_auditor",
        f"audit-internal-{cycle}-{hop}",
        requester="codex",
        hop=hop,
        reason=reason,
    )


def _opus_transient_retries_used(
    records: list[dict[str, Any]],
    cycle: int,
) -> int:
    """Count fresh transient-retry Opus children already spent this cycle."""

    return sum(
        1
        for record in records
        if (
            (parsed := _stage(record.get("title"))) is not None
            and parsed.kind == "audit_retry"
            and parsed.cycle == cycle
        )
    )


def _opus_transient_retry_available(
    records: list[dict[str, Any]],
    cycle: int,
) -> bool:
    """Return whether another transient Opus retry is still within budget."""

    return _opus_transient_retries_used(records, cycle) < _OPUS_TRANSIENT_RETRY_CAP


def _opus_transient_retry_route(
    records: list[dict[str, Any]],
    cycle: int,
    *,
    reason: str = "",
) -> _Route:
    """Recover a transient Opus stream death with a fresh, unique-title child.

    This is not a model-quality cycle: it never advances the cycle counter, is
    scoped to one parent's ledger, and the truncated packet that triggered it is
    never read as audit evidence. Bounded to two retries per cycle; a third
    transient death is terminal infrastructure failure.
    """

    used = _opus_transient_retries_used(records, cycle)
    if used >= _OPUS_TRANSIENT_RETRY_CAP:
        return _Route(
            "infrastructure_failed",
            cycle,
            reason=(
                f"Opus transient stream-error retries exhausted in cycle {cycle}"
                + (f": {reason}" if reason else "")
            ),
        )
    hop = used + 1
    return _Route(
        "dispatch",
        cycle,
        "opus_auditor",
        f"audit-retry-{cycle}-{hop}",
        requester="opus",
        hop=hop,
        reason=(
            "transient Opus stream death recovered with a fresh child"
            + (f": {reason}" if reason else "")
        ),
    )


def _prior_equivalent_rework(
    records: list[dict[str, Any]],
    cycle: int,
    payload: dict[str, Any],
) -> bool:
    """Compare consecutive cycles by exact normalized canonical gap IDs."""

    current = payload.get("punch_list_signature")
    if cycle <= 1 or not isinstance(current, str) or not current:
        return False
    for record in reversed(records[:-1]):
        parsed = _stage(record.get("title"))
        if (
            parsed is None
            or parsed.cycle != cycle - 1
            or parsed.kind not in {"judge", "judge_repair"}
        ):
            continue
        previous = _valid_judgment(record.get("output"))
        return (
            previous is not None
            and previous.get("verdict") == "REWORK"
            and previous.get("punch_list_signature") == current
        )
    return False


def _verdict_route(
    records: list[dict[str, Any]],
    *,
    requester: str,
    cycle: int,
    payload: dict[str, Any],
) -> _Route:
    verdict = str(payload["verdict"])
    if verdict == "NEEDS_WEB":
        used = sum(
            1
            for record in records
            if (
                (parsed := _stage(record.get("title"))) is not None
                and parsed.kind == "cursor_web"
                and parsed.cycle == cycle
                and parsed.requester == requester
            )
        )
        if used >= 2:
            return _Route(
                "best_effort",
                cycle,
                requester=requester,
                hop=used,
                reason=f"{requester} web-hop cap exhausted in cycle {cycle}",
            )
        hop = used + 1
        return _Route(
            "dispatch",
            cycle,
            "cursor_workhorse",
            f"cursor-web-{requester}-{cycle}-{hop}",
            requester=requester,
            hop=hop,
        )
    if requester == "codex" and verdict == "NEEDS_INTERNAL":
        return _internal_reaudit_route(
            records,
            cycle,
            reason="Codex identified a material internal evidence gap",
        )
    if requester == "opus":
        # FAIL still goes to Codex.  Opus is an adversarial auditor; Codex is
        # the contract's final adjudicator and may return the concrete REWORK.
        return _Route("dispatch", cycle, "codex_judge", f"judge-cycle-{cycle}")
    if verdict == "REWORK":
        punch_list = json.dumps(
            payload.get("punch_list_for_cursor", []),
            ensure_ascii=False,
        ).lower()
        # Only a capability failure when a profile is actually configured. With
        # voice rendering off there is no file to fail to read, so the same words
        # are a stray punch item and must not end the run.
        voice_basename = os.path.basename(_voice_profile_path()).lower()
        if voice_basename and (
            voice_basename in punch_list
            or (
                "voice profile" in punch_list
                and any(word in punch_list for word in ("read", "access", "sha-256"))
            )
        ):
            return _Route(
                "infrastructure_failed",
                cycle,
                reason=(
                    "Codex could not access the configured voice profile; "
                    "this is a launch capability failure, not Cursor rework"
                ),
            )
        if payload.get("requires_convergence_adjudication") is True or (
            _prior_equivalent_rework(records, cycle, payload)
        ):
            title = f"judge-convergence-{cycle}"
            if any(record.get("title") == title for record in records):
                return _Route(
                    "best_effort",
                    cycle,
                    reason=(
                        "bounded convergence adjudication declined STAMP; "
                        "no further full cycle is allowed"
                    ),
                )
            return _Route(
                "dispatch",
                cycle,
                "codex_judge",
                title,
                requester="codex",
                reason=(
                    "two consecutive structured punch lists are canonically "
                    "equivalent"
                ),
            )
        if cycle >= _MAX_CYCLES:
            return _Route(
                "best_effort",
                cycle,
                reason=(
                    "Codex requested rework after configured final cycle "
                    f"{_MAX_CYCLES}"
                ),
            )
        return _Route(
            "dispatch", cycle + 1, "cursor_workhorse", f"cursor-cycle-{cycle + 1}"
        )
    return _Route("success", cycle)


def _next_route(
    records: list[dict[str, Any]],
    parent_session_id: str = "",
) -> _Route:
    """Reduce the ledger to one deterministic next transition."""

    if parent_session_id:
        records = [
            record
            for record in records
            if record.get("parent_session_id") == parent_session_id
        ]
    if not records:
        return _Route("dispatch", 1, "cursor_workhorse", "cursor-cycle-1")
    last = records[-1]
    parsed = _stage(last.get("title"))
    if parsed is None:
        return _Route("infrastructure_failed", 0, reason="unrecognized ledger title")
    if parsed.cycle > _MAX_CYCLES:
        return _Route(
            "validation_failed",
            parsed.cycle,
            reason=f"cycle {parsed.cycle} exceeds configured maximum {_MAX_CYCLES}",
        )
    # A transient, platform-side Opus stream death (server error mid-response,
    # truncated stream) on any audit stage is recoverable with a fresh child
    # under a unique title. Intercept it before the generic failure guard and
    # before any audit parsing, so an incomplete packet is never read as
    # evidence and a genuine failure (native timeout, empty output, pin break)
    # still falls through to terminal infrastructure failure below.
    if parsed.kind in _OPUS_AUDIT_KINDS and _is_transient_opus_stream_error(last):
        return _opus_transient_retry_route(
            records,
            parsed.cycle,
            reason="Opus native audit stream ended mid-response",
        )
    if last.get("status") != "completed" or not str(last.get("output", "")).strip():
        return _Route(
            "infrastructure_failed",
            parsed.cycle,
            reason="failed, cancelled, or empty worker result",
        )
    cycle = parsed.cycle
    if parsed.kind == "cursor_grunt":
        return _Route("dispatch", cycle, "opus_auditor", f"audit-cycle-{cycle}")
    if parsed.kind == "cursor_web":
        if parsed.requester == "opus":
            # Every re-grade is a new native Claude process. Reusing
            # (opus_auditor, audit-cycle-N) asks Omnigent to inject into the
            # prior TUI after its completed turn and races terminal teardown.
            return _Route(
                "dispatch",
                cycle,
                "opus_auditor",
                f"audit-cycle-{cycle}-web-{parsed.hop}",
                requester="opus",
                hop=parsed.hop,
            )
        title = f"judge-cycle-{cycle}"
        return _Route(
            "dispatch",
            cycle,
            "codex_judge",
            title,
            requester="codex",
            hop=parsed.hop,
            resume_child_session_id=_resume_child(
                records,
                "codex_judge",
                title,
                parent_session_id=parent_session_id,
            ),
        )
    if parsed.kind in _OPUS_AUDIT_KINDS:
        audit_observation = (
            last.get("internal_mcp_observation")
            if isinstance(last.get("internal_mcp_observation"), dict)
            else None
        )
        if parsed.kind in {"audit_repair", "audit_repair_web"}:
            audit_observation = next(
                (
                    record.get("internal_mcp_observation")
                    for record in reversed(records[:-1])
                    if (
                        (prior := _stage(record.get("title"))) is not None
                        and prior.cycle == cycle
                        and prior.kind
                        in {"audit", "audit_web", "audit_internal"}
                        and isinstance(
                            record.get("internal_mcp_observation"),
                            dict,
                        )
                    )
                ),
                None,
            )
        payload, validation = _audit_payload_and_validation(
            last.get("output"),
            audit_observation,
        )
        if (
            payload is not None
            and validation is not None
            and validation.get("status") == "invalid_tool_claims"
        ):
            names = ", ".join(
                sorted(
                    {
                        str(item.get("tool") or "")
                        for item in validation.get("invalid_tool_claims", [])
                        if isinstance(item, dict) and item.get("tool")
                    }
                )
            )
            return _internal_reaudit_route(
                records,
                cycle,
                reason=(
                    "Opus audit ledger named tools not observed for its exact "
                    f"child: {names or 'unnamed invalid claims'}"
                ),
            )
        if payload is None:
            repair_suffix = (
                f"-web-{parsed.hop}"
                if parsed.kind in {"audit_web", "audit_repair_web"}
                else ""
            )
            repair_title = f"audit-format-repair-{cycle}{repair_suffix}"
            if parsed.kind in {"audit_repair", "audit_repair_web"} or any(
                record.get("title") == repair_title
                for record in records
            ):
                return _Route(
                    "infrastructure_failed",
                    cycle,
                    reason="Opus format repair remained malformed",
                )
            return _Route(
                "dispatch",
                cycle,
                "opus_auditor",
                repair_title,
            )
        return _verdict_route(
            records, requester="opus", cycle=cycle, payload=payload
        )
    if parsed.kind == "judge_convergence":
        payload = _valid_judgment(last.get("output"))
        if payload is None:
            return _Route(
                "infrastructure_failed",
                cycle,
                reason="Codex convergence judgment was incomplete or malformed",
            )
        if payload is not None and payload.get("verdict") == "STAMP":
            return _Route("success", cycle)
        limitations = (
            payload.get("limitations")
            if isinstance(payload, dict)
            and isinstance(payload.get("limitations"), list)
            else []
        )
        return _Route(
            "best_effort",
            cycle,
            reason=(
                "bounded convergence adjudication did not STAMP"
                + (
                    f"; limitations: {'; '.join(str(item) for item in limitations)}"
                    if limitations
                    else ""
                )
            ),
        )
    if parsed.kind in {"judge", "judge_repair"}:
        raw_judgment = next(
            iter(_mapping_candidates(str(last.get("output") or ""))),
            None,
        )
        voice_name = os.path.basename(_voice_profile_path())
        if (
            voice_name
            and isinstance(raw_judgment, dict)
            and raw_judgment.get("verdict") == "REWORK"
            and (
                _contains_text(
                    raw_judgment.get("punch_list_for_cursor"),
                    voice_name,
                )
                or _contains_text(
                    raw_judgment.get("punch_list_for_cursor"),
                    "voice profile",
                )
            )
        ):
            return _Route(
                "infrastructure_failed",
                cycle,
                reason=(
                    "Codex could not access the configured voice profile; "
                    "this is a launch capability failure, not Cursor rework"
                ),
            )
        payload = _valid_judgment(last.get("output"))
        if payload is None:
            if parsed.kind == "judge_repair" or any(
                record.get("title") == f"judge-format-repair-{cycle}"
                for record in records
            ):
                return _Route(
                    "infrastructure_failed",
                    cycle,
                    reason="Codex format repair remained malformed",
                )
            return _Route(
                "dispatch",
                cycle,
                "codex_judge",
                f"judge-format-repair-{cycle}",
            )
        return _verdict_route(
            records, requester="codex", cycle=cycle, payload=payload
        )
    return _Route("infrastructure_failed", cycle, reason="invalid route state")


def _route_dispatch_pending(
    route: _Route,
    records: list[dict[str, Any]] | None = None,
    dispatches: list[dict[str, Any]] | None = None,
    *,
    parent_session_id: str = "",
) -> bool:
    """Return whether the canonical route has an uncollected dispatch in flight."""

    if route.status != "dispatch" or not route.agent or not route.title:
        return False
    collected = (
        records
        if records is not None
        else read_collections(parent_session_id=parent_session_id)
    )
    sent = (
        dispatches
        if dispatches is not None
        else read_dispatches(parent_session_id=parent_session_id)
    )

    def matches(record: dict[str, Any]) -> bool:
        return (
            record.get("agent") == route.agent
            and record.get("title") == route.title
            and (
                not parent_session_id
                or record.get("parent_session_id") == parent_session_id
            )
        )

    return sum(matches(record) for record in sent) > sum(
        matches(record) for record in collected
    )


def _observed_nonmax_opus_effort(
    records: list[dict[str, Any]],
    cycle: int,
) -> bool:
    """Return whether this cycle objectively observed degraded Opus effort."""

    for record in records:
        if record.get("agent") != "opus_auditor":
            continue
        parsed = _stage(record.get("title"))
        if parsed is None or parsed.cycle != cycle:
            continue
        observation = record.get("opus_effort_observation")
        if (
            isinstance(observation, dict)
            and observation.get("status") == "observed"
            and observation.get("compliant") is False
        ):
            return True
    return False


def _has_required_stage_chain(records: list[dict[str, Any]], cycle: int) -> bool:
    """Confirm the final cycle collected Cursor and Opus before Codex.

    This is a final-attestation prerequisite, not routing authorization. Tool
    calls remain unrestricted by message content or project-owned stage gates.
    """

    if _observed_nonmax_opus_effort(records, cycle):
        return False
    cursor_seen = False
    for record in records:
        if record.get("status") != "completed":
            continue
        title = str(record.get("title") or "")
        if title == f"cursor-cycle-{cycle}":
            cursor_seen = True
        elif cursor_seen and title in {
            f"audit-cycle-{cycle}",
            f"audit-format-repair-{cycle}",
        } or (
            cursor_seen
            and re.fullmatch(
                rf"audit-(?:cycle|format-repair)-{cycle}-web-[1-2]"
                rf"|audit-retry-{cycle}-[1-2]",
                title,
            )
            is not None
        ):
            return True
    return False


def _latest_codex_stamp(
    parent_session_id: str = "",
) -> tuple[str, int] | None:
    records = (
        read_collections(parent_session_id=parent_session_id)
        if parent_session_id
        else _completion_records()
    )
    for record in reversed(records):
        if record["agent"] != "codex_judge" or record["status"] != "completed":
            continue
        match = re.fullmatch(
            r"judge-(?:cycle|convergence)-([1-4])",
            record["title"],
        )
        if match is None:
            continue
        for payload in _mapping_candidates(record["output"]):
            answer = _valid_stamp(payload)
            if answer is not None:
                return answer, int(match.group(1))
    return None


def _contains_text(value: object, needle: str) -> bool:
    if isinstance(value, str):
        return needle in value
    if isinstance(value, dict):
        return any(_contains_text(child, needle) for child in value.values())
    if isinstance(value, list):
        return any(_contains_text(child, needle) for child in value)
    return False


def _valid_stamp(payload: dict[str, Any] | None) -> str | None:
    if payload is None or payload.get("verdict") != "STAMP":
        return None
    if payload.get("needs_web") is not False:
        return None
    if payload.get("needs_internal") is not False:
        return None
    if not _valid_materiality(payload, "STAMP"):
        return None
    answer = payload.get("shippable_answer")
    citations = payload.get("citations_that_hold")
    check = payload.get("voice_profile_check")
    expected_digest = os.environ.get("TRIPLE_STAMP_VOICE_PROFILE_SHA256", "")
    expected_path = _voice_profile_path()
    if not isinstance(answer, str) or not answer.strip() or "\u2014" in answer:
        return None
    if not isinstance(citations, list) or not citations:
        return None
    # Voice rendering is optional. When a profile is configured the stamp must
    # prove Codex read that exact file; when it is not, there is nothing to
    # prove and requiring a digest would reject every otherwise valid stamp.
    if expected_path or expected_digest:
        if (
            not expected_digest
            or not _contains_text(check, expected_path)
            or not _contains_text(check, expected_digest)
        ):
            return None
    return answer


def _safe_customer_answer(value: object) -> str:
    if not isinstance(value, str):
        return ""
    answer = value.strip()
    if not answer or answer.startswith(
        (
            "PIPELINE_VALIDATION_FAILED:",
            "PIPELINE_INFRASTRUCTURE_ERROR:",
            "CURSOR_FINALIZATION_REQUIRED:",
            "CURSOR_WORKER_STALLED:",
            "CURSOR_WORKER_FAILED:",
            "CURSOR_WORKER_TIMEOUT:",
            "TRIPLE_STAMP_NONTERMINAL_CONTINUATION",
        )
    ):
        return ""
    return answer


def _cursor_answer_draft(output: object) -> str:
    """Extract only Cursor's explicitly completed customer-answer draft."""

    if not isinstance(output, str):
        return ""
    for payload in _mapping_candidates(output):
        answer = _safe_customer_answer(
            payload.get("suggested_customer_answer_draft")
        )
        if answer:
            return answer
    marker = re.search(
        r"(?im)^\s*(?:[-*]\s*)?suggested_customer_answer_draft\s*:\s*",
        output,
    )
    if marker is None:
        return ""
    remainder = output[marker.end() :]
    boundary = re.search(
        (
            r"(?im)^\s*(?:[-*]\s*)?"
            r"(?:question_restated|purpose_run|findings|web_sources|unverified|"
            r"weakest_part)\s*:"
        ),
        remainder,
    )
    draft = remainder[: boundary.start()] if boundary is not None else remainder
    return _safe_customer_answer(textwrap.dedent(draft))


def _judgment_gaps(payload: dict[str, Any]) -> list[str]:
    """Render concrete unresolved evidence gaps without infrastructure prose."""

    gaps: list[str] = []
    limitations = payload.get("limitations")
    if isinstance(limitations, list):
        gaps.extend(
            str(item).strip()
            for item in limitations
            if isinstance(item, str) and item.strip()
        )
    field = {
        "REWORK": "punch_list_for_cursor",
        "NEEDS_WEB": "web_queries",
        "NEEDS_INTERNAL": "internal_queries",
    }.get(str(payload.get("verdict")), "")
    items = payload.get(field) if field else []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, str) and item.strip():
                gaps.append(item.strip())
                continue
            if not isinstance(item, dict):
                continue
            claim = str(item.get("claim") or "").strip()
            proof = str(
                item.get("requested_proof")
                or item.get("prove_condition")
                or item.get("where_to_look")
                or ""
            ).strip()
            if claim and proof:
                gaps.append(f"{claim} Required evidence: {proof}")
            elif claim or proof:
                gaps.append(claim or proof)
    if not gaps:
        why = str(payload.get("why") or "").strip()
        if why:
            gaps.append(why)
    deduplicated: list[str] = []
    seen: set[str] = set()
    for gap in gaps:
        normalized = _normalize_gap_text(gap)
        if normalized and normalized not in seen:
            seen.add(normalized)
            deduplicated.append(gap)
    return deduplicated


def _best_effort_answer(
    records: list[dict[str, Any]],
    route: _Route,
) -> str | None:
    """Build a finished conclusion-plus-gaps answer from complete packets only."""

    if route.status != "best_effort" or not _has_required_stage_chain(
        records, route.cycle
    ):
        return None
    complete_cursor = next(
        (
            record
            for record in reversed(records)
            if record.get("agent") == "cursor_workhorse"
            and record.get("status") == "completed"
            and record.get("title") == f"cursor-cycle-{route.cycle}"
            and _safe_customer_answer(record.get("output"))
        ),
        None,
    )
    if complete_cursor is None:
        return None
    review: dict[str, Any] | None = None
    for record in reversed(records):
        parsed = _stage(record.get("title"))
        if (
            record.get("agent") != "codex_judge"
            or record.get("status") != "completed"
            or parsed is None
            or parsed.cycle != route.cycle
            or parsed.kind not in {"judge", "judge_repair", "judge_convergence"}
        ):
            continue
        review = _valid_judgment(record.get("output"))
        if review is not None and review.get("verdict") != "STAMP":
            break
        review = None
    if review is None:
        for record in reversed(records):
            parsed = _stage(record.get("title"))
            if (
                record.get("agent") != "opus_auditor"
                or record.get("status") != "completed"
                or parsed is None
                or parsed.cycle != route.cycle
                or parsed.kind not in {"audit", "audit_web", "audit_repair_web"}
            ):
                continue
            review = _valid_audit(record.get("output"))
            if review is not None and review.get("verdict") == "NEEDS_WEB":
                break
            review = None
    if review is None:
        return None

    conclusion = _safe_customer_answer(review.get("best_supported_answer"))
    if not conclusion:
        conclusion = _cursor_answer_draft(complete_cursor.get("output"))
    if not conclusion:
        conclusion = _safe_customer_answer(complete_cursor.get("output"))
    if not conclusion:
        why = _safe_customer_answer(review.get("why"))
        if not why:
            return None
        conclusion = (
            "Based on the completed review, the strongest supported conclusion "
            f"is: {why}"
        )

    gaps = _judgment_gaps(review)
    gap_lines = "\n".join(f"- {gap}" for gap in gaps)
    if not gap_lines:
        gap_lines = "- Final quality approval was not granted."
    return (
        f"{conclusion}\n\n"
        "Remaining evidence gaps:\n"
        f"{gap_lines}\n\n"
        "These gaps are explicit because the completed evidence did not support "
        "final quality approval."
    )


def _has_terminal_worker_failure(parent_session_id: str = "") -> bool:
    pin_markers = (
        "pin broke",
        "model drift",
        "wrong model",
        "wrong harness",
        "produced no output",
    )
    records = (
        read_collections(parent_session_id=parent_session_id)
        if parent_session_id
        else _completion_records()
    )
    for record in records:
        output = str(record.get("output") or "")
        if record.get("status") in {"failed", "cancelled"} or not output.strip():
            # A transient Opus stream death is only terminal once its bounded
            # retry budget for the cycle is spent. While a fresh retry child is
            # still available, this is recoverable, not a terminal failure, and
            # the user must not see PIPELINE_INFRASTRUCTURE_ERROR.
            parsed = _stage(record.get("title"))
            if (
                parsed is not None
                and parsed.kind in _OPUS_AUDIT_KINDS
                and _is_transient_opus_stream_error(record)
                and _opus_transient_retry_available(records, parsed.cycle)
            ):
                continue
            return True
        lowered = output.lower()
        if any(marker in lowered for marker in pin_markers):
            return True
    for record in read_collections(parent_session_id=parent_session_id):
        title = str(record.get("title", ""))
        if title.startswith("audit-format-repair-") and _valid_audit(
            record.get("output")
        ) is None:
            return True
    return False


def _max_unstamped_judgments(parent_session_id: str = "") -> bool:
    cycles: set[int] = set()
    records = (
        read_collections(parent_session_id=parent_session_id)
        if parent_session_id
        else _completion_records()
    )
    for record in records:
        if record["agent"] != "codex_judge" or record["status"] != "completed":
            continue
        title = str(record.get("title") or "")
        match = re.fullmatch(r"judge-cycle-([1-4])", title)
        if match is None:
            continue
        payload = _valid_judgment(record["output"])
        if payload is not None and payload.get("verdict") in {
            "REWORK",
            "NEEDS_WEB",
            "NEEDS_INTERNAL",
        }:
            cycles.add(int(match.group(1)))
    return len(cycles) >= _MAX_CYCLES


def _failure_metrics(
    parent_session_id: str = "",
) -> tuple[float | None, int | None]:
    raw = os.environ.get("TRIPLE_STAMP_RUN_DIR", "")
    if not raw:
        return None, None
    run_dir = Path(raw)
    try:
        budget = read_budget_state(parent_session_id)
        cost = float(budget["cost_usd"])
    except (KeyError, TypeError, ValueError):
        cost = None
    try:
        tool_calls = [
            json.loads(line)
            for line in (
                (run_dir / "supervisor-tool-calls.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            )
            if line.strip()
        ]
        calls = sum(
            1
            for record in tool_calls
            if isinstance(record, dict)
            and (
                not parent_session_id
                or record.get("parent_session_id") == parent_session_id
            )
        )
    except (OSError, ValueError, json.JSONDecodeError):
        calls = 0
    if calls == 0:
        calls = None
    return cost, calls


def _mark_terminal(
    kind: str,
    answer: str | None = None,
    *,
    reason: str = "",
    parent_session_id: str = "",
) -> None:
    raw = os.environ.get("TRIPLE_STAMP_RUN_DIR", "")
    if not raw:
        return
    if kind != "STAMP":
        route = _next_route(
            read_collections(parent_session_id=parent_session_id),
            parent_session_id=parent_session_id,
        )
        cost, calls = _failure_metrics(parent_session_id)
        record_terminal_failure(
            kind,
            reason or route.reason or "pipeline reached a terminal failure",
            stage=route.title,
            cycle=route.cycle,
            cost_usd=cost,
            calls=calls,
            parent_session_id=parent_session_id,
        )
    try:
        from triple_stamp_runtime_state import _parent_artifact_path

        run_dir = Path(raw)
        marker = _parent_artifact_path(
            run_dir,
            "pipeline-terminal",
            parent_session_id,
        )
        marker.write_text(kind + "\n", encoding="utf-8")
        marker.chmod(0o600)
        if answer is not None:
            data = answer.encode("utf-8")
            relay = _parent_artifact_path(
                run_dir,
                "stamped-answer.bin",
                parent_session_id,
            )
            relay.write_bytes(data)
            relay.chmod(0o600)
            digest = _parent_artifact_path(
                run_dir,
                "stamped-answer.sha256",
                parent_session_id,
            )
            digest.write_text(hashlib.sha256(data).hexdigest() + "\n", encoding="ascii")
            digest.chmod(0o600)
    except OSError:
        return


def supervisor_contract(
    enabled: bool = True,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Observe routing calls and enforce only terminal response attestation.

    Worker sends and inbox reads are authorized by their mechanically minimal
    tool surface, not by this policy. Native Omnigent validation and errors pass
    through unchanged.
    """

    def evaluate(event: dict[str, Any]) -> dict[str, Any]:
        event_type = event.get("type")
        event_context = event.get("context")
        parent_session_id = (
            str(
                event_context.get("root_conversation_id")
                or event_context.get("conversation_id")
                or ""
            )
            if isinstance(event_context, dict)
            else ""
        )
        if event_type == "tool_call":
            data = event.get("data")
            raw_name = data.get("name") if isinstance(data, dict) else ""
            try:
                append_supervisor_tool_call(
                    str(raw_name or "unknown"),
                    parent_session_id=parent_session_id,
                )
            except OSError:
                # Observability must never become routing authorization.
                pass
            return {"result": "ALLOW"}
        if event_type != "response":
            return {"result": "ALLOW"}
        if not enabled:
            return {"result": "DENY", "reason": "final response contract is disabled"}

        response = event.get("data")
        if not isinstance(response, str):
            return {"result": "DENY", "reason": "supervisor response is not text"}
        if (
            not parent_session_id
            and any(
                record.get("parent_session_id")
                for record in read_collections()
            )
        ):
            if response == "":
                return {"result": "ALLOW"}
            return {
                "result": "DENY",
                "reason": (
                    "Supervisor response lacks one authoritative root "
                    "conversation id; cross-parent ledger reduction is forbidden."
                ),
            }
        collections = read_collections(parent_session_id=parent_session_id)
        latest_stamp = _latest_codex_stamp(parent_session_id)
        if latest_stamp is not None:
            stamped, cycle = latest_stamp
        else:
            stamped, cycle = None, 0
        if (
            stamped is not None
            and _has_required_stage_chain(collections, cycle)
        ):
            if response == stamped:
                _mark_terminal(
                    "STAMP",
                    stamped,
                    parent_session_id=parent_session_id,
                )
                return {"result": "ALLOW"}
            return {
                "result": "DENY",
                "reason": (
                    "A valid Codex STAMP already exists and its byte-exact "
                    "shippable_answer must be preserved."
                ),
            }
        route = _next_route(
            collections,
            parent_session_id=parent_session_id,
        )
        best_effort = _best_effort_answer(collections, route)
        if best_effort is not None and response == best_effort:
            persisted = record_best_effort_answer(
                best_effort,
                collections,
                cycle=route.cycle,
                reason=route.reason,
                parent_session_id=parent_session_id,
            )
            if persisted == best_effort:
                return {"result": "ALLOW"}
        if (
            response == ""
            and _route_dispatch_pending(
                route,
                collections,
                read_dispatches(parent_session_id=parent_session_id),
                parent_session_id=parent_session_id,
            )
        ):
            # The runtime continuation guard suppressed an ordinary status
            # reply after the required child was durably dispatched. Empty
            # completion keeps Omnigent in its native async waiting state.
            return {"result": "ALLOW"}
        if response.startswith(_INFRA_PREFIX) and (
            _has_terminal_worker_failure(parent_session_id)
            or route.status == "infrastructure_failed"
        ):
            _mark_terminal(
                "PIPELINE_INFRASTRUCTURE_ERROR",
                reason=route.reason or "worker or native infrastructure failed",
                parent_session_id=parent_session_id,
            )
            return {"result": "ALLOW"}
        if (
            route.status != "best_effort"
            and response.startswith(_VALIDATION_PREFIX)
            and (
                _max_unstamped_judgments(parent_session_id)
                or route.status == "validation_failed"
            )
        ):
            _mark_terminal(
                "PIPELINE_VALIDATION_FAILED",
                reason=route.reason
                or (
                    f"{_MAX_CYCLES} complete cycles ended without "
                    "Codex STAMP"
                ),
                parent_session_id=parent_session_id,
            )
            return {"result": "ALLOW"}
        return {
            "result": "DENY",
            "reason": (
                "Supervisor output is forbidden until a persisted Codex STAMP "
                "proves the byte-exact shippable_answer."
            ),
        }

    return evaluate


def supervisor_route_call_limit(
    limit: int = _SUPERVISOR_ROUTE_LIMIT,
    counted_tools: Iterable[str] | None = None,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Limit verified root-supervisor routing calls, never child work.

    Omnigent 0.12 function-policy events do not natively expose conversation
    identity. The project runtime bridge adds the current and root conversation
    IDs from ``PolicyEngine`` to the structured event context. This policy
    increments only when those IDs are both present and equal. Missing or
    malformed identity/state abstains without inventing a count; a DENY is
    possible only from a verified persisted root-supervisor count.
    """

    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    selected = frozenset(
        _normalize_tool_name(name)
        for name in (
            counted_tools
            if counted_tools is not None
            else _SUPERVISOR_ROUTE_TOOLS
        )
        if isinstance(name, str) and name
    )
    if not selected:
        raise ValueError("counted_tools must contain at least one tool name")

    def evaluate(event: dict[str, Any]) -> dict[str, Any]:
        if event.get("type") != "tool_call":
            return {"result": "ALLOW"}
        data = event.get("data")
        raw_name = data.get("name") if isinstance(data, dict) else event.get("target")
        tool_name = _normalize_tool_name(raw_name)
        if tool_name not in selected:
            return {"result": "ALLOW"}

        context = event.get("context")
        if not isinstance(context, dict):
            return {"result": "ALLOW"}
        conversation_id = context.get("conversation_id")
        root_conversation_id = context.get("root_conversation_id")
        if (
            not isinstance(conversation_id, str)
            or not conversation_id
            or not isinstance(root_conversation_id, str)
            or not root_conversation_id
            or conversation_id != root_conversation_id
        ):
            return {"result": "ALLOW"}

        state = event.get("session_state")
        if not isinstance(state, dict):
            return {"result": "ALLOW"}
        raw_count = state.get(_SUPERVISOR_ROUTE_COUNT_STATE_KEY, 0)
        if (
            isinstance(raw_count, bool)
            or not isinstance(raw_count, int)
            or raw_count < 0
        ):
            return {"result": "ALLOW"}
        if raw_count >= limit:
            return {
                "result": "DENY",
                "reason": (
                    f"Verified supervisor route-call limit {limit} reached; "
                    f"{tool_name} denied"
                ),
            }
        return {
            "result": "ALLOW",
            "state_updates": [
                {
                    "key": _SUPERVISOR_ROUTE_COUNT_STATE_KEY,
                    "action": "increment",
                    "value": 1,
                }
            ],
        }

    return evaluate


def _number(value: object) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _model_rates(model: str) -> tuple[float, float, float, float]:
    lowered = model.lower()
    if "cursor" in lowered or "gpt-5.6-sol-xhigh" in lowered:
        return _COST_CEILINGS["cursor"]
    if "opus" in lowered:
        return _COST_CEILINGS["opus"]
    if "sonnet" in lowered:
        return _COST_CEILINGS["sonnet"]
    if "gpt-5.6-sol" in lowered:
        return _COST_CEILINGS["gpt-5.6-sol"]
    return _COST_CEILINGS["unknown"]


def _estimate_unpriced_bucket(model: str, usage: dict[str, Any]) -> float:
    """Price only a bucket for which the provider supplied no USD value."""

    input_rate, output_rate, cache_read_rate, cache_write_rate = _model_rates(model)
    return (
        _number(usage.get("input_tokens")) * input_rate
        + _number(usage.get("output_tokens")) * output_rate
        + _number(usage.get("cache_read_input_tokens")) * cache_read_rate
        + _number(usage.get("cache_creation_input_tokens")) * cache_write_rate
    ) / 1_000_000.0


def _conversation_model(conversation: object) -> str:
    agent_model = {
        "cursor_workhorse": "cursor-grok-4.6-xhigh",
        "opus_auditor": _OPUS_MODEL,
        "codex_judge": _CODEX_MODEL,
    }.get(getattr(conversation, "sub_agent_name", None))
    return str(
        getattr(conversation, "reported_model", None)
        or getattr(conversation, "model_override", None)
        or agent_model
        or _SUPERVISOR_MODEL
    )


def _cost_snapshot_from_conversations(
    conversations: Iterable[object],
) -> dict[str, Any]:
    """Build one whole-tree total from each unique session's own usage row."""

    reported_total = 0.0
    estimated_total = 0.0
    sessions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for conversation in conversations:
        session_id = str(getattr(conversation, "id", "") or "")
        if session_id in seen:
            continue
        seen.add(session_id)
        usage = getattr(conversation, "session_usage", None)
        if not isinstance(usage, dict) or not usage:
            continue
        by_model = usage.get("by_model")
        reported = (
            _number(usage["total_cost_usd"])
            if "total_cost_usd" in usage
            else 0.0
        )
        estimated = 0.0
        model_rows: list[dict[str, Any]] = []
        if isinstance(by_model, dict) and by_model:
            bucket_reported = 0.0
            for raw_model, raw_bucket in by_model.items():
                if not isinstance(raw_bucket, dict):
                    continue
                model = str(raw_model)
                priced = "total_cost_usd" in raw_bucket
                bucket_cost = (
                    _number(raw_bucket.get("total_cost_usd"))
                    if priced
                    else _estimate_unpriced_bucket(model, raw_bucket)
                )
                if priced:
                    bucket_reported += bucket_cost
                else:
                    estimated += bucket_cost
                model_rows.append(
                    {
                        "model": model,
                        "reported_usd": bucket_cost if priced else 0.0,
                        "estimated_unpriced_usd": 0.0 if priced else bucket_cost,
                        "input_tokens": int(_number(raw_bucket.get("input_tokens"))),
                        "output_tokens": int(_number(raw_bucket.get("output_tokens"))),
                        "cache_read_input_tokens": int(
                            _number(raw_bucket.get("cache_read_input_tokens"))
                        ),
                        "cache_creation_input_tokens": int(
                            _number(raw_bucket.get("cache_creation_input_tokens"))
                        ),
                    }
                )
            if "total_cost_usd" not in usage:
                reported = bucket_reported
        else:
            model = _conversation_model(conversation)
            priced = "total_cost_usd" in usage
            if not priced:
                estimated = _estimate_unpriced_bucket(model, usage)
            model_rows.append(
                {
                    "model": model,
                    "reported_usd": reported,
                    "estimated_unpriced_usd": estimated,
                    "input_tokens": int(_number(usage.get("input_tokens"))),
                    "output_tokens": int(_number(usage.get("output_tokens"))),
                    "cache_read_input_tokens": int(
                        _number(usage.get("cache_read_input_tokens"))
                    ),
                    "cache_creation_input_tokens": int(
                        _number(usage.get("cache_creation_input_tokens"))
                    ),
                }
            )
        reported_total += reported
        estimated_total += estimated
        sessions.append(
            {
                "session_id": session_id,
                "agent": getattr(conversation, "sub_agent_name", None),
                "reported_usd": reported,
                "estimated_unpriced_usd": estimated,
                "status_line_usd": _number(usage.get("total_cost_usd")),
                "policy_cost_usd": (
                    _number(usage.get("policy_cost_usd"))
                    if "policy_cost_usd" in usage
                    else None
                ),
                "models": model_rows,
            }
        )
    return {
        "reported_usd": reported_total,
        "estimated_unpriced_usd": estimated_total,
        "total_usd": reported_total + estimated_total,
        "sessions": sessions,
        "source_error": "",
    }


def _stored_cost_snapshot(
    event: dict[str, Any],
    parent_session_id: str = "",
) -> dict[str, Any]:
    """Read the canonical per-session rows; event usage is test-only fallback."""

    source_error = ""
    try:
        from omnigent.debug_logging import runner_primary_session_id
        from omnigent.runtime import get_conversation_store

        root_id = parent_session_id or runner_primary_session_id()
        if root_id:
            store = get_conversation_store()
            cursor: str | None = None
            conversations: list[object] = []
            while True:
                page = store.list_conversations(
                    limit=100,
                    after=cursor,
                    kind=None,
                    root_conversation_id=root_id,
                    order="asc",
                )
                conversations.extend(page.data)
                if not page.has_more or not page.last_id:
                    break
                cursor = page.last_id
            snapshot = _cost_snapshot_from_conversations(conversations)
            snapshot.update(available=True, source="conversation_store")
            return snapshot
    except Exception as exc:  # noqa: BLE001 - runner/server process boundary
        # Runner-side policy evaluation has no initialized ConversationStore.
        # It must abstain; the server-side evaluation owns authoritative usage.
        source_error = type(exc).__name__
    context = event.get("context")
    usage = context.get("usage") if isinstance(context, dict) else None
    if not isinstance(usage, dict):
        snapshot = _cost_snapshot_from_conversations(())
        snapshot.update(
            available=False,
            source="unavailable",
            source_error=source_error or "usage_unavailable",
        )
        return snapshot
    fallback = type(
        "_StaticUsage",
        (),
        {
            "id": "static-policy-check",
            "session_usage": usage,
            "sub_agent_name": None,
            "model_override": context.get("model") if isinstance(context, dict) else None,
        },
    )()
    snapshot = _cost_snapshot_from_conversations((fallback,))
    snapshot.update(
        available=True,
        source="event_usage",
        source_error=source_error,
    )
    return snapshot


def _project_dispatch_cost(event: dict[str, Any]) -> tuple[str, float]:
    if event.get("type") != "tool_call":
        return "", 0.0
    data = event.get("data")
    if not isinstance(data, dict) or _normalize_tool_name(data.get("name")) != "sys_session_send":
        return "", 0.0
    arguments = data.get("arguments")
    if not isinstance(arguments, dict):
        return "", 0.0
    agent = str(arguments.get("agent") or "")
    title = str(arguments.get("title") or "")
    projection = _STAGE_PROJECTIONS.get(agent)
    if projection is None:
        return title, 0.0
    payload = arguments.get("args")
    if isinstance(payload, dict):
        handoff = payload.get("input")
    else:
        handoff = payload
    handoff_bytes = len(handoff.encode("utf-8")) if isinstance(handoff, str) else 0
    model, base_input_tokens, output_tokens = projection
    # Three bytes/token deliberately overstates normal English and JSON.
    input_tokens = base_input_tokens + (handoff_bytes + 2) // 3
    reserve = _estimate_unpriced_bucket(
        model,
        {"input_tokens": input_tokens, "output_tokens": output_tokens},
    )
    return title, reserve


def strict_cost_budget(
    max_cost_usd: float = 50.0,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Gate one canonical session-row total plus estimates for unpriced tokens."""

    if max_cost_usd <= 0:
        raise ValueError("max_cost_usd must be positive")

    def evaluate(event: dict[str, Any]) -> dict[str, Any]:
        if event.get("type") not in {"request", "tool_call", "response"}:
            return {"result": "ALLOW"}
        context = event.get("context")
        parent_session_id = (
            str(
                context.get("root_conversation_id")
                or context.get("conversation_id")
                or ""
            )
            if isinstance(context, dict)
            else ""
        )
        if (
            not parent_session_id
            and any(
                record.get("parent_session_id")
                for record in read_collections()
            )
        ):
            return {"result": "ALLOW"}
        snapshot = _stored_cost_snapshot(event, parent_session_id)
        if snapshot.get("available") is False:
            # The runner cannot observe canonical usage in-process. Allow this
            # evaluation to fall through to the server-side policy evaluation.
            return {"result": "ALLOW"}
        cost = float(snapshot["total_usd"])
        reported = float(snapshot["reported_usd"])
        estimated = float(snapshot["estimated_unpriced_usd"])
        route = _next_route(
            read_collections(parent_session_id=parent_session_id),
            parent_session_id=parent_session_id,
        )
        projected_stage, reserve = _project_dispatch_cost(event)
        stage = projected_stage or route.title
        remaining = max(0.0, max_cost_usd - cost)
        cap_reached = cost >= max_cost_usd
        predispatch_denied = bool(projected_stage) and reserve > remaining
        denied = cap_reached or predispatch_denied
        denial_reason = ""
        if cap_reached:
            denial_reason = (
                f"budget denied at {stage} (cycle {route.cycle}): provider-reported "
                f"${reported:.2f} + conservative unpriced estimate ${estimated:.2f} "
                f"= ${cost:.2f}; remaining $0.00 of ${max_cost_usd:.2f}; "
                "continuation denied because the hard cap is reached"
            )
        elif predispatch_denied:
            denial_reason = (
                f"budget denied before {stage} (cycle {route.cycle}): provider-reported "
                f"${reported:.2f} + conservative unpriced estimate ${estimated:.2f} "
                f"= ${cost:.2f}; remaining ${remaining:.2f} of ${max_cost_usd:.2f}; "
                f"projected next-stage reserve ${reserve:.2f} cannot fit, so dispatch "
                "was denied before additional spend"
            )
        authoritative = snapshot.get("source", "conversation_store") == (
            "conversation_store"
        )
        if authoritative:
            write_budget_state(
                cost,
                max_cost_usd,
                denied,
                reported_usd=reported,
                estimated_unpriced_usd=estimated,
                remaining_usd=remaining,
                projected_stage=projected_stage,
                projected_reserve_usd=reserve,
                denial_reason=denial_reason,
                sessions=snapshot.get("sessions", []),
                parent_session_id=parent_session_id,
            )
        if denied:
            if authoritative:
                _cost_value, calls = _failure_metrics(parent_session_id)
                record_terminal_failure(
                    "PIPELINE_INFRASTRUCTURE_ERROR",
                    denial_reason,
                    stage=stage,
                    cycle=route.cycle,
                    cost_usd=cost,
                    calls=calls,
                    parent_session_id=parent_session_id,
                )
            return {"result": "DENY", "reason": denial_reason}
        return {"result": "ALLOW"}

    return evaluate
