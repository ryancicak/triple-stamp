"""Cursor lifecycle boundary for the generated triple-stamp runtime.

Omnigent 0.12 treats Cursor's stop-hook marker as a terminal ``idle`` edge.
Cursor can emit that marker between tool calls, so this project gates the
native status forwarder on Cursor's actual transcript and keeps inbox reads
non-blocking and cancellation-safe.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote

_CURSOR_COMPLETION_STABLE_S = 2.0
_CURSOR_STAGE_INACTIVITY_S = 5 * 60
_CURSOR_STAGE_ABSOLUTE_S = 15 * 60
_CURSOR_POST_CLAIM_S = 30.0
_INBOX_TIME_CONTRACT_S = 5.0
_CURSOR_PARENT_WAKE_RETRY_BASE_S = 1.0
_CURSOR_PARENT_WAKE_RETRY_MAX_S = 30.0
_CURSOR_PARENT_WAKE_MAX_ATTEMPTS = 5
_CURSOR_PARENT_WAKE_DEADLINE_S = 180.0
_NO_OUTPUT = "[System: sub-agent completed with no output]"
_PENDING_PREFIX = "[System: sub-agent task pending —"
_MISSING_PARENT_INBOX = "Error: sys_session_send requires parent session inbox"
_MISSING_PARENT_INBOX_TERMINAL = (
    "PIPELINE_INFRASTRUCTURE_ERROR: parent session inbox was not initialized "
    "before sys_session_send; retry denied"
)
_OPUS_UNKNOWN_COMPLETION_PREFIX = (
    "PIPELINE_INFRASTRUCTURE_ERROR: Opus native dispatch timed out after "
    "child creation; completion is unknown and duplicate paid launch is forbidden"
)
_DUPLICATE_CURSOR_DISPATCH = (
    "Error: duplicate Cursor dispatch denied; this attempt already dispatched "
    "or completed that cycle, or advanced to a later stage"
)
_DUPLICATE_OPUS_RETRY_DISPATCH = (
    "Error: duplicate paid Opus retry denied; this attempt/cycle already "
    "dispatched its one reserved retry"
)
_DISPATCH_BOOKKEEPING_ERRORS = (
    AttributeError,
    IndexError,
    OSError,
    TypeError,
    ValueError,
)
PARENT_INBOX_PROBE_KEY = "_triple_stamp_parent_inbox_probe"
PARENT_INBOX_PROBE_VALUE = "v1"
PARENT_INBOX_READY = "TRIPLE_STAMP_PARENT_INBOX_READY:v1"
_parent_inbox_failures: dict[str, str] = {}
_unknown_opus_dispatches: dict[str, str] = {}
_CursorWakeKey = tuple[str, str, str]
_cursor_parent_wake_tasks: dict[_CursorWakeKey, asyncio.Task[None]] = {}


async def _cursor_parent_wake_retry_sleep(seconds: float) -> None:
    """Pace stranded parent-wake retries without a hot loop."""

    await asyncio.sleep(seconds)


async def _retry_cursor_parent_wake(
    *,
    client: Any,
    parent_session_id: str,
    child_session_id: str,
    work_id: str,
    notice: str,
    created_by: str | None,
    started_at_monotonic: float | None = None,
) -> None:
    """Retry one queued Cursor completion, then terminalize if unreachable."""

    from omnigent.runner import app as runner_app
    from triple_stamp_runtime_state import (
        mutate_cursor_lifecycle,
        read_dispatches,
        record_terminal_failure,
    )

    attempts = 0
    started = (
        started_at_monotonic
        if started_at_monotonic is not None
        else time.monotonic()
    )
    while (
        attempts < _CURSOR_PARENT_WAKE_MAX_ATTEMPTS
        and time.monotonic() - started < _CURSOR_PARENT_WAKE_DEADLINE_S
    ):
        remaining = _CURSOR_PARENT_WAKE_DEADLINE_S - (
            time.monotonic() - started
        )
        if remaining <= 0:
            break
        attempts += 1
        try:
            delivered = await asyncio.wait_for(
                runner_app._deliver_subagent_wake_post(
                    client,
                    parent_session_id,
                    notice,
                    created_by=created_by,
                ),
                timeout=remaining,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - stranded wake remains retryable
            delivered = False

        def observe(
            generation: dict[str, Any],
            _state: dict[str, Any],
        ) -> None:
            generation["parent_wake_attempts"] = attempts
            generation["parent_wake_pending"] = not delivered
            if delivered:
                generation["parent_wake_delivered_at_ns"] = time.time_ns()

        mutate_cursor_lifecycle(child_session_id, work_id, observe)
        if delivered:
            return
        elapsed = time.monotonic() - started
        if (
            attempts >= _CURSOR_PARENT_WAKE_MAX_ATTEMPTS
            or elapsed >= _CURSOR_PARENT_WAKE_DEADLINE_S
        ):
            break
        delay = min(
            _CURSOR_PARENT_WAKE_RETRY_BASE_S * (2 ** min(attempts - 1, 8)),
            _CURSOR_PARENT_WAKE_RETRY_MAX_S,
            max(0.0, _CURSOR_PARENT_WAKE_DEADLINE_S - elapsed),
        )
        await _cursor_parent_wake_retry_sleep(delay)

    dispatch = next(
        (
            record
            for record in reversed(read_dispatches())
            if record.get("child_session_id") == child_session_id
            and record.get("work_id") == work_id
        ),
        {},
    )
    reason = (
        "Cursor parent wake exhausted its bounded recovery "
        f"(attempts={attempts}, deadline_s={_CURSOR_PARENT_WAKE_DEADLINE_S:.0f}); "
        "the terminal packet remains queued locally"
    )
    failure = record_terminal_failure(
        "PIPELINE_INFRASTRUCTURE_ERROR",
        reason,
        stage=str(dispatch.get("title") or "cursor-parent-wake"),
        cycle=int(dispatch.get("cycle") or 0),
        parent_session_id=parent_session_id,
    )
    parent_events = runner_app._session_event_queues_ref.setdefault(
        parent_session_id,
        asyncio.Queue(),
    )
    parent_events.put_nowait(
        {
            "type": "session.status",
            "status": "failed",
            "error": {
                "type": "CursorParentWakeExhausted",
                "message": failure,
            },
        }
    )
    inbox = ensure_parent_inbox(parent_session_id)
    queued = False
    for packet in tuple(inbox._queue):  # type: ignore[attr-defined]
        if (
            isinstance(packet, dict)
            and packet.get("work_id") == work_id
            and packet.get("conversation_id") == child_session_id
        ):
            packet["status"] = "failed"
            packet["output"] = failure
            queued = True
            break
    entry = runner_app.get_subagent_work(child_session_id)
    if entry is not None:
        entry.status = "failed"
        entry.output = failure
        entry.completed_at = time.time()
        entry.delivered = True
    if not queued:
        inbox.put_nowait(
            {
                "type": "sub_agent",
                "work_id": work_id,
                "task_id": child_session_id,
                "handle_id": child_session_id,
                "conversation_id": child_session_id,
                "tool_name": (
                    entry.agent if entry is not None else "cursor_workhorse"
                ),
                "agent": (
                    entry.agent if entry is not None else "cursor_workhorse"
                ),
                "title": (
                    entry.title
                    if entry is not None
                    else str(dispatch.get("title") or "")
                ),
                "status": "failed",
                "output": failure,
            }
        )

    def terminalize(
        generation: dict[str, Any],
        _state: dict[str, Any],
    ) -> None:
        generation["parent_wake_attempts"] = attempts
        generation["parent_wake_pending"] = False
        generation["parent_wake_exhausted"] = True
        generation["parent_notification_queued"] = True
        generation["delivery_committed"] = False
        generation["terminal_status"] = "failed"
        generation["terminal_output"] = failure
        generation["terminal_phase"] = "wake_exhausted"

    mutate_cursor_lifecycle(child_session_id, work_id, terminalize)


def _schedule_cursor_parent_wake_retry(
    *,
    client: Any,
    parent_session_id: str,
    child_session_id: str,
    work_id: str,
    notice: str,
    created_by: str | None,
    started_at_monotonic: float | None = None,
) -> None:
    """Keep exactly one paced stranded-wake task per child generation."""

    key = (parent_session_id, child_session_id, work_id)
    existing = _cursor_parent_wake_tasks.get(key)
    if existing is not None and not existing.done():
        return
    task = asyncio.create_task(
        _retry_cursor_parent_wake(
            client=client,
            parent_session_id=parent_session_id,
            child_session_id=child_session_id,
            work_id=work_id,
            notice=notice,
            created_by=created_by,
            started_at_monotonic=started_at_monotonic,
        ),
        name=f"triple-stamp-cursor-wake-{parent_session_id}",
    )
    _cursor_parent_wake_tasks[key] = task

    def clear(completed: asyncio.Task[None]) -> None:
        if _cursor_parent_wake_tasks.get(key) is completed:
            _cursor_parent_wake_tasks.pop(key, None)

    task.add_done_callback(clear)


def _cursor_retry_note(title: str) -> str:
    """Name the sole fresh retry for an original Cursor cycle failure."""

    match = re.fullmatch(r"cursor-cycle-([1-4])", title)
    if match is None:
        return ""
    cycle = match.group(1)
    return (
        "\n\n[System-required next dispatch: Cursor ended without a complete "
        "packet in a retryable transport failure. Dispatch cursor_workhorse "
        f"as one fresh child with title cursor-retry-{cycle}-1. Keep cycle "
        f"{cycle}; do not resume the failed child. This retry is separate "
        "from the Opus transient-retry budget.]"
    )


def _cursor_failure_requires_terminal_latch(
    title: str,
    status: str,
    output: str,
) -> bool:
    """Return whether a delivered Cursor failure has no recovery route."""

    if status != "failed":
        return False
    if re.fullmatch(r"cursor-retry-[1-4]-1", title):
        return True
    if re.fullmatch(r"cursor-cycle-[1-4]", title):
        retryable = (
            output.startswith("CURSOR_WORKER_TIMEOUT: kind=inactivity")
            and "assistant_chars=0" in output
        ) or output.startswith(
            "CURSOR_WORKER_FAILED: Cursor turn ended with status "
        )
        return not retryable
    return re.fullmatch(
        r"cursor-web-(?:opus|codex)-[1-4]-[1-2]",
        title,
    ) is not None


def _install_codex_voice_environment() -> None:
    """Thread the validated voice profile into codex-native app-server env."""

    from omnigent import codex_native_app_server

    original = codex_native_app_server.build_codex_native_server
    if getattr(original, "__triple_stamp_voice_environment__", False):
        return

    def voice_aware_server(*args: object, **kwargs: object) -> Any:
        server = original(*args, **kwargs)
        for name in (
            "TRIPLE_STAMP_VOICE_PROFILE",
            "TRIPLE_STAMP_VOICE_PROFILE_SHA256",
        ):
            value = os.environ.get(name, "")
            if value:
                server.env[name] = value
        return server

    voice_aware_server.__triple_stamp_voice_environment__ = True
    voice_aware_server.__triple_stamp_original__ = original
    codex_native_app_server.build_codex_native_server = voice_aware_server

    original_terminal_env = codex_native_app_server.codex_terminal_env

    def voice_aware_terminal_env(server: Any) -> dict[str, str]:
        env = original_terminal_env(server)
        for name in (
            "TRIPLE_STAMP_VOICE_PROFILE",
            "TRIPLE_STAMP_VOICE_PROFILE_SHA256",
        ):
            value = server.env.get(name)
            if isinstance(value, str) and value:
                env[name] = value
        return env

    voice_aware_terminal_env.__triple_stamp_voice_environment__ = True
    voice_aware_terminal_env.__triple_stamp_original__ = original_terminal_env
    codex_native_app_server.codex_terminal_env = voice_aware_terminal_env


def _codex_runtime_handoff(
    args: object,
    *,
    title: object,
    parent_session_id: str,
    read_attempt_collections: Any,
) -> object:
    """Append launch truth that a model-authored handoff cannot override.

    The routing supervisor cannot inspect its own environment.  It can therefore
    emit a stale sentence claiming that voice rendering is disabled even while
    the runner has a validated profile configured.  Codex receives the actual
    environment through :func:`_install_codex_voice_environment`; this suffix
    makes the same fact explicit in the turn that tells the judge what to do.

    Format-repair dispatches also receive the actual collected raw judgment.
    Asking the supervisor to reproduce a near-10KB packet proved lossy in a live
    run: it sent a prose placeholder instead, so Codex had nothing to normalize.
    """

    if not isinstance(args, dict):
        return args
    nested = args.get("args")
    if isinstance(nested, dict):
        child_args = dict(nested)
        handoff = child_args.get("input")
    else:
        child_args = None
        handoff = nested
    text = handoff if isinstance(handoff, str) else ""
    additions: list[str] = []

    profile = os.environ.get("TRIPLE_STAMP_VOICE_PROFILE", "").strip()
    digest = os.environ.get("TRIPLE_STAMP_VOICE_PROFILE_SHA256", "").strip()
    if profile:
        additions.append(
            "[System-authoritative Codex launch context]\n"
            f"TRIPLE_STAMP_VOICE_PROFILE is configured as {profile!r} and is "
            "present in the Codex app-server and terminal environment. "
            f"TRIPLE_STAMP_VOICE_PROFILE_SHA256 is {digest!r}. Any earlier "
            "handoff sentence claiming that no voice profile is configured is "
            "stale and false. Read the exact live path from the environment. "
            "A STAMP must not report voice rendering disabled; it must apply "
            "the profile and return its path and computed SHA-256."
        )

    match = re.fullmatch(r"judge-format-repair-([1-4])", str(title or ""))
    if match is not None and parent_session_id:
        source_title = f"judge-cycle-{match.group(1)}"
        source = next(
            (
                str(record.get("output") or "")
                for record in reversed(
                    read_attempt_collections(
                        parent_session_id=parent_session_id
                    )
                )
                if record.get("agent") == "codex_judge"
                and record.get("title") == source_title
                and record.get("status") == "completed"
                and str(record.get("output") or "").strip()
            ),
            "",
        )
        if source and source not in text:
            additions.append(
                "[System-authoritative raw judgment for format repair]\n"
                "The exact collected packet follows. It supersedes any "
                "placeholder claiming the packet was unavailable.\n"
                f"{source}"
            )

    if not additions:
        return args
    enriched = "\n\n".join((text, *additions)).strip()
    result = dict(args)
    if child_args is not None:
        child_args["input"] = enriched
        result["args"] = child_args
    else:
        result["args"] = {"input": enriched}
    return result


def _observe_dispatch_bookkeeping(
    result: object,
    *,
    args: object,
    conversation_id: str | None,
    agent: object,
    title: object,
    get_subagent_work: Any,
    append_dispatch: Any,
    record_tool_dispatch_exception: Any,
) -> object:
    """Observe a returned native send without changing its outcome."""

    if isinstance(result, str) and result.startswith("Error:"):
        # This is an explicit tool-level non-launch result, not a malformed
        # successful handle and not a ledger-observer failure.
        return result
    native_send_status = "returned"
    try:
        payload = json.loads(result)
        child_session_id = payload.get("conversation_id")
        entry = (
            get_subagent_work(child_session_id)
            if isinstance(child_session_id, str)
            else None
        )
        if (
            payload.get("status") != "launching"
            or entry is None
            or not isinstance(agent, str)
            or not isinstance(title, str)
        ):
            return result
        native_send_status = "launched"
        raw_handoff = args.get("args") if isinstance(args, dict) else None
        handoff = (
            raw_handoff.get("input")
            if isinstance(raw_handoff, dict)
            else raw_handoff
        )
        handoff_bytes = handoff.encode("utf-8") if isinstance(handoff, str) else b""
        append_dispatch(
            {
                "parent_session_id": conversation_id,
                "child_session_id": child_session_id,
                "work_id": entry.work_id,
                "agent": agent,
                "title": title,
                "handoff_bytes": len(handoff_bytes),
                "handoff_sha256": hashlib.sha256(handoff_bytes).hexdigest(),
            }
        )
    except _DISPATCH_BOOKKEEPING_ERRORS as exc:
        diagnostic = record_tool_dispatch_exception(
            parent_session_id=str(conversation_id or ""),
            title=str(title or ""),
            phase="ledger_observer",
            native_send_status=native_send_status,
            exc=exc,
        )
        logging.getLogger(__name__).warning(
            "dispatch ledger observer failed; turn=%s title=%s "
            "native_send_status=%s error=%s",
            conversation_id or "",
            title or "",
            native_send_status,
            diagnostic["reason"],
        )
    return result


def latch_unknown_opus_completion(
    parent_session_id: str,
    *,
    title: str,
    child_session_id: str,
    child_state: str,
    append_collection: Any,
) -> str:
    """Record one terminal unknown-completion result and deduplicate retries."""

    existing = _unknown_opus_dispatches.get(parent_session_id)
    if existing is not None:
        return existing
    failure = (
        f"{_OPUS_UNKNOWN_COMPLETION_PREFIX}: ReadTimeout; "
        f"child={child_session_id or 'unknown'}; {child_state}. "
        "Retry is latched until `/quit` reaps the run."
    )
    _unknown_opus_dispatches[parent_session_id] = failure
    try:
        append_collection(
            {
                "parent_session_id": parent_session_id,
                "child_session_id": child_session_id,
                "work_id": f"unknown-{child_session_id or 'completion'}",
                "agent": "opus_auditor",
                "title": title,
                "status": "failed",
                "output": failure,
            }
        )
    except OSError:
        pass
    return failure


def _runner_session_request(
    method: str,
    path: str,
) -> tuple[str, str] | None:
    """Classify runner routes that initialize or clean session-local state."""

    parts = path.split("/")
    if len(parts) < 4 or parts[1:3] != ["v1", "sessions"]:
        return None
    session_id = unquote(parts[3])
    if not session_id or "/" in session_id:
        return None
    if method == "DELETE" and len(parts) == 4:
        return "cleanup", session_id
    if len(parts) < 5:
        return None
    if method == "GET" and parts[4:] == ["stream"]:
        return "initialize", session_id
    if method == "POST" and parts[4:] in (["events"], ["mcp", "execute"]):
        return "initialize", session_id
    return None


def ensure_parent_inbox(session_id: str) -> asyncio.Queue[dict[str, object]]:
    """Idempotently create the runner-local queue for one parent session."""

    if not session_id:
        raise ValueError("parent session id is required")
    from omnigent.runner import app as runner_app

    return runner_app._session_inboxes_ref.setdefault(
        session_id,
        asyncio.Queue(),
    )


def _install_runner_session_inbox_initialization() -> None:
    """Provision browser sessions before their first turn or MCP call."""

    from omnigent.runner import app as runner_app

    original_create_runner_app = runner_app.create_runner_app
    if getattr(
        original_create_runner_app,
        "__triple_stamp_parent_inbox_init__",
        False,
    ):
        return

    def guarded_create_runner_app(*args: object, **kwargs: object) -> Any:
        application = original_create_runner_app(*args, **kwargs)
        server_client = kwargs.get("server_client")

        @application.middleware("http")
        async def parent_inbox_lifecycle(request: Any, call_next: Any) -> Any:
            operation = _runner_session_request(
                str(request.method),
                str(request.url.path),
            )
            if operation is not None and operation[0] == "initialize":
                ensure_parent_inbox(operation[1])
            active_child_ids: list[str] = []
            if operation is not None and operation[0] == "cleanup":
                active_child_ids = [
                    str(entry.child_session_id)
                    for entry in runner_app.list_subagent_work(operation[1])
                    if getattr(entry, "status", "") in {
                        "launching",
                        "running",
                        "waiting",
                    }
                ]
            response = await call_next(request)
            if (
                operation is not None
                and operation[0] == "cleanup"
                and response.status_code < 400
            ):
                session_id = operation[1]
                from triple_stamp_runtime_state import tombstone_parent_session

                tombstone_parent_session(session_id)
                if server_client is not None:
                    for child_id in active_child_ids:
                        try:
                            await server_client.delete(
                                f"/v1/sessions/{child_id}",
                                timeout=30.0,
                            )
                        except Exception as exc:  # noqa: BLE001 - cleanup is best effort
                            logging.getLogger(__name__).warning(
                                "failed to cancel child %s for deleted parent %s: %s",
                                child_id,
                                session_id,
                                type(exc).__name__,
                            )
                runner_app._session_inboxes_ref.pop(session_id, None)
                _parent_inbox_failures.pop(session_id, None)
                _unknown_opus_dispatches.pop(session_id, None)
                for key, wake_task in tuple(_cursor_parent_wake_tasks.items()):
                    if key[0] == session_id:
                        _cursor_parent_wake_tasks.pop(key, None)
                        wake_task.cancel()
            return response

        # ``add_middleware`` prepends. Move this layer behind the runner's
        # existing authentication middleware so rejected local requests cannot
        # allocate session queues.
        application.user_middleware.append(application.user_middleware.pop(0))
        application.state.triple_stamp_parent_inbox_init = True
        return application

    guarded_create_runner_app.__triple_stamp_parent_inbox_init__ = True
    guarded_create_runner_app.__triple_stamp_original__ = original_create_runner_app
    runner_app.create_runner_app = guarded_create_runner_app


def _install_parent_inbox_probe() -> None:
    """Add a non-draining readiness probe to the existing inbox read path."""

    from omnigent.runner import tool_dispatch as dispatch

    original_async_inbox = dispatch._execute_async_inbox_tool
    if getattr(
        original_async_inbox,
        "__triple_stamp_parent_inbox_probe__",
        False,
    ):
        return

    async def guarded_async_inbox(*call_args: object, **call_kwargs: object) -> str:
        tool_name = call_args[0] if call_args else call_kwargs.get("tool_name")
        arguments = call_args[1] if len(call_args) > 1 else call_kwargs.get("args")
        session_inbox = call_kwargs.get("session_inbox")
        if (
            tool_name == "sys_read_inbox"
            and isinstance(arguments, dict)
            and arguments.get(PARENT_INBOX_PROBE_KEY) == PARENT_INBOX_PROBE_VALUE
        ):
            if session_inbox is None:
                return "Error: sys_read_inbox requires parent session inbox"
            return PARENT_INBOX_READY
        return await original_async_inbox(*call_args, **call_kwargs)

    guarded_async_inbox.__triple_stamp_parent_inbox_probe__ = True
    guarded_async_inbox.__triple_stamp_original__ = original_async_inbox
    dispatch._execute_async_inbox_tool = guarded_async_inbox


def _cursor_data_root(run_dir: Path) -> Path:
    """Return Cursor's isolated data root, never the runner provider HOME."""

    raw_data = os.environ.get("CURSOR_DATA_DIR")
    raw_home = os.environ.get("TRIPLE_STAMP_CURSOR_HOME")
    candidate = (
        Path(raw_data)
        if raw_data
        else Path(raw_home) / ".cursor"
        if raw_home
        else run_dir / "home/.cursor"
    )
    try:
        candidate.resolve().relative_to(run_dir.resolve())
    except (OSError, ValueError):
        return run_dir / "home/.cursor"
    return candidate


