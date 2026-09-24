"""Deterministic one-shot continuation for the triple-stamp supervisor."""

from __future__ import annotations

import dis
import json
import logging
import os
import re
import sys
import time
from collections.abc import AsyncIterator
from types import CodeType
from typing import Any

# Give the routing-only supervisor two deterministic reprompts. A recoverable
# empty turn is never terminalized if the model still declines the required
# dispatch; a later child wake can continue the same attempt.
_CONTINUATION_LIMIT = 2
_HEADLESS_PIPELINE_TIMEOUT_S: float | None = None
_OMNIGENT_HEADLESS_EXTRA_TURN_LIMIT = 30
# More than the pipeline can ever consume (two or four complete cycles plus
# bounded hunts/repairs), while still encodable by Python 3.14's
# ``LOAD_SMALL_INT`` instruction.
_HEADLESS_PIPELINE_EXTRA_TURN_LIMIT = 255
_CONTINUATION_PREFIX = "TRIPLE_STAMP_NONTERMINAL_CONTINUATION"
_FRAMEWORK_WAKE = re.compile(
    r"^\[System: sub-agent "
    r"(?:cursor_workhorse|opus_auditor|codex_judge)/[^\s\]]+ finished "
    r"\((?:completed|failed|cancelled)\) — "
    r"\d+ results? waiting in inbox\. Call sys_read_inbox to collect\.\]$"
)
_LOGGER = logging.getLogger(__name__)
# Every ledger action after which the guard has already streamed the recorded
# terminal failure to the reader. A later wake from a child that was still in
# flight must stay silent rather than print a second copy.
_TERMINAL_RELAY_ACTIONS = frozenset(
    {
        "deterministic_best_effort_relay",
        "judge_format_repair_best_effort_relay",
        "supervisor_failure_terminalized",
        "terminal_stamp_delivered",
        "terminal_failure_relayed",
        "continuation_exhausted",
    }
)

# What the reader is told when each stage starts. The supervisor is forbidden to
# emit status prose and every attempt is suppressed, which is why the UI showed
# raw `sys_session_send` rows and nothing else. The runtime holds the same facts
# and cannot drift from them, so it narrates instead of the model.
_NARRATED_STAGES: dict[tuple[str, str, int], set[str]] = {}
_STAGE_STEP = {
    "cursor_workhorse": ("1 of 3", "public-web research"),
    "opus_auditor": ("2 of 3", "internal audit"),
    "codex_judge": ("3 of 3", "final judgment"),
}
_STAGE_DOING = {
    "cursor_workhorse": (
        "Cursor, pinned to Grok 4.6 Extra High, is gathering public-web"
        " evidence and has to attach a URL, a quote, and a retrieval date to"
        " every claim. It has no access to internal systems."
    ),
    "opus_auditor": (
        "Opus is attacking that evidence and checking it against Glean, Jira,"
        " Slack, Confluence, and SAFE. This is normally the slowest step, and"
        " its verdict is an opinion for the judge rather than a decision."
    ),
    "codex_judge": (
        "Codex, on Sol Ultra, decides whether this ships as written or goes"
        " back for another cycle, and writes the final answer in Ryan's voice"
        " if it stamps. It does no research of its own."
    ),
}


def _safe_titles(titles: list[str]) -> list[str]:
    """Bound title diagnostics without exposing arbitrary tool arguments."""

    return [" ".join(str(title).split())[:160] for title in titles]


def _missing_dispatch_reason(
    route: object,
    *,
    sent_titles: list[str],
    diagnostic: dict[str, Any] | None,
    abstained: bool = False,
) -> str:
    """Explain a missing ledger row without misclassifying observer failures."""

    expected = str(getattr(route, "title", "") or getattr(route, "status", ""))
    actual = _safe_titles(sent_titles)
    if not actual and diagnostic is not None:
        actual = _safe_titles([str(diagnostic.get("title") or "")])
    prefix = (
        "no matching durable dispatch exists for "
        f"{expected}; actual title(s) sent in turn={actual!r}"
    )
    if diagnostic is None:
        detail = (
            "native send failure or non-launch result; "
            "ledger observer failure=none recorded"
        )
    elif diagnostic.get("phase") == "ledger_observer":
        detail = (
            f"native send status={diagnostic.get('native_send_status') or 'unknown'}; "
            "ledger observer failure="
            f"{diagnostic.get('reason') or 'unknown'}"
        )
    else:
        detail = (
            f"native send failure={diagnostic.get('reason') or 'unknown'}; "
            "ledger observer failure=none recorded"
        )
    suffix = (
        "; continuation guard abstained because native dispatch is authoritative"
        if abstained
        else ""
    )
    return f"{prefix}; {detail}{suffix}"


def _route_snapshot(route: object) -> dict[str, Any]:
    return {
        "status": str(getattr(route, "status", "") or ""),
        "cycle": int(getattr(route, "cycle", 0) or 0),
        "agent": str(getattr(route, "agent", "") or ""),
        "title": str(getattr(route, "title", "") or ""),
        "requester": str(getattr(route, "requester", "") or ""),
        "hop": int(getattr(route, "hop", 0) or 0),
        "resume_child_session_id": str(
            getattr(route, "resume_child_session_id", "") or ""
        ),
        "reason": str(getattr(route, "reason", "") or ""),
    }


def _continuation_prompt(
    route: object,
    *,
    original_request: str = "",
) -> str:
    """Build one bounded prompt from the durable state-machine route."""

    snapshot = _route_snapshot(route)
    status = snapshot["status"]
    request_context = (
        "\n\nORIGINAL REQUEST (verbatim):\n" + original_request
        if original_request
        else ""
    )
    if status == "dispatch":
        resume = (
            f" Resume child session {snapshot['resume_child_session_id']}."
            if snapshot["resume_child_session_id"]
            else ""
        )
        return (
            f"{_CONTINUATION_PREFIX}: The previous supervisor response ended "
            "without executing the durable next route. Continue now by calling "
            f"sys_session_send exactly once for agent {snapshot['agent']!r} with "
            f"title {snapshot['title']!r}.{resume} Use the already collected "
            "packets and the original request in conversation history. Do not "
            "emit status prose, poll, or answer the user."
            f"{request_context}"
        )
    if status == "success":
        return (
            f"{_CONTINUATION_PREFIX}: The durable ledger contains a valid Codex "
            "STAMP. Relay only its shippable_answer byte-for-byte now, with "
            "nothing before or after it."
        )
    if status == "best_effort":
        return (
            f"{_CONTINUATION_PREFIX}: The durable route reached bounded "
            "customer finalization. Do not emit pipeline vocabulary or dispatch "
            "more work. The runtime will relay the completed answer with its "
            "remaining evidence gaps."
        )
    if status == "validation_failed":
        return (
            f"{_CONTINUATION_PREFIX}: The durable route is terminal validation "
            f"failure in cycle {snapshot['cycle']}. Emit only "
            f"PIPELINE_VALIDATION_FAILED: {snapshot['reason'] or 'route bound exhausted'}"
        )
    return (
        f"{_CONTINUATION_PREFIX}: The durable route is terminal infrastructure "
        f"failure in cycle {snapshot['cycle']}. Emit only "
        f"PIPELINE_INFRASTRUCTURE_ERROR: "
        f"{snapshot['reason'] or 'invalid durable route state'}"
    )


