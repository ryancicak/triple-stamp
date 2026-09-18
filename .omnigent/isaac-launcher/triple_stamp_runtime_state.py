"""Simple run-local routing ledger for the isolated supervisor."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

_LOGGER = logging.getLogger(__name__)
_DISPATCH_TITLE_PATTERNS = (
    (r"cursor-cycle-([1-4])", "cursor_grunt", ""),
    (r"audit-cycle-([1-4])", "audit", "opus"),
    (r"audit-cycle-([1-4])-web-([1-2])", "audit_web", "opus"),
    (r"audit-internal-([1-4])-([1-2])", "audit_internal", "codex"),
    (r"judge-cycle-([1-4])", "judge", "codex"),
    (r"judge-convergence-([1-4])", "judge_convergence", "codex"),
    (r"audit-format-repair-([1-4])", "audit_repair", "opus"),
    (
        r"audit-format-repair-([1-4])-web-([1-2])",
        "audit_repair_web",
        "opus",
    ),
    (r"judge-format-repair-([1-4])", "judge_repair", "codex"),
    (r"cursor-web-opus-([1-4])-([1-2])", "cursor_web", "opus"),
    (r"cursor-web-codex-([1-4])-([1-2])", "cursor_web", "codex"),
)
_HOP_STAGE_IDS = frozenset(
    {"audit_web", "audit_internal", "audit_repair_web", "cursor_web"}
)
_TOOL_DISPATCH_EXCEPTIONS = (
    AttributeError,
    IndexError,
    OSError,
    TypeError,
    ValueError,
)


def _validate_dispatch_title_patterns() -> None:
    """Fail at import if a parser row cannot satisfy its group contract."""

    for pattern, stage_id, _requester in _DISPATCH_TITLE_PATTERNS:
        expected = 2 if stage_id in _HOP_STAGE_IDS else 1
        actual = re.compile(pattern).groups
        if actual != expected:
            raise RuntimeError(
                f"dispatch title pattern for {stage_id} has {actual} groups; "
                f"expected {expected}"
            )


_validate_dispatch_title_patterns()


def parse_dispatch_title(title: object) -> dict[str, Any] | None:
    """Parse one canonical router title using the shared full-match table."""

    value = str(title or "")
    for pattern, stage_id, requester in _DISPATCH_TITLE_PATTERNS:
        match = re.fullmatch(pattern, value)
        if match is None:
            continue
        groups = match.groups()
        cycle_text, *hop_text = groups
        return {
            "stage_id": stage_id,
            "cycle": int(cycle_text),
            "hop": int(hop_text[0]) if hop_text else 0,
            "requester": requester,
        }
    return None


def _run_dir() -> Path | None:
    raw = os.environ.get("TRIPLE_STAMP_RUN_DIR")
    return Path(raw) if raw else None


def _parent_artifact_path(
    run_dir: Path,
    filename: str,
    parent_session_id: str = "",
) -> Path:
    """Return a parent-owned artifact path, preserving legacy single-run names."""

    if not parent_session_id:
        return run_dir / filename
    digest = hashlib.sha256(parent_session_id.encode("utf-8")).hexdigest()[:16]
    path = Path(filename)
    return run_dir / f"{path.stem}-{digest}{path.suffix}"


def _scope_parent_records(
    records: list[dict[str, Any]],
    parent_session_id: str,
) -> list[dict[str, Any]]:
    """Return only one parent's live ledger generation."""

    if not parent_session_id:
        return records
    scoped = [
        record
        for record in records
        if record.get("parent_session_id") == parent_session_id
    ]
    last_tombstone = max(
        (
            index
            for index, record in enumerate(scoped)
            if record.get("record_type") == "parent_tombstone"
        ),
        default=-1,
    )
    return scoped[last_tombstone + 1 :]


def _append(filename: str, payload: dict[str, Any]) -> None:
    run_dir = _run_dir()
    if run_dir is None:
        return
    data = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    fd = os.open(
        run_dir / filename,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o600,
    )
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _read(filename: str) -> list[dict[str, Any]]:
    run_dir = _run_dir()
    if run_dir is None:
        return []
    try:
        lines = (run_dir / filename).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _write_last_tool_dispatch_exception(payload: dict[str, Any]) -> None:
    """Best-effort atomic latest-value diagnostic alongside the history."""

    run_dir = _run_dir()
    if run_dir is None:
        return
    data = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    temporary = run_dir / f".last-tool-dispatch-exception-{os.getpid()}.tmp"
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, run_dir / "last-tool-dispatch-exception.json")
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def record_tool_dispatch_exception(
    *,
    parent_session_id: str,
    title: str,
    phase: str,
    native_send_status: str,
    exc: BaseException,
) -> dict[str, Any]:
    """Expose a sanitized native-send or ledger-observer failure."""

    record = {
        "turn_id": str(parent_session_id),
        "title": str(title),
        "phase": str(phase),
        "native_send_status": str(native_send_status),
        "reason": _observation_reason(exc),
        "observed_at_ns": time.time_ns(),
    }
    for writer in (
        lambda: _append("tool-dispatch-exceptions.jsonl", record),
        lambda: _write_last_tool_dispatch_exception(record),
    ):
        try:
            writer()
        except _TOOL_DISPATCH_EXCEPTIONS as diagnostic_exc:
            _LOGGER.warning(
                "tool dispatch diagnostic persistence failed; turn=%s "
                "title=%s error=%s",
                parent_session_id,
                title,
                _observation_reason(diagnostic_exc),
            )
    return record


def read_last_tool_dispatch_exception(
    *,
    parent_session_id: str = "",
    titles: list[str] | tuple[str, ...] = (),
    observed_after_ns: int = 0,
) -> dict[str, Any] | None:
    """Return the newest matching sanitized tool-dispatch diagnostic."""

    records = _read("tool-dispatch-exceptions.jsonl") + _read(
        "last-tool-dispatch-exception.json"
    )
    records.sort(key=lambda record: int(record.get("observed_at_ns") or 0))
    allowed_titles = {str(title) for title in titles}
    for record in reversed(records):
        if (
            parent_session_id
            and record.get("turn_id") != parent_session_id
        ):
            continue
        if allowed_titles and str(record.get("title") or "") not in allowed_titles:
            continue
        if int(record.get("observed_at_ns") or 0) < max(0, observed_after_ns):
            continue
        return dict(record)
    return None