def _latest_dispatch_for_child(
    child_session_id: str,
    dispatches: list[dict[str, Any]],
) -> dict[str, Any]:
    for record in reversed(dispatches):
        if (
            record.get("agent") == "cursor_workhorse"
            and record.get("child_session_id") == child_session_id
        ):
            return record
    return {}


def _dispatch_for_bridge(
    bridge_dir: Path,
    dispatches: list[dict[str, Any]],
) -> dict[str, Any]:
    bridge_name = bridge_dir.name
    for record in reversed(dispatches):
        child = str(record.get("child_session_id") or "")
        if (
            record.get("agent") == "cursor_workhorse"
            and child
            and hashlib.sha256(child.encode("utf-8")).hexdigest()[:32]
            == bridge_name
        ):
            return record
    return {}


def _startup_state(bridge_dir: Path) -> tuple[str, str, str]:
    startup = bridge_dir / "triple-stamp-startup.json"
    try:
        payload = json.loads(startup.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return "", "", ""
    if not isinstance(payload, dict):
        return "", "", ""
    return (
        str(payload.get("state") or ""),
        str(payload.get("reason") or ""),
        str(payload.get("cursor_session_id") or ""),
    )


def _cursor_transcript_snapshot(
    child_session_id: str,
    bridge_dir: Path | None = None,
) -> dict[str, Any]:
    """Read the latest Cursor turn tied to an Omnigent child identity."""

    raw_run_dir = os.environ.get("TRIPLE_STAMP_RUN_DIR", "")
    if not raw_run_dir or not child_session_id:
        return {
            "output": "",
            "diagnostic": "cursor transcript lookup lacks run/child id",
            "seen": False,
            "active": False,
            "complete": False,
            "turn_id": "",
            "fingerprint": "",
        }
    run_dir = Path(raw_run_dir)
    bridge_id = hashlib.sha256(child_session_id.encode("utf-8")).hexdigest()[:32]
    bridge = bridge_dir or (
        run_dir
        / "tmp"
        / f"omnigent-{os.getuid()}"
        / "cursor-native"
        / bridge_id
    )
    forwarder = bridge / "cursor_forwarder.json"
    process_state, process_reason, startup_session_id = _startup_state(bridge)
    cursor_session_id = startup_session_id
    metadata_source = "startup" if cursor_session_id else "none"
    try:
        metadata = json.loads(forwarder.read_text(encoding="utf-8"))
        store_path = Path(str(metadata["store_path"]))
        cursor_session_id = store_path.parent.name
        metadata_source = "forwarder"
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        pass

    project_root = _cursor_data_root(run_dir) / "projects"
    if cursor_session_id:
        candidates = sorted(
            project_root.glob(
                f"*/agent-transcripts/{cursor_session_id}/{cursor_session_id}.jsonl"
            )
        )
    else:
        # The first Cursor generation may briefly precede its forwarder file,
        # so the historical single-transcript fallback remains useful there.
        # It is unsafe after any earlier Cursor dispatch: run-00jfpj73 cycle 2
        # started while cycle 1 was the sole transcript and incorrectly claimed
        # cycle 1's completed packet before its own forwarder identity appeared.
        from triple_stamp_runtime_state import read_dispatches

        dispatches = read_dispatches()
        current_index = next(
            (
                index
                for index in range(len(dispatches) - 1, -1, -1)
                if dispatches[index].get("agent") == "cursor_workhorse"
                and dispatches[index].get("child_session_id") == child_session_id
            ),
            -1,
        )
        current_dispatch = (
            dispatches[current_index] if current_index >= 0 else {}
        )
        parent_session_id = str(
            current_dispatch.get("parent_session_id") or ""
        )
        has_prior_cursor = current_index > 0 and any(
            record.get("agent") == "cursor_workhorse"
            and str(record.get("parent_session_id") or "")
            == parent_session_id
            for record in dispatches[:current_index]
        )
        has_foreign_cursor = any(
            record.get("agent") == "cursor_workhorse"
            and str(record.get("parent_session_id") or "")
            != parent_session_id
            for record in dispatches
        )
        candidates = (
            []
            if has_prior_cursor or has_foreign_cursor
            else sorted(project_root.glob("*/agent-transcripts/*/*.jsonl"))
        )
        if len(candidates) == 1:
            cursor_session_id = candidates[0].parent.name
            metadata_source = "single-isolated-transcript"
        else:
            candidates = []
            if has_prior_cursor:
                metadata_source = "forwarder-required-after-prior-dispatch"
            elif has_foreign_cursor:
                metadata_source = "forwarder-required-after-parent-dispatch"

    snapshots: list[tuple[int, int, str, bytes]] = []
    for transcript in candidates:
        try:
            data = transcript.read_bytes()
            stat = transcript.stat()
        except OSError:
            continue
        snapshots.append((stat.st_mtime_ns, stat.st_size, str(transcript), data))

    latest = ""
    recovered_parts: list[str] = []
    prompt_line = -1
    success_line = -1
    turn_ended_status = ""
    turn_ended_error = ""
    recoverable_complete = False
    active = False
    complete = False
    malformed = 0
    selected_path = ""
    selected_mtime = 0
    selected_size = 0
    line_count = 0
    if snapshots:
        selected_mtime, selected_size, selected_path, selected_data = max(snapshots)
        lines = selected_data.decode("utf-8", errors="replace").splitlines()
        line_count = len(lines)
        for index, line in enumerate(lines):
            try:
                payload = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                malformed += 1
                continue
            if not isinstance(payload, dict):
                continue
            role = payload.get("role")
            message = payload.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if role == "user" and isinstance(content, list):
                prompt_line = index
                success_line = -1
                latest = ""
                recovered_parts = []
                turn_ended_status = ""
                turn_ended_error = ""
                recoverable_complete = False
                active = False
                complete = False
                continue
            if prompt_line < 0:
                continue
            if role == "assistant" and isinstance(content, list):
                # Any assistant activity after a success sentinel invalidates
                # that sentinel. A later success sentinel must close this turn.
                complete = False
                success_line = -1
                has_tool = any(
                    isinstance(block, dict) and block.get("type") == "tool_use"
                    for block in content
                )
                text = "".join(
                    str(block.get("text", ""))
                    for block in content
                    if isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                ).strip()
                if text and (not recovered_parts or recovered_parts[-1] != text):
                    recovered_parts.append(text)
                if has_tool:
                    latest = ""
                    active = True
                elif text:
                    latest = text
                    active = False
                elif any(
                    isinstance(block, dict)
                    and block.get("type") in {"thinking", "tool_result"}
                    for block in content
                ):
                    active = True
            elif role in {"tool", "tool_result"}:
                complete = False
                success_line = -1
                latest = ""
                active = True

            if payload.get("type") == "turn_ended":
                turn_ended_status = str(payload.get("status") or "")
                turn_ended_error = str(payload.get("error") or "")
                recoverable_complete = (
                    turn_ended_status != "success"
                    and bool(latest)
                    and not active
                )
                complete = (
                    turn_ended_status == "success"
                    and bool(latest)
                    and not active
                )
                success_line = index if complete else -1

    recovered_output = "\n\n".join(recovered_parts).strip()
    observed_output = latest or recovered_output
    turn_id = (
        hashlib.sha256(
            f"{cursor_session_id}|{selected_path}|{prompt_line}".encode()
        ).hexdigest()
        if prompt_line >= 0 and cursor_session_id
        else ""
    )
    fingerprint = hashlib.sha256(
        (
            f"{selected_path}|{selected_mtime}|{selected_size}|{line_count}|"
            f"{prompt_line}|{success_line}|{turn_ended_status}|"
            f"{hashlib.sha256(observed_output.encode()).hexdigest()}|"
            f"{process_state}|{process_reason}"
        ).encode()
    ).hexdigest()
    diagnostic = (
        f"cursor bridge={bridge_id} cursor_session={cursor_session_id or 'unknown'} "
        f"metadata_source={metadata_source} forwarder_present={forwarder.is_file()} "
        f"transcripts={len(candidates)} prompt_line={prompt_line} "
        f"turn_ended_status={turn_ended_status or 'none'} "
        f"turn_ended_success={complete} active={active} "
        f"assistant_chars={len(observed_output)} lines={line_count} "
        f"malformed_lines={malformed} process_state={process_state or 'unknown'} "
        f"process_reason={json.dumps(process_reason)}"
    )
    return {
        "output": latest,
        "recovered_output": recovered_output,
        "recoverable_complete": recoverable_complete,
        "diagnostic": diagnostic,
        "seen": bool(snapshots),
        "active": active,
        "complete": complete,
        "turn_id": turn_id,
        "fingerprint": fingerprint,
        "turn_ended_status": turn_ended_status,
        "turn_ended_error": turn_ended_error,
        "process_state": process_state,
        "process_reason": process_reason,
    }


def _cursor_transcript_output(
    child_session_id: str,
) -> tuple[str, str, bool, bool, bool]:
    """Compatibility view used by direct lifecycle tests."""

    snapshot = _cursor_transcript_snapshot(child_session_id)
    return (
        str(snapshot["output"]),
        str(snapshot["diagnostic"]),
        bool(snapshot["seen"]),
        bool(snapshot["active"]),
        bool(snapshot["complete"]),
    )


def _observe_cursor_lifecycle(
    dispatch: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Advance one generation from transcript evidence without waiting."""

    from triple_stamp_runtime_state import mutate_cursor_lifecycle

    child = str(dispatch.get("child_session_id") or "")
    work_id = str(dispatch.get("work_id") or "")
    now_ns = time.time_ns()

    def update(generation: dict[str, Any], state: dict[str, Any]) -> None:
        if generation.get("delivery_committed"):
            return
        fingerprint = str(snapshot.get("fingerprint") or "")
        if fingerprint and fingerprint != generation.get("last_fingerprint"):
            generation["last_fingerprint"] = fingerprint
            generation["last_progress_at_ns"] = now_ns

        turn_id = str(snapshot.get("turn_id") or "")
        prior_turn_ids = {
            str(other.get("completed_turn_id") or "")
            for other in state.get("generations", {}).values()
            if isinstance(other, dict)
            and other.get("delivery_committed")
            and other is not generation
        }
        complete = (
            (
                bool(snapshot.get("complete"))
                or bool(snapshot.get("recoverable_complete"))
            )
            and bool(snapshot.get("output"))
            and bool(turn_id)
            and turn_id not in prior_turn_ids
        )
        if complete:
            if generation.get("candidate_fingerprint") != fingerprint:
                generation["candidate_fingerprint"] = fingerprint
                generation["candidate_since_ns"] = now_ns
                generation["terminal_phase"] = "active"
                return
            stable_ns = int(_CURSOR_COMPLETION_STABLE_S * 1_000_000_000)
            if now_ns - int(generation.get("candidate_since_ns") or 0) >= stable_ns:
                generation["completed_turn_id"] = turn_id
                generation["terminal_status"] = "completed"
                generation["terminal_output"] = str(snapshot["output"])
                generation["terminal_phase"] = "ready"
            return

        generation["candidate_fingerprint"] = ""
        generation["candidate_since_ns"] = 0
        generation["terminal_phase"] = "active"
        generation["terminal_status"] = ""
        generation["terminal_output"] = ""

        turn_ended_status = str(snapshot.get("turn_ended_status") or "")
        if turn_ended_status and turn_ended_status != "success":
            recovered = str(snapshot.get("recovered_output") or "")
            retry_note = _cursor_retry_note(str(generation.get("title") or ""))
            generation["terminal_status"] = "failed"
            generation["terminal_output"] = (
                "CURSOR_WORKER_FAILED: Cursor turn ended with status "
                f"{turn_ended_status} before a complete packet; "
                f"error={json.dumps(str(snapshot.get('turn_ended_error') or ''))}; "
                f"recovered_assistant_output={json.dumps(recovered[:4000])}; "
                f"{snapshot['diagnostic']}"
                f"{retry_note}"
            )
            generation["terminal_phase"] = "ready"
            return

        process_state = str(snapshot.get("process_state") or "")
        process_reason = str(snapshot.get("process_reason") or "")
        if process_state in {"failed", "exited"} and not process_reason.endswith(
            "(exit 0)"
        ):
            generation["terminal_status"] = "failed"
            generation["terminal_output"] = (
                "CURSOR_WORKER_FAILED: Cursor process ended before a successful "
                f"turn_ended result; {snapshot['diagnostic']}"
            )
            generation["terminal_phase"] = "ready"
            return

        started_ns = int(generation.get("started_at_ns") or now_ns)
        last_progress_ns = int(generation.get("last_progress_at_ns") or started_ns)
        elapsed_s = max(0.0, (now_ns - started_ns) / 1_000_000_000)
        inactive_s = max(0.0, (now_ns - last_progress_ns) / 1_000_000_000)
        timeout_kind = ""
        if elapsed_s >= _CURSOR_STAGE_ABSOLUTE_S:
            timeout_kind = "absolute"
        elif inactive_s >= _CURSOR_STAGE_INACTIVITY_S:
            timeout_kind = "inactivity"
        if timeout_kind:
            retry_note = _cursor_retry_note(str(generation.get("title") or ""))
            generation["terminal_status"] = "failed"
            generation["terminal_output"] = (
                f"CURSOR_WORKER_TIMEOUT: kind={timeout_kind} "
                f"elapsed_s={elapsed_s:.3f} inactivity_s={inactive_s:.3f} "
                f"inactivity_limit_s={_CURSOR_STAGE_INACTIVITY_S:.3f} "
                f"absolute_limit_s={_CURSOR_STAGE_ABSOLUTE_S:.3f}; "
                f"{snapshot['diagnostic']}"
                f"{retry_note}"
            )
            generation["terminal_phase"] = "ready"

    return mutate_cursor_lifecycle(child, work_id, update)


def _incomplete_cursor_packet(payload: dict[str, object]) -> bool:
    if payload.get("type") != "sub_agent":
        return False
    if str(payload.get("agent") or payload.get("tool_name") or "") != "cursor_workhorse":
        return False
    output = payload.get("output")
    return (
        payload.get("_triple_stamp_pending") is True
        or not isinstance(output, str)
        or not output.strip()
        or output.strip() == _NO_OUTPUT
        or output.startswith(
            ("CURSOR_FINALIZATION_REQUIRED:", "CURSOR_WORKER_STALLED:")
        )
    )


def _packet_identity(payload: dict[str, object]) -> tuple[str, str]:
    child = str(
        payload.get("conversation_id")
        or payload.get("task_id")
        or payload.get("handle_id")
        or ""
    )
    return child, str(payload.get("work_id") or "")


def _enrich_packet(
    payload: dict[str, object],
    dispatches: list[dict[str, Any]],
) -> dict[str, object]:
    child, work_id = _packet_identity(payload)
    observed = next(
        (
            record
            for record in reversed(dispatches)
            if (work_id and record.get("work_id") == work_id)
            or (child and record.get("child_session_id") == child)
        ),
        {},
    )
    enriched = dict(payload)
    enriched["agent"] = payload.get("agent") or observed.get("agent")
    enriched["title"] = payload.get("title") or observed.get("title")
    enriched["conversation_id"] = child or observed.get("child_session_id")
    enriched["work_id"] = work_id or observed.get("work_id")
    return enriched


def _restore_evaluated_packet_identity(
    evaluated: dict[str, object],
    source: dict[str, object],
) -> dict[str, object]:
    """Carry collection identity across Omnigent's evaluation boundary."""

    restored = dict(evaluated)
    child, work_id = _packet_identity(source)
    for field in ("type", "agent", "tool_name", "title", "status"):
        if source.get(field) is not None:
            restored[field] = source[field]
    if child:
        restored["conversation_id"] = child
        restored["child_session_id"] = child
    if work_id:
        restored["work_id"] = work_id
    agent = str(restored.get("agent") or restored.get("tool_name") or "")
    if (
        agent == "opus_auditor"
        and restored.get("status") == "completed"
        and not child
    ):
        restored.update(
            status="failed",
            output=(
                "PIPELINE_INFRASTRUCTURE_ERROR: completed Opus packet lacked "
                "the child identity required for exact-child observation"
            ),
        )
    return restored


def _rearm_legacy_packet(
    payload: dict[str, object],
    runner_app: Any,
) -> dict[str, object]:
    """Requeue a legacy empty packet and reopen its delivery bit."""

    from triple_stamp_runtime_state import mutate_cursor_lifecycle

    child, work_id = _packet_identity(payload)
    entry = runner_app.get_subagent_work(child) if child else None
    if entry is not None and (not work_id or getattr(entry, "work_id", None) == work_id):
        entry.status = "running"
        entry.output = None
        entry.completed_at = None
        entry.delivered = False
    if child and work_id:
        def update(generation: dict[str, Any], _state: dict[str, Any]) -> None:
            generation["legacy_pending"] = True
            generation["delivery_committed"] = False
            if generation.get("terminal_phase") not in {"ready", "posting"}:
                generation["terminal_phase"] = "active"

        mutate_cursor_lifecycle(child, work_id, update)
    return {**payload, "_triple_stamp_pending": True}


def _pending_notice(payload: dict[str, object]) -> str:
    child, work_id = _packet_identity(payload)
    title = str(payload.get("title") or "cursor")
    return (
        f"{_PENDING_PREFIX} {title} child={child or 'unknown'} "
        f"work={work_id or 'unknown'} is still active; completion will wake "
        "the parent after a stable successful turn_ended result.]"
    )


def install_parent_inbox_guard() -> None:
    """Install source lifecycle gating and a prompt cancellation-safe inbox."""

    from omnigent import cursor_native_forwarder as cursor_forwarder
    from omnigent import cursor_native_status as cursor_status
    from omnigent import _native_post_delivery as native_delivery
    from omnigent.runner import app as runner_app
    from omnigent.runner import tool_dispatch as dispatch
    from triple_stamp_runtime_state import (
        add_opus_effort_observation,
        add_opus_internal_mcp_observation,
        append_collection,
        append_dispatch,
        attest_codex_stamp,
        mutate_cursor_lifecycle,
        parse_dispatch_title,
        read_attempt_collections,
        read_attempt_dispatches,
        read_collections,
        read_cursor_lifecycle,
        read_dispatches,
        release_retry_reservation,
        record_terminal_failure,
        record_tool_dispatch_exception,
    )

    _install_codex_voice_environment()
    _install_runner_session_inbox_initialization()

    original_chats_root = cursor_forwarder._cursor_chats_root
    if not getattr(original_chats_root, "__triple_stamp_cursor_home__", False):
        def isolated_cursor_chats_root() -> Path:
            raw_run_dir = os.environ.get("TRIPLE_STAMP_RUN_DIR", "")
            if not raw_run_dir:
                return original_chats_root()
            return _cursor_data_root(Path(raw_run_dir)) / "chats"

        isolated_cursor_chats_root.__triple_stamp_cursor_home__ = True
        isolated_cursor_chats_root.__triple_stamp_original__ = original_chats_root
        cursor_forwarder._cursor_chats_root = isolated_cursor_chats_root

    original_count = cursor_status.count_turn_ends
    if not getattr(original_count, "__triple_stamp_lifecycle_gate__", False):
        original_posted = cursor_status.read_posted_count

        def gated_count_turn_ends(bridge_dir: Path) -> int:
            raw_count = original_count(bridge_dir)
            posted_count = original_posted(bridge_dir)
            record = _dispatch_for_bridge(bridge_dir, read_dispatches())
            if not record:
                return raw_count
            snapshot = _cursor_transcript_snapshot(
                str(record.get("child_session_id") or ""),
                bridge_dir,
            )
            generation = _observe_cursor_lifecycle(record, snapshot)
            if generation.get("delivery_committed"):
                return posted_count
            if generation.get("terminal_phase") == "ready":
                return max(raw_count, posted_count + 1)
            # Intermediate stop markers remain unacknowledged. The existing
            # forwarder checks again outside every MCP request.
            return posted_count

        gated_count_turn_ends.__triple_stamp_lifecycle_gate__ = True
        gated_count_turn_ends.__triple_stamp_original__ = original_count
        cursor_status.count_turn_ends = gated_count_turn_ends

    original_status_post = cursor_forwarder._post_external_session_status
    if not getattr(original_status_post, "__triple_stamp_lifecycle_gate__", False):
        async def guarded_status_post(
            client: Any,
            *,
            session_id: str,
            status: str,
        ) -> None:
            record = _latest_dispatch_for_child(session_id, read_dispatches())
            if not record:
                await original_status_post(client, session_id=session_id, status=status)
                return
            work_id = str(record.get("work_id") or "")
            generation = read_cursor_lifecycle(session_id, work_id)
            if generation.get("delivery_committed"):
                return
            now_ns = time.time_ns()
            claim_token = hashlib.sha256(
                f"{session_id}|{work_id}|{now_ns}|{os.getpid()}".encode()
            ).hexdigest()
            claimed: dict[str, Any] = {}

            def claim(current: dict[str, Any], _state: dict[str, Any]) -> None:
                nonlocal claimed
                prior_claim_ns = int(current.get("post_claimed_at_ns") or 0)
                stale = (
                    now_ns - prior_claim_ns
                    >= int(_CURSOR_POST_CLAIM_S * 1_000_000_000)
                )
                if current.get("delivery_committed"):
                    claimed = {"already": True}
                    return
                if current.get("terminal_phase") == "posting" and not stale:
                    claimed = {}
                    return
                if current.get("terminal_phase") not in {"ready", "posting"}:
                    claimed = {}
                    return
                current["terminal_phase"] = "posting"
                current["post_claim_token"] = claim_token
                current["post_claimed_at_ns"] = now_ns
                claimed = dict(current)

            mutate_cursor_lifecycle(session_id, work_id, claim)
            if claimed.get("already"):
                return
            if not claimed:
                raise RuntimeError(
                    "suppressed premature Cursor terminal status without "
                    "stable turn_ended evidence"
                )
            terminal_status = str(claimed.get("terminal_status") or "")
            outgoing_status = "idle" if terminal_status == "completed" else "failed"
            output = str(claimed.get("terminal_output") or "")
            if not output:
                raise RuntimeError("Cursor terminal lifecycle lacks output")
            try:
                response = await client.post(
                    f"/v1/sessions/{session_id}/events",
                    json={
                        "type": "external_session_status",
                        "data": {"status": outgoing_status, "output": output},
                    },
                )
                response.raise_for_status()
            except BaseException:
                def release(current: dict[str, Any], _state: dict[str, Any]) -> None:
                    if (
                        not current.get("delivery_committed")
                        and current.get("post_claim_token") == claim_token
                    ):
                        current["terminal_phase"] = "ready"
                        current["post_claim_token"] = ""

                mutate_cursor_lifecycle(session_id, work_id, release)
                raise

            def confirm(current: dict[str, Any], _state: dict[str, Any]) -> None:
                if current.get("post_claim_token") == claim_token:
                    current["post_confirmed_at_ns"] = time.time_ns()
                    if current.get("delivery_committed"):
                        current["terminal_phase"] = "delivered"

            mutate_cursor_lifecycle(session_id, work_id, confirm)

        guarded_status_post.__triple_stamp_lifecycle_gate__ = True
        guarded_status_post.__triple_stamp_original__ = original_status_post
        cursor_forwarder._post_external_session_status = guarded_status_post

    original_native_status_post = native_delivery.post_external_session_status
    if not getattr(
        original_native_status_post,
        "__triple_stamp_cursor_delivery__",
        False,
    ):
        async def guarded_native_status_post(
            client: Any,
            *,
            session_id: str,
            status: str,
            output: str | None = None,
            **kwargs: Any,
        ) -> None:
            record = _latest_dispatch_for_child(
                session_id,
                read_dispatches(),
            )
            if not record or status not in {"idle", "failed"}:
                await original_native_status_post(
                    client,
                    session_id=session_id,
                    status=status,
                    output=output,
                    **kwargs,
                )
                return
            try:
                await cursor_forwarder._post_external_session_status(
                    client,
                    session_id=session_id,
                    status=status,
                )
                return
            except RuntimeError as exc:
                if str(exc).startswith(
                    "suppressed premature Cursor terminal status"
                ):
                    # A stop/usage hook can precede stable transcript evidence.
                    # A later transcript-forwarder pass owns the real terminal
                    # edge; acknowledging this hook prevents a hot retry loop.
                    return
                raise
            except Exception as exc:  # noqa: BLE001 - inspect structured 503
                response = getattr(exc, "response", None)
                try:
                    body = response.json() if response is not None else {}
                except Exception:  # noqa: BLE001 - best-effort response parse
                    body = {}
                missing_work_entry = (
                    getattr(response, "status_code", None) == 503
                    and isinstance(body, dict)
                    and body.get("error") == "subagent_delivery_not_confirmed"
                    and body.get("reason") == "missing_work_entry"
                )
                if not missing_work_entry:
                    raise

            work_id = str(record.get("work_id") or "")
            generation = read_cursor_lifecycle(session_id, work_id)
            desired = str(generation.get("terminal_status") or "")
            terminal_output = str(generation.get("terminal_output") or "")
            parent_session_id = str(record.get("parent_session_id") or "")
            ensure_parent_inbox(parent_session_id)
            entry = runner_app.get_subagent_work(session_id)
            if entry is None:
                entry = runner_app.register_subagent_work(
                    parent_session_id=parent_session_id,
                    child_session_id=session_id,
                    agent=str(record.get("agent") or "cursor_workhorse"),
                    title=str(record.get("title") or ""),
                )
            entry.parent_session_id = parent_session_id
            entry.work_id = work_id
            entry.agent = str(record.get("agent") or "cursor_workhorse")
            entry.title = str(record.get("title") or "")
            try:
                # Retry once through the server after reconstructing the exact
                # work entry. This preserves its normal parent-wake scheduling,
                # unlike pushing directly into the runner-local queue.
                await cursor_forwarder._post_external_session_status(
                    client,
                    session_id=session_id,
                    status=(
                        "idle" if desired == "completed" else "failed"
                    ),
                )
                return
            except Exception:  # noqa: BLE001 - one bounded recovery attempt
                acknowledgement = runner_app.mark_subagent_work_terminal(
                    session_id,
                    status=desired,
                    output=terminal_output,
                )
                if acknowledgement.delivered:
                    if (
                        acknowledgement.delivered_now
                        and acknowledgement.entry is not None
                    ):
                        parent_inbox = ensure_parent_inbox(parent_session_id)
                        notice = runner_app._format_subagent_wake_notice(
                            agent=entry.agent,
                            title=entry.title,
                            status=entry.status,
                            pending=parent_inbox.qsize(),
                        )
                        wake_started = time.monotonic()
                        try:
                            wake_delivered = await asyncio.wait_for(
                                runner_app._deliver_subagent_wake_post(
                                    client,
                                    parent_session_id,
                                    notice,
                                    created_by=entry.created_by,
                                ),
                                timeout=_CURSOR_PARENT_WAKE_DEADLINE_S,
                            )
                        except TimeoutError:
                            wake_delivered = False
                        if not wake_delivered:
                            def mark_wake_pending(
                                current: dict[str, Any],
                                _state: dict[str, Any],
                            ) -> None:
                                current["parent_wake_pending"] = True
                                current["parent_wake_attempts"] = 0

                            mutate_cursor_lifecycle(
                                session_id,
                                work_id,
                                mark_wake_pending,
                            )
                            _schedule_cursor_parent_wake_retry(
                                client=client,
                                parent_session_id=parent_session_id,
                                child_session_id=session_id,
                                work_id=work_id,
                                notice=notice,
                                created_by=entry.created_by,
                                started_at_monotonic=wake_started,
                            )
                    return
            reason = (
                "Cursor terminal delivery recovery exhausted after one "
                "reconstructed missing_work_entry retry; duplicate delivery "
                "loop stopped"
            )
            record_terminal_failure(
                "PIPELINE_INFRASTRUCTURE_ERROR",
                reason,
                stage=str(record.get("title") or ""),
                cycle=int(record.get("cycle") or 0),
                parent_session_id=parent_session_id,
            )

        guarded_native_status_post.__triple_stamp_cursor_delivery__ = True
        guarded_native_status_post.__triple_stamp_original__ = (
            original_native_status_post
        )
        native_delivery.post_external_session_status = guarded_native_status_post

    original_mark_terminal = runner_app.mark_subagent_work_terminal
    if not getattr(
        original_mark_terminal,
        "__triple_stamp_lifecycle_gate__",
        False,
    ):
        def guarded_mark_terminal(
            child_session_id: str,
            *,
            status: str,
            output: str | None,
        ) -> Any:
            record = _latest_dispatch_for_child(
                child_session_id,
                read_dispatches(),
            )
            if not record:
                return original_mark_terminal(
                    child_session_id,
                    status=status,
                    output=output,
                )
            work_id = str(record.get("work_id") or "")
            generation = read_cursor_lifecycle(child_session_id, work_id)
            desired = str(generation.get("terminal_status") or "")
            if generation.get("delivery_committed"):
                return runner_app._SubagentDeliveryAck(
                    entry=runner_app.get_subagent_work(child_session_id),
                    delivered=True,
                    delivered_now=False,
                    reason=runner_app._SUBAGENT_DELIVERY_ALREADY_DELIVERED,
                )
            if (
                generation.get("terminal_phase") not in {"ready", "posting"}
                or desired != status
                or not isinstance(output, str)
                or not output
            ):
                return runner_app._SubagentDeliveryAck(
                    entry=None,
                    delivered=False,
                    delivered_now=False,
                    reason=runner_app._SUBAGENT_DELIVERY_UNTRACKED,
                )
            acknowledgement = original_mark_terminal(
                child_session_id,
                status=status,
                output=output,
            )
            if acknowledgement.delivered:
                def commit(current: dict[str, Any], _state: dict[str, Any]) -> None:
                    current["delivery_committed"] = True
                    current["delivery_committed_at_ns"] = time.time_ns()
                    current["terminal_phase"] = "delivered"

                mutate_cursor_lifecycle(child_session_id, work_id, commit)
                title = str(record.get("title") or "")
                if _cursor_failure_requires_terminal_latch(
                    title,
                    status,
                    output,
                ):
                    parsed = parse_dispatch_title(title) or {}
                    record_terminal_failure(
                        "PIPELINE_INFRASTRUCTURE_ERROR",
                        output,
                        stage=title or "cursor",
                        cycle=int(parsed.get("cycle") or 0),
                        parent_session_id=str(
                            record.get("parent_session_id") or ""
                        ),
                    )
            return acknowledgement

        guarded_mark_terminal.__triple_stamp_lifecycle_gate__ = True
        guarded_mark_terminal.__triple_stamp_original__ = original_mark_terminal
        runner_app.mark_subagent_work_terminal = guarded_mark_terminal

    original_send = dispatch._execute_subagent_tool
    if not getattr(original_send, "__triple_stamp_inbox_guard__", False):
        async def reconcile_timed_out_opus(
            server_client: Any,
            *,
            parent_session_id: str,
            title: str,
        ) -> tuple[str, str]:
            child_id = ""
            child_state = "not_observed"
            try:
                response = await server_client.get(
                    f"/v1/sessions/{parent_session_id}/child_sessions",
                    params={
                        "limit": 100,
                        "order": "desc",
                        "tool": "opus_auditor",
                        "session_name": title,
                    },
                    timeout=5.0,
                )
                response.raise_for_status()
                body = response.json()
                rows = body.get("data") if isinstance(body, dict) else None
                if isinstance(rows, list):
                    match = next(
                        (
                            row
                            for row in rows
                            if isinstance(row, dict)
                            and row.get("tool") == "opus_auditor"
                            and row.get("session_name") == title
                        ),
                        None,
                    )
                    if isinstance(match, dict):
                        child_id = str(match.get("id") or "")
                        child_state = (
                            f"task={match.get('current_task_status') or 'unknown'},"
                            f"busy={bool(match.get('busy'))}"
                        )
            except Exception as exc:  # noqa: BLE001 - reconciliation is best effort
                child_state = f"reconciliation_failed={type(exc).__name__}"
            return child_id, child_state

        async def guarded_execute_subagent_tool(
            args: object,
            *,
            server_client: object = None,
            conversation_id: str | None = None,
            agent_spec: object = None,
            publish_event: object = None,
            session_inbox: asyncio.Queue[dict[str, object]] | None = None,
        ) -> str:
            if conversation_id in _parent_inbox_failures:
                return _parent_inbox_failures[conversation_id]
            agent = args.get("agent") if isinstance(args, dict) else None
            title = args.get("title") if isinstance(args, dict) else None
            if (
                agent == "opus_auditor"
                and conversation_id in _unknown_opus_dispatches
            ):
                return _unknown_opus_dispatches[conversation_id]
            parent_session_id = str(conversation_id or "")
            parsed = parse_dispatch_title(title)
            if parent_session_id and parsed is not None:
                dispatches = read_attempt_dispatches(
                    parent_session_id=parent_session_id
                )
                collections = read_attempt_collections(
                    parent_session_id=parent_session_id
                )
                active = (*dispatches, *collections)
                if (
                    agent == "cursor_workhorse"
                    and parsed["stage_id"] in {"cursor_grunt", "cursor_retry"}
                    and (
                        any(record.get("title") == title for record in active)
                        or any(
                            (
                                later := parse_dispatch_title(
                                    record.get("title")
                                )
                            )
                            is not None
                            and later["cycle"] == parsed["cycle"]
                            and (
                                (
                                    parsed["stage_id"] == "cursor_grunt"
                                    and later["stage_id"] != "cursor_grunt"
                                )
                                or (
                                    parsed["stage_id"] == "cursor_retry"
                                    and later["stage_id"]
                                    not in {"cursor_grunt", "cursor_retry"}
                                )
                            )
                            for record in active
                        )
                    )
                ):
                    return _DUPLICATE_CURSOR_DISPATCH
                if (
                    agent == "opus_auditor"
                    and parsed["stage_id"] == "audit_retry"
                    and any(
                        (
                            prior := parse_dispatch_title(
                                record.get("title")
                            )
                        )
                        is not None
                        and prior["stage_id"] == "audit_retry"
                        and prior["cycle"] == parsed["cycle"]
                        for record in active
                    )
                ):
                    return _DUPLICATE_OPUS_RETRY_DISPATCH
            if agent == "codex_judge":
                args = _codex_runtime_handoff(
                    args,
                    title=title,
                    parent_session_id=parent_session_id,
                    read_attempt_collections=read_attempt_collections,
                )
            try:
                result = await original_send(
                    args,
                    server_client=server_client,
                    conversation_id=conversation_id,
                    agent_spec=agent_spec,
                    publish_event=publish_event,
                    session_inbox=session_inbox,
                )
            except Exception as exc:
                diagnostic = record_tool_dispatch_exception(
                    parent_session_id=str(conversation_id or ""),
                    title=str(title or ""),
                    phase="native_send",
                    native_send_status="failed",
                    exc=exc,
                )
                logging.getLogger(__name__).warning(
                    "native sys_session_send failed; turn=%s title=%s error=%s",
                    conversation_id or "",
                    title or "",
                    diagnostic["reason"],
                )
                if (
                    agent != "opus_auditor"
                    or not conversation_id
                    or type(exc).__name__ != "ReadTimeout"
                ):
                    raise
                child_id, child_state = await reconcile_timed_out_opus(
                    server_client,
                    parent_session_id=conversation_id,
                    title=str(title or ""),
                )
                return latch_unknown_opus_completion(
                    conversation_id,
                    title=str(title or ""),
                    child_session_id=child_id,
                    child_state=child_state,
                    append_collection=append_collection,
                )
            if (
                isinstance(result, str)
                and result.startswith("Error:")
                and parent_session_id
                and parsed is not None
                and parsed["stage_id"] in {"cursor_retry", "audit_retry"}
            ):
                # Omnigent returns Error: text only on paths that did not post
                # the child turn (including paths that created then tore down a
                # child). No paid worker launched, so the policy reservation
                # may be retried. Exceptions and unknown outcomes stay latched.
                release_retry_reservation(
                    parent_session_id,
                    int(parsed["cycle"]),
                    stage=(
                        "cursor"
                        if parsed["stage_id"] == "cursor_retry"
                        else "opus"
                    ),
                    native_send_status="not_launched",
                )
            if result == _MISSING_PARENT_INBOX and conversation_id:
                terminal = _parent_inbox_failures.setdefault(
                    conversation_id,
                    _MISSING_PARENT_INBOX_TERMINAL,
                )
                logging.getLogger(__name__).error(
                    "parent inbox invariant failed for %s; future sends are blocked",
                    conversation_id,
                )
                return terminal
            return _observe_dispatch_bookkeeping(
                result,
                args=args,
                conversation_id=conversation_id,
                agent=agent,
                title=title,
                get_subagent_work=runner_app.get_subagent_work,
                append_dispatch=append_dispatch,
                record_tool_dispatch_exception=record_tool_dispatch_exception,
            )

        guarded_execute_subagent_tool.__triple_stamp_inbox_guard__ = True
        guarded_execute_subagent_tool.__triple_stamp_original__ = original_send
        dispatch._execute_subagent_tool = guarded_execute_subagent_tool

    _install_parent_inbox_probe()

    original_drain = dispatch._drain_inbox
    if not getattr(original_drain, "__triple_stamp_prompt_inbox__", False):
        async def guarded_drain_inbox(
            inbox: asyncio.Queue[dict[str, object]] | None,
            *,
            server_client: object = None,
            conversation_id: str | None = None,
        ) -> str:
            if inbox is None:
                return "Error: sys_read_inbox requires parent session inbox"
            if inbox.empty():
                return "Inbox is empty — no completed tasks."

            leased: list[dict[str, object]] = []
            while not inbox.empty():
                try:
                    leased.append(inbox.get_nowait())
                except asyncio.QueueEmpty:
                    break
            all_dispatches = read_dispatches()
            dispatches = read_dispatches(
                parent_session_id=str(conversation_id or "")
            )
            local_payloads: list[dict[str, object]] = []
            for payload in leased:
                child, work_id = _packet_identity(payload)
                owner = next(
                    (
                        row
                        for row in reversed(all_dispatches)
                        if (work_id and row.get("work_id") == work_id)
                        or (child and row.get("child_session_id") == child)
                    ),
                    {},
                )
                owner_parent = str(owner.get("parent_session_id") or "")
                if (
                    owner_parent
                    and conversation_id
                    and owner_parent != conversation_id
                ):
                    ensure_parent_inbox(owner_parent).put_nowait(payload)
                    continue
                local_payloads.append(payload)
            enriched = [
                _enrich_packet(payload, dispatches)
                for payload in local_payloads
            ]
            complete_cursor_ids = {
                _packet_identity(payload)
                for payload in enriched
                if payload.get("type") == "sub_agent"
                and str(payload.get("agent") or payload.get("tool_name") or "")
                == "cursor_workhorse"
                and not _incomplete_cursor_packet(payload)
            }

            items: list[str] = []
            processable: list[dict[str, object]] = []
            seen_complete: set[tuple[str, str]] = set()
            for payload in enriched:
                if _incomplete_cursor_packet(payload):
                    identity = _packet_identity(payload)
                    if identity in complete_cursor_ids:
                        continue
                    pending = _rearm_legacy_packet(payload, runner_app)
                    # Requeue before any await. Cancellation cannot strand it.
                    inbox.put_nowait(pending)
                    items.append(_pending_notice(pending))
                    continue
                identity = _packet_identity(payload)
                if (
                    payload.get("type") == "sub_agent"
                    and identity in seen_complete
                ):
                    continue
                if payload.get("type") == "sub_agent":
                    seen_complete.add(identity)
                processable.append(payload)

            index = 0
            try:
                while index < len(processable):
                    payload = processable[index]
                    if payload.get("type") == "terminal_idle":
                        items.append(dispatch._format_terminal_idle_item(payload))
                        index += 1
                        continue
                    evaluation = await dispatch._evaluate_subagent_inbox_output(
                        payload,
                        server_client=server_client,
                        conversation_id=conversation_id,
                    )
                    evaluated_payload = (
                        evaluation.payload
                        if evaluation.retry_original
                        else add_opus_effort_observation(
                            await add_opus_internal_mcp_observation(
                                _restore_evaluated_packet_identity(
                                    evaluation.payload,
                                    payload,
                                ),
                                server_client=server_client,
                            )
                        )
                    )
                    items.append(dispatch._format_async_task_item(evaluated_payload))
                    if evaluation.retry_original:
                        inbox.put_nowait(payload)
                    else:
                        dispatch._cleanup_drained_subagent_work(evaluated_payload)
                        if evaluated_payload.get("type") == "sub_agent":
                            child, _work_id = _packet_identity(evaluated_payload)
                            packet_output = evaluated_payload.get("output")
                            collected = {
                                "parent_session_id": conversation_id,
                                "child_session_id": child,
                                "work_id": evaluated_payload.get("work_id"),
                                "agent": evaluated_payload.get("agent")
                                or evaluated_payload.get("tool_name"),
                                "title": evaluated_payload.get("title"),
                                "status": evaluated_payload.get("status"),
                                "output": (
                                    packet_output
                                    if isinstance(packet_output, str)
                                    else ""
                                ),
                            }
                            if "internal_mcp_observation" in evaluated_payload:
                                collected["internal_mcp_observation"] = (
                                    evaluated_payload["internal_mcp_observation"]
                                )
                            if "opus_effort_observation" in evaluated_payload:
                                collected["opus_effort_observation"] = (
                                    evaluated_payload["opus_effort_observation"]
                                )
                            append_collection(collected)
                            attest_codex_stamp(collected)
                    index += 1
            except BaseException:
                # The current item and every not-yet-processed item remain
                # leased. Put them back synchronously before propagating cancel.
                for payload in processable[index:]:
                    inbox.put_nowait(payload)
                raise
            return (
                "\n\n".join(items)
                if items
                else "Inbox is empty — no completed tasks."
            )

        guarded_drain_inbox.__triple_stamp_prompt_inbox__ = True
        guarded_drain_inbox.__triple_stamp_original__ = original_drain
        guarded_drain_inbox.__triple_stamp_cursor_transcript__ = (
            _cursor_transcript_output
        )
        guarded_drain_inbox.__triple_stamp_inbox_time_contract_s__ = (
            _INBOX_TIME_CONTRACT_S
        )
        dispatch._drain_inbox = guarded_drain_inbox

    logging.getLogger(__name__).debug(
        "installed Cursor transcript lifecycle boundary"
    )
