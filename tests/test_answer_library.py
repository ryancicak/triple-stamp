from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


runtime_state = _module(
    "triple_stamp_runtime_state",
    ROOT / ".omnigent/isaac-launcher/triple_stamp_runtime_state.py",
)
library = _module(
    "triple_stamp_answer_library_under_test",
    ROOT / ".omnigent/answer_library.py",
)
launcher = _module(
    "triple_stamp_launcher_for_library_tests",
    ROOT / ".omnigent/launcher.py",
)

PARENT = "ab" * 16
STARTUP = "cd" * 16
QUESTION = "What are the current Lakebase connection limits?"
ANSWER = "Lakebase allows up to N connections per compute.[1]"


def _audit() -> str:
    coverage = {
        system: {
            "system": system,
            "status": "unavailable_after_retry",
            "routes": [],
            "tools_called": [],
            "queries": [f"select:mcp__{system}__read", f"select:mcp__{system}__read"],
            "results_seen": 0,
            "evidence_refs": [],
            "note": "two genuine ToolSearch attempts were unavailable",
        }
        for system in ("glean", "jira", "slack", "confluence", "safe")
    }
    return json.dumps(
        {
            "verdict": "PASS",
            "needs_web": False,
            "web_queries": [],
            "attacks": ["checked the connection limit against the docs"],
            "must_retest": [],
            "acceptable_as_is": True,
            "punch_list_for_cursor": [],
            "internal_sources_consulted": [],
            "internal_sources_not_required_reason": "public-only fixture",
            "internal_coverage": coverage,
        }
    )


def _stamp() -> str:
    return json.dumps(
        {
            "verdict": "STAMP",
            "needs_web": False,
            "needs_internal": False,
            "why": "the limit is documented",
            "gap_materiality": "none",
            "limitations": ["Pricing tiers were out of scope."],
            "citations_that_hold": ["https://docs.databricks.com/lakebase/limits"],
            "voice_profile_check": {
                "source_path": "",
                "sha256": "",
                "constraints_applied": "none; voice rendering disabled",
            },
            "shippable_answer": ANSWER,
        }
    )


def _packet(agent: str, title: str, output: str, stamp: int) -> dict:
    return {
        "parent_session_id": PARENT,
        "child_session_id": f"{title}-child",
        "work_id": f"{title}-work",
        "agent": agent,
        "title": title,
        "status": "completed",
        "output": output,
        "collected_at_ns": stamp,
    }


def _rewrite(path: Path, data: bytes) -> None:
    """Replace a published, read-only terminal artifact behind its alias."""

    target = path.resolve()
    target.chmod(0o600)
    target.write_bytes(data)


def _chat_store(run: Path) -> None:
    (run / "state").mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(run / "state/chat.db")
    connection.execute("CREATE TABLE conversations (id BLOB, title TEXT)")
    connection.execute(
        "CREATE TABLE conversation_items "
        "(conversation_id BLOB, position INTEGER, data TEXT, created_at REAL)"
    )
    connection.execute(
        "INSERT INTO conversations VALUES (?, ?)",
        (bytes.fromhex(PARENT), "Lakebase connection limits"),
    )
    for position, item in enumerate(
        (
            {"role": "user", "content": [{"type": "input_text", "text": QUESTION}]},
            {"role": "assistant", "content": [{"type": "output_text", "text": ANSWER}]},
        )
    ):
        connection.execute(
            "INSERT INTO conversation_items VALUES (?, ?, ?, ?)",
            (bytes.fromhex(PARENT), position, json.dumps(item), 1_790_000_000.0),
        )
    connection.commit()
    connection.close()