def append_dispatch(payload: dict[str, Any]) -> None:
    """Record one worker generation after sys_session_send succeeds."""

    record = dict(payload)
    record.setdefault("dispatched_at_ns", time.time_ns())
    title = str(record.get("title") or "")
    parsed = parse_dispatch_title(title)
    if parsed is not None:
        record.update(parsed)
    prior = next(
        (
            item
            for item in read_dispatches(
                parent_session_id=str(record.get("parent_session_id") or "")
            )
            if item.get("agent") == record.get("agent")
            and item.get("title") == record.get("title")
        ),
        None,
    )
    if prior is not None and record.get("agent") != "opus_auditor":
        record["resume_child_session_id"] = str(
            prior.get("child_session_id") or ""
        )
    _append("routing-dispatches.jsonl", record)
    if record.get("agent") == "cursor_workhorse":
        begin_cursor_lifecycle(record)


def read_dispatches(parent_session_id: str = "") -> list[dict[str, Any]]:
    return _scope_parent_records(
        _read("routing-dispatches.jsonl"),
        parent_session_id,
    )


def _cursor_state_path(child_session_id: str) -> Path | None:
    run_dir = _run_dir()
    if run_dir is None or not child_session_id:
        return None
    digest = hashlib.sha256(child_session_id.encode("utf-8")).hexdigest()
    return run_dir / "cursor-lifecycle" / f"{digest}.json"


def _mutate_cursor_state(
    child_session_id: str,
    update: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """Atomically update one child's run-scoped Cursor lifecycle state."""

    path = _cursor_state_path(child_session_id)
    if path is None:
        return {}
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        state.setdefault("version", 1)
        state["child_session_id"] = child_session_id
        generations = state.setdefault("generations", {})
        if not isinstance(generations, dict):
            state["generations"] = {}
        update(state)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, path)
        return state
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def begin_cursor_lifecycle(dispatch: dict[str, Any]) -> dict[str, Any]:
    """Create one durable lifecycle generation keyed by child and work ids."""

    child_session_id = str(dispatch.get("child_session_id") or "")
    work_id = str(dispatch.get("work_id") or "")
    if not child_session_id or not work_id:
        return {}

    def update(state: dict[str, Any]) -> None:
        generations = state["generations"]
        generation = generations.setdefault(work_id, {})
        generation.setdefault("work_id", work_id)
        generation.setdefault("child_session_id", child_session_id)
        generation.setdefault(
            "parent_session_id", str(dispatch.get("parent_session_id") or "")
        )
        generation.setdefault("title", str(dispatch.get("title") or ""))
        generation.setdefault(
            "started_at_ns",
            int(dispatch.get("dispatched_at_ns") or time.time_ns()),
        )
        generation.setdefault("last_progress_at_ns", generation["started_at_ns"])
        generation.setdefault("last_fingerprint", "")
        generation.setdefault("candidate_fingerprint", "")
        generation.setdefault("candidate_since_ns", 0)
        generation.setdefault("completed_turn_id", "")
        generation.setdefault("terminal_status", "")
        generation.setdefault("terminal_output", "")
        generation.setdefault("terminal_phase", "active")
        generation.setdefault("delivery_committed", False)
        generation.setdefault("legacy_pending", False)
        state["current_work_id"] = work_id

    return _mutate_cursor_state(child_session_id, update)


def read_cursor_lifecycle(
    child_session_id: str,
    work_id: str | None = None,
) -> dict[str, Any]:
    """Read one generation from the durable Cursor lifecycle ledger."""

    path = _cursor_state_path(child_session_id)
    if path is None:
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(state, dict):
        return {}
    selected = work_id or str(state.get("current_work_id") or "")
    generations = state.get("generations")
    if not isinstance(generations, dict):
        return {}
    generation = generations.get(selected)
    return dict(generation) if isinstance(generation, dict) else {}


def mutate_cursor_lifecycle(
    child_session_id: str,
    work_id: str,
    update: Callable[[dict[str, Any], dict[str, Any]], None],
) -> dict[str, Any]:
    """Atomically mutate one existing lifecycle generation."""

    selected: dict[str, Any] = {}

    def mutate(state: dict[str, Any]) -> None:
        nonlocal selected
        generations = state["generations"]
        generation = generations.setdefault(
            work_id,
            {
                "work_id": work_id,
                "child_session_id": child_session_id,
                "started_at_ns": time.time_ns(),
                "last_progress_at_ns": time.time_ns(),
                "terminal_phase": "active",
                "delivery_committed": False,
            },
        )
        update(generation, state)
        selected = dict(generation)

    _mutate_cursor_state(child_session_id, mutate)
    return selected


_INTERNAL_SYSTEMS = ("glean", "jira", "slack", "confluence", "safe")
_OPUS_STAGE_TITLE = re.compile(
    r"(?:"
    r"audit-cycle-[1-4](?:-web-[1-2])?"
    r"|audit-internal-[1-4]-[1-2]"
    r"|audit-format-repair-[1-4](?:-web-[1-2])?"
    r")"
)
_AUDIT_CYCLE_HOP = re.compile(
    r"^audit-(?:cycle|internal)-(?P<cycle>[1-4])(?:-web-(?P<hop>[1-2]))?"
)


def _format_repair_title(title: str) -> str:
    """Name the repair stage a mechanically invalid audit must be sent to.

    Mirrors the router's own suffix rule: only a web re-audit keeps its hop, so
    `audit-cycle-2-web-1` repairs as `audit-format-repair-2-web-1` while both
    `audit-cycle-2` and `audit-internal-2-1` repair as `audit-format-repair-2`.
    Returns "" for a title that is already a repair, since a repair that is
    still invalid is a terminal condition rather than another repair.
    """

    if title.startswith("audit-format-repair-"):
        return ""
    match = _AUDIT_CYCLE_HOP.match(title)
    if match is None:
        return ""
    suffix = (
        f"-web-{match.group('hop')}"
        if match.group("hop") and title.startswith("audit-cycle-")
        else ""
    )
    return f"audit-format-repair-{match.group('cycle')}{suffix}"


