"""Deterministic one-shot continuation for the triple-stamp supervisor."""

from __future__ import annotations

import logging
import os
import sys
import time
from collections.abc import AsyncIterator
from typing import Any

_CONTINUATION_LIMIT = 1
_HEADLESS_PIPELINE_TIMEOUT_S: float | None = None
_OMNIGENT_HEADLESS_EXTRA_TURN_LIMIT = 30
_HEADLESS_PIPELINE_EXTRA_TURN_LIMIT = sys.maxsize
_CONTINUATION_PREFIX = "TRIPLE_STAMP_NONTERMINAL_CONTINUATION"
_LOGGER = logging.getLogger(__name__)
# Every ledger action after which the guard has already streamed the recorded
# terminal failure to the reader. A later wake from a child that was still in
# flight must stay silent rather than print a second copy.
_TERMINAL_RELAY_ACTIONS = frozenset(
    {
        "terminal_failure_relayed",
        "continuation_exhausted",
    }
)

# What the reader is told when each stage starts. The supervisor is forbidden to
# emit status prose and every attempt is suppressed, which is why the UI showed
# raw `sys_session_send` rows and nothing else. The runtime holds the same facts
# and cannot drift from them, so it narrates instead of the model.
_NARRATED_STAGES: dict[str, set[str]] = {}
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


def _inject_voice_note(arguments: dict[str, object]) -> None:
    """Put the run's voice configuration into the Codex handoff itself.

    The note is appended to the Opus packet so it can travel onward, but that
    journey runs through the supervisor's own prose and is not guaranteed. Two
    runs lost a complete STAMP because Codex reported "the authoritative
    configuration line could not be located", defaulted to disabled, and
    `_valid_stamp` then correctly rejected the mismatch. The runtime knows the
    answer for certain, so it states it where Codex cannot miss it, exactly as
    it already does on the collected packets.
    """

    try:
        from triple_stamp_runtime_state import _voice_configuration_note

        note = _voice_configuration_note()
    except Exception:
        return
    if not note:
        return
    payload = arguments.get("args")
    if isinstance(payload, dict):
        text = payload.get("input")
        if isinstance(text, str) and note.strip() not in text:
            payload["input"] = text + note
    elif isinstance(payload, str) and note.strip() not in payload:
        arguments["args"] = payload + note


def _repair_defect(title: str) -> str:
    """Return the exact defect a format repair has to correct, if it is one.

    A repair that is only told "the previous output was malformed" cannot act,
    and the note the runtime appends to the collected packet reaches the child
    only through the supervisor's own prose, which is not guaranteed: an
    inspection of what the Codex child actually received showed neither the
    defect note nor the voice line present. The continuation prompt is authored
    by the runtime and delivered straight to the supervisor, so it is the one
    channel that cannot silently drop the reason.
    """

    if "format-repair" not in title:
        return ""
    try:
        from triple_stamp_runtime_state import _judgment_defect, read_collections

        agent = "codex_judge" if title.startswith("judge-") else "opus_auditor"
        packets = [
            record
            for record in read_collections()
            if record.get("agent") == agent
            and isinstance(record.get("output"), str)
        ]
        if not packets:
            return ""
        if agent == "codex_judge":
            return _judgment_defect(str(packets[-1]["output"]))
        body = str(packets[-1]["output"]).split("\n\n[System")[0].strip()
        if not body.startswith("{"):
            return (
                "the audit was prose rather than one JSON object; the whole "
                "reply must be the audit object and nothing else"
            )
    except Exception:
        return ""
    return ""


