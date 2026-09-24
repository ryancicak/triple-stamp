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
    (r"cursor-retry-([1-4])-(1)", "cursor_retry", ""),
    (r"audit-cycle-([1-4])", "audit", "opus"),
    (r"audit-cycle-([1-4])-web-([1-2])", "audit_web", "opus"),
    (r"audit-retry-([1-4])-(1)", "audit_retry", "opus"),
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
    {
        "audit_web",
        "audit_retry",
        "audit_internal",
        "audit_repair_web",
        "cursor_retry",
        "cursor_web",
    }
)
_TOOL_DISPATCH_EXCEPTIONS = (
    AttributeError,
    IndexError,
    OSError,
    TypeError,
    ValueError,
)


class AttemptInFlightError(RuntimeError):
    """A distinct top-level request arrived before its predecessor terminated."""


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


_ATTEMPTS_DIRECTORY = ".triple-stamp-attempts"
_ATTEMPT_ARTIFACTS = (
    "best-effort-answer.bin",
    "best-effort-attestation.json",
    "budget-state.json",
    "failure-attestation.json",
    "pipeline-terminal",
    "stamp-attestation.json",
    "stamped-answer.bin",
    "stamped-answer.sha256",
    "terminal-failure.txt",
)
_TERMINAL_ARTIFACTS = frozenset(
    {
        "best-effort-answer.bin",
        "best-effort-attestation.json",
        "failure-attestation.json",
        "stamp-attestation.json",
        "stamped-answer.bin",
        "terminal-failure.txt",
    }
)


def _parent_scope_digest(parent_session_id: str) -> str:
    value = parent_session_id or "__legacy_single_parent__"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _attempt_root(run_dir: Path, parent_session_id: str) -> Path:
    return (
        run_dir
        / _ATTEMPTS_DIRECTORY
        / _parent_scope_digest(parent_session_id)
    )


def _attempt_state_path(run_dir: Path, parent_session_id: str) -> Path:
    return _attempt_root(run_dir, parent_session_id) / "state.json"