def _audit_next_dispatch_note(
    title: str,
    record: dict[str, Any],
    *,
    audit_unusable: bool,
) -> str:
    """State the one authorized next stage for a collected Opus audit.

    ROUTE step 3 already declares this line authoritative over any verdict
    string in the audit text, and a mechanically valid audit needs the statement
    just as much as an invalid one. On 2026-09-11 run-4gqa_fil the audit was
    `mechanically_validated` with 16 observed internal calls and read
    `"verdict": "FAIL"` beside a 12-item `punch_list_for_cursor`; the supervisor
    dispatched `cursor-cycle-2` while the durable route required
    `judge-cycle-1`, which ended a healthy run as a terminal infrastructure
    error. A FAIL verdict is Opus stating an opinion, not choosing a route.

    The target is read back from `_next_route` over the records plus this
    not-yet-appended one, so the sentence cannot drift from the state machine it
    is quoting. That matters for the audit verdict that genuinely does route
    away from Codex: `NEEDS_WEB` goes to `cursor-web-opus-N-H`, never to
    `cursor-cycle-N+1`.
    """

    if audit_unusable:
        repair_title = _format_repair_title(title)
        if not repair_title:
            return ""
        return (
            "\n\n[System-required next dispatch: this audit FAILED mechanical"
            " validation and carries no usable verdict object, regardless of"
            " any verdict string in its text. Dispatch opus_auditor with"
            f" title {repair_title} and the fixed FORMAT REPAIR ONLY"
            " instruction. Do not dispatch codex_judge for this cycle until"
            " a valid audit is collected.]"
        )
    try:
        from triple_stamp_isaac_launcher import _next_route

        parent_session_id = str(record.get("parent_session_id") or "")
        route = _next_route(
            [*read_collections(parent_session_id=parent_session_id), record],
            parent_session_id=parent_session_id,
        )
    except (ImportError, TypeError, ValueError):
        return ""
    agent = str(getattr(route, "agent", "") or "")
    target = str(getattr(route, "title", "") or "")
    if str(getattr(route, "status", "") or "") != "dispatch" or not agent or not target:
        return ""
    return (
        "\n\n[System-required next dispatch: this audit passed mechanical"
        f" validation, so the one authorized next stage is {agent} with title"
        f" {target}. That is the durable state machine's own route and it"
        " overrides every routing-shaped field in the audit above:"
        " punch_list_for_cursor, must_retest, and web_queries are evidence for"
        " the next stage to weigh, never an instruction to dispatch. PASS,"
        " PASS_WITH_GAPS, and FAIL all route to codex_judge, because Opus audits"
        " while Codex alone adjudicates STAMP versus a concrete REWORK."
        f" Dispatch exactly {target} and no other title.]"
    )


def _observation_reason(exc: BaseException) -> str:
    """Return a bounded diagnostic without URLs, headers, or response bodies."""

    status = getattr(getattr(exc, "response", None), "status_code", None)
    suffix = f" HTTP {status}" if isinstance(status, int) else ""
    return f"{type(exc).__name__}{suffix}"[:160]


def _record_observer_error(
    *,
    child_session_id: str,
    title: str,
    reason: str,
) -> None:
    try:
        _append(
            "observer-errors.jsonl",
            {
                "child_session_id": child_session_id,
                "title": title,
                "reason": reason,
                "observed_at_ns": time.time_ns(),
            },
        )
    except OSError:
        pass


def _opus_effort_not_observed(
    *,
    child_session_id: str,
    title: str,
    reason: str,
    claude_session_id: str = "",
    transcript_ref: str = "",
    assistant_rows: int | None = None,
) -> dict[str, Any]:
    """Return the advisory branch of the Opus effort tri-state."""

    _record_observer_error(
        child_session_id=child_session_id,
        title=title,
        reason=reason,
    )
    return {
        "child_session_id": child_session_id,
        "claude_session_id": claude_session_id,
        "title": title,
        "expected": "max",
        "status": "not_observed",
        "outcome": "not_observed",
        "compliant": None,
        "assistant_rows": assistant_rows,
        "values": [],
        "transcript_ref": transcript_ref,
        "reason": reason,
    }


