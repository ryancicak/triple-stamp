"""Keep every finished Triple-stamp answer with a verifiable evidence dossier.

Browser runs are ephemeral: ``/quit`` deletes the private run directory, and
with it the answer, its attestation, and every packet that justified it. A
chat also keeps only its newest answer there, so a follow-up question hides
the previous one. The outer launcher therefore calls :func:`export_run_answers`
every few seconds while the run is live and once more before cleanup, so each
terminal result survives as a small folder: the exact answer bytes, the
attestation, and a readable dossier of how the answer was verified.

Only the outer launcher runs this, outside the Seatbelt. It reads the durable
ledgers and never changes them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

ANSWER_DIR_ENV = "TRIPLE_STAMP_ANSWER_DIR"
_DISABLED = frozenset({"", "0", "off", "none"})
_TERMINALS = (
    ("stamp-attestation", "stamped-answer.bin", "STAMP"),
    ("best-effort-attestation", "best-effort-answer.bin", "BEST_EFFORT"),
    ("failure-attestation", "terminal-failure.txt", "FAILED"),
)
_WORKERS = {
    "cursor_workhorse": "Cursor (Grok 4.6 Extra High)",
    "opus_auditor": "Opus 5 (max effort)",
    "codex_judge": "Codex (GPT-5.6 Sol, ultra)",
}
_VERDICT_LINES = {
    "STAMP": "STAMP: every stage passed; read it through, then send it",
    "BEST_EFFORT": (
        "BEST EFFORT: complete answer with its evidence gaps named; not stamped"
    ),
    "FAILED": "FAILED: the run ended without an answer; evidence so far is below",
}


def answer_library_dir(real_home: Path) -> Path | None:
    """Return the configured library, the private default, or ``None`` if off."""

    configured = os.environ.get(ANSWER_DIR_ENV)
    if configured is None:
        return real_home / ".local/share/triple-stamp/answers"
    if configured.strip().lower() in _DISABLED:
        return None
    return Path(configured).expanduser()


def _digest(parent_session_id: str) -> str:
    return hashlib.sha256(parent_session_id.encode("utf-8")).hexdigest()[:16]


def _jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _conversation(run_dir: Path, parent_session_id: str) -> tuple[str, str, float]:
    """Return ``(title, question, asked_at)`` from the run's chat store."""

    try:
        from omnigent.db.compression import decode
    except ImportError:
        return "", "", 0.0
    try:
        connection = sqlite3.connect(
            f"file:{run_dir / 'state/chat.db'}?mode=ro", uri=True
        )
    except sqlite3.Error:
        return "", "", 0.0
    try:
        row = connection.execute(
            "SELECT title FROM conversations WHERE lower(hex(id)) = ?",
            (parent_session_id.lower(),),
        ).fetchone()
        title = str(row[0] or "") if row else ""
        items = connection.execute(
            "SELECT data, created_at FROM conversation_items "
            "WHERE lower(hex(conversation_id)) = ? ORDER BY position",
            (parent_session_id.lower(),),
        ).fetchall()
    except sqlite3.Error:
        return "", "", 0.0
    finally:
        connection.close()
    question, asked_at = "", 0.0
    for data, created_at in items:
        try:
            item = json.loads(decode(data) if isinstance(data, bytes) else data)
        except (TypeError, ValueError):
            continue
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        text = "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict)
        ) if isinstance(content, list) else str(content or "")
        if text.strip() and not text.startswith(("[System:", "TRIPLE_STAMP_")):
            # The latest genuine user turn is the question this attempt answered.
            question, asked_at = text.strip(), float(created_at or 0)
    return title, question, asked_at


def _payload(output: str) -> dict:
    """Return the first JSON object in a packet, or ``{}``."""

    start = output.find("{")
    while start >= 0:
        try:
            value, _end = json.JSONDecoder().raw_decode(output[start:])
        except ValueError:
            start = output.find("{", start + 1)
            continue
        return value if isinstance(value, dict) else {}
    return {}


def _duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds:02d}s"


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 ._-]+", " ", text).strip()
    return re.sub(r"\s+", " ", cleaned)[:70].strip() or "answer"


