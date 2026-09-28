"""Concurrent questions must never receive each other's Cursor research.

On 2026-09-26 two questions asked at once swapped Stage 1 packets: every Cursor
worker in a run shares one workspace, so a new worker's chat could only be
told from a sibling's by creation time, and each worker claimed the other's.
Cursor launches are now ordered so a worker starts only after its siblings
have claimed their own chats.
"""

from __future__ import annotations

import asyncio
import fcntl
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import triple_stamp_runtime_state as runtime_state
from omnigent.runner import app as runner_app
from omnigent.runner import tool_dispatch

ROOT = Path(__file__).resolve().parents[1]
lifecycle_spec = importlib.util.spec_from_file_location(
    "triple_stamp_cursor_launch_order_under_test",
    ROOT / ".omnigent/runtime-python/triple_stamp_cursor_lifecycle.py",
)
assert lifecycle_spec is not None and lifecycle_spec.loader is not None
lifecycle = importlib.util.module_from_spec(lifecycle_spec)
lifecycle_spec.loader.exec_module(lifecycle)

FAST = {"_CURSOR_LAUNCH_POLL_S": 0.02, "_CURSOR_CLAIM_WAIT_S": 5.0}


def _dispatch(child: str, *, agent: str = "cursor_workhorse", age_s: float = 0) -> dict:
    return {
        "parent_session_id": f"{child}-parent",
        "child_session_id": child,
        "work_id": f"{child}-work",
        "agent": agent,
        "title": "cursor-cycle-1",
        "dispatched_at_ns": time.time_ns() - int(age_s * 1e9),
    }


def _claim(run: Path, child: str) -> None:
    bridge = lifecycle._cursor_bridge_dir(run, child)
    bridge.mkdir(parents=True, exist_ok=True)
    (bridge / "cursor_forwarder.json").write_text(
        json.dumps({"store_path": str(run / f"chats/{child}/store.db")}),
        encoding="utf-8",
    )


class _Patched:
    """Apply the fast timing constants for one test."""

    def __init__(self, **overrides: float) -> None:
        self._patches = [
            mock.patch.object(lifecycle, name, value)
            for name, value in {**FAST, **overrides}.items()
        ]

    def __enter__(self) -> None:
        for patch in self._patches:
            patch.start()

    def __exit__(self, *_exc: object) -> None:
        for patch in reversed(self._patches):
            patch.stop()


class UnclaimedWorkerTests(unittest.TestCase):
    def test_bridge_dir_matches_omnigent(self) -> None:
        try:
            from omnigent.harnesses.cursor_native.bridge import bridge_dir_for_session_id
        except ImportError:  # Omnigent 0.12 keeps the legacy module path
            from omnigent.cursor_native_bridge import bridge_dir_for_session_id
        run = Path("/tmp/example-run")
        child = "0123456789abcdef0123456789abcdef"
        self.assertEqual(
            lifecycle._cursor_bridge_dir(run, child).relative_to(run / "tmp"),
            bridge_dir_for_session_id(child).relative_to(Path(tempfile.gettempdir())),
        )

    def test_only_recent_unfinished_unclaimed_cursor_children_wait(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            run = Path(value)
            _claim(run, "claimed")
            dispatches = [
                _dispatch("unclaimed"),
                _dispatch("claimed"),
                _dispatch("finished"),
                _dispatch("stale", age_s=16 * 60),
                _dispatch("audit", agent="opus_auditor"),
            ]
            collections = [{"work_id": "finished-work", "status": "failed"}]

            waiting = lifecycle._unclaimed_cursor_workers(run, dispatches, collections)

        self.assertEqual(waiting, ["unclaimed"])


class LaunchTurnTests(unittest.TestCase):
    def _enter(self, run: Path, dispatches: list[dict]) -> float:
        async def enter() -> float:
            started = time.monotonic()
            async with lifecycle._cursor_launch_turn(lambda: dispatches, list):
                return time.monotonic() - started

        with mock.patch.dict(os.environ, {"TRIPLE_STAMP_RUN_DIR": str(run)}):
            return asyncio.run(enter())

    def test_a_lone_question_never_waits(self) -> None:
        with tempfile.TemporaryDirectory() as value, _Patched():
            self.assertLess(self._enter(Path(value), []), 0.5)

    def test_launch_waits_until_the_sibling_claims_its_chat(self) -> None:
        with tempfile.TemporaryDirectory() as value, _Patched():
            run = Path(value)
            dispatches = [_dispatch("sibling")]

            async def claim_later() -> None:
                await asyncio.sleep(0.3)
                _claim(run, "sibling")

            async def enter() -> float:
                started = time.monotonic()
                claimer = asyncio.create_task(claim_later())
                async with lifecycle._cursor_launch_turn(lambda: dispatches, list):
                    waited = time.monotonic() - started
                await claimer
                return waited

            with mock.patch.dict(os.environ, {"TRIPLE_STAMP_RUN_DIR": str(run)}):
                waited = asyncio.run(enter())

        self.assertGreaterEqual(waited, 0.3)
        self.assertLess(waited, 3.0)

    def test_a_sibling_that_never_claims_delays_only_boundedly(self) -> None:
        with tempfile.TemporaryDirectory() as value, _Patched(_CURSOR_CLAIM_WAIT_S=0.3):
            waited = self._enter(Path(value), [_dispatch("stuck")])

        self.assertGreaterEqual(waited, 0.3)
        self.assertLess(waited, 2.0)

    def test_ordering_is_skipped_outside_a_run(self) -> None:
        async def enter() -> bool:
            async with lifecycle._cursor_launch_turn(lambda: [_dispatch("x")], list):
                return True

        with mock.patch.dict(os.environ, {}, clear=False), _Patched():
            os.environ.pop("TRIPLE_STAMP_RUN_DIR", None)
            self.assertTrue(asyncio.run(enter()))

    def test_a_ledger_error_lets_the_launch_proceed(self) -> None:
        def broken() -> list[dict]:
            raise OSError("ledger unavailable")

        async def enter() -> bool:
            async with lifecycle._cursor_launch_turn(broken, list):
                return True

        with tempfile.TemporaryDirectory() as value, _Patched(), mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value},
        ):
            self.assertTrue(asyncio.run(enter()))