def observe_opus_effort(
    child_session_id: str,
    *,
    title: str,
) -> dict[str, Any]:
    """Inspect one completed Opus child's exact raw Claude transcript.

    The child id selects one bridge, whose pinned Claude session id selects one
    transcript under this run's isolated HOME. There is deliberately no glob
    fallback: missing identity or an unreadable transcript is advisory
    ``not_observed`` rather than evidence borrowed from another child.
    """

    if not child_session_id:
        return _opus_effort_not_observed(
            child_session_id=child_session_id,
            title=title,
            reason="child session id unavailable",
        )
    run_dir = _run_dir()
    if run_dir is None:
        return _opus_effort_not_observed(
            child_session_id=child_session_id,
            title=title,
            reason="current run directory unavailable",
        )
    try:
        from omnigent.claude_native_bridge import (
            bridge_dir_for_conversation_id,
            read_claude_session_id,
            read_transcript_path,
        )

        bridge_dir = bridge_dir_for_conversation_id(child_session_id)
        claude_session_id = read_claude_session_id(bridge_dir) or ""
        transcript_path = read_transcript_path(bridge_dir)
    except Exception as exc:  # noqa: BLE001 - advisory runtime observation
        return _opus_effort_not_observed(
            child_session_id=child_session_id,
            title=title,
            reason=f"bridge identity unreadable: {_observation_reason(exc)}",
        )
    if not claude_session_id or transcript_path is None:
        return _opus_effort_not_observed(
            child_session_id=child_session_id,
            title=title,
            reason="bridge lacks pinned Claude session or transcript identity",
            claude_session_id=claude_session_id,
        )

    expected_root = (run_dir / "home" / ".claude" / "projects").resolve(
        strict=False
    )
    resolved_path = transcript_path.resolve(strict=False)
    transcript_ref = ""
    try:
        transcript_ref = str(resolved_path.relative_to(run_dir.resolve()))
    except ValueError:
        pass
    if (
        not resolved_path.is_relative_to(expected_root)
        or resolved_path.name != f"{claude_session_id}.jsonl"
    ):
        return _opus_effort_not_observed(
            child_session_id=child_session_id,
            title=title,
            reason="transcript identity is outside this run or mismatched",
            claude_session_id=claude_session_id,
            transcript_ref=transcript_ref,
        )

    try:
        lines = resolved_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return _opus_effort_not_observed(
            child_session_id=child_session_id,
            title=title,
            reason=f"raw Claude transcript unreadable: {_observation_reason(exc)}",
            claude_session_id=claude_session_id,
            transcript_ref=transcript_ref,
        )

    efforts: list[str] = []
    assistant_rows = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (ValueError, json.JSONDecodeError) as exc:
            return _opus_effort_not_observed(
                child_session_id=child_session_id,
                title=title,
                reason=f"raw Claude transcript malformed: {_observation_reason(exc)}",
                claude_session_id=claude_session_id,
                transcript_ref=transcript_ref,
                assistant_rows=assistant_rows,
            )
        if not isinstance(row, dict) or row.get("type") != "assistant":
            continue
        row_session_id = row.get("session_id") or row.get("sessionId")
        if row_session_id != claude_session_id:
            continue
        assistant_rows += 1
        effort = row.get("effort")
        if not isinstance(effort, str) or not effort:
            return _opus_effort_not_observed(
                child_session_id=child_session_id,
                title=title,
                reason="matching assistant row lacks effort",
                claude_session_id=claude_session_id,
                transcript_ref=transcript_ref,
                assistant_rows=assistant_rows,
            )
        efforts.append(effort)
    if not efforts:
        return _opus_effort_not_observed(
            child_session_id=child_session_id,
            title=title,
            reason="no assistant rows matched the pinned Claude session",
            claude_session_id=claude_session_id,
            transcript_ref=transcript_ref,
            assistant_rows=assistant_rows,
        )

    values = list(dict.fromkeys(efforts))
    compliant = all(value == "max" for value in efforts)
    return {
        "child_session_id": child_session_id,
        "claude_session_id": claude_session_id,
        "title": title,
        "expected": "max",
        "status": "observed",
        "outcome": "all_max" if compliant else "non_max",
        "compliant": compliant,
        "assistant_rows": assistant_rows,
        "values": values,
        "transcript_ref": transcript_ref,
        "reason": "",
    }