def _session_id(messages: list[dict[str, Any]]) -> str:
    # Omnigent's executor adapter installs the authoritative parent session in
    # telemetry context before entering ``run_turn``. System-generated child
    # completion wakes do not always repeat ``session_id`` in their message,
    # so consulting only the messages collapses those wakes to ``default`` and
    # makes a healthy session look like it has no collected stages.
    try:
        from omnigent.runtime.telemetry import current_session_id

        active_session_id = current_session_id()
    except (ImportError, RuntimeError):
        active_session_id = None
    if active_session_id:
        return str(active_session_id)
    for message in reversed(messages):
        value = message.get("session_id")
        if value:
            return str(value)
        metadata = message.get("metadata")
        if isinstance(metadata, dict) and metadata.get("session_id"):
            return str(metadata["session_id"])
    return "default"


def _attempt_request_identity(messages: list[dict[str, Any]]) -> str:
    """Fingerprint the latest genuine top-level user turn."""

    from triple_stamp_runtime_state import attempt_request_identity

    original = _original_request(messages)
    if not original:
        return ""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        if _message_text(message.get("content")) != original:
            continue
        metadata = message.get("metadata")
        message_id = (
            metadata.get("message_id")
            if isinstance(metadata, dict)
            else None
        )
        return attempt_request_identity(
            original,
            message_id=str(message_id or ""),
        )
    return ""


def _message_text(content: object) -> str:
    """Return readable user text from supported conversation shapes."""

    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        nested = content.get("user_content")
        return nested if isinstance(nested, str) else ""
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or block.get("input_text") or "")
            for block in content
            if isinstance(block, dict)
            and isinstance(
                block.get("text") or block.get("input_text"),
                str,
            )
        )
    return ""


def _original_request(messages: list[dict[str, Any]]) -> str:
    """Return the latest genuine user request, excluding runtime wakes."""

    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = _message_text(message.get("content"))
        if (
            not content
            or content.startswith(
                (_CONTINUATION_PREFIX, "[System: sub-agent task ")
            )
            or _FRAMEWORK_WAKE.fullmatch(content.strip()) is not None
        ):
            continue
        return content
    return ""


def _tool_child_session_ids(result: object) -> set[str]:
    """Extract routed child ids from an internal ``sys_session_send`` result."""

    if isinstance(result, str):
        try:
            return _tool_child_session_ids(json.loads(result))
        except (json.JSONDecodeError, TypeError):
            return set(
                re.findall(r"\btask ([0-9a-f]{32})\b", result)
            )
    if isinstance(result, dict):
        found = {
            str(result[name])
            for name in ("task_id", "handle_id", "conversation_id")
            if result.get(name)
        }
        for value in result.values():
            found.update(_tool_child_session_ids(value))
        return found
    if isinstance(result, (list, tuple)):
        found: set[str] = set()
        for value in result:
            found.update(_tool_child_session_ids(value))
        return found
    return set()


def _tool_parent_session_ids(result: object) -> set[str]:
    """Extract parent ids from packets returned by ``sys_read_inbox``."""

    if isinstance(result, str):
        try:
            return _tool_parent_session_ids(json.loads(result))
        except (json.JSONDecodeError, TypeError):
            return set()
    if isinstance(result, dict):
        found = (
            {str(result["parent_session_id"])}
            if result.get("parent_session_id")
            else set()
        )
        for value in result.values():
            found.update(_tool_parent_session_ids(value))
        return found
    if isinstance(result, (list, tuple)):
        found: set[str] = set()
        for value in result:
            found.update(_tool_parent_session_ids(value))
        return found
    return set()


def _dispatch_parent_session_id(
    dispatches: list[dict[str, Any]],
    child_session_ids: set[str],
) -> str:
    """Resolve the parent from the exact child returned by this turn's tool."""

    parents = {
        str(record["parent_session_id"])
        for record in dispatches
        if str(record.get("child_session_id") or "") in child_session_ids
        and record.get("parent_session_id")
    }
    return next(iter(parents)) if len(parents) == 1 else ""


def _records_for_session(
    records: list[dict[str, Any]],
    session_id: str,
) -> list[dict[str, Any]]:
    """Scope durable packets after a concrete browser/terminal parent is known."""

    if not session_id or session_id == "default":
        return records
    if not any(record.get("parent_session_id") for record in records):
        return records
    return [
        record
        for record in records
        if record.get("parent_session_id") == session_id
    ]


def _attested_stamp_answer(
    route: object,
    records: list[dict[str, Any]],
) -> str | None:
    """Return the validated stamped answer for a success route, else ``None``.

    Applies exactly the gates :func:`_terminal_response_allowed` applies -- a
    valid stamp on the newest record plus a complete stage chain for the cycle --
    and returns the bytes instead of comparing them, so the runtime can relay
    the answer itself rather than requiring the supervisor to reproduce it.
    """

    from triple_stamp_isaac_launcher import (
        _has_required_stage_chain,
        _mapping_candidates,
        _valid_stamp,
    )

    if str(getattr(route, "status", "") or "") != "success" or not records:
        return None
    cycle = int(getattr(route, "cycle", 0) or 0)
    for payload in _mapping_candidates(str(records[-1].get("output") or "")):
        answer = _valid_stamp(payload)
        if answer is not None and _has_required_stage_chain(records, cycle):
            return answer
    return None