def _continuation_prompt(route: object) -> str:
    """Build one bounded prompt from the durable state-machine route."""

    snapshot = _route_snapshot(route)
    status = snapshot["status"]
    if status == "dispatch":
        resume = (
            f" Resume child session {snapshot['resume_child_session_id']}."
            if snapshot["resume_child_session_id"]
            else ""
        )
        defect = _repair_defect(str(snapshot["title"]))
        correction = (
            " That title is a format repair. Quote this exact defect to the "
            f"child verbatim in the handoff, because it is the only reason the "
            f"previous output was rejected and the child cannot see it "
            f"otherwise: {defect}"
            if defect
            else ""
        )
        return (
            f"{_CONTINUATION_PREFIX}: The previous supervisor response ended "
            "without executing the durable next route. Continue now by calling "
            f"sys_session_send exactly once for agent {snapshot['agent']!r} with "
            f"title {snapshot['title']!r}.{resume} Use the already collected "
            "packets and the original request in conversation history. Do not "
            f"emit status prose, poll, or answer the user.{correction}"
        )
    if status == "success":
        return (
            f"{_CONTINUATION_PREFIX}: The durable ledger contains a valid Codex "
            "STAMP. Relay only its shippable_answer byte-for-byte now, with "
            "nothing before or after it."
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
    for message in reversed(messages):
        value = message.get("session_id")
        if value:
            return str(value)
        metadata = message.get("metadata")
        if isinstance(metadata, dict) and metadata.get("session_id"):
            return str(metadata["session_id"])
    return "default"


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


def _progress_note(agent: str, title: str, announced: set[str]) -> str:
    """Author one deterministic status line for a stage that is starting now."""

    from triple_stamp_runtime_state import (
        parse_dispatch_title,
        read_collections,
        read_dispatches,
    )

    step, kind = _STAGE_STEP.get(agent, ("", ""))
    if not step or not title:
        return ""
    parsed = parse_dispatch_title(title)
    cycle = f"cycle {parsed['cycle']}" if parsed else "cycle ?"
    lines = [f"**Triple-stamp, {cycle} - step {step}, {kind}** (`{title}`)"]
    finished = _finished_clause(read_collections(), read_dispatches(), announced)
    if finished:
        lines.append(finished)
    lines.append(_STAGE_DOING[agent])
    return "\n\n".join(lines) + "\n\n"


def _terminal_response_allowed(
    route: object,
    response: str,
    records: list[dict[str, Any]],
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
    if status == "validation_failed":
        return response.startswith(_VALIDATION_PREFIX)
    if status == "infrastructure_failed":
        return response.startswith(_INFRA_PREFIX)
    return False


def _record(action: str, route: object, *, attempt: int, reason: str) -> None:
    from triple_stamp_runtime_state import append_supervisor_continuation

    append_supervisor_continuation(
        {
            "action": action,
            "attempt": attempt,
            "reason": reason,
            "route": _route_snapshot(route),
        }
    )


def install_headless_pipeline_wait() -> None:
    """Remove Omnigent's unrelated one-shot timeout and 30-turn deadline."""

    if not os.environ.get("TRIPLE_STAMP_RUN_ID"):
        return
    from omnigent import chat

    chat._LOOP_TIMEOUT_S = _HEADLESS_PIPELINE_TIMEOUT_S
    query_once = chat._query_sessions_once
    if getattr(query_once, "__triple_stamp_headless_wait__", False):
        return
    code = query_once.__code__
    matches = [
        index
        for index, value in enumerate(code.co_consts)
        if type(value) is int and value == _OMNIGENT_HEADLESS_EXTRA_TURN_LIMIT
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "Omnigent 0.12 headless extra-turn guard drifted; expected one "
            f"{_OMNIGENT_HEADLESS_EXTRA_TURN_LIMIT}-turn constant, found "
            f"{len(matches)}"
        )
    constants = list(code.co_consts)
    constants[matches[0]] = _HEADLESS_PIPELINE_EXTRA_TURN_LIMIT
    query_once.__code__ = code.replace(co_consts=tuple(constants))
    query_once.__triple_stamp_headless_wait__ = True


def install_supervisor_continuation_guard() -> None:
    """Suppress nonterminal replies and force one deterministic re-entry."""

    if not os.environ.get("TRIPLE_STAMP_RUN_ID"):
        return
    from omnigent.inner import claude_sdk_executor
    from omnigent.inner.executor import (
        TextChunk,
        ToolCallRequest,
        TurnComplete,
    )
    from triple_stamp_isaac_launcher import (
        _failure_metrics,
        _next_route,
        _route_dispatch_pending,
    )
    from triple_stamp_runtime_state import (
        read_collections,
        read_dispatches,
        read_last_tool_dispatch_exception,
        read_supervisor_continuations,
        read_terminal_failure,
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
        session_id = _session_id(messages)
        # One narration per stage title for the life of the run. The guard is
        # re-entered on every wake, so this cannot live in the loop. Keyed by run
        # directory rather than run id because the directory is unique per run
        # even when a single process serves several.
        if len(_NARRATED_STAGES) > 8:
            _NARRATED_STAGES.clear()
        narrated = _NARRATED_STAGES.setdefault(
            os.environ.get("TRIPLE_STAMP_RUN_DIR", ""), set()
        )
        while True:
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
            # the recorded text instead. The exit code does not depend on the
            # event shape: `_validated_pipeline_exit` returns zero only for a
            # STAMP attestation with a valid stage chain.
            recorded_failure = read_terminal_failure()
            if recorded_failure:
                relayed = any(
                    row.get("action") in _TERMINAL_RELAY_ACTIONS
                    for row in read_supervisor_continuations()
                )
                route = _next_route(read_collections())
                if relayed:
                    # A child that was already in flight when the run died still
                    # wakes this loop once. The reader has the reason; repeating
                    # it would be its own kind of noise.
                    _record(
                        "terminal_failure_suppressed",
                        route,
                        attempt=continuation_attempt,
                        reason="terminal failure was already relayed to the reader",
                    )
                    yield TurnComplete(response="", modified_by_policy=True)
                    return
                _record(
                    "terminal_failure_relayed",
                    route,
                    attempt=continuation_attempt,
                    reason="run already has a recorded terminal failure",
                )
                yield TextChunk(text=recorded_failure)
                yield TurnComplete(
                    response=recorded_failure,
                    modified_by_policy=True,
                )
                return
            buffered_text: list[TextChunk] = []
            completed: TurnComplete | None = None
            send_attempted = False
            sent_titles: list[str] = []
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
                        send_attempted = True
                        arguments = event.args if isinstance(event.args, dict) else {}
                        sent_title = str(arguments.get("title") or "<missing>")
                        sent_titles.append(sent_title)
                        if str(arguments.get("agent") or "") == "codex_judge":
                            _inject_voice_note(arguments)
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
                            )
                            if note:
                                yield TextChunk(text=note)
                    yield event
                    continue
                if isinstance(event, TurnComplete):
                    completed = event
                    continue
                yield event

            if completed is None:
                return

            records = read_collections()
            dispatches = read_dispatches()
            route = _next_route(records)
            response = completed.response
            if not isinstance(response, str):
                response = "".join(event.text for event in buffered_text)

            if _terminal_response_allowed(route, response, records):
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
                yield TurnComplete(
                    response=attested,
                    modified_by_policy=True,
                    usage=completed.usage,
                )
                return

            if _route_dispatch_pending(route, records, dispatches):
                _record(
                    "ordinary_response_suppressed",
                    route,
                    attempt=continuation_attempt,
                    reason="required route was already durably dispatched",
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
                    )
                    current_messages = [
                        {
                            "role": "user",
                            "content": _continuation_prompt(route),
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
                )
                current_messages = [
                    {
                        "role": "user",
                        "content": _continuation_prompt(route),
                        "session_id": session_id,
                    }
                ]
                continue

            reason = (
                "supervisor ended twice without executing the deterministic "
                f"continuation for {getattr(route, 'title', '') or route.status}"
            )
            _LOGGER.warning(reason)
            _record(
                "continuation_exhausted",
                route,
                attempt=continuation_attempt,
                reason=reason,
            )
            cost, calls = _failure_metrics()
            terminal = record_terminal_failure(
                "PIPELINE_INFRASTRUCTURE_ERROR",
                reason,
                stage=str(getattr(route, "title", "") or ""),
                cycle=int(getattr(route, "cycle", 0) or 0),
                cost_usd=cost,
                calls=calls,
            )
            yield TextChunk(text=terminal)
            yield TurnComplete(
                response=terminal,
                modified_by_policy=True,
                usage=completed.usage,
            )
            return

    guarded_run_turn.__triple_stamp_continuation_guard__ = True
    guarded_run_turn.__triple_stamp_original__ = original
    claude_sdk_executor.ClaudeSDKExecutor.run_turn = guarded_run_turn