def _stage_rows(
    collections: list[dict],
    dispatches: list[dict],
) -> list[str]:
    rows = []
    started = {
        str(row.get("work_id") or ""): int(row.get("dispatched_at_ns") or 0)
        for row in dispatches
    }
    for record in collections:
        agent = str(record.get("agent") or "")
        output = str(record.get("output") or "")
        payload = _payload(output)
        verdict = str(payload.get("verdict") or record.get("status") or "")
        if agent == "cursor_workhorse":
            urls = sorted(set(re.findall(r"https?://[^\s)\]>\"'`]+", output)))
            result = f"{len(urls)} distinct URLs cited"
        elif agent == "opus_auditor":
            observation = record.get("internal_mcp_observation") or {}
            counts = observation.get("by_system") or {}
            effort = record.get("opus_effort_observation") or {}
            internal = ", ".join(f"{name} {counts[name]}" for name in counts)
            result = (
                f"{verdict}; internal calls: {internal or 'not observed'}; "
                f"effort {'max' if effort.get('compliant') else effort.get('status', 'unknown')}"
            )
        else:
            limitations = payload.get("limitations") or []
            result = f"{verdict}; {len(limitations)} limitation(s)"
        begin = started.get(str(record.get("work_id") or ""), 0)
        end = int(record.get("collected_at_ns") or 0)
        took = _duration((end - begin) / 1e9) if begin and end > begin else "n/a"
        rows.append(
            f"| {record.get('title')} | {_WORKERS.get(agent, agent)} | "
            f"{result.replace('|', '/')} | {took} |"
        )
    return rows