def _raw_stamp_fallback_answer(
    records: list[dict[str, Any]],
    *,
    dispatches: list[dict[str, Any]] | None = None,
    parent_session_id: str = "",
) -> tuple[str, int, dict[str, Any] | None]:
    """Recover a safe answer when routing after a raw STAMP cannot continue.

    This is deliberately a BEST_EFFORT candidate, not a STAMP: fields such as
    the configured voice-profile receipt may still be invalid. The completed
    Cursor-to-Opus chain and raw Codex verdict must nevertheless agree that the
    answer is shippable, so a later format-repair REWORK plus bounded supervisor
    refusal cannot leave the UI empty.
    """

    from triple_stamp_isaac_launcher import (
        _candidate_provenance,
        _has_required_stage_chain,
        _mapping_candidates,
        _safe_customer_answer,
        _valid_stamp,
    )

    profile = os.environ.get("TRIPLE_STAMP_VOICE_PROFILE", "").strip()
    digest = os.environ.get("TRIPLE_STAMP_VOICE_PROFILE_SHA256", "").strip()
    if not profile or not digest:
        return "", 0, None
    for index in range(len(records) - 1, -1, -1):
        record = records[index]
        title = str(record.get("title") or "")
        match = re.fullmatch(r"judge-cycle-([1-4])", title)
        if (
            match is None
            or record.get("agent") != "codex_judge"
            or record.get("status") != "completed"
        ):
            continue
        cycle = int(match.group(1))
        if not _has_required_stage_chain(records, cycle):
            continue
        for payload in _mapping_candidates(str(record.get("output") or "")):
            if (
                payload.get("verdict") != "STAMP"
                or payload.get("needs_web") is not False
                or payload.get("needs_internal") is not False
            ):
                continue
            answer = _safe_customer_answer(payload.get("shippable_answer"))
            receipt_probe = {
                **payload,
                "voice_profile_check": {
                    "source_path": profile,
                    "sha256": digest,
                    "constraints_applied": "receipt validation probe only",
                },
            }
            if (
                answer
                and "\u2014" not in answer
                and _valid_stamp(receipt_probe) == answer
                and _candidate_provenance(
                    records,
                    cycle,
                    index,
                    dispatches=dispatches,
                    parent_session_id=parent_session_id,
                )
                is not None
            ):
                return answer, cycle, record
    return "", 0, None


def _deterministic_cursor_cycle_handoff(
    route: object,
    *,
    original_request: str,
    records: list[dict[str, Any]],
) -> str:
    """Build the rework handoff when the supervisor refuses the durable route."""

    if (
        str(getattr(route, "status", "") or "") != "dispatch"
        or str(getattr(route, "agent", "") or "") != "cursor_workhorse"
    ):
        return ""
    title = str(getattr(route, "title", "") or "")
    match = re.fullmatch(r"cursor-cycle-([2-4])", title)
    if match is None or not original_request:
        return ""
    previous_cycle = int(match.group(1)) - 1
    prior_packets = [
        str(record.get("output") or "")
        for record in records
        if record.get("agent") == "codex_judge"
        and record.get("status") == "completed"
        and record.get("title")
        in {
            f"judge-cycle-{previous_cycle}",
            f"judge-format-repair-{previous_cycle}",
        }
        and str(record.get("output") or "").strip()
    ]
    context = "\n\n".join(
        f"PRIOR CODEX PACKET {index + 1}:\n{packet}"
        for index, packet in enumerate(prior_packets)
    )
    return (
        f"TRIPLE STAMP CURSOR REWORK CYCLE {match.group(1)}\n\n"
        "Work directly in the current Cursor session. Return a complete Stage 1 "
        "evidence packet inline. Do not invoke a Skill, workflow, subagent, Task, "
        "nested agent, Omnigent, triple-stamp, run-with-isaac, or --self-test. "
        "Use at most one tool call per assistant turn. Address the original "
        "request and every concrete gap in the prior Codex packets.\n\n"
        f"ORIGINAL REQUEST (verbatim):\n{original_request}"
        + (f"\n\n{context}" if context else "")
    )


def _thousands(value: object) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "?"