def _patched_modules() -> list[object]:
    """Omnigent modules whose functions the lifecycle guard install replaces."""

    names = (
        "cursor_native_forwarder",
        "cursor_native_status",
        "_native_post_delivery",
        "codex_native_app_server",
    )
    return [
        tool_dispatch,
        runner_app,
        *(importlib.import_module(f"omnigent.{name}") for name in names),
    ]


class GuardOrderingTests(unittest.TestCase):
    """Drive the real installed dispatch guard with a recording native send.

    The guard install is process-global, so every replaced function is put
    back afterwards; otherwise this module's copy would own the patches that
    later test files install from their own copies.
    """

    def setUp(self) -> None:
        saved = [
            (module, name, value)
            for module in _patched_modules()
            for name, value in vars(module).items()
            if callable(value)
        ]

        def restore() -> None:
            for module, name, value in saved:
                setattr(module, name, value)

        self.addCleanup(restore)

    def _run_guard(self, scenario: object, *, send_error: bool = False) -> list:
        prior_send = tool_dispatch._execute_subagent_tool
        calls: list[tuple[str, str, float]] = []

        async def recording_send(args: dict, **kwargs: object) -> str:
            parent = str(kwargs["conversation_id"])
            calls.append((args["agent"], parent, time.monotonic()))
            if send_error:
                raise RuntimeError("native launch failed")
            return json.dumps(
                {"status": "launching", "conversation_id": f"{parent}-child"}
            )

        class Entry:
            def __init__(self, child: str) -> None:
                self.work_id = f"{child}-work"

        try:
            tool_dispatch._execute_subagent_tool = recording_send
            lifecycle.install_parent_inbox_guard()
            guarded = tool_dispatch._execute_subagent_tool
            self.assertTrue(getattr(guarded, "__triple_stamp_inbox_guard__", False))
            self.assertIs(guarded.__triple_stamp_original__, recording_send)
            with tempfile.TemporaryDirectory() as value, _Patched(), mock.patch.dict(
                os.environ,
                {
                    "TRIPLE_STAMP_RUN_DIR": value,
                    "TRIPLE_STAMP_VOICE_PROFILE": "",
                    "TRIPLE_STAMP_VOICE_PROFILE_SHA256": "",
                },
                clear=False,
            ), mock.patch.object(runner_app, "get_subagent_work", side_effect=Entry):
                asyncio.run(scenario(guarded, Path(value)))  # type: ignore[operator]
        finally:
            tool_dispatch._execute_subagent_tool = prior_send
        return calls

    @staticmethod
    async def _send(guarded: object, parent: str, agent: str = "cursor_workhorse") -> str:
        runtime_state.activate_parent_attempt(parent, "request")
        return await guarded(  # type: ignore[operator]
            {
                "agent": agent,
                "title": "cursor-cycle-1" if agent == "cursor_workhorse" else "audit-cycle-1",
                "args": "Research the original request.",
            },
            server_client=object(),
            conversation_id=parent,
            agent_spec=None,
        )

    def test_two_questions_at_once_launch_cursor_one_after_the_other(self) -> None:
        claimed_at: list[float] = []

        async def scenario(guarded: object, run: Path) -> None:
            async def claim_first_child() -> None:
                while not any(
                    record.get("child_session_id") == "first-child"
                    for record in runtime_state.read_dispatches()
                ):
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.3)
                _claim(run, "first-child")
                claimed_at.append(time.monotonic())

            async def second() -> str:
                await asyncio.sleep(0.05)
                return await self._send(guarded, "second")

            await asyncio.gather(
                self._send(guarded, "first"),
                second(),
                claim_first_child(),
            )

        calls = self._run_guard(scenario)

        self.assertEqual([call[1] for call in calls], ["first", "second"])
        self.assertGreaterEqual(calls[1][2], claimed_at[0])

    def test_audits_and_judgments_are_never_held_by_cursor_ordering(self) -> None:
        async def scenario(guarded: object, run: Path) -> None:
            await self._send(guarded, "first")  # launches, never claims a chat
            await self._send(guarded, "auditing", agent="opus_auditor")

        calls = self._run_guard(scenario)

        self.assertEqual([call[0] for call in calls], ["cursor_workhorse", "opus_auditor"])
        self.assertLess(calls[1][2] - calls[0][2], 1.0)

    def test_a_failed_launch_releases_the_turn(self) -> None:
        async def scenario(guarded: object, run: Path) -> None:
            with self.assertRaises(RuntimeError):
                await self._send(guarded, "first")
            fd = os.open(run / lifecycle._CURSOR_LAUNCH_LOCK, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)

        self.assertEqual(len(self._run_guard(scenario, send_error=True)), 1)


if __name__ == "__main__":
    unittest.main()
