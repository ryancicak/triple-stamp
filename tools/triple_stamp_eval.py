#!/usr/bin/env python3
"""Run a question set through the real Triple-stamp pipeline and score it.

    python3 tools/triple_stamp_eval.py eval/questions.example.json \
        [--voice ~/path/profile.md] [--out /tmp/triple-stamp-eval] [--timeout-min 150]

This makes paid model calls: it launches ``./triple-stamp`` exactly as a user
would (browser mode, no TTY), submits every question through the same local
API the web UI uses, one chat per question, waits for each durable terminal
result, stops the run the way ``/quit`` does, and writes ``scorecard.md`` plus
``results.json``. Nothing here changes pipeline behavior; it only observes the
run directory's ledgers and the chat store read-only.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_DIR = Path("/tmp") / (
    f"omnigent-triple-stamp-{os.getuid()}-"
    + hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]
)
_ATTACHED = "[System-attached exact packets]"
# Review-process notes that belong in the judge's limitations, never in the
# text the requester sends. Citing an internal document stays allowed.
# A bare "during validation" also matched "Keep the message bus in place during
# validation", advice about the customer's own pilot, so only the review's own
# validation wording counts.
_PROCESS_NOTE = re.compile(
    r"Review caveat|Triple-stamp"
    r"|during (?:this |the )?(?:review|validation) (?:stage|process|pipeline|run)"
    r"|\b(?:Glean|Jira|Slack|Confluence|SAFE)\b[^.\n]{0,80}"
    r"\b(?:unavailable|unreachable|could not be (?:reached|searched))"
)
_EXPECTED_ACTIONS = {
    "continuation_enqueued",
    "deterministic_stamp_relay",
    "terminal_stamp_delivered",
    "terminal_stamp_suppressed",
    "deterministic_best_effort_relay",
    "ordinary_response_suppressed",
    "question_queued",
    "question_admitted",
}
# A question that waited for a free slot shows this line before its first step.
_QUEUE_NOTE = re.compile(r"\AWaiting for a free slot:[^\n]*\n\n")


def _progress_readable(shown: list[str]) -> bool:
    return any(
        _QUEUE_NOTE.sub("", text).startswith("Step 1 of 3: ") for text in shown
    ) and not any("TRIPLE_STAMP_PROGRESS" in text for text in shown)


def _no_policy_text(shown: list[str]) -> bool:
    # Omnigent saves a denied reply as "[Denied by policy: <reason>]".
    return not any("[Denied by policy" in text for text in shown)


def _api(base: str, method: str, path: str, body: object | None = None) -> dict:
    request = urllib.request.Request(
        base + path,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
    value = json.loads(raw) if raw else {}
    return value if isinstance(value, dict) else {}


def _jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return [row for row in rows if isinstance(row, dict)]


def _payload(text: str) -> dict:
    start = text.find("{")
    while start >= 0:
        try:
            value, _ = json.JSONDecoder().raw_decode(text[start:])
            return value if isinstance(value, dict) else {}
        except ValueError:
            start = text.find("{", start + 1)
    return {}


def _assistant_texts(base: str, session: str) -> list[str]:
    """Return one chat's assistant messages through the API the web UI reads."""

    texts: list[str] = []
    after = None
    while True:
        query = "?limit=1000&order=asc" + (f"&after={after}" if after else "")
        rows = _api(base, "GET", f"/v1/sessions/{session}/items{query}").get("data", [])
        for item in rows:
            if not isinstance(item, dict):
                continue
            role = item.get("role") or (item.get("data") or {}).get("role")
            content = item.get("content") or (item.get("data") or {}).get("content")
            if role == "assistant" and isinstance(content, list):
                text = "".join(
                    str(part.get("text") or "") for part in content if isinstance(part, dict)
                )
                if text:
                    texts.append(text)
        if len(rows) < 1000:
            return texts
        after = rows[-1].get("id")


def _words(text: str) -> set[str]:
    # Five-letter stems, so "blockers" in research matches "blocking" in the
    # question; whole words failed a pilot chat whose own research said
    # "blockers" throughout.
    return {word[:5] for word in re.findall(r"[a-z]{5,}", text.lower())}