def _read_attempt_state_unlocked(
    run_dir: Path,
    parent_session_id: str,
) -> dict[str, Any]:
    try:
        value = json.loads(
            _attempt_state_path(run_dir, parent_session_id).read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_attempt_state_unlocked(
    run_dir: Path,
    parent_session_id: str,
    state: dict[str, Any],
) -> None:
    root = _attempt_root(run_dir, parent_session_id)
    temporary = root / f".state-{os.getpid()}-{time.time_ns()}.tmp"
    data = (
        json.dumps(state, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, root / "state.json")


def _attempt_generation_dir(
    run_dir: Path,
    parent_session_id: str,
    generation: int,
) -> Path:
    return _attempt_root(run_dir, parent_session_id) / (
        f"generation-{max(1, int(generation)):08d}"
    )


def _artifact_target_from_generation(
    run_dir: Path,
    filename: str,
    parent_session_id: str,
    generation: int,
) -> Path:
    generation_dir = _attempt_generation_dir(
        run_dir,
        parent_session_id,
        generation,
    )
    if filename in _TERMINAL_ARTIFACTS:
        return generation_dir / "terminal" / filename
    return generation_dir / filename


def _legacy_parent_artifact_path(
    run_dir: Path,
    filename: str,
    parent_session_id: str = "",
) -> Path:
    if not parent_session_id:
        return run_dir / filename
    digest = hashlib.sha256(parent_session_id.encode("utf-8")).hexdigest()[:16]
    path = Path(filename)
    return run_dir / f"{path.stem}-{digest}{path.suffix}"


def _ensure_artifact_aliases(
    run_dir: Path,
    parent_session_id: str,
) -> None:
    """Install stable launcher-compatible names through one atomic current link."""

    root = _attempt_root(run_dir, parent_session_id)
    if not (root / "state.json").is_file():
        return
    for filename in _ATTEMPT_ARTIFACTS:
        alias = _legacy_parent_artifact_path(
            run_dir,
            filename,
            parent_session_id,
        )
        if alias.is_symlink() or alias.exists():
            continue
        suffix = (
            Path("terminal") / filename
            if filename in _TERMINAL_ARTIFACTS
            else Path(filename)
        )
        target = root / "current" / suffix
        relative = os.path.relpath(target, start=alias.parent)
        try:
            os.symlink(relative, alias)
        except FileExistsError:
            pass


def current_attempt_generation(parent_session_id: str = "") -> int:
    """Return the active immutable-attempt generation for one parent."""

    run_dir = _run_dir()
    if run_dir is None:
        return 1
    root = _attempt_root(run_dir, parent_session_id)
    if not root.exists():
        return 1
    lock_fd = os.open(root / "attempt.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        state = _read_attempt_state_unlocked(run_dir, parent_session_id)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    try:
        return max(1, int(state.get("current_generation") or 1))
    except (TypeError, ValueError):
        return 1


def _reserve_stage_retry(
    parent_session_id: str,
    cycle: int,
    *,
    stage: str,
) -> bool:
    """Atomically reserve one retry for this attempt, stage, and cycle.

    The reservation is created before native dispatch. It remains spent after
    any launch or unknown completion, and may be released only when the native
    send returns explicit proof that it did not launch a child. Generation-local
    exclusive files make duplicate policy evaluations and concurrent sends
    converge on one winner without trusting a post-launch dispatch observer.
    """

    run_dir = _run_dir()
    if (
        run_dir is None
        or not parent_session_id
        or isinstance(cycle, bool)
        or not isinstance(cycle, int)
        or cycle < 1
        or cycle > 4
        or stage not in {"cursor", "opus"}
    ):
        return False
    activate_parent_attempt(parent_session_id, "")
    root = _attempt_root(run_dir, parent_session_id)
    lock_fd = os.open(root / "attempt.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        state = _read_attempt_state_unlocked(run_dir, parent_session_id)
        try:
            generation = max(1, int(state.get("current_generation") or 1))
        except (TypeError, ValueError):
            return False
        reservation_dir = (
            _attempt_generation_dir(
                run_dir,
                parent_session_id,
                generation,
            )
            / "retry-reservations"
        )
        reservation_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        reservation = reservation_dir / f"{stage}-cycle-{cycle}.json"
        try:
            fd = os.open(
                reservation,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o400,
            )
        except FileExistsError:
            return False
        data = (
            json.dumps(
                {
                    "version": 1,
                    "parent_session_id": parent_session_id,
                    "attempt_generation": generation,
                    "stage": stage,
                    "cycle": cycle,
                    "reserved_at_ns": time.time_ns(),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        reservation_dir_fd = os.open(reservation_dir, os.O_RDONLY)
        try:
            os.fsync(reservation_dir_fd)
        finally:
            os.close(reservation_dir_fd)
        return True
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def release_retry_reservation(
    parent_session_id: str,
    cycle: int,
    *,
    stage: str,
    native_send_status: str,
) -> bool:
    """Release a reservation only after an explicit non-launch result."""

    run_dir = _run_dir()
    if (
        run_dir is None
        or not parent_session_id
        or isinstance(cycle, bool)
        or not isinstance(cycle, int)
        or cycle < 1
        or cycle > 4
        or stage not in {"cursor", "opus"}
        or native_send_status != "not_launched"
    ):
        return False
    root = _attempt_root(run_dir, parent_session_id)
    lock_fd = os.open(root / "attempt.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        state = _read_attempt_state_unlocked(run_dir, parent_session_id)
        try:
            generation = max(1, int(state.get("current_generation") or 1))
        except (TypeError, ValueError):
            return False
        reservation_dir = (
            _attempt_generation_dir(
                run_dir,
                parent_session_id,
                generation,
            )
            / "retry-reservations"
        )
        reservation = reservation_dir / f"{stage}-cycle-{cycle}.json"
        try:
            reservation.unlink()
        except FileNotFoundError:
            return False
        reservation_dir_fd = os.open(reservation_dir, os.O_RDONLY)
        try:
            os.fsync(reservation_dir_fd)
        finally:
            os.close(reservation_dir_fd)
        return True
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def reserve_opus_retry(
    parent_session_id: str,
    cycle: int,
) -> bool:
    """Reserve the one paid transient Opus retry independently of Cursor."""

    return _reserve_stage_retry(
        parent_session_id,
        cycle,
        stage="opus",
    )


def reserve_cursor_retry(
    parent_session_id: str,
    cycle: int,
) -> bool:
    """Reserve the one fresh Cursor recovery child independently of Opus."""

    return _reserve_stage_retry(
        parent_session_id,
        cycle,
        stage="cursor",
    )


def _observed_budget_totals(payload: dict[str, Any]) -> dict[str, float] | None:
    try:
        reported = float(
            payload.get("observed_reported_usd", payload["reported_usd"])
        )
        estimated = float(
            payload.get(
                "observed_estimated_unpriced_usd",
                payload["estimated_unpriced_usd"],
            )
        )
        total = float(
            payload.get("observed_cost_usd", reported + estimated)
        )
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "reported_usd": max(0.0, reported),
        "estimated_unpriced_usd": max(0.0, estimated),
        "cost_usd": max(0.0, total),
    }


def attempt_request_identity(
    content: object,
    *,
    message_id: str = "",
) -> str:
    """Return one stable identity shared by request policy and executor history."""

    attachments: object = []
    normalized_content = content
    if isinstance(content, dict) and "user_content" in content:
        normalized_content = content.get("user_content")
        attachments = content.get("attachments") or []
    elif isinstance(content, list):
        text_parts = [
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        if text_parts:
            normalized_content = "\n".join(text_parts)
    encoded = json.dumps(
        {
            "message_id": str(message_id),
            "content": normalized_content,
            "attachments": attachments,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "request:" + hashlib.sha256(encoded).hexdigest()


def activate_parent_attempt(
    parent_session_id: str,
    request_identity: str,
    *,
    new_request: bool = False,
) -> int:
    """Activate exactly one generation per top-level user-message history.

    Child-completion wakes and harness-generated continuation prompts stay in
    the same nonterminal generation even when their request-phase wrappers have
    different text. A later user message advances the atomic ``current``
    symlink only after the prior attempt published a terminal bundle, making
    every terminal artifact, budget, route ledger, and retry counter from the
    prior question unreachable without deleting or rewriting its bytes.
    """

    run_dir = _run_dir()
    if run_dir is None:
        return 1
    root = _attempt_root(run_dir, parent_session_id)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_fd = os.open(root / "attempt.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        state = _read_attempt_state_unlocked(run_dir, parent_session_id)
        try:
            generation = max(
                1,
                int(state.get("current_generation") or 1),
            )
        except (TypeError, ValueError):
            generation = 1
        prior_identity = str(state.get("request_identity") or "")
        terminal_exists = _attempt_generation_dir(
            run_dir,
            parent_session_id,
            generation,
        ).joinpath("terminal").is_dir()
        if (
            new_request
            and request_identity
            and prior_identity
            and request_identity != prior_identity
            and not terminal_exists
        ):
            raise AttemptInFlightError(
                "prior attempt still in flight; refusing to merge request identities"
            )
        changed = bool(
            request_identity
            and terminal_exists
            and (
                new_request
                or (
                    prior_identity
                    and request_identity != prior_identity
                )
            )
        )
        if changed:
            prior_budget: dict[str, Any] = {}
            try:
                prior_budget = json.loads(
                    _artifact_target_from_generation(
                        run_dir,
                        "budget-state.json",
                        parent_session_id,
                        generation,
                    ).read_text(encoding="utf-8")
                )
            except (OSError, ValueError, json.JSONDecodeError):
                prior_budget = {}
            baseline = (
                _observed_budget_totals(prior_budget)
                if isinstance(prior_budget, dict)
                else None
            )
            generation += 1
        else:
            baseline = (
                state.get("cost_baseline")
                if "cost_baseline" in state
                else {
                    "reported_usd": 0.0,
                    "estimated_unpriced_usd": 0.0,
                    "cost_usd": 0.0,
                }
            )
        generation_dir = _attempt_generation_dir(
            run_dir,
            parent_session_id,
            generation,
        )
        generation_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        next_state = {
            "version": 1,
            "parent_session_id": parent_session_id,
            "current_generation": generation,
            "request_identity": request_identity or prior_identity,
            "cost_baseline": baseline,
            "activated_at_ns": time.time_ns(),
        }
        _write_attempt_state_unlocked(
            run_dir,
            parent_session_id,
            next_state,
        )
        temporary_link = root / (
            f".current-{os.getpid()}-{time.time_ns()}.tmp"
        )
        os.symlink(generation_dir.name, temporary_link)
        os.replace(temporary_link, root / "current")
        root_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    _ensure_artifact_aliases(run_dir, parent_session_id)
    return generation


def read_attempt_cost_baseline(
    parent_session_id: str = "",
) -> dict[str, float] | None:
    """Return the active attempt's cumulative-usage baseline, if initialized."""

    run_dir = _run_dir()
    if run_dir is None:
        return {
            "reported_usd": 0.0,
            "estimated_unpriced_usd": 0.0,
            "cost_usd": 0.0,
        }
    state = _read_attempt_state_unlocked(run_dir, parent_session_id)
    value = state.get("cost_baseline")
    if not isinstance(value, dict):
        return None
    return _observed_budget_totals(
        {
            "reported_usd": value.get("reported_usd"),
            "estimated_unpriced_usd": value.get(
                "estimated_unpriced_usd"
            ),
            "observed_cost_usd": value.get("cost_usd"),
        }
    )


def initialize_attempt_cost_baseline(
    parent_session_id: str,
    *,
    reported_usd: float,
    estimated_unpriced_usd: float,
    cost_usd: float,
) -> dict[str, float]:
    """Initialize a later attempt's baseline from its first authoritative read."""

    run_dir = _run_dir()
    proposed = {
        "reported_usd": max(0.0, float(reported_usd)),
        "estimated_unpriced_usd": max(
            0.0,
            float(estimated_unpriced_usd),
        ),
        "cost_usd": max(0.0, float(cost_usd)),
    }
    if run_dir is None:
        return proposed
    activate_parent_attempt(parent_session_id, "")
    root = _attempt_root(run_dir, parent_session_id)
    lock_fd = os.open(root / "attempt.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        state = _read_attempt_state_unlocked(run_dir, parent_session_id)
        existing = state.get("cost_baseline")
        if isinstance(existing, dict):
            observed = _observed_budget_totals(
                {
                    "reported_usd": existing.get("reported_usd"),
                    "estimated_unpriced_usd": existing.get(
                        "estimated_unpriced_usd"
                    ),
                    "observed_cost_usd": existing.get("cost_usd"),
                }
            )
            if observed is not None:
                return observed
        state["cost_baseline"] = proposed
        _write_attempt_state_unlocked(
            run_dir,
            parent_session_id,
            state,
        )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    return proposed


def _parent_artifact_path(
    run_dir: Path,
    filename: str,
    parent_session_id: str = "",
) -> Path:
    """Return a parent-owned artifact path, preserving legacy single-run names."""

    if _attempt_state_path(run_dir, parent_session_id).is_file():
        _ensure_artifact_aliases(run_dir, parent_session_id)
    return _legacy_parent_artifact_path(
        run_dir,
        filename,
        parent_session_id,
    )


def _read_artifact_path(
    run_dir: Path,
    filename: str,
    parent_session_id: str,
) -> Path:
    """Prefer the active generation, with legacy direct-file compatibility."""

    if _attempt_state_path(run_dir, parent_session_id).is_file():
        target = _artifact_target_from_generation(
            run_dir,
            filename,
            parent_session_id,
            current_attempt_generation(parent_session_id),
        )
        if target.exists():
            return target
    return _parent_artifact_path(run_dir, filename, parent_session_id)


def _scope_parent_records(
    records: list[dict[str, Any]],
    parent_session_id: str,
) -> list[dict[str, Any]]:
    """Return only one parent's live ledger generation."""

    if not parent_session_id:
        return records
    active_generation = current_attempt_generation(parent_session_id)
    scoped: list[dict[str, Any]] = []
    for record in records:
        if record.get("parent_session_id") != parent_session_id:
            continue
        try:
            generation = int(record.get("attempt_generation") or 1)
        except (TypeError, ValueError):
            continue
        if generation == active_generation:
            scoped.append(record)
    last_tombstone = max(
        (
            index
            for index, record in enumerate(scoped)
            if record.get("record_type") == "parent_tombstone"
        ),
        default=-1,
    )
    return scoped[last_tombstone + 1 :]


def _scope_logical_attempt_records(
    records: list[dict[str, Any]],
    parent_session_id: str,
) -> list[dict[str, Any]]:
    """Recover one attempt across nonterminal generation fragmentation.

    A legitimate new attempt can start only after the preceding generation
    atomically publishes ``terminal/``. Therefore contiguous later generations
    without such a boundary belong to the same logical attempt even if an old
    runtime accidentally advanced ``current_generation`` on synthetic wakes.
    """

    if not parent_session_id:
        return records
    run_dir = _run_dir()
    active_generation = current_attempt_generation(parent_session_id)
    start_generation = 1
    if run_dir is not None:
        root = _attempt_root(run_dir, parent_session_id)
        try:
            generation_dirs = tuple(root.iterdir())
        except OSError:
            generation_dirs = ()
        terminal_generations: list[int] = []
        for generation_dir in generation_dirs:
            match = re.fullmatch(r"generation-([0-9]{8})", generation_dir.name)
            if match is None or not generation_dir.joinpath("terminal").is_dir():
                continue
            generation = int(match.group(1))
            if generation < active_generation:
                terminal_generations.append(generation)
        if terminal_generations:
            start_generation = max(terminal_generations) + 1
    scoped: list[dict[str, Any]] = []
    for record in records:
        if record.get("parent_session_id") != parent_session_id:
            continue
        try:
            generation = int(record.get("attempt_generation") or 1)
        except (TypeError, ValueError):
            continue
        if start_generation <= generation <= active_generation:
            scoped.append(record)
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
    payload = dict(payload)
    parent_session_id = str(
        payload.get("parent_session_id")
        or payload.get("turn_id")
        or ""
    )
    if parent_session_id:
        payload.setdefault(
            "attempt_generation",
            current_attempt_generation(parent_session_id),
        )
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
        "attempt_generation": current_attempt_generation(parent_session_id),
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
    active_generation = current_attempt_generation(parent_session_id)
    for record in reversed(records):
        if (
            parent_session_id
            and record.get("turn_id") != parent_session_id
        ):
            continue
        if parent_session_id:
            try:
                generation = int(record.get("attempt_generation") or 1)
            except (TypeError, ValueError):
                continue
            if generation != active_generation:
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
    parent_session_id = str(record.get("parent_session_id") or "")
    if parent_session_id:
        record.setdefault(
            "attempt_generation",
            current_attempt_generation(parent_session_id),
        )
    record.setdefault("dispatched_at_ns", time.time_ns())
    title = str(record.get("title") or "")
    parsed = parse_dispatch_title(title)
    if parsed is not None:
        record.update(parsed)
    prior = next(
        (
            item
            for item in read_dispatches(
                parent_session_id=parent_session_id
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


def read_attempt_dispatches(parent_session_id: str = "") -> list[dict[str, Any]]:
    """Return this logical attempt's dispatches across accidental fragments."""

    return _scope_logical_attempt_records(
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
    r"|audit-retry-[1-4]-1"
    r"|audit-internal-[1-4]-[1-2]"
    r"|audit-format-repair-[1-4](?:-web-[1-2])?"
    r")"
)
_AUDIT_CYCLE_HOP = re.compile(
    r"^audit-(?:cycle|internal|retry)-(?P<cycle>[1-4])(?:-web-(?P<hop>[1-2]))?"
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
        r"(?:cycle-|repair-|internal-|retry-)([1-4])(?:-|$)", title
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
    parent_session_id = str(record.get("parent_session_id") or "")
    if parent_session_id and "attempt_generation" not in record:
        child_session_id = str(record.get("child_session_id") or "")
        work_id = str(record.get("work_id") or "")
        prior_dispatch = next(
            (
                dispatch
                for dispatch in reversed(_read("routing-dispatches.jsonl"))
                if dispatch.get("parent_session_id") == parent_session_id
                and (
                    not child_session_id
                    or dispatch.get("child_session_id") == child_session_id
                )
                and (
                    not work_id
                    or dispatch.get("work_id") == work_id
                )
            ),
            None,
        )
        if prior_dispatch is not None:
            record["attempt_generation"] = int(
                prior_dispatch.get("attempt_generation") or 1
            )
        else:
            record["attempt_generation"] = current_attempt_generation(
                parent_session_id
            )
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


def read_attempt_collections(parent_session_id: str = "") -> list[dict[str, Any]]:
    """Return this logical attempt's packets across accidental fragments."""

    return _scope_logical_attempt_records(
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


def _publish_terminal_bundle(
    parent_session_id: str,
    artifacts: dict[str, bytes],
) -> bool:
    """Publish one complete terminal generation with one directory rename."""

    run_dir = _run_dir()
    if run_dir is None:
        return False
    generation = activate_parent_attempt(parent_session_id, "")
    generation_dir = _attempt_generation_dir(
        run_dir,
        parent_session_id,
        generation,
    )
    destination = generation_dir / "terminal"
    staging = generation_dir / (
        f".terminal-{os.getpid()}-{time.time_ns()}.tmp"
    )
    staging.mkdir(mode=0o700)
    try:
        for filename, data in artifacts.items():
            fd = os.open(
                staging / filename,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o400,
            )
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
        staging_fd = os.open(staging, os.O_RDONLY)
        try:
            os.fsync(staging_fd)
        finally:
            os.close(staging_fd)
        try:
            os.rename(staging, destination)
        except OSError:
            if not destination.is_dir():
                raise
            for path in staging.iterdir():
                path.unlink(missing_ok=True)
            staging.rmdir()
            return False
        generation_fd = os.open(generation_dir, os.O_RDONLY)
        try:
            os.fsync(generation_fd)
        finally:
            os.close(generation_fd)
        return True
    except BaseException:
        # A process death can leave only this hidden, unreferenced staging
        # directory. The stable aliases remain dangling until a later retry
        # atomically renames a complete directory into place.
        raise


def attest_codex_stamp(payload: dict[str, Any]) -> bool:
    """Persist one valid STAMP across a contiguous logical attempt."""

    run_dir = _run_dir()
    parent_session_id = str(payload.get("parent_session_id") or "")
    if run_dir is None or payload.get("agent") != "codex_judge":
        return False
    attempt_records = read_attempt_collections(
        parent_session_id=parent_session_id
    )
    if parent_session_id:
        child_session_id = str(payload.get("child_session_id") or "")
        work_id = str(payload.get("work_id") or "")
        recorded = next(
            (
                record
                for record in reversed(attempt_records)
                if record.get("agent") == "codex_judge"
                and record.get("title") == payload.get("title")
                and (
                    not child_session_id
                    or record.get("child_session_id") == child_session_id
                )
                and (
                    not work_id
                    or record.get("work_id") == work_id
                )
                and record.get("output") == payload.get("output")
            ),
            None,
        )
        if recorded is None:
            return False
        payload = recorded
    output = payload.get("output")
    if not isinstance(output, str):
        return False
    try:
        from triple_stamp_isaac_launcher import (
            _has_required_stage_chain,
            _mapping_candidates,
            _valid_stamp,
            _valid_voice_profile_check,
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
    if (
        not parent_session_id
        and any(record.get("parent_session_id") for record in read_collections())
    ):
        return False
    check = stamp.get("voice_profile_check") if isinstance(stamp, dict) else None
    answer = stamp.get("shippable_answer") if isinstance(stamp, dict) else None
    expected_profile = os.environ.get("TRIPLE_STAMP_VOICE_PROFILE_SHA256", "")
    if (
        not isinstance(answer, str)
        or not answer
        or not isinstance(stamp.get("citations_that_hold"), list)
        or not stamp["citations_that_hold"]
    ):
        return False
    # Voice rendering is optional; only demand the receipt when one is expected.
    if not _valid_voice_profile_check(check):
        return False
    answer_bytes = answer.encode("utf-8")
    evidence_bytes = output.encode("utf-8")
    title = str(payload.get("title") or "")
    match = re.fullmatch(r"judge-(?:cycle|convergence)-([1-4])", title)
    generation = current_attempt_generation(parent_session_id)
    if match is None or not _has_required_stage_chain(
        attempt_records,
        int(match.group(1)),
        parent_session_id=parent_session_id,
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
        "attempt_generation": generation,
        "answer_sha256": hashlib.sha256(answer_bytes).hexdigest(),
        "answer_length": len(answer_bytes),
        "voice_profile_sha256": expected_profile,
        "evidence_packet_sha256": hashlib.sha256(evidence_bytes).hexdigest(),
        "evidence_packet_length": len(evidence_bytes),
    }

    try:
        return _publish_terminal_bundle(
            parent_session_id,
            {
                "stamped-answer.bin": answer_bytes,
                "stamp-attestation.json": (
                json.dumps(attestation, sort_keys=True, separators=(",", ":"))
                + "\n"
                ).encode("utf-8"),
            },
        )
    except OSError:
        return False


def attest_latest_codex_stamp(parent_session_id: str) -> bool:
    """Recover an already-collected STAMP before any further routing."""

    if read_attested_answer(parent_session_id):
        return True
    for record in reversed(read_attempt_collections(parent_session_id)):
        if (
            record.get("agent") == "codex_judge"
            and record.get("status") == "completed"
            and attest_codex_stamp(record)
        ):
            return True
    return False


def read_attested_answer(parent_session_id: str = "") -> str:
    """Return one parent's immutable attested answer after digest verification."""

    run_dir = _run_dir()
    if run_dir is None:
        return ""
    generation = current_attempt_generation(parent_session_id)
    try:
        attestation = json.loads(
            _read_artifact_path(
                run_dir,
                "stamp-attestation.json",
                parent_session_id,
            ).read_text(encoding="utf-8")
        )
        answer = _read_artifact_path(
            run_dir,
            "stamped-answer.bin",
            parent_session_id,
        ).read_bytes()
    except (OSError, ValueError, json.JSONDecodeError):
        return ""
    try:
        attested_generation = int(
            attestation.get("attempt_generation") or 1
        )
    except (AttributeError, TypeError, ValueError):
        return ""
    if (
        not isinstance(attestation, dict)
        or attestation.get("verdict") != "STAMP"
        or (
            parent_session_id
            and attestation.get("parent_session_id") != parent_session_id
        )
        or attested_generation != generation
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
    candidate_record: dict[str, Any] | None = None,
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
    durable_scoped = read_attempt_collections(parent_session_id)
    if durable_scoped:
        enriched: list[dict[str, Any]] = []
        for record in scoped:
            durable = next(
                (
                    candidate
                    for candidate in durable_scoped
                    if candidate.get("agent") == record.get("agent")
                    and candidate.get("title") == record.get("title")
                    and str(candidate.get("child_session_id") or "")
                    == str(record.get("child_session_id") or "")
                    and str(candidate.get("work_id") or "")
                    == str(record.get("work_id") or "")
                    and candidate.get("output") == record.get("output")
                ),
                None,
            )
            enriched.append(durable or record)
        scoped = enriched
    generation = current_attempt_generation(parent_session_id)
    try:
        from triple_stamp_isaac_launcher import (
            _best_effort_provenance,
            _candidate_provenance,
            _has_required_stage_chain,
        )
    except ImportError:
        return ""
    if not _has_required_stage_chain(
        scoped,
        cycle,
        parent_session_id=parent_session_id,
        attempt_generation=generation,
    ):
        return ""
    dispatches = read_attempt_dispatches(
        parent_session_id=parent_session_id
    )
    provenance = None
    if candidate_record is not None:
        candidate_index = next(
            (
                index
                for index, record in enumerate(scoped)
                if record.get("agent") == candidate_record.get("agent")
                and record.get("title") == candidate_record.get("title")
                and str(record.get("child_session_id") or "")
                == str(candidate_record.get("child_session_id") or "")
                and str(record.get("work_id") or "")
                == str(candidate_record.get("work_id") or "")
                and record.get("output") == candidate_record.get("output")
            ),
            None,
        )
        if candidate_index is not None:
            provenance = _candidate_provenance(
                scoped,
                cycle,
                candidate_index,
                dispatches=dispatches,
                parent_session_id=parent_session_id,
            )
    else:
        provenance = _best_effort_provenance(
            scoped,
            cycle,
            dispatches=dispatches,
            parent_session_id=parent_session_id,
        )
    if provenance is None:
        return ""

    source_packets = []
    source_roles = ["cursor", "opus"]
    if provenance.get("codex") is not None:
        source_roles.append("codex")
    for role in source_roles:
        record = provenance[role]
        output_bytes = str(record["output"]).encode("utf-8")
        source_packets.append(
            {
                "agent": record["agent"],
                "title": str(record.get("title") or ""),
                "child_session_id": str(record.get("child_session_id") or ""),
                "work_id": str(record.get("work_id") or ""),
                "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
                "output_length": len(output_bytes),
                "collection_sequence": int(
                    provenance["collection_sequences"][role]
                ),
                "collected_at_ns": int(record.get("collected_at_ns") or 0),
                "dispatch_sequence": int(
                    provenance["dispatch_sequences"][role]
                ),
                "dispatched_at_ns": int(
                    provenance["dispatch_timestamps"][role]
                ),
            }
        )

    answer_bytes = clean_answer.encode("utf-8")
    attestation = {
        "version": 1,
        "verdict": "BEST_EFFORT",
        "quality_approved": False,
        "cycle": max(0, int(cycle)),
        "parent_session_id": parent_session_id,
        "attempt_generation": generation,
        "reason": " ".join(str(reason).replace("\x00", " ").split())[:2000],
        "answer_sha256": hashlib.sha256(answer_bytes).hexdigest(),
        "answer_length": len(answer_bytes),
        "provenance_version": 1,
        "source_packets": source_packets,
    }

    try:
        published = _publish_terminal_bundle(
            parent_session_id,
            {
                "best-effort-answer.bin": answer_bytes,
                "best-effort-attestation.json": (
                    json.dumps(
                        attestation,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8"),
            },
        )
    except OSError:
        return ""
    if not published:
        return read_best_effort_answer(parent_session_id)
    return clean_answer


def read_best_effort_answer(parent_session_id: str = "") -> str:
    """Return one parent's complete non-STAMP answer after digest verification."""

    run_dir = _run_dir()
    if run_dir is None:
        return ""
    generation = current_attempt_generation(parent_session_id)
    try:
        attestation = json.loads(
            _read_artifact_path(
                run_dir,
                "best-effort-attestation.json",
                parent_session_id,
            ).read_text(encoding="utf-8")
        )
        answer = _read_artifact_path(
            run_dir,
            "best-effort-answer.bin",
            parent_session_id,
        ).read_bytes()
    except (OSError, ValueError, json.JSONDecodeError):
        return ""
    try:
        attested_generation = int(
            attestation.get("attempt_generation") or 1
        )
    except (AttributeError, TypeError, ValueError):
        return ""
    if (
        not isinstance(attestation, dict)
        or attestation.get("verdict") != "BEST_EFFORT"
        or attestation.get("quality_approved") is not False
        or attestation.get("provenance_version") != 1
        or attested_generation != generation
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
    sources = attestation.get("source_packets")
    if not isinstance(sources, list):
        return ""
    records = read_attempt_collections(parent_session_id)
    dispatches = read_attempt_dispatches(parent_session_id)
    try:
        from triple_stamp_isaac_launcher import (
            _best_effort_provenance,
            _candidate_provenance,
        )

        codex_source = next(
            (
                source
                for source in sources
                if isinstance(source, dict)
                and source.get("agent") == "codex_judge"
            ),
            None,
        )
        if codex_source is not None:
            candidate_index = next(
                (
                    index
                    for index, record in enumerate(records)
                    if record.get("agent") == codex_source.get("agent")
                    and record.get("title") == codex_source.get("title")
                    and str(record.get("child_session_id") or "")
                    == str(codex_source.get("child_session_id") or "")
                    and str(record.get("work_id") or "")
                    == str(codex_source.get("work_id") or "")
                ),
                None,
            )
            provenance = (
                _candidate_provenance(
                    records,
                    int(attestation.get("cycle") or 0),
                    candidate_index,
                    dispatches=dispatches,
                    parent_session_id=parent_session_id,
                )
                if candidate_index is not None
                else None
            )
        else:
            provenance = _best_effort_provenance(
                records,
                int(attestation.get("cycle") or 0),
                dispatches=dispatches,
                parent_session_id=parent_session_id,
            )
    except (ImportError, TypeError, ValueError):
        return ""
    if provenance is None:
        return ""
    roles = ["cursor", "opus"]
    if provenance.get("codex") is not None:
        roles.append("codex")
    if len(sources) != len(roles):
        return ""
    for source, role in zip(sources, roles, strict=True):
        record = provenance[role]
        if not isinstance(source, dict):
            return ""
        output = str(record.get("output") or "").encode("utf-8")
        if (
            source.get("agent") != record.get("agent")
            or source.get("title") != str(record.get("title") or "")
            or source.get("child_session_id")
            != str(record.get("child_session_id") or "")
            or source.get("work_id") != str(record.get("work_id") or "")
            or source.get("collection_sequence")
            != int(provenance["collection_sequences"][role])
            or source.get("collected_at_ns")
            != int(record.get("collected_at_ns") or 0)
            or source.get("dispatch_sequence")
            != int(provenance["dispatch_sequences"][role])
            or source.get("dispatched_at_ns")
            != int(provenance["dispatch_timestamps"][role])
            or source.get("output_length") != len(output)
            or source.get("output_sha256")
            != hashlib.sha256(output).hexdigest()
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


def read_supervisor_tool_calls(
    parent_session_id: str = "",
) -> list[dict[str, Any]]:
    """Return only the active attempt's durable supervisor tool calls."""

    return _scope_parent_records(
        _read("supervisor-tool-calls.jsonl"),
        parent_session_id,
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
    observed_cost_usd: float | None = None,
    observed_reported_usd: float | None = None,
    observed_estimated_unpriced_usd: float | None = None,
) -> None:
    """Atomically retain the latest attempt-local budget decision."""

    run_dir = _run_dir()
    if run_dir is None:
        return
    generation = activate_parent_attempt(parent_session_id, "")
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
        "attempt_generation": generation,
        "observed_cost_usd": round(
            max(
                0.0,
                float(
                    observed_cost_usd
                    if observed_cost_usd is not None
                    else cost
                ),
            ),
            6,
        ),
        "observed_reported_usd": round(
            max(
                0.0,
                float(
                    observed_reported_usd
                    if observed_reported_usd is not None
                    else reported_usd
                    if reported_usd is not None
                    else cost
                ),
            ),
            6,
        ),
        "observed_estimated_unpriced_usd": round(
            max(
                0.0,
                float(
                    observed_estimated_unpriced_usd
                    if observed_estimated_unpriced_usd is not None
                    else estimated_unpriced_usd
                    or 0.0
                ),
            ),
            6,
        ),
    }
    destination = _artifact_target_from_generation(
        run_dir,
        "budget-state.json",
        parent_session_id,
        generation,
    )
    temporary = destination.with_name(
        f".{destination.name}-{os.getpid()}-{time.time_ns()}.tmp"
    )
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
    """Return the active attempt's latest budget decision."""

    run_dir = _run_dir()
    if run_dir is None:
        return {}
    try:
        payload = json.loads(
            _read_artifact_path(
                run_dir,
                "budget-state.json",
                parent_session_id,
            ).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    try:
        generation = int(payload.get("attempt_generation") or 1)
    except (TypeError, ValueError):
        return {}
    if generation != current_attempt_generation(parent_session_id):
        return {}
    return payload


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
    result_bytes = result.encode("utf-8")
    generation = current_attempt_generation(parent_session_id)
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
        "attempt_generation": generation,
        "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
        "result_length": len(result_bytes),
    }

    try:
        _publish_terminal_bundle(
            parent_session_id,
            {
                "terminal-failure.txt": result_bytes,
                "failure-attestation.json": (
                json.dumps(attestation, sort_keys=True, separators=(",", ":"))
                + "\n"
                ).encode("utf-8"),
            },
        )
    except OSError:
        return result
    try:
        return _read_artifact_path(
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
    generation = current_attempt_generation(parent_session_id)
    try:
        data = _read_artifact_path(
            run_dir,
            "terminal-failure.txt",
            parent_session_id,
        ).read_bytes()
        attestation = json.loads(
            _read_artifact_path(
                run_dir,
                "failure-attestation.json",
                parent_session_id,
            ).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return ""
    try:
        attested_generation = int(
            attestation.get("attempt_generation") or 1
        )
    except (AttributeError, TypeError, ValueError):
        return ""
    if (
        not isinstance(attestation, dict)
        or attested_generation != generation
        or (
            parent_session_id
            and attestation.get("parent_session_id") != parent_session_id
        )
    ):
        return ""
    result_digest = attestation.get("result_sha256")
    result_length = attestation.get("result_length")
    if result_digest is None and result_length is None and generation == 1:
        # Compatibility with retained pre-generation failure pairs. New writes
        # always carry both fields and publish atomically.
        pass
    elif (
        result_length != len(data)
        or not isinstance(result_digest, str)
        or hashlib.sha256(data).hexdigest() != result_digest
    ):
        return ""
    try:
        return data.decode("utf-8").strip()
    except UnicodeDecodeError:
        return ""