class AnswerLibraryTests(unittest.TestCase):
    def _stamped_run(self, run: Path) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "TRIPLE_STAMP_RUN_DIR": str(run),
                "TRIPLE_STAMP_VOICE_PROFILE": "",
                "TRIPLE_STAMP_VOICE_PROFILE_SHA256": "",
            },
            clear=False,
        ):
            # The UI's startup session leaves aliases that never resolve.
            runtime_state.activate_parent_attempt(STARTUP, "")
            base = 1_790_000_000 * 10**9
            for record in (
                _packet(
                    "cursor_workhorse",
                    "cursor-cycle-1",
                    "Limit documented at https://docs.databricks.com/lakebase/limits",
                    base + 60 * 10**9,
                ),
                _packet("opus_auditor", "audit-cycle-1", _audit(), base + 300 * 10**9),
            ):
                runtime_state.append_collection(record)
            judge = _packet("codex_judge", "judge-cycle-1", _stamp(), base + 360 * 10**9)
            runtime_state.append_collection(judge)
            self.assertTrue(runtime_state.attest_codex_stamp(judge))
        _chat_store(run)

    def test_stamp_is_saved_once_with_exact_answer_and_dossier(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            run, shelf = Path(value) / "run", Path(value) / "answers"
            run.mkdir()
            self._stamped_run(run)

            written = library.export_run_answers(run, shelf)
            again = library.export_run_answers(run, shelf)

            self.assertEqual(len(written), 1)
            self.assertEqual(again, [])
            folder = written[0]
            self.assertIn("STAMP Lakebase connection limits", folder.name)
            self.assertEqual((folder / "answer.md").read_text("utf-8"), ANSWER)
            dossier = (folder / "dossier.md").read_text("utf-8")
            self.assertIn("**Verdict:** STAMP", dossier)
            self.assertIn(f"> {QUESTION}", dossier)
            self.assertIn("| cursor-cycle-1 | Cursor", dossier)
            self.assertIn("| judge-cycle-1 | Codex", dossier)
            self.assertIn("- Pricing tiers were out of scope.", dossier)
            self.assertIn("plain prose (no voice profile)", dossier)
            self.assertIn("<summary>audit-cycle-1 from Opus", dossier)
            self.assertEqual(
                json.loads((folder / "attestation.json").read_text("utf-8"))["verdict"],
                "STAMP",
            )
            self.assertIn(folder.name, (shelf / "index.md").read_text("utf-8"))
            self.assertEqual(oct((folder / "answer.md").stat().st_mode & 0o777), "0o600")
            # The judge's notes lead the dossier because the sendable answer
            # no longer carries review-process caveats.
            self.assertLess(
                dossier.index("## Notes for you (not part of the answer)"),
                dossier.index("## Answer"),
            )

    def test_live_save_waits_for_the_question_and_never_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            run, shelf = Path(value) / "run", Path(value) / "answers"
            run.mkdir()
            self._stamped_run(run)
            chat = run / "state/chat.db"
            hidden = chat.with_name("chat.db.later")
            chat.rename(hidden)

            # Mid-run the chat store may be unreadable: wait, never file blind.
            self.assertEqual(library.export_run_answers(run, shelf, live=True), [])
            hidden.rename(chat)
            first = library.export_run_answers(run, shelf, live=True)
            self.assertEqual(len(first), 1)

            # A later title change must not file the same answer twice.
            connection = sqlite3.connect(chat)
            connection.execute("UPDATE conversations SET title = 'Renamed chat'")
            connection.commit()
            connection.close()
            self.assertEqual(library.export_run_answers(run, shelf, live=True), [])
            self.assertEqual(library.export_run_answers(run, shelf), [])
            self.assertEqual(len(list(shelf.glob("*/entry.json"))), 1)

    def test_bytes_that_fail_their_digest_are_never_filed(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            run, shelf = Path(value) / "run", Path(value) / "answers"
            run.mkdir()
            self._stamped_run(run)
            _rewrite(
                run / f"stamped-answer-{library._digest(PARENT)}.bin",
                b"half-written",
            )

            self.assertEqual(library.export_run_answers(run, shelf), [])
            self.assertFalse(shelf.exists())

    def test_a_follow_up_answer_is_kept_beside_the_first(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            run, shelf = Path(value) / "run", Path(value) / "answers"
            run.mkdir()
            self._stamped_run(run)
            first = library.export_run_answers(run, shelf, live=True)

            # Same chat, same minute, same title, different attested answer.
            digest = library._digest(PARENT)
            follow_up = b"A second, different stamped answer.[1]"
            _rewrite(run / f"stamped-answer-{digest}.bin", follow_up)
            attestation_path = run / f"stamp-attestation-{digest}.json"
            attestation = json.loads(attestation_path.read_text("utf-8"))
            attestation["answer_sha256"] = library.hashlib.sha256(follow_up).hexdigest()
            attestation["answer_length"] = len(follow_up)
            _rewrite(attestation_path, json.dumps(attestation).encode("utf-8"))
            second = library.export_run_answers(run, shelf, live=True)

            self.assertEqual(len(first), 1)
            self.assertEqual(len(second), 1)
            self.assertNotEqual(first[0], second[0])
            self.assertEqual((first[0] / "answer.md").read_text("utf-8"), ANSWER)
            self.assertEqual((second[0] / "answer.md").read_bytes(), follow_up)
            index = (shelf / "index.md").read_text("utf-8")
            self.assertIn(first[0].name, index)
            self.assertIn(second[0].name, index)

    def test_launcher_saves_answers_while_the_run_is_live(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            run, shelf = Path(value) / "run", Path(value) / "answers"
            run.mkdir()
            self._stamped_run(run)
            with mock.patch.dict(
                os.environ,
                {library.ANSWER_DIR_ENV: str(shelf)},
            ), mock.patch.object(
                launcher,
                "_ANSWER_SAVE_INTERVAL_S",
                0.05,
            ), mock.patch.object(launcher, "_eprint") as printed:
                saver = launcher._start_answer_saver(run, Path(value))
                self.assertIsNotNone(saver)
                deadline = launcher.time.time() + 10
                while not saver.saved and launcher.time.time() < deadline:
                    launcher.time.sleep(0.05)
                saved_live = saver.stop()
                self.assertEqual(len(saved_live), 1)
                # The live pass is silent; the exit pass reports the total.
                printed.assert_not_called()
                self.assertEqual(
                    launcher._export_answers(
                        run,
                        Path(value),
                        saved_earlier=len(saved_live),
                    ),
                    [],
                )
            self.assertIn("saved 1 answer(s)", printed.call_args.args[0])
            self.assertEqual(len(list(shelf.glob("*/entry.json"))), 1)

    def test_library_location_is_private_by_default_and_can_be_turned_off(self) -> None:
        home = Path("/Users/example")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(library.ANSWER_DIR_ENV, None)
            self.assertEqual(
                library.answer_library_dir(home),
                home / ".local/share/triple-stamp/answers",
            )
        for disabled in ("", "0", "off", "OFF", "none"):
            with mock.patch.dict(os.environ, {library.ANSWER_DIR_ENV: disabled}):
                self.assertIsNone(library.answer_library_dir(home))
        with mock.patch.dict(os.environ, {library.ANSWER_DIR_ENV: "~/answers"}):
            self.assertEqual(
                library.answer_library_dir(home),
                Path("~/answers").expanduser(),
            )

    def test_launcher_saving_answers_can_never_fail_the_exit(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            run = Path(value) / "run"
            run.mkdir()
            self._stamped_run(run)
            blocked = Path(value) / "not-a-directory"
            blocked.write_text("file in the way", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {library.ANSWER_DIR_ENV: str(blocked)},
            ), mock.patch.object(launcher, "_eprint") as printed:
                launcher._export_answers(run, Path(value))

            self.assertIn("could not save answers", printed.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