def _on_topic(research: str, question: str, rivals: list[str]) -> bool | None:
    """Whether research covers its own question better than any rival's.

    Only the words that set each question apart count, compared as the share
    of each question's own words the research uses, so a longer question does
    not win by size. This catches concurrent chats swapping their research.
    Research that uses at least half of its own distinctive words is on topic
    even when a rival on the same subject scores higher: on 2026-09-27 a pilot
    question with three distinctive words and a customer email about the same
    pilot tripped this check although each chat had its own research.
    ``None`` means the question has no distinctive words (``5+5=?``) to judge.
    """

    def share(words: set[str]) -> float:
        return len(words & found) / len(words) if words else 0.0

    own_words = _words(question) - set().union(*(_words(rival) for rival in rivals))
    if not own_words:
        return None
    found = _words(research)
    own = share(own_words)
    return own >= 0.5 or all(own > share(_words(rival) - _words(question)) for rival in rivals)


_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\([^)\s]*\)")
_BARE_URL = re.compile(r"https?://\S+")
# Salesforce record pages; Glean returns one as each Salesforce result's `url`.
_SALESFORCE_LINK = r"https://[^\s)\]]*(?:lightning\.force\.com|\.my\.salesforce\.com)/"
# Links that a reader outside Databricks cannot open.
_INTERNAL_LINK = re.compile(
    r"https?://[^\s)\]]*(?:slack\.com|atlassian\.net|force\.com|glean\.com"
    r"|docs\.google\.com|drive\.google\.com|azuredatabricks\.net)"
    r"|(?:\]\(|https?://)go/"
)
# Opus's coverage line reports tools outside the five families, such as the
# usage add-on's tool.
_USAGE_OBSERVED = re.compile(r"other=[^\]]*\busage=[1-9]")
_USAGE_TOOL_MENTION = re.compile(r"customer_usage|usage (?:tool|add-on|server)", re.IGNORECASE)


def _usage_checks(
    question: Question,
    answer: str,
    audit_outputs: list[str],
    limitations: list[str],
    installed: bool,
) -> dict[str, bool]:
    """Usage for account questions, with or without a usage add-on.

    With one, the figures must come from an observed and cited call. Without
    one, the tool does not exist, and neither the answer nor the notes the
    requester reads may mention it. Opus's own packet may record that the tool
    was absent; that is evidence, not noise.
    """

    if not question.expect_usage:
        return {}
    audits = [_payload(text) for text in audit_outputs]
    if not installed:
        return {
            "usage_quiet": not any(
                _USAGE_TOOL_MENTION.search(text) for text in (answer, *limitations)
            )
        }
    cited = any(
        str(source.get("system") or "").lower() == "usage"
        for audit in audits
        for source in audit.get("internal_sources_consulted") or []
        if isinstance(source, dict)
    )
    checks = {
        "usage_evidence": cited
        and any(_USAGE_OBSERVED.search(text) for text in audit_outputs)
    }
    # Only a question about spend must quote it; a pilot-status answer may use
    # usage as context without dollar figures.
    if question.expect_usage_figures and question.audience != "customer":
        checks["usage_in_answer"] = bool(re.search(r"\$\s?\d", answer))
    return checks


def _salesforce_evidence(audits: list[dict]) -> bool:
    """Whether any audit relied on a Salesforce record and linked it."""

    return any(
        re.search(_SALESFORCE_LINK, str(source.get("url_or_record_id") or ""))
        for audit in audits
        for source in audit.get("internal_sources_consulted") or []
        if isinstance(source, dict)
    )


def _audience_checks(question: Question, answer: str, audits: list[dict]) -> dict[str, bool]:
    """Salesforce use for account questions; no internal links for customers."""

    checks: dict[str, bool] = {}
    if question.expect_salesforce:
        checks["salesforce_evidence"] = _salesforce_evidence(audits)
        if question.audience != "customer":
            checks["salesforce_cited"] = bool(re.search(r"\]\(" + _SALESFORCE_LINK, answer))
    if question.audience == "customer":
        checks["no_internal_links"] = _INTERNAL_LINK.search(answer) is None
    return checks