def _elapsed(started_ns: object) -> str:
    try:
        seconds = max(0, int((time.time_ns() - int(started_ns)) // 1_000_000_000))
    except (TypeError, ValueError):
        return ""
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def _finished_clause(
    records: list[dict[str, Any]],
    dispatches: list[dict[str, Any]],
    announced: set[str],
) -> str:
    """Describe the newest collected triple-stamp packet in one sentence.

    Returns "" when the newest packet was already announced, which happens on a
    rework cycle where the judge sends work back without a new packet arriving.
    """

    known = [record for record in records if record.get("agent") in _STAGE_STEP]
    if not known:
        return ""
    newest = known[-1]
    title = str(newest.get("title") or "")
    marker = f"packet:{title}"
    if not title or marker in announced:
        return ""
    announced.add(marker)
    started = next(
        (
            row.get("dispatched_at_ns")
            for row in reversed(dispatches)
            if row.get("title") == title
        ),
        None,
    )
    # Measured from its dispatch to now, so it includes the supervisor's own
    # hand-off turn and is very slightly longer than the child's own runtime.
    took = _elapsed(started) if started else ""
    parts = [
        f"Just in: {title}, {_thousands(newest.get('output_bytes'))} bytes"
        + (f", {took} since it was dispatched" if took else "")
        + "."
    ]
    observation = newest.get("internal_mcp_observation")
    if isinstance(observation, dict) and observation.get("status") == "observed":
        families = ", ".join(
            f"{system} {count}"
            for system, count in (observation.get("by_system") or {}).items()
            if count
        )
        parts.append(
            f"The runtime observed {observation.get('count')} internal MCP calls"
            + (f" ({families})." if families else ".")
        )
    validation = newest.get("audit_validation")
    if isinstance(validation, dict) and validation.get("status"):
        parts.append(f"Receipt check: {validation['status']}.")
    return " ".join(parts)


def _progress_note(
    agent: str,
    title: str,
    announced: set[str],
    *,
    parent_session_id: str,
) -> str:
    """Author one deterministic status line for a stage that is starting now."""

    from triple_stamp_runtime_state import (
        parse_dispatch_title,
        read_attempt_collections,
        read_attempt_dispatches,
    )

    step, kind = _STAGE_STEP.get(agent, ("", ""))
    if not step or not title:
        return ""
    parsed = parse_dispatch_title(title)
    cycle = f"cycle {parsed['cycle']}" if parsed else "cycle ?"
    lines = [f"**Triple-stamp, {cycle} - step {step}, {kind}** (`{title}`)"]
    finished = _finished_clause(
        read_attempt_collections(parent_session_id=parent_session_id),
        read_attempt_dispatches(parent_session_id=parent_session_id),
        announced,
    )
    if finished:
        lines.append(finished)
    lines.append(_STAGE_DOING[agent])
    return "\n\n".join(lines) + "\n\n"


def _terminal_response_allowed(
    route: object,
    response: str,
    records: list[dict[str, Any]],
    *,
    dispatches: list[dict[str, Any]] | None = None,
    parent_session_id: str = "",
) -> bool:
    """Recognize only the three terminal response shapes."""

    from triple_stamp_isaac_launcher import (
        _INFRA_PREFIX,
        _VALIDATION_PREFIX,
    )

    status = str(getattr(route, "status", "") or "")
    if status == "success":
        answer = _attested_stamp_answer(route, records)
        return answer is not None and response == answer
    if status == "best_effort":
        from triple_stamp_isaac_launcher import _best_effort_answer

        answer = _best_effort_answer(
            records,
            route,
            dispatches=dispatches,
            parent_session_id=parent_session_id,
        )
        return answer is not None and response == answer
    if status == "validation_failed":
        return response.startswith(_VALIDATION_PREFIX)
    if status == "infrastructure_failed":
        return response.startswith(_INFRA_PREFIX)
    return False


def _customer_safe_terminal_failure() -> str:
    """Return a retryable reader-facing failure without policy internals."""

    return (
        "I couldn't complete this request because the research run ended "
        "unexpectedly. Please ask again to start a fresh attempt."
    )


def _record(
    action: str,
    route: object,
    *,
    attempt: int,
    reason: str,
    parent_session_id: str = "",
) -> None:
    from triple_stamp_runtime_state import append_supervisor_continuation

    append_supervisor_continuation(
        {
            "action": action,
            "attempt": attempt,
            "reason": reason,
            "route": _route_snapshot(route),
            "parent_session_id": parent_session_id,
        }
    )


def _replace_code_int_constant(
    code: CodeType,
    old: int,
    new: int,
) -> tuple[CodeType, int]:
    """Replace one integer constant anywhere in a function's code tree."""

    constants = list(code.co_consts)
    bytecode = bytearray(code.co_code)
    matches = 0
    changed = False
    for index, value in enumerate(constants):
        if type(value) is int and value == old:
            constants[index] = new
            matches += 1
            changed = True
        elif isinstance(value, CodeType):
            replacement, nested_matches = _replace_code_int_constant(
                value,
                old,
                new,
            )
            matches += nested_matches
            if replacement is not value:
                constants[index] = replacement
                changed = True
    for instruction in dis.get_instructions(code, show_caches=True):
        if instruction.opname != "LOAD_SMALL_INT" or instruction.argval != old:
            continue
        if not 0 <= new <= 255:
            raise RuntimeError(
                "replacement for Python LOAD_SMALL_INT is not byte-encodable"
            )
        bytecode[instruction.offset + 1] = new
        matches += 1
        changed = True
    if not changed:
        return code, matches
    return code.replace(
        co_code=bytes(bytecode),
        co_consts=tuple(constants),
    ), matches


def install_headless_pipeline_wait() -> None:
    """Remove Omnigent's unrelated one-shot timeout and 30-turn deadline."""

    if not os.environ.get("TRIPLE_STAMP_RUN_ID"):
        return
    from omnigent import chat

    chat._LOOP_TIMEOUT_S = _HEADLESS_PIPELINE_TIMEOUT_S
    query_once = chat._query_sessions_once
    if getattr(query_once, "__triple_stamp_headless_wait__", False):
        return
    replacement, matches = _replace_code_int_constant(
        query_once.__code__,
        _OMNIGENT_HEADLESS_EXTRA_TURN_LIMIT,
        _HEADLESS_PIPELINE_EXTRA_TURN_LIMIT,
    )
    if matches != 1:
        raise RuntimeError(
            "Omnigent 0.12 headless extra-turn guard drifted; expected one "
            f"{_OMNIGENT_HEADLESS_EXTRA_TURN_LIMIT}-turn constant, found "
            f"{matches}"
        )
    query_once.__code__ = replacement
    query_once.__triple_stamp_headless_wait__ = True


def install_supervisor_continuation_guard() -> None:
    """Suppress nonterminal replies and force bounded deterministic re-entry."""

    if not os.environ.get("TRIPLE_STAMP_RUN_ID"):
        return
    from omnigent.inner import claude_sdk_executor
    from omnigent.inner.executor import (
        TextChunk,
        ToolCallComplete,
        ToolCallRequest,
        TurnComplete,
    )
    from triple_stamp_isaac_launcher import (
        _INFRA_PREFIX,
        _Route,
        _failure_metrics,
        _best_effort_answer,
        _next_route,
        _route_dispatch_pending,
    )
    from triple_stamp_runtime_state import (
        activate_parent_attempt,
        attest_latest_codex_stamp,
        read_attempt_collections,
        read_attempt_dispatches,
        read_dispatches,
        read_attested_answer,
        read_best_effort_answer,
        read_last_tool_dispatch_exception,
        read_supervisor_continuations,
        read_terminal_failure,
        record_best_effort_answer,
        record_terminal_failure,
    )

    original = claude_sdk_executor.ClaudeSDKExecutor.run_turn
    if getattr(original, "__triple_stamp_continuation_guard__", False):
        return

    async def guarded_run_turn(
        self: object,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_prompt: str,
        config: object = None,
    ) -> AsyncIterator[object]:
        if getattr(self, "_agent_name", None) != "triple-stamp":
            async for event in original(
                self,
                messages,
                tools,
                system_prompt,
                config,
            ):
                yield event
            return

        continuation_attempt = 0
        current_messages = messages
        original_request = _original_request(messages)
        session_id = _session_id(messages)
        if not session_id or session_id == "default":
            # Never reduce a run-global union when a wake lacks one authoritative
            # root conversation id. A later concrete wake can safely continue.
            yield TurnComplete(response="", modified_by_policy=True)
            return
        # The request policy normally owns attempt identity because it sees the
        # genuine top-level submit event. Preserve its generation on every
        # ordinary child wake. The fallback identity is used only when a prior
        # terminal artifact exists, allowing direct executor callers and a new
        # user turn to open a fresh attempt without reviving synthetic wakes.
        terminal_exists = bool(
            read_terminal_failure(parent_session_id=session_id)
            or read_attested_answer(parent_session_id=session_id)
            or read_best_effort_answer(parent_session_id=session_id)
        )
        attempt_generation = activate_parent_attempt(
            session_id,
            _attempt_request_identity(messages) if terminal_exists else "",
        )
        # One narration per stage title for the life of the run. The guard is
        # re-entered on every wake, so this cannot live in the loop. Keyed by run
        # directory rather than run id because the directory is unique per run
        # even when a single process serves several.
        if len(_NARRATED_STAGES) > 8:
            _NARRATED_STAGES.clear()
        narrated = _NARRATED_STAGES.setdefault(
            (
                os.environ.get("TRIPLE_STAMP_RUN_DIR", ""),
                session_id,
                attempt_generation,
            ),
            set(),
        )
        while True:
            attest_latest_codex_stamp(session_id)
            attested_answer = read_attested_answer(
                parent_session_id=session_id
            )
            if attested_answer:
                route = _next_route(
                    read_attempt_collections(parent_session_id=session_id),
                    parent_session_id=session_id,
                )
                if any(
                    row.get("action") in _TERMINAL_RELAY_ACTIONS
                    for row in read_supervisor_continuations(
                        parent_session_id=session_id
                    )
                ):
                    _record(
                        "terminal_stamp_suppressed",
                        route,
                        attempt=continuation_attempt,
                        reason=(
                            "STAMP was already relayed; stale completion wake "
                            "cannot reopen routing"
                        ),
                        parent_session_id=session_id,
                    )
                    yield TurnComplete(response="", modified_by_policy=True)
                    return
                _record(
                    "deterministic_stamp_relay",
                    route,
                    attempt=continuation_attempt,
                    reason="parent already has an immutable STAMP attestation",
                    parent_session_id=session_id,
                )
                yield TextChunk(text=attested_answer)
                # A continuation record written before TextChunk is only relay
                # intent: a crash at that boundary must replay the durable
                # answer. Once the generator is resumed after TextChunk, the UI
                # has consumed the bytes, so this marker may safely suppress
                # stale wakes. A crash before this marker can duplicate text,
                # but it cannot lose the answer.
                _record(
                    "terminal_stamp_delivered",
                    route,
                    attempt=continuation_attempt,
                    reason="attested STAMP TextChunk was consumed",
                    parent_session_id=session_id,
                )
                yield TurnComplete(
                    response=attested_answer,
                    modified_by_policy=True,
                )
                return
            best_effort_answer = read_best_effort_answer(
                parent_session_id=session_id
            )
            if best_effort_answer:
                route = _next_route(
                    read_attempt_collections(parent_session_id=session_id),
                    parent_session_id=session_id,
                )
                if any(
                    row.get("action") in _TERMINAL_RELAY_ACTIONS
                    for row in read_supervisor_continuations(
                        parent_session_id=session_id
                    )
                ):
                    _record(
                        "terminal_best_effort_suppressed",
                        route,
                        attempt=continuation_attempt,
                        reason=(
                            "completed answer was already relayed; stale "
                            "completion wake cannot reopen routing"
                        ),
                        parent_session_id=session_id,
                    )
                    yield TurnComplete(response="", modified_by_policy=True)
                    return
                _record(
                    "deterministic_best_effort_relay",
                    route,
                    attempt=continuation_attempt,
                    reason=(
                        "parent already has an immutable completed answer "
                        "with explicit evidence gaps"
                    ),
                    parent_session_id=session_id,
                )
                yield TextChunk(text=best_effort_answer)
                yield TurnComplete(
                    response=best_effort_answer,
                    modified_by_policy=True,
                )
                return
            # A recorded terminal failure ends the run, and it has to end it
            # here, before the model is asked for another turn. Two things went
            # wrong on 2026-09-11 run-4gqa_fil once `judge-cycle-1` failed at
            # 20:08:27. The reader saw a bare red "Something went wrong",
            # because an ExecutorError is the one terminal shape the UI cannot
            # render as text, while the actual reason sat in
            # `terminal-failure.txt`. And the pipeline kept working for ten more
            # minutes past its own death notice: a full `cursor-cycle-2` ran to
            # completion and `audit-cycle-2` was dispatched and then killed
            # mid-tool-call, because nothing consulted that file again. Relay
            # a customer-safe retry message instead of exposing policy or
            # infrastructure internals. The exit code does not depend on the
            # event shape: `_validated_pipeline_exit` returns zero only for a
            # STAMP attestation with a valid stage chain.
            recorded_failure = read_terminal_failure(
                parent_session_id=session_id
            )
            if recorded_failure:
                relayed = any(
                    row.get("action") in _TERMINAL_RELAY_ACTIONS
                    for row in read_supervisor_continuations(
                        parent_session_id=session_id
                    )
                )
                route = _next_route(
                    read_attempt_collections(parent_session_id=session_id),
                    parent_session_id=session_id,
                )
                if relayed:
                    # A child that was already in flight when the run died still
                    # wakes this loop once. The reader has the reason; repeating
                    # it would be its own kind of noise.
                    _record(
                        "terminal_failure_suppressed",
                        route,
                        attempt=continuation_attempt,
                        reason="terminal failure was already relayed to the reader",
                        parent_session_id=session_id,
                    )
                    yield TurnComplete(response="", modified_by_policy=True)
                    return
                _record(
                    "terminal_failure_relayed",
                    route,
                    attempt=continuation_attempt,
                    reason="parent already has a recorded terminal failure",
                    parent_session_id=session_id,
                )
                safe_failure = _customer_safe_terminal_failure()
                yield TextChunk(text=safe_failure)
                yield TurnComplete(
                    response=safe_failure,
                    modified_by_policy=True,
                )
                return
            buffered_text: list[TextChunk] = []
            completed: TurnComplete | None = None
            send_attempted = False
            terminal_dispatch_suppressed = False
            sent_titles: list[str] = []
            sent_child_session_ids: set[str] = set()
            observed_parent_session_ids: set[str] = set()
            turn_started_at_ns = time.time_ns()
            async for event in original(
                self,
                current_messages,
                tools,
                system_prompt,
                config,
            ):
                if isinstance(event, TextChunk):
                    buffered_text.append(event)
                    continue
                if isinstance(event, ToolCallRequest):
                    normalized = event.name.rsplit("__", 1)[-1]
                    if normalized == "sys_session_send":
                        if (
                            read_attested_answer(parent_session_id=session_id)
                            or read_best_effort_answer(parent_session_id=session_id)
                            or read_terminal_failure(parent_session_id=session_id)
                        ):
                            # A child can terminalize the attempt while this
                            # already-started model turn is still streaming.
                            # Drop the raced dispatch before it reaches the UI;
                            # the native send boundary also turns it into a
                            # no-op, so no paid child can launch.
                            terminal_dispatch_suppressed = True
                            continue
                        send_attempted = True
                        arguments = event.args if isinstance(event.args, dict) else {}
                        sent_title = str(arguments.get("title") or "<missing>")
                        sent_titles.append(sent_title)
                        # Narrate the stage before its tool row, once per title.
                        # The UI otherwise shows only raw sys_session_send JSON
                        # for runs that take half an hour, because every attempt
                        # the supervisor makes to say what it is doing is
                        # suppressed by design.
                        if sent_title not in narrated:
                            narrated.add(sent_title)
                            note = _progress_note(
                                str(arguments.get("agent") or ""),
                                sent_title,
                                narrated,
                                parent_session_id=session_id,
                            )
                            if note:
                                yield TextChunk(text=note)
                    yield event
                    continue
                if isinstance(event, ToolCallComplete):
                    normalized = event.name.rsplit("__", 1)[-1]
                    if (
                        normalized == "sys_session_send"
                        and terminal_dispatch_suppressed
                    ):
                        continue
                    if normalized == "sys_session_send":
                        sent_child_session_ids.update(
                            _tool_child_session_ids(event.result)
                        )
                    elif normalized == "sys_read_inbox":
                        sent_child_session_ids.update(
                            _tool_child_session_ids(event.result)
                        )
                        observed_parent_session_ids.update(
                            _tool_parent_session_ids(event.result)
                        )
                    yield event
                    continue
                if isinstance(event, TurnComplete):
                    completed = event
                    continue
                yield event

            if completed is None:
                return
            if terminal_dispatch_suppressed:
                # Re-enter the terminal checks. They relay an undelivered
                # terminal once or suppress an already-delivered stale wake.
                continue

            records = read_attempt_collections(parent_session_id=session_id)
            dispatches = read_attempt_dispatches(parent_session_id=session_id)
            resolved_parent = _dispatch_parent_session_id(
                read_dispatches(),
                sent_child_session_ids,
            )
            if resolved_parent:
                session_id = resolved_parent
            elif len(observed_parent_session_ids) == 1:
                session_id = next(iter(observed_parent_session_ids))
            elif observed_parent_session_ids or sent_child_session_ids:
                # Ambiguous native identity is not permission to merge ledgers.
                yield TurnComplete(
                    response="",
                    modified_by_policy=True,
                    usage=completed.usage,
                )
                return
            records = read_attempt_collections(parent_session_id=session_id)
            dispatches = read_attempt_dispatches(parent_session_id=session_id)
            route = _next_route(records, parent_session_id=session_id)
            response = completed.response
            if not isinstance(response, str):
                response = "".join(event.text for event in buffered_text)
            if (
                str(getattr(route, "status", "") or "")
                == "infrastructure_failed"
                and records
                and str(records[-1].get("title") or "").startswith(
                    "judge-format-repair-"
                )
            ):
                fallback_route = _Route(
                    "best_effort",
                    int(getattr(route, "cycle", 0) or 0),
                    reason=(
                        "completed evidence contains a safe supported answer, "
                        "but the judge format repair was unrecoverable"
                    ),
                )
                fallback = _best_effort_answer(
                    records,
                    fallback_route,
                    dispatches=dispatches,
                    parent_session_id=session_id,
                )
                if fallback:
                    persisted = record_best_effort_answer(
                        fallback,
                        records,
                        cycle=fallback_route.cycle,
                        reason=fallback_route.reason,
                        parent_session_id=session_id,
                    )
                    if persisted:
                        _record(
                            "judge_format_repair_best_effort_relay",
                            fallback_route,
                            attempt=continuation_attempt,
                            reason=(
                                "unrecoverable format repair fell back to the "
                                "validated best-supported answer"
                            ),
                            parent_session_id=session_id,
                        )
                        yield TextChunk(text=persisted)
                        yield TurnComplete(
                            response=persisted,
                            modified_by_policy=True,
                            usage=completed.usage,
                        )
                        return
            if response.startswith(_INFRA_PREFIX):
                if _route_dispatch_pending(
                    route,
                    records,
                    dispatches,
                    parent_session_id=session_id,
                ):
                    _record(
                        "inflight_failure_claim_suppressed",
                        route,
                        attempt=continuation_attempt,
                        reason=(
                            "supervisor claimed infrastructure failure while "
                            "the durable required child remains in flight"
                        ),
                        parent_session_id=session_id,
                    )
                    yield TurnComplete(
                        response="",
                        modified_by_policy=True,
                        usage=completed.usage,
                    )
                    return
                failure_diagnostic = read_last_tool_dispatch_exception(
                    parent_session_id=session_id,
                    titles=sent_titles,
                    observed_after_ns=turn_started_at_ns,
                )
                genuine_native_failure = (
                    failure_diagnostic is not None
                    and failure_diagnostic.get("phase") == "native_send"
                    and failure_diagnostic.get("native_send_status") == "failed"
                )
                if (
                    str(getattr(route, "status", "") or "")
                    != "infrastructure_failed"
                    and not genuine_native_failure
                ):
                    if str(getattr(route, "title", "") or "").startswith(
                        "cursor-retry-"
                    ):
                        # The model's generic "required child failed" rule is
                        # stale for this one mechanically retryable transport
                        # failure. Continue into the bounded deterministic
                        # dispatch path instead of accepting or surfacing it.
                        response = ""
                    else:
                        _record(
                            "unverified_failure_claim_suppressed",
                            route,
                            attempt=continuation_attempt,
                            reason=(
                                "supervisor emitted an unverified infrastructure "
                                "failure; no continuation or terminal mutation "
                                "is authorized"
                            ),
                            parent_session_id=session_id,
                        )
                        yield TurnComplete(
                            response="",
                            modified_by_policy=True,
                            usage=completed.usage,
                        )
                        return
                else:
                    record_terminal_failure(
                        "PIPELINE_INFRASTRUCTURE_ERROR",
                        response.removeprefix(_INFRA_PREFIX).strip(),
                        stage=str(getattr(route, "title", "") or route.status),
                        cycle=int(getattr(route, "cycle", 0) or 0),
                        parent_session_id=session_id,
                    )
                    _record(
                        "supervisor_failure_terminalized",
                        route,
                        attempt=continuation_attempt,
                        reason=(
                            "supervisor emitted an infrastructure failure; "
                            "continuation dispatch is forbidden"
                        ),
                        parent_session_id=session_id,
                    )
                    safe_failure = _customer_safe_terminal_failure()
                    yield TextChunk(text=safe_failure)
                    yield TurnComplete(
                        response=safe_failure,
                        modified_by_policy=True,
                        usage=completed.usage,
                    )
                    return

            bounded_answer = _best_effort_answer(
                records,
                route,
                dispatches=dispatches,
                parent_session_id=session_id,
            )
            if bounded_answer is not None:
                persisted = record_best_effort_answer(
                    bounded_answer,
                    records,
                    cycle=int(getattr(route, "cycle", 0) or 0),
                    reason=str(getattr(route, "reason", "") or ""),
                    parent_session_id=session_id,
                )
                if persisted:
                    _record(
                        "deterministic_best_effort_relay",
                        route,
                        attempt=continuation_attempt,
                        reason=(
                            "bounded route preserved completed evidence and "
                            "disclosed the judge's remaining gaps"
                        ),
                        parent_session_id=session_id,
                    )
                    yield TextChunk(text=persisted)
                    yield TurnComplete(
                        response=persisted,
                        modified_by_policy=True,
                        usage=completed.usage,
                    )
                    return

            if _terminal_response_allowed(
                route,
                response,
                records,
                dispatches=dispatches,
                parent_session_id=session_id,
            ):
                for event in buffered_text:
                    yield event
                yield completed
                return

            # A success route requires a byte-exact relay of Codex's
            # `shippable_answer`. Asking a model to reproduce several thousand
            # bytes verbatim is not a reliable operation, and on 2026-09-11
            # run-n0zgj9tp it cost a completed run: Codex STAMPed, the answer was
            # durably attested to `stamped-answer.bin`, and the supervisor still
            # failed the equality check twice and ended in
            # PIPELINE_INFRASTRUCTURE_ERROR. The bytes are already validated
            # here, so relay them deterministically instead of re-prompting.
            # Buffered prose is deliberately dropped: FINAL OUTPUT permits
            # nothing before or after the answer.
            attested = _attested_stamp_answer(route, records)
            if attested is not None:
                _record(
                    "deterministic_stamp_relay",
                    route,
                    attempt=continuation_attempt,
                    reason=(
                        "supervisor response was not byte-exact; relayed the "
                        "attested stamped answer"
                    ),
                    parent_session_id=session_id,
                )
                # The answer has to be STREAMED, not just declared on
                # TurnComplete. The success path above yields its buffered
                # TextChunk events before `completed` because those chunks are
                # what the conversation store and UI persist as the assistant
                # message. Relaying only TurnComplete completes the turn with no
                # text: on 2026-09-11 run-ii3vl_xu stamped cleanly and the
                # parent conversation still held nothing but two ~200-byte
                # messages, so the answer never reached the reader.
                yield TextChunk(text=attested)
                _record(
                    "terminal_stamp_delivered",
                    route,
                    attempt=continuation_attempt,
                    reason="attested STAMP TextChunk was consumed",
                    parent_session_id=session_id,
                )
                yield TurnComplete(
                    response=attested,
                    modified_by_policy=True,
                    usage=completed.usage,
                )
                return

            if _route_dispatch_pending(
                route,
                records,
                dispatches,
                parent_session_id=session_id,
            ):
                _record(
                    "ordinary_response_suppressed",
                    route,
                    attempt=continuation_attempt,
                    reason="required route was already durably dispatched",
                    parent_session_id=session_id,
                )
                yield TurnComplete(
                    response="",
                    modified_by_policy=True,
                    usage=completed.usage,
                )
                return

            current_diagnostic = read_last_tool_dispatch_exception(
                parent_session_id=session_id,
                titles=sent_titles,
                observed_after_ns=turn_started_at_ns,
            )
            observer_diagnostic = (
                current_diagnostic
                if current_diagnostic is not None
                and current_diagnostic.get("phase") == "ledger_observer"
                else None
            )
            if (
                observer_diagnostic is None
                and str(getattr(route, "status", "") or "") == "dispatch"
                and str(getattr(route, "title", "") or "")
            ):
                candidate = read_last_tool_dispatch_exception(
                    parent_session_id=session_id,
                    titles=[str(getattr(route, "title", "") or "")],
                )
                if (
                    candidate is not None
                    and candidate.get("phase") == "ledger_observer"
                ):
                    observer_diagnostic = candidate

            if send_attempted:
                diagnostic = observer_diagnostic or current_diagnostic
                if observer_diagnostic is not None:
                    reason = _missing_dispatch_reason(
                        route,
                        sent_titles=sent_titles,
                        diagnostic=observer_diagnostic,
                        abstained=True,
                    )
                    _LOGGER.warning(reason)
                    _record(
                        "dispatch_observer_failure_abstained",
                        route,
                        attempt=continuation_attempt,
                        reason=reason,
                        parent_session_id=session_id,
                    )
                    yield TurnComplete(
                        response="",
                        modified_by_policy=True,
                        usage=completed.usage,
                    )
                    return
                reason = _missing_dispatch_reason(
                    route,
                    sent_titles=sent_titles,
                    diagnostic=diagnostic,
                )
                # Dispatching the wrong title is a routing mistake, not a dead
                # pipeline, and `_route_dispatch_pending` was already False, so
                # the required route has nothing in flight and re-prompting
                # cannot double-dispatch it. On 2026-09-11 run-4gqa_fil this
                # branch destroyed a healthy run 19 minutes in: Cursor and a
                # mechanically validated 16-call Opus audit were both collected,
                # the route was `judge-cycle-1`, the supervisor sent
                # `cursor-cycle-2`, and the only outcome available was a terminal
                # infrastructure error. Spend the bounded continuation naming the
                # correct route first, and abstain once that is exhausted.
                if continuation_attempt < _CONTINUATION_LIMIT:
                    continuation_attempt += 1
                    _LOGGER.warning(reason)
                    _record(
                        "misrouted_dispatch_continuation",
                        route,
                        attempt=continuation_attempt,
                        reason=reason,
                        parent_session_id=session_id,
                    )
                    current_messages = [
                        *messages,
                        {
                            "role": "user",
                            "content": _continuation_prompt(
                                route,
                                original_request=original_request,
                            ),
                            "session_id": session_id,
                        }
                    ]
                    continue
                # A misroute must not record a terminal failure even after the
                # continuation is spent, because run-4gqa_fil settled the
                # question empirically: the guard declared this exact condition
                # terminal at 20:08:27, the pipeline ignored the death notice,
                # and cycle 2 STAMPed a clean answer at 20:28 with
                # `gap_materiality: nonmaterial`. Killing the run there would
                # have destroyed a good answer. Abstain the way a ledger
                # observer failure abstains and let the cycle bounds decide,
                # which keeps a recorded terminal failure meaning what the stop
                # check at the top of this loop assumes it means.
                _LOGGER.warning(reason)
                _record(
                    "misrouted_dispatch_abstained",
                    route,
                    attempt=continuation_attempt,
                    reason=reason,
                    parent_session_id=session_id,
                )
                yield TurnComplete(
                    response="",
                    modified_by_policy=True,
                    usage=completed.usage,
                )
                return

            if observer_diagnostic is not None:
                reason = _missing_dispatch_reason(
                    route,
                    sent_titles=[],
                    diagnostic=observer_diagnostic,
                    abstained=True,
                )
                _LOGGER.warning(reason)
                _record(
                    "continuation_abstained_observer_failure",
                    route,
                    attempt=continuation_attempt,
                    reason=reason,
                    parent_session_id=session_id,
                )
                yield TurnComplete(
                    response="",
                    modified_by_policy=True,
                    usage=completed.usage,
                )
                return

            if continuation_attempt < _CONTINUATION_LIMIT:
                continuation_attempt += 1
                _record(
                    "continuation_enqueued",
                    route,
                    attempt=continuation_attempt,
                    reason="ordinary final response left durable route unexecuted",
                    parent_session_id=session_id,
                )
                current_messages = [
                    *messages,
                    {
                        "role": "user",
                        "content": _continuation_prompt(
                            route,
                            original_request=original_request,
                        ),
                        "session_id": session_id,
                    }
                ]
                continue

            deterministic_handoff = _deterministic_cursor_cycle_handoff(
                route,
                original_request=original_request,
                records=records,
            )
            if deterministic_handoff:
                title = str(getattr(route, "title", "") or "")
                tool_executor = getattr(self, "_tool_executor", None)
                if callable(tool_executor):
                    if title not in narrated:
                        narrated.add(title)
                        note = _progress_note(
                            "cursor_workhorse",
                            title,
                            narrated,
                            parent_session_id=session_id,
                        )
                        if note:
                            yield TextChunk(text=note)
                    forced_args = {
                        "agent": "cursor_workhorse",
                        "title": title,
                        "args": {"input": deterministic_handoff},
                    }
                    try:
                        await tool_executor("sys_session_send", forced_args)
                    except Exception as exc:  # noqa: BLE001 - fallback below
                        _LOGGER.warning(
                            "deterministic rework dispatch failed: %s",
                            type(exc).__name__,
                        )
                    dispatches = read_attempt_dispatches(
                        parent_session_id=session_id
                    )
                    if _route_dispatch_pending(
                        route,
                        records,
                        dispatches,
                        parent_session_id=session_id,
                    ):
                        _record(
                            "deterministic_rework_dispatch",
                            route,
                            attempt=continuation_attempt,
                            reason=(
                                "supervisor exhausted its continuation prompts; "
                                "runtime executed the durable Cursor rework route"
                            ),
                            parent_session_id=session_id,
                        )
                        yield TurnComplete(
                            response="",
                            modified_by_policy=True,
                            usage=completed.usage,
                        )
                        return
                    _record(
                        "deterministic_rework_dispatch_failed",
                        route,
                        attempt=continuation_attempt,
                        reason=(
                            "runtime dispatch produced no durable Cursor "
                            "cycle record; preserving raw STAMP candidate"
                        ),
                        parent_session_id=session_id,
                    )

            fallback, fallback_cycle, fallback_source = (
                _raw_stamp_fallback_answer(
                    records,
                    dispatches=dispatches,
                    parent_session_id=session_id,
                )
            )
            if fallback:
                fallback_records = [
                    record
                    for record in records
                    if record.get("agent") != "codex_judge"
                    or record is fallback_source
                ]
                persisted = record_best_effort_answer(
                    fallback,
                    fallback_records,
                    cycle=fallback_cycle,
                    reason=(
                        "raw Codex STAMP contained a shippable answer, but "
                        "judge format repair could not be dispatched within "
                        "the deterministic continuation bound"
                    ),
                    parent_session_id=session_id,
                    candidate_record=fallback_source,
                )
                if persisted:
                    _record(
                        "judge_format_repair_best_effort_relay",
                        route,
                        attempt=continuation_attempt,
                        reason=(
                            "bounded format-repair dispatch failed; relayed "
                            "the raw STAMP answer as unapproved BEST_EFFORT"
                        ),
                        parent_session_id=session_id,
                    )
                    yield TextChunk(text=persisted)
                    yield TurnComplete(
                        response=persisted,
                        modified_by_policy=True,
                        usage=completed.usage,
                    )
                    return
            reason = (
                "supervisor exhausted deterministic continuation prompts "
                f"without dispatching {getattr(route, 'title', '') or route.status}; "
                "leaving the recoverable route open"
            )
            _LOGGER.warning(reason)
            _record(
                "continuation_exhausted_abstained",
                route,
                attempt=continuation_attempt,
                reason=reason,
                parent_session_id=session_id,
            )
            yield TurnComplete(
                response="",
                modified_by_policy=True,
                usage=completed.usage,
            )
            return

    guarded_run_turn.__triple_stamp_continuation_guard__ = True
    guarded_run_turn.__triple_stamp_original__ = original
    claude_sdk_executor.ClaudeSDKExecutor.run_turn = guarded_run_turn