def _dossier(
    *,
    verdict: str,
    title: str,
    question: str,
    asked_at: float,
    answer: str,
    attestation: dict,
    budget: dict,
    collections: list[dict],
    dispatches: list[dict],
) -> str:
    judgment = next(
        (
            _payload(str(record.get("output") or ""))
            for record in reversed(collections)
            if record.get("agent") == "codex_judge"
        ),
        {},
    )
    finished = max(
        (int(record.get("collected_at_ns") or 0) for record in collections),
        default=0,
    )
    elapsed = (
        _duration(finished / 1e9 - asked_at) if finished and asked_at else "n/a"
    )
    cost = budget.get("cost_usd")
    cost_text = (
        f"${float(cost):.2f} (provider-reported ${float(budget.get('reported_usd') or 0):.2f}"
        f" + conservative estimate ${float(budget.get('estimated_unpriced_usd') or 0):.2f})"
        if isinstance(cost, (int, float))
        else "unavailable"
    )
    voice = judgment.get("voice_profile_check") or {}
    voice_text = (
        f"{Path(str(voice.get('source_path'))).name} (sha256 {str(voice.get('sha256'))[:12]}...)"
        if voice.get("source_path")
        else "plain prose (no voice profile)"
    )
    lines = [
        f"# {title or question.splitlines()[0][:90] if question else 'Triple-stamp answer'}",
        "",
        f"**Verdict:** {_VERDICT_LINES.get(verdict, verdict)}",
        "",
        f"- **Asked:** {datetime.fromtimestamp(asked_at).strftime('%Y-%m-%d %H:%M') if asked_at else 'n/a'}",
        f"- **Answered in:** {elapsed} (cycle {attestation.get('cycle', 'n/a')})",
        f"- **Cost:** {cost_text}",
        f"- **Voice:** {voice_text}",
        "",
    ]
    # The sendable answer carries no review-process caveats, so the judge's
    # notes for the requester come first, before anything is sent.
    notes = judgment.get("limitations")
    if isinstance(notes, list) and notes:
        lines += [
            "## Notes for you (not part of the answer)",
            "",
            *(f"- {note}" for note in notes),
            "",
        ]
    lines += [
        "## Question",
        "",
        *(f"> {line}" if line else ">" for line in (question or "(unavailable)").splitlines()),
        "",
        "## Answer",
        "",
        answer.rstrip(),
        "",
        "## How it was verified",
        "",
        "| Stage | Worker | Result | Took |",
        "| --- | --- | --- | --- |",
        *_stage_rows(collections, dispatches),
    ]
    citations = judgment.get("citations_that_hold")
    if isinstance(citations, list) and citations:
        lines += [
            "",
            "### Citations the judge kept",
            "",
            *(f"- {citation}" for citation in citations),
        ]
    lines += ["", "## Exact evidence packets", ""]
    for record in collections:
        output = str(record.get("output") or "")
        lines += [
            "<details>",
            f"<summary>{record.get('title')} from {_WORKERS.get(str(record.get('agent')), record.get('agent'))}"
            f" ({len(output):,} chars)</summary>",
            "",
            "````text",
            output.rstrip(),
            "````",
            "",
            "</details>",
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _rebuild_index(library: Path) -> None:
    entries = []
    for dossier in library.glob("*/dossier.md"):
        try:
            meta = json.loads((dossier.parent / "entry.json").read_text("utf-8"))
        except (OSError, ValueError):
            continue
        entries.append((str(meta.get("asked") or ""), dossier.parent.name, meta))
    lines = ["# Triple-stamp answers", "", "Newest first. Each folder keeps the exact answer, its attestation, and a dossier.", ""]
    for asked, folder, meta in sorted(entries, reverse=True):
        lines.append(
            f"- {asked} · {meta.get('verdict')} · [{meta.get('title') or folder}](<{folder}/dossier.md>)"
        )
    _write_private(library / "index.md", ("\n".join(lines) + "\n").encode("utf-8"))


def _known_answers(library: Path) -> set[tuple[str, str]]:
    """Return the ``(parent, answer sha256)`` pairs the library already keeps."""

    known = set()
    for marker in library.glob("*/entry.json"):
        try:
            meta = json.loads(marker.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict):
            known.add(
                (
                    str(meta.get("parent_session_id") or ""),
                    str(meta.get("answer_sha256") or ""),
                )
            )
    return known


def export_run_answers(
    run_dir: Path,
    library: Path,
    *,
    live: bool = False,
) -> list[Path]:
    """Save each parent's terminal result from ``run_dir`` into ``library``.

    Re-exporting the same attested bytes is a no-op wherever the earlier copy
    was filed, so calling this repeatedly during and after a run is safe. Bytes
    that do not match their attestation's digest are never filed. With
    ``live``, a result whose question the chat store cannot supply yet waits
    for a later call instead of being filed without it.

    :returns: Folders written by this call.
    """

    written: list[Path] = []
    known = _known_answers(library)
    collections_all = _jsonl(run_dir / "routing-collections.jsonl")
    dispatches_all = _jsonl(run_dir / "routing-dispatches.jsonl")
    seen_parents: set[str] = set()
    for prefix, answer_name, verdict in _TERMINALS:
        for attestation_path in sorted(run_dir.glob(f"{prefix}-*.json")):
            try:
                attestation = json.loads(attestation_path.read_text("utf-8"))
            except (OSError, ValueError):
                continue  # a dangling alias: that parent never finished
            parent = str(attestation.get("parent_session_id") or "")
            if not parent or parent in seen_parents:
                continue
            seen_parents.add(parent)
            digest = _digest(parent)
            stem, suffix = os.path.splitext(answer_name)
            try:
                answer = (run_dir / f"{stem}-{digest}{suffix}").read_bytes()
            except OSError:
                continue
            answer_digest = hashlib.sha256(answer).hexdigest()
            expected = attestation.get("answer_sha256") or attestation.get(
                "result_sha256"
            )
            if isinstance(expected, str) and expected != answer_digest:
                continue  # read mid-publish; the next call sees the finished pair
            if (parent, answer_digest) in known:
                continue
            generation = int(attestation.get("attempt_generation") or 1)
            scoped = [
                row
                for row in collections_all
                if row.get("parent_session_id") == parent
                and int(row.get("attempt_generation") or 1) == generation
            ]
            dispatches = [
                row for row in dispatches_all if row.get("parent_session_id") == parent
            ]
            try:
                budget = json.loads(
                    (run_dir / f"budget-state-{digest}.json").read_text("utf-8")
                )
            except (OSError, ValueError):
                budget = {}
            title, question, asked_at = _conversation(run_dir, parent)
            if live and not question:
                continue
            asked = (
                datetime.fromtimestamp(asked_at).strftime("%Y-%m-%d %H:%M")
                if asked_at
                else datetime.now().strftime("%Y-%m-%d %H:%M")
            )
            name = f"{asked.replace(':', '')} {verdict} {_slug(title or question)}"
            folder = library / name
            copy = 2
            while (folder / "entry.json").exists():
                # A different answer already owns this name; keep both.
                folder = library / f"{name} ({copy})"
                copy += 1
            marker = folder / "entry.json"
            _write_private(folder / "answer.md", answer)
            _write_private(
                folder / "attestation.json",
                (json.dumps(attestation, indent=2, sort_keys=True) + "\n").encode(),
            )
            _write_private(
                folder / "dossier.md",
                _dossier(
                    verdict=verdict,
                    title=title,
                    question=question,
                    asked_at=asked_at,
                    answer=answer.decode("utf-8", errors="replace"),
                    attestation=attestation,
                    budget=budget,
                    collections=scoped,
                    dispatches=dispatches,
                ).encode("utf-8"),
            )
            _write_private(
                marker,
                json.dumps(
                    {
                        "asked": asked,
                        "verdict": verdict,
                        "title": title or question[:90],
                        "answer_sha256": answer_digest,
                        "parent_session_id": parent,
                    },
                    indent=2,
                ).encode("utf-8"),
            )
            known.add((parent, answer_digest))
            written.append(folder)
    if written:
        _rebuild_index(library)
    return written