def _unlinked_internal_refs(answer: str, evidence: str) -> list[str]:
    """Return linked-in-evidence Jira keys and Slack threads the answer never links.

    Only references the audit itself linked are checked, so ordinary tokens
    such as ``SHA-256`` never count. A reference linked once may be named
    again in plain prose; one that appears only bare is reported.
    """

    links = " ".join(
        [*_MARKDOWN_LINK.findall(answer), *_BARE_URL.findall(answer)]
    )
    plain = _BARE_URL.sub(" ", _MARKDOWN_LINK.sub(" ", answer))
    refs = set(re.findall(r"/browse/([A-Z][A-Z0-9]+-\d+)", evidence))
    refs |= {
        f"{channel}/{message}"
        for channel, message in re.findall(r"/archives/(C[A-Z0-9]+)/(p\d+)", evidence)
    }
    return sorted(
        ref
        for ref in refs
        if re.search(rf"(?<![\w/-]){re.escape(ref)}(?![\w-])", plain) and ref not in links
    )


def _blank_in_terminal(answer: str) -> list[str]:
    """Return answer paragraphs the terminal REPL's Markdown renderer draws blank.

    The REPL renders assistant prose paragraph by paragraph with ``rich``; a
    stamped ``10.`` parses as an empty numbered list and showed nothing.
    """

    try:
        from rich.console import Console
        from rich.markdown import Markdown
    except ImportError:  # the harness may run outside the managed runtime
        return []
    blank = []
    for paragraph in answer.split("\n\n"):
        if not paragraph.strip():
            continue
        rendered = io.StringIO()
        Console(file=rendered, width=100, color_system=None).print(Markdown(paragraph))
        if not rendered.getvalue().strip():
            blank.append(paragraph)
    return blank