def add_opus_effort_observation(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach runtime effort evidence to every completed Opus hop."""

    record = dict(payload)
    title = str(record.get("title") or "")
    if (
        record.get("agent") != "opus_auditor"
        or record.get("status") != "completed"
        or _OPUS_STAGE_TITLE.fullmatch(title) is None
    ):
        return record
    observation = observe_opus_effort(
        str(record.get("child_session_id") or ""),
        title=title,
    )
    record["opus_effort_observation"] = observation
    output = record.get("output")
    if not isinstance(output, str):
        return record
    if observation["status"] == "not_observed":
        sentence = (
            "status=not_observed; expected=max; compliant=unknown; "
            f"reason={observation['reason']}. This is advisory only and must "
            "never be treated as low effort."
        )
    elif observation["compliant"]:
        sentence = (
            "status=observed; expected=max; "
            f"values={','.join(observation['values'])}; compliant=true."
        )
    else:
        sentence = (
            "status=observed; expected=max; "
            f"values={','.join(observation['values'])}; compliant=false. "
            "This is a material runtime effort degradation: Codex must return "
            "REWORK and must not STAMP."
        )
    record["output"] = (
        output
        + "\n\n[System-observed Opus runtime effort: "
        + sentence
        + "]"
    )
    return record


async def observe_opus_internal_mcp_calls(
    server_client: Any,
    child_session_id: str,
    *,
    title: str,
) -> dict[str, Any]:
    """Observe persisted Opus tool uses through the authenticated server API."""

    cycle_match = re.search(
        r"(?:cycle-|repair-|internal-)([1-4])(?:-|$)", title
    )
    cycle = int(cycle_match.group(1)) if cycle_match else 0
    base = {
        "child_session_id": child_session_id,
        "title": title,
        "cycle": cycle,
    }
    if server_client is None or not child_session_id:
        reason = "authenticated server client or child session id unavailable"
        _record_observer_error(
            child_session_id=child_session_id,
            title=title,
            reason=reason,
        )
        return {
            **base,
            "available": False,
            "status": "not_observed",
            "count": None,
            "tools": [],
            "call_ids": [],
            "calls": [],
            "by_system": {},
            "tools_by_system": {},
            "toolsearch_count": None,
            "reason": reason,
        }

    limit = 1000
    after: str | None = None
    items: list[dict[str, Any]] = []
    try:
        while True:
            params: dict[str, Any] = {"limit": limit, "order": "asc"}
            if after:
                params["after"] = after
            response = await server_client.get(
                f"/v1/sessions/{quote(child_session_id, safe='')}/items",
                params=params,
                timeout=30.0,
            )
            response.raise_for_status()
            body = response.json()
            page = body.get("data") if isinstance(body, dict) else None
            if not isinstance(page, list):
                raise TypeError("session items response lacks a data array")
            rows = [item for item in page if isinstance(item, dict)]
            items.extend(rows)
            if len(page) < limit:
                break
            last_id = (
                body.get("last_id")
                if isinstance(body, dict)
                else None
            ) or (rows[-1].get("id") if rows else None)
            if not isinstance(last_id, str) or not last_id or last_id == after:
                raise ValueError("session items pagination lacks a forward cursor")
            after = last_id
    except Exception as exc:  # noqa: BLE001 - retained as an explicit tri-state
        reason = _observation_reason(exc)
        _record_observer_error(
            child_session_id=child_session_id,
            title=title,
            reason=reason,
        )
        return {
            **base,
            "available": False,
            "status": "not_observed",
            "count": None,
            "tools": [],
            "call_ids": [],
            "calls": [],
            "by_system": {},
            "tools_by_system": {},
            "toolsearch_count": None,
            "reason": reason,
        }

    result_ids = {
        str(item.get("call_id"))
        for item in items
        if item.get("type") == "function_call_output"
        and isinstance(item.get("call_id"), str)
        and item.get("call_id")
    }
    calls: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    toolsearch_count = 0
    by_system: dict[str, int] = {system: 0 for system in _INTERNAL_SYSTEMS}
    tools_by_system: dict[str, list[str]] = {
        system: [] for system in _INTERNAL_SYSTEMS
    }
    for item in items:
        if item.get("type") != "function_call":
            continue
        name = item.get("name")
        if name == "ToolSearch":
            toolsearch_count += 1
            continue
        if not isinstance(name, str) or not name.startswith("mcp__"):
            continue
        raw_id = item.get("call_id")
        item_id = item.get("id")
        call_id = (
            raw_id
            if isinstance(raw_id, str) and raw_id
            else f"item:{item_id}"
            if isinstance(item_id, str) and item_id
            else ""
        )
        if not call_id or call_id in seen_ids:
            continue
        seen_ids.add(call_id)
        parts = name.split("__", 2)
        system = parts[1] if len(parts) == 3 else ""
        if system in _INTERNAL_SYSTEMS:
            by_system[system] = by_system.get(system, 0) + 1
            family_tools = tools_by_system.setdefault(system, [])
            if name not in family_tools:
                family_tools.append(name)
        calls.append(
            {
                "call_id": call_id,
                "name": name,
                "system": system,
                "result_observed": call_id in result_ids,
            }
        )

    return {
        **base,
        "available": True,
        "status": "observed" if calls else "observed_zero",
        "count": len(calls),
        "tools": list(dict.fromkeys(call["name"] for call in calls)),
        "call_ids": [call["call_id"] for call in calls],
        "calls": calls,
        "by_system": by_system,
        "tools_by_system": tools_by_system,
        "toolsearch_count": toolsearch_count,
        "reason": "",
    }


async def add_opus_internal_mcp_observation(
    payload: dict[str, Any],
    *,
    server_client: Any,
) -> dict[str, Any]:
    """Attach objective coverage evidence without making it a launch gate."""

    record = dict(payload)
    title = str(record.get("title") or "")
    if (
        record.get("agent") != "opus_auditor"
        or record.get("status") != "completed"
        or _OPUS_STAGE_TITLE.fullmatch(title) is None
    ):
        return record
    observation = await observe_opus_internal_mcp_calls(
        server_client,
        str(record.get("child_session_id") or ""),
        title=title,
    )
    record["internal_mcp_observation"] = observation
    output = record.get("output")
    if isinstance(output, str):
        validation: dict[str, Any] = {}
        # `None` here means the audit carries no usable verdict object, whatever
        # verdict string its prose contains. The supervisor cannot see that: its
        # repair rule triggers on output "without a valid verdict object", and a
        # mechanically rejected audit can still read `"verdict": "FAIL"` to a
        # human. On 2026-09-11 run-r477r1_k that gap deadlocked a run - the
        # router required `audit-format-repair-1`, the supervisor dispatched
        # `judge-cycle-1`, and the resulting Codex STAMP was discarded for want
        # of a valid chain. Surface it explicitly below.
        audit_unusable = False
        try:
            from triple_stamp_isaac_launcher import (
                _audit_payload_and_validation,
            )

            semantic_payload, candidate = _audit_payload_and_validation(
                output,
                (
                    None
                    if title.startswith("audit-format-repair-")
                    else observation
                ),
            )
            if isinstance(candidate, dict):
                validation = candidate
            audit_unusable = semantic_payload is None
        except (ImportError, TypeError, ValueError):
            validation = {}
        if validation:
            record["audit_validation"] = validation
        if observation["status"] == "not_observed":
            sentence = (
                "status=not_observed; coverage unverified; "
                f"reason={observation['reason']}. Judge the audit ledger; "
                "this observer result is advisory and no mechanical tool-name "
                "validation is claimed."
            )
        else:
            families = ", ".join(
                (
                    f"{system}={observation['by_system'].get(system, 0)}"
                    "["
                    + ",".join(
                        observation["tools_by_system"].get(system, [])
                    )
                    + "]"
                )
                for system in _INTERNAL_SYSTEMS
            )
            sentence = (
                f"status={observation['status']}; calls={observation['count']}; "
                f"families={families}. "
            )
        if validation:
            invalid = ", ".join(
                sorted(
                    {
                        str(item.get("tool") or "")
                        for item in validation["invalid_tool_claims"]
                        if isinstance(item, dict) and item.get("tool")
                    }
                )
            )
            sentence += (
                f" audit_status={validation['status']}; "
                f"mechanical_tool_validation="
                f"{str(validation['tool_claims_validated']).lower()}; "
                f"invalid_tools={invalid or 'none'}; "
                f"form_issues={len(validation['form_issues'])}. "
                "Codex has no independent tool catalog and must rely on this "
                "exact-child observation and audit status."
            )
        record["output"] = (
            output
            + "\n\n[System-observed Opus internal MCP coverage: "
            + sentence
            + "]"
        )
        record["output"] += _audit_next_dispatch_note(
            title,
            record,
            audit_unusable=audit_unusable,
        )
    return record


def _add_codex_punch_metadata(record: dict[str, Any]) -> dict[str, Any]:
    """Persist deterministic structured punch-list identities per cycle."""

    if (
        record.get("agent") != "codex_judge"
        or record.get("status") != "completed"
        or not isinstance(record.get("output"), str)
    ):
        return record
    try:
        from triple_stamp_isaac_launcher import (
            _mapping_candidates,
            _normalized_codex_punch_list,
            _punch_list_signature,
        )

        payload = next(
            (
                candidate
                for candidate in _mapping_candidates(record["output"])
                if candidate.get("verdict") == "REWORK"
            ),
            None,
        )
        normalized = (
            _normalized_codex_punch_list(payload)
            if isinstance(payload, dict)
            else []
        )
    except (ImportError, TypeError, ValueError):
        normalized = []
    if not normalized:
        return record
    enriched = dict(record)
    signature = _punch_list_signature(normalized)
    title = str(enriched.get("title") or "")
    match = re.search(r"([1-4])$", title)
    metadata = {
        "cycle": int(match.group(1)) if match else 0,
        "title": title,
        "child_session_id": str(enriched.get("child_session_id") or ""),
        "work_id": str(enriched.get("work_id") or ""),
        "normalized_punch_list": normalized,
        "punch_list_signature": signature,
        "recorded_at_ns": time.time_ns(),
    }
    enriched.update(
        normalized_punch_list=normalized,
        punch_list_signature=signature,
    )
    _append("codex-punch-lists.jsonl", metadata)
    return enriched


def append_collection(payload: dict[str, Any]) -> None:
    """Record one packet plus an immutable digest-addressed artifact."""

    record = _add_codex_punch_metadata(dict(payload))
    record.setdefault("collected_at_ns", time.time_ns())
    output = record.get("output")
    run_dir = _run_dir()
    if run_dir is not None and isinstance(output, str):
        packet = output.encode("utf-8")
        digest = hashlib.sha256(packet).hexdigest()
        title = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(record.get("title") or "packet"))
        work_id = re.sub(
            r"[^A-Za-z0-9_.-]+",
            "-",
            str(record.get("work_id") or record.get("child_session_id") or digest[:16]),
        )
        relative = Path("packets") / f"{title}-{work_id}-{digest[:16]}.packet"
        destination = run_dir / relative
        destination.parent.mkdir(mode=0o700, exist_ok=True)
        try:
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        except FileExistsError:
            pass
        else:
            try:
                os.write(fd, packet)
                os.fsync(fd)
            finally:
                os.close(fd)
        record.update(
            packet_ref=str(relative),
            output_sha256=digest,
            output_bytes=len(packet),
        )
    _append("routing-collections.jsonl", record)


def read_collections(parent_session_id: str = "") -> list[dict[str, Any]]:
    return _scope_parent_records(
        _read("routing-collections.jsonl"),
        parent_session_id,
    )


def tombstone_parent_session(parent_session_id: str) -> None:
    """End one parent's ledger generation without affecting sibling sessions."""

    if not parent_session_id:
        return
    marker = {
        "record_type": "parent_tombstone",
        "parent_session_id": parent_session_id,
        "recorded_at_ns": time.time_ns(),
    }
    _append("routing-dispatches.jsonl", marker)
    _append("routing-collections.jsonl", marker)


def attest_codex_stamp(payload: dict[str, Any]) -> bool:
    """Persist an immutable attestation for one mechanically valid Codex STAMP."""

    run_dir = _run_dir()
    output = payload.get("output")
    if run_dir is None or payload.get("agent") != "codex_judge" or not isinstance(
        output, str
    ):
        return False
    try:
        from triple_stamp_isaac_launcher import (
            _has_required_stage_chain,
            _mapping_candidates,
            _valid_stamp,
        )

        stamp = next(
            (
                candidate
                for candidate in _mapping_candidates(output)
                if _valid_stamp(candidate) is not None
            ),
            None,
        )
    except (ImportError, TypeError, ValueError):
        return False
    if stamp is None:
        return False
    parent_session_id = str(payload.get("parent_session_id") or "")
    if (
        not parent_session_id
        and any(record.get("parent_session_id") for record in read_collections())
    ):
        return False
    check = stamp.get("voice_profile_check") if isinstance(stamp, dict) else None
    answer = stamp.get("shippable_answer") if isinstance(stamp, dict) else None
    expected_profile = os.environ.get("TRIPLE_STAMP_VOICE_PROFILE_SHA256", "")
    expected_path = os.environ.get("TRIPLE_STAMP_VOICE_PROFILE", "").strip()
    if (
        not isinstance(answer, str)
        or not answer
        or not isinstance(stamp.get("citations_that_hold"), list)
        or not stamp["citations_that_hold"]
    ):
        return False
    # Voice rendering is optional; only demand the receipt when one is expected.
    if (expected_path or expected_profile) and (
        not isinstance(check, dict)
        or check.get("source_path") != expected_path
        or check.get("sha256") != expected_profile
    ):
        return False
    answer_bytes = answer.encode("utf-8")
    evidence_bytes = output.encode("utf-8")
    title = str(payload.get("title") or "")
    match = re.fullmatch(r"judge-(?:cycle|convergence)-([1-4])", title)
    if match is None or not _has_required_stage_chain(
        read_collections(parent_session_id=parent_session_id),
        int(match.group(1)),
    ):
        return False
    attestation = {
        "version": 1,
        "verdict": "STAMP",
        "cycle": int(match.group(1)),
        "agent": "codex_judge",
        "title": title,
        "child_session_id": payload.get("child_session_id"),
        "work_id": payload.get("work_id"),
        "parent_session_id": parent_session_id,
        "answer_sha256": hashlib.sha256(answer_bytes).hexdigest(),
        "answer_length": len(answer_bytes),
        "voice_profile_sha256": expected_profile,
        "evidence_packet_sha256": hashlib.sha256(evidence_bytes).hexdigest(),
        "evidence_packet_length": len(evidence_bytes),
    }

    def create_once(path: Path, data: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)

    try:
        create_once(
            _parent_artifact_path(
                run_dir,
                "stamped-answer.bin",
                parent_session_id,
            ),
            answer_bytes,
        )
        create_once(
            _parent_artifact_path(
                run_dir,
                "stamp-attestation.json",
                parent_session_id,
            ),
            (
                json.dumps(attestation, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8"),
        )
    except FileExistsError:
        return False
    return True


def read_attested_answer(parent_session_id: str = "") -> str:
    """Return one parent's immutable attested answer after digest verification."""

    run_dir = _run_dir()
    if run_dir is None:
        return ""
    try:
        attestation = json.loads(
            _parent_artifact_path(
                run_dir,
                "stamp-attestation.json",
                parent_session_id,
            ).read_text(encoding="utf-8")
        )
        answer = _parent_artifact_path(
            run_dir,
            "stamped-answer.bin",
            parent_session_id,
        ).read_bytes()
    except (OSError, ValueError, json.JSONDecodeError):
        return ""
    if (
        not isinstance(attestation, dict)
        or attestation.get("verdict") != "STAMP"
        or (
            parent_session_id
            and attestation.get("parent_session_id") != parent_session_id
        )
        or attestation.get("answer_length") != len(answer)
        or not isinstance(attestation.get("answer_sha256"), str)
        or not hashlib.sha256(answer).hexdigest()
        == attestation["answer_sha256"]
    ):
        return ""
    try:
        return answer.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def record_best_effort_answer(
    answer: str,
    records: list[dict[str, Any]],
    *,
    cycle: int,
    reason: str,
    parent_session_id: str = "",
) -> str:
    """Persist a complete non-STAMP answer with its exact packet provenance."""

    stamped = read_attested_answer(parent_session_id)
    if stamped:
        return stamped
    existing = read_best_effort_answer(parent_session_id)
    if existing:
        return existing
    run_dir = _run_dir()
    clean_answer = answer.strip()
    if (
        run_dir is None
        or not clean_answer
        or clean_answer.startswith(
            ("PIPELINE_VALIDATION_FAILED:", "PIPELINE_INFRASTRUCTURE_ERROR:")
        )
    ):
        return ""

    scoped = _scope_parent_records(records, parent_session_id)

    def complete(record: dict[str, Any], agent: str) -> bool:
        output = record.get("output")
        parsed = parse_dispatch_title(record.get("title"))
        return (
            record.get("agent") == agent
            and record.get("status") == "completed"
            and isinstance(output, str)
            and bool(output.strip())
            and parsed is not None
            and parsed["cycle"] == cycle
            and not output.startswith(
                (
                    "CURSOR_FINALIZATION_REQUIRED:",
                    "CURSOR_WORKER_STALLED:",
                    "CURSOR_WORKER_FAILED:",
                    "CURSOR_WORKER_TIMEOUT:",
                )
            )
        )

    cursor = next(
        (
            record
            for record in reversed(scoped)
            if complete(record, "cursor_workhorse")
            and record.get("title") == f"cursor-cycle-{cycle}"
        ),
        None,
    )
    opus = next(
        (
            record
            for record in reversed(scoped)
            if complete(record, "opus_auditor")
            and str(record.get("title") or "").startswith("audit-")
        ),
        None,
    )
    codex = next(
        (
            record
            for record in reversed(scoped)
            if complete(record, "codex_judge")
            and str(record.get("title") or "").startswith("judge-")
        ),
        None,
    )
    if cursor is None or opus is None:
        return ""

    source_packets = []
    sources = [cursor, opus]
    if codex is not None:
        sources.append(codex)
    for record in sources:
        output_bytes = str(record["output"]).encode("utf-8")
        source_packets.append(
            {
                "agent": record["agent"],
                "title": str(record.get("title") or ""),
                "child_session_id": str(record.get("child_session_id") or ""),
                "work_id": str(record.get("work_id") or ""),
                "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
                "output_length": len(output_bytes),
            }
        )

    answer_bytes = clean_answer.encode("utf-8")
    attestation = {
        "version": 1,
        "verdict": "BEST_EFFORT",
        "quality_approved": False,
        "cycle": max(0, int(cycle)),
        "parent_session_id": parent_session_id,
        "reason": " ".join(str(reason).replace("\x00", " ").split())[:2000],
        "answer_sha256": hashlib.sha256(answer_bytes).hexdigest(),
        "answer_length": len(answer_bytes),
        "source_packets": source_packets,
    }

    def create_once(path: Path, data: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)

    try:
        create_once(
            _parent_artifact_path(
                run_dir,
                "best-effort-answer.bin",
                parent_session_id,
            ),
            answer_bytes,
        )
        create_once(
            _parent_artifact_path(
                run_dir,
                "best-effort-attestation.json",
                parent_session_id,
            ),
            (
                json.dumps(attestation, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8"),
        )
    except FileExistsError:
        return read_best_effort_answer(parent_session_id)
    return clean_answer


def read_best_effort_answer(parent_session_id: str = "") -> str:
    """Return one parent's complete non-STAMP answer after digest verification."""

    run_dir = _run_dir()
    if run_dir is None:
        return ""
    try:
        attestation = json.loads(
            _parent_artifact_path(
                run_dir,
                "best-effort-attestation.json",
                parent_session_id,
            ).read_text(encoding="utf-8")
        )
        answer = _parent_artifact_path(
            run_dir,
            "best-effort-answer.bin",
            parent_session_id,
        ).read_bytes()
    except (OSError, ValueError, json.JSONDecodeError):
        return ""
    if (
        not isinstance(attestation, dict)
        or attestation.get("verdict") != "BEST_EFFORT"
        or attestation.get("quality_approved") is not False
        or (
            parent_session_id
            and attestation.get("parent_session_id") != parent_session_id
        )
        or attestation.get("answer_length") != len(answer)
        or not isinstance(attestation.get("answer_sha256"), str)
        or not hashlib.sha256(answer).hexdigest()
        == attestation["answer_sha256"]
    ):
        return ""
    try:
        decoded = answer.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    if not decoded.strip() or decoded.startswith(
        ("PIPELINE_VALIDATION_FAILED:", "PIPELINE_INFRASTRUCTURE_ERROR:")
    ):
        return ""
    return decoded


def append_supervisor_tool_call(
    name: str,
    parent_session_id: str = "",
) -> int:
    """Record and return the durable supervisor tool-call count."""

    _append(
        "supervisor-tool-calls.jsonl",
        {
            "name": str(name),
            "parent_session_id": parent_session_id,
        },
    )
    return len(
        _scope_parent_records(
            _read("supervisor-tool-calls.jsonl"),
            parent_session_id,
        )
    )


def append_supervisor_continuation(payload: dict[str, Any]) -> None:
    """Record one bounded runtime continuation or suppressed ordinary reply."""

    record = dict(payload)
    record.setdefault("recorded_at_ns", time.time_ns())
    _append("supervisor-continuations.jsonl", record)


def read_supervisor_continuations(
    parent_session_id: str = "",
) -> list[dict[str, Any]]:
    """Return the run-local supervisor continuation ledger."""

    return _scope_parent_records(
        _read("supervisor-continuations.jsonl"),
        parent_session_id,
    )


def write_budget_state(
    cost: float,
    maximum: float,
    denied: bool,
    *,
    reported_usd: float | None = None,
    estimated_unpriced_usd: float | None = None,
    remaining_usd: float | None = None,
    projected_stage: str = "",
    projected_reserve_usd: float = 0.0,
    denial_reason: str = "",
    sessions: list[dict[str, Any]] | None = None,
    parent_session_id: str = "",
) -> None:
    """Atomically retain the latest canonical cumulative budget decision."""

    run_dir = _run_dir()
    if run_dir is None:
        return
    payload = {
        "cost_usd": round(max(0.0, float(cost)), 6),
        "max_cost_usd": float(maximum),
        "denied": bool(denied),
        "reported_usd": round(
            max(0.0, float(reported_usd if reported_usd is not None else cost)),
            6,
        ),
        "estimated_unpriced_usd": round(
            max(0.0, float(estimated_unpriced_usd or 0.0)),
            6,
        ),
        "remaining_usd": round(
            max(
                0.0,
                float(
                    remaining_usd
                    if remaining_usd is not None
                    else maximum - cost
                ),
            ),
            6,
        ),
        "projected_stage": str(projected_stage),
        "projected_reserve_usd": round(
            max(0.0, float(projected_reserve_usd)),
            6,
        ),
        "denial_reason": " ".join(str(denial_reason).split())[:2000],
        "sessions": sessions or [],
    }
    destination = _parent_artifact_path(
        run_dir,
        "budget-state.json",
        parent_session_id,
    )
    temporary = destination.with_name(f".{destination.name}-{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def read_budget_state(parent_session_id: str = "") -> dict[str, Any]:
    """Return the latest budget decision for one root conversation."""

    run_dir = _run_dir()
    if run_dir is None:
        return {}
    try:
        payload = json.loads(
            _parent_artifact_path(
                run_dir,
                "budget-state.json",
                parent_session_id,
            ).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def record_terminal_failure(
    kind: str,
    reason: str,
    *,
    stage: str = "",
    cycle: int = 0,
    cost_usd: float | None = None,
    calls: int | None = None,
    parent_session_id: str = "",
) -> str:
    """Create one immutable sanitized terminal failure result and attestation."""

    completed_answer = read_attested_answer(parent_session_id) or (
        read_best_effort_answer(parent_session_id)
    )
    if completed_answer:
        return completed_answer
    run_dir = _run_dir()
    normalized_kind = (
        "PIPELINE_VALIDATION_FAILED"
        if kind == "PIPELINE_VALIDATION_FAILED"
        else "PIPELINE_INFRASTRUCTURE_ERROR"
    )
    clean_reason = " ".join(str(reason).replace("\x00", " ").split())[:2000]
    if run_dir is None:
        return (
            f"{normalized_kind}: stage {stage or 'unknown'}, cycle {max(0, int(cycle))}; "
            f"continuation denied: {clean_reason or 'unspecified terminal failure'}"
        )
    budget = read_budget_state(parent_session_id)
    reported = budget.get("reported_usd")
    estimated = budget.get("estimated_unpriced_usd")
    remaining = budget.get("remaining_usd")
    maximum = budget.get("max_cost_usd")
    effective_cost = cost_usd if cost_usd is not None else budget.get("cost_usd")

    def money(value: object) -> str:
        try:
            return f"${max(0.0, float(value)):.2f}"
        except (TypeError, ValueError):
            return "unavailable"

    result = (
        f"{normalized_kind}: stage {stage or 'unknown'}, cycle {max(0, int(cycle))}; "
        f"provider-reported {money(reported)} + conservative unpriced estimate "
        f"{money(estimated)} = {money(effective_cost)}; remaining {money(remaining)} "
        f"of {money(maximum)}; continuation denied: "
        f"{clean_reason or 'unspecified terminal failure'}"
    )
    attestation = {
        "version": 1,
        "result": normalized_kind,
        "stage": str(stage),
        "cycle": max(0, int(cycle)),
        "reason": clean_reason,
        "cost_usd": effective_cost,
        "reported_usd": reported,
        "estimated_unpriced_usd": estimated,
        "remaining_usd": remaining,
        "max_cost_usd": maximum,
        "calls": calls,
        "parent_session_id": parent_session_id,
    }

    def create_once(path: Path, data: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)

    for path, data in (
        (
            _parent_artifact_path(
                run_dir,
                "terminal-failure.txt",
                parent_session_id,
            ),
            result.encode("utf-8"),
        ),
        (
            _parent_artifact_path(
                run_dir,
                "failure-attestation.json",
                parent_session_id,
            ),
            (
                json.dumps(attestation, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8"),
        ),
    ):
        try:
            create_once(path, data)
        except FileExistsError:
            pass
    try:
        return _parent_artifact_path(
            run_dir,
            "terminal-failure.txt",
            parent_session_id,
        ).read_text(encoding="utf-8")
    except OSError:
        return result


def read_terminal_failure(parent_session_id: str = "") -> str:
    """Return one parent's recorded terminal failure text, or "" if absent.

    Each parent gets an immutable file, so a sibling failure cannot terminate
    or overwrite this conversation.
    """

    run_dir = _run_dir()
    if run_dir is None:
        return ""
    try:
        return _parent_artifact_path(
            run_dir,
            "terminal-failure.txt",
            parent_session_id,
        ).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