def _saved(library: Path, session: str, answer: str) -> bool:
    digest = hashlib.sha256(answer.encode("utf-8")).hexdigest()
    for marker in library.glob("*/entry.json"):
        try:
            meta = json.loads(marker.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if meta.get("parent_session_id") == session and meta.get("answer_sha256") == digest:
            return True
    return False


def _child_prompts(run: Path) -> dict[str, str]:
    """Map child conversation title to its first user prompt (read-only)."""

    try:
        from omnigent.db.compression import decode
    except ImportError:  # the harness may run outside the managed runtime
        decode = None
    prompts: dict[str, str] = {}
    try:
        db = sqlite3.connect(f"file:{run / 'state/chat.db'}?mode=ro", uri=True)
        rows = db.execute(
            "SELECT lower(hex(c.id)), c.title, i.data FROM conversations c "
            "JOIN conversation_items i ON i.conversation_id = c.id "
            "WHERE i.type = 1 ORDER BY c.created_at, i.position"
        ).fetchall()
        db.close()
    except sqlite3.Error:
        return prompts
    for conv_id, title, data in rows:
        if conv_id in prompts:
            continue
        try:
            raw = decode(data) if decode and isinstance(data, bytes) else data
            item = json.loads(raw)
        except (TypeError, ValueError):
            continue
        content = item.get("content") if isinstance(item, dict) else None
        if isinstance(content, list) and item.get("role") == "user":
            prompts[conv_id] = f"{title}\n" + "".join(
                str(part.get("text") or "") for part in content if isinstance(part, dict)
            )
    return prompts


class Question:
    def __init__(self, spec: dict, index: int) -> None:
        self.label = str(spec.get("label") or f"q{index + 1}")
        self.text = str(spec["question"]).strip()
        # Arithmetic needs no web citation; internal questions need internal links.
        self.expect_citations = spec.get("expect_citations", True) is not False
        self.expect_internal_links = spec.get("expect_internal_links") is True
        # Account questions should draw on Salesforce; a "customer" audience
        # means the answer will leave Databricks and must carry no internal links.
        self.expect_salesforce = spec.get("expect_salesforce") is True
        # Account questions that should draw on a usage add-on when one exists.
        self.expect_usage = spec.get("expect_usage") is True
        self.expect_usage_figures = spec.get("expect_usage_figures") is True
        self.audience = str(spec.get("audience") or "")
        self.session = ""
        self.posted_ns = 0
        self.snapshot: dict[str, str] = {}
        self.result: dict = {}


def _terminal(run: Path, parent: str) -> dict[str, str]:
    digest = hashlib.sha256(parent.encode()).hexdigest()[:16]
    found = {}
    for name in ("stamp-attestation", "best-effort-attestation", "failure-attestation"):
        try:
            found[name] = (run / f"{name}-{digest}.json").read_text("utf-8")
        except OSError:
            pass
    return found


def _score(
    run: Path,
    base: str,
    question: Question,
    kind: str,
    attestation: dict,
    rivals: list[str],
) -> dict:
    collections = [
        row
        for row in _jsonl(run / "routing-collections.jsonl")
        if row.get("parent_session_id") == question.session
        and int(row.get("collected_at_ns") or 0) >= question.posted_ns
    ]
    dispatches = {
        str(row.get("work_id")): int(row.get("dispatched_at_ns") or 0)
        for row in _jsonl(run / "routing-dispatches.jsonl")
        if row.get("parent_session_id") == question.session
    }
    digest = hashlib.sha256(question.session.encode()).hexdigest()[:16]
    answer_name = {
        "stamp-attestation": "stamped-answer",
        "best-effort-attestation": "best-effort-answer",
    }.get(kind)
    try:
        answer = (
            (run / f"{answer_name}-{digest}.bin").read_text("utf-8")
            if answer_name
            else (run / f"terminal-failure-{digest}.txt").read_text("utf-8")
        )
    except OSError:
        answer = ""
    try:
        budget = json.loads((run / f"budget-state-{digest}.json").read_text("utf-8"))
    except (OSError, ValueError):
        budget = {}
    judgment = next(
        (
            _payload(str(row.get("output") or ""))
            for row in reversed(collections)
            if row.get("agent") == "codex_judge"
        ),
        {},
    )
    limitations = [str(item) for item in judgment.get("limitations") or []]
    stages = []
    for row in collections:
        begin = dispatches.get(str(row.get("work_id")), 0)
        end = int(row.get("collected_at_ns") or 0)
        stages.append(
            {
                "title": row.get("title"),
                "status": row.get("status"),
                "seconds": round((end - begin) / 1e9) if begin and end > begin else None,
                "bytes": len(str(row.get("output") or "")),
            }
        )
    # Only this question's children count; concurrent chats share one store.
    children = {
        str(row.get("child_session_id") or "")
        for row in _jsonl(run / "routing-dispatches.jsonl")
        if row.get("parent_session_id") == question.session
    }
    prompts = _child_prompts(run)
    downstream = [
        text
        for child, text in prompts.items()
        if child in children
        and re.match(r"(opus_auditor|codex_judge):(audit|judge)-", text)
    ]
    continuations = {
        str(row.get("action"))
        for row in _jsonl(run / "supervisor-continuations.jsonl")
        if row.get("parent_session_id") == question.session
    }
    voice = judgment.get("voice_profile_check") or {}
    expected_voice = os.environ.get("TRIPLE_STAMP_VOICE_PROFILE", "").strip()
    shown: list[str] = []
    relay_deadline = time.time() + 90
    while time.time() < relay_deadline:
        # The attestation lands a moment before the supervisor relays it.
        try:
            shown = _assistant_texts(base, question.session)
        except OSError:
            shown = []
        if answer and answer in shown:
            break
        time.sleep(3)
    checks = {
        "stamped": kind == "stamp-attestation",
        "no_em_dash": "—" not in answer,
        "has_citations": not question.expect_citations
        or bool(re.search(r"https?://", answer))
        or "[1]" in answer,
        "voice_receipt": (
            voice.get("source_path") == expected_voice
            if expected_voice
            else not voice.get("source_path")
        ),
        "exact_packets_transported": bool(downstream)
        and all(_ATTACHED in text for text in downstream),
        "judge_saw_runtime_lines": not any(
            re.search(r"could not (read|locate)|not supplied", item, re.I)
            and "System-observed" in item
            for item in limitations
        ),
        "no_route_anomalies": continuations <= _EXPECTED_ACTIONS,
        "send_ready": _PROCESS_NOTE.search(answer) is None,
        "progress_readable": _progress_readable(shown),
        "no_policy_text": _no_policy_text(shown),
        "answer_shown_exactly": bool(answer) and answer in shown,
    }
    research = "\n".join(
        str(row.get("output") or "")
        for row in collections
        if row.get("agent") == "cursor_workhorse"
    )
    on_topic = _on_topic(research, question.text, rivals) if rivals and research else None
    if on_topic is not None:
        checks["research_on_topic"] = on_topic
    checks["renders_in_terminal"] = bool(answer) and not _blank_in_terminal(answer)
    if question.expect_internal_links:
        checks["internal_links_present"] = bool(
            re.search(r"\]\(https://[^)\s]*(atlassian\.net|slack\.com)", answer)
        )
    checks["internal_links_clickable"] = not _unlinked_internal_refs(
        answer,
        "\n".join(str(row.get("output") or "") for row in collections),
    )
    audits = [
        _payload(str(row.get("output") or ""))
        for row in collections
        if row.get("agent") == "opus_auditor"
    ]
    checks.update(_audience_checks(question, answer, audits))
    checks.update(
        _usage_checks(
            question,
            answer,
            [str(row.get("output") or "") for row in collections if row.get("agent") == "opus_auditor"],
            limitations,
            # The launcher writes this only when the run has a usage add-on.
            (run / "usage.json").is_file(),
        )
    )
    return {
        "label": question.label,
        "terminal": kind,
        "verdict": attestation.get("verdict") or attestation.get("result"),
        "cycle": attestation.get("cycle"),
        "minutes": round((time.time_ns() - question.posted_ns) / 6e10, 1),
        "cost_usd": budget.get("cost_usd"),
        "answer_chars": len(answer),
        "limitations": limitations,
        "stages": stages,
        "continuations": sorted(continuations),
        "checks": checks,
        "answer": answer,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("questions")
    parser.add_argument("--voice", default="")
    parser.add_argument("--out", default="/tmp/triple-stamp-eval")
    parser.add_argument("--timeout-min", type=int, default=150)
    options = parser.parse_args()
    out = Path(options.out)
    out.mkdir(parents=True, exist_ok=True)
    questions = [
        Question(spec, index)
        for index, spec in enumerate(json.loads(Path(options.questions).read_text("utf-8")))
    ]
    env = dict(os.environ)
    # Keep eval answers apart from the user's own library unless told otherwise.
    library = Path(env.setdefault("TRIPLE_STAMP_ANSWER_DIR", str(out / "answers")))
    env.pop("TRIPLE_STAMP_VOICE_PROFILE", None)
    if options.voice:
        env["TRIPLE_STAMP_VOICE_PROFILE"] = str(Path(options.voice).expanduser())
    os.environ["TRIPLE_STAMP_VOICE_PROFILE"] = env.get("TRIPLE_STAMP_VOICE_PROFILE", "")
    log = (out / "launcher.log").open("w", encoding="utf-8")
    launcher = subprocess.Popen(
        [str(ROOT / "triple-stamp")],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        started = time.time()
        url = ""
        while not url and time.time() - started < 25 * 60:
            if launcher.poll() is not None:
                raise SystemExit(f"launcher exited early ({launcher.returncode}); see {log.name}")
            match = re.search(r"Omnigent session: (\S+)", (out / "launcher.log").read_text())
            url = match.group(1) if match else ""
            time.sleep(2)
        base = re.match(r"(https?://[^/]+)", url).group(1)
        prepared = url.rstrip("/").rsplit("/", 1)[-1]
        session_info = _api(base, "GET", f"/v1/sessions/{prepared}")
        run = max(
            (p for p in BASE_DIR.glob("run-*") if (p / "owner-pid").exists()),
            key=lambda p: p.stat().st_mtime,
        )
        for index, question in enumerate(questions):
            question.session = prepared if index == 0 else _api(
                base,
                "POST",
                "/v1/sessions",
                {
                    "agent_id": session_info.get("agent_id"),
                    "host_id": session_info.get("host_id"),
                    "workspace": str(ROOT),
                },
            )["id"]
            question.snapshot = _terminal(run, question.session)
            _api(
                base,
                "POST",
                f"/v1/sessions/{question.session}/events",
                {
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": question.text}],
                    },
                },
            )
            question.posted_ns = time.time_ns()
            print(f"[eval] {question.label}: posted to chat {question.session}", flush=True)
        deadline = time.time() + options.timeout_min * 60
        pending = list(questions)
        while pending and time.time() < deadline:
            for question in list(pending):
                now = _terminal(run, question.session)
                changed = {k: v for k, v in now.items() if question.snapshot.get(k) != v}
                if changed:
                    kind, text = next(iter(changed.items()))
                    time.sleep(5)
                    question.result = _score(
                        run,
                        base,
                        question,
                        kind,
                        json.loads(text),
                        [other.text for other in questions if other is not question],
                    )
                    pending.remove(question)
                    passed = sum(question.result["checks"].values())
                    print(
                        f"[eval] {question.label}: {question.result['verdict']} in "
                        f"{question.result['minutes']} min; checks {passed}/"
                        f"{len(question.result['checks'])}",
                        flush=True,
                    )
            time.sleep(10)
        for question in pending:
            question.result = {"label": question.label, "terminal": "timeout", "checks": {}}
        # Each answer must reach the library while the run is still live, not
        # only in the exit pass; the saver polls every few seconds.
        save_deadline = time.time() + 60
        for question in questions:
            answer = question.result.get("answer") or ""
            if not answer:
                continue
            while not _saved(library, question.session, answer) and time.time() < save_deadline:
                time.sleep(2)
            question.result["checks"]["saved_while_live"] = _saved(
                library, question.session, answer
            )
    finally:
        if launcher.poll() is None:
            launcher.send_signal(signal.SIGTERM)
        try:
            exit_code = launcher.wait(timeout=180)
        except subprocess.TimeoutExpired:
            launcher.kill()
            exit_code = launcher.wait()
        log.close()
    results = [question.result for question in questions]
    (out / "results.json").write_text(
        json.dumps({"exit_code": exit_code, "results": results}, indent=2),
        encoding="utf-8",
    )
    lines = [
        "# Triple-stamp eval scorecard",
        "",
        f"- Voice profile: {options.voice or 'none'}",
        f"- Answer library: {library}",
        f"- Launcher exit after /quit-equivalent SIGTERM: {exit_code}",
        "",
        "| Question | Verdict | Minutes | Cost | Checks |",
        "| --- | --- | --- | --- | --- |",
    ]
    for result in results:
        checks = result.get("checks") or {}
        failed = [name for name, ok in checks.items() if not ok]
        cost = result.get("cost_usd")
        lines.append(
            f"| {result['label']} | {result.get('verdict', result.get('terminal'))} | "
            f"{result.get('minutes', '')} | "
            f"{f'${cost:.2f}' if isinstance(cost, (int, float)) else 'n/a'} | "
            f"{sum(checks.values())}/{len(checks)}"
            + (f" (failed: {', '.join(failed)})" if failed else "")
            + " |"
        )
    for result in results:
        if result.get("answer"):
            lines += ["", f"## {result['label']}", "", result["answer"].rstrip()]
    (out / "scorecard.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[eval] scorecard: {out / 'scorecard.md'} (launcher exit {exit_code})", flush=True)
    all_ok = exit_code == 0 and all(
        result.get("checks") and all(result["checks"].values()) for result in results
    )
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
