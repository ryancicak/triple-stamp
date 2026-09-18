from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import triple_stamp_isaac_launcher as contract
import triple_stamp_runtime_state as runtime_state
from omnigent import cursor_native_forwarder, cursor_native_status
from omnigent.runner import app as runner_app
from omnigent.runner import tool_dispatch

ROOT = Path(__file__).resolve().parents[1]
lifecycle_spec = importlib.util.spec_from_file_location(
    "triple_stamp_cursor_lifecycle_under_test",
    ROOT / ".omnigent/runtime-python/triple_stamp_cursor_lifecycle.py",
)
assert lifecycle_spec is not None and lifecycle_spec.loader is not None
lifecycle = importlib.util.module_from_spec(lifecycle_spec)
lifecycle_spec.loader.exec_module(lifecycle)


FIXTURE = json.loads(
    (ROOT / "tests/fixtures/run-80z0p3az-cursor-lifecycle.json").read_text(
        encoding="utf-8"
    )
)


class _Response:
    status_code = 204
    text = ""

    @staticmethod
    def raise_for_status() -> None:
        return None

    @staticmethod
    def json() -> dict[str, str]:
        return {"result": "POLICY_ACTION_ALLOW"}


class _AllowPolicyClient:
    @staticmethod
    async def post(*_args: object, **_kwargs: object) -> _Response:
        return _Response()


class _LoopbackStatusClient:
    def __init__(self, child: str) -> None:
        self.child = child
        self.posts = 0
        self.wakes = 0

    async def post(self, _url: str, *, json: dict[str, object]) -> _Response:
        self.posts += 1
        data = json["data"]
        assert isinstance(data, dict)
        status = "completed" if data["status"] == "idle" else "failed"
        acknowledgement = runner_app.mark_subagent_work_terminal(
            self.child,
            status=status,
            output=str(data["output"]),
        )
        if acknowledgement.delivered_now:
            self.wakes += 1
        return _Response()


class CursorLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        lifecycle.install_parent_inbox_guard()

    def _paths(
        self,
        run: Path,
        *,
        child: str,
        cursor_session_id: str,
    ) -> tuple[Path, Path]:
        bridge = (
            run
            / "tmp"
            / f"omnigent-{os.getuid()}"
            / "cursor-native"
            / __import__("hashlib").sha256(child.encode()).hexdigest()[:32]
        )
        bridge.mkdir(parents=True)
        store = run / "home/.cursor/chats/chat" / cursor_session_id / "store.db"
        store.parent.mkdir(parents=True)
        (bridge / "cursor_forwarder.json").write_text(
            json.dumps({"store_path": str(store)}),
            encoding="utf-8",
        )
        (bridge / "triple-stamp-startup.json").write_text(
            json.dumps({"state": "ready"}),
            encoding="utf-8",
        )
        transcript = (
            run
            / "home/.cursor/projects/project/agent-transcripts"
            / cursor_session_id
            / f"{cursor_session_id}.jsonl"
        )
        transcript.parent.mkdir(parents=True)
        return bridge, transcript

    @staticmethod
    def _line(role: str, blocks: list[dict[str, object]]) -> str:
        return json.dumps({"role": role, "message": {"content": blocks}})

    def test_single_transcript_fallback_never_crosses_parent_sessions(
        self,
    ) -> None:
        parent_a = "cursor-parent-a"
        parent_b = "cursor-parent-b"
        child_a = "cursor-child-a"
        child_b = "cursor-child-b"
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value},
            clear=False,
        ):
            run = Path(value)
            transcript = (
                run
                / "home/.cursor/projects/project/agent-transcripts"
                / "cursor-session-a"
                / "cursor-session-a.jsonl"
            )
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                self._line(
                    "assistant",
                    [{"type": "text", "text": "math answer"}],
                )
                + "\n",
                encoding="utf-8",
            )
            bridge_b = (
                run
                / "tmp"
                / f"omnigent-{os.getuid()}"
                / "cursor-native"
                / __import__("hashlib").sha256(
                    child_b.encode()
                ).hexdigest()[:32]
            )
            bridge_b.mkdir(parents=True)
            (bridge_b / "triple-stamp-startup.json").write_text(
                json.dumps({"state": "ready"}),
                encoding="utf-8",
            )
            self._dispatch(
                parent=parent_a,
                child=child_a,
                work_id="work-a",
                title="cursor-cycle-1",
            )
            self._dispatch(
                parent=parent_b,
                child=child_b,
                work_id="work-b",
                title="cursor-cycle-1",
            )

            snapshot = lifecycle._cursor_transcript_snapshot(
                child_b,
                bridge_b,
            )

        self.assertFalse(snapshot["seen"])
        self.assertEqual(snapshot["output"], "")
        self.assertIn(
            "metadata_source=forwarder-required-after-parent-dispatch",
            snapshot["diagnostic"],
        )

    def _dispatch(
        self,
        *,
        parent: str,
        child: str,
        work_id: str,
        title: str,
    ) -> None:
        runtime_state.append_dispatch(
            {
                "parent_session_id": parent,
                "child_session_id": child,
                "work_id": work_id,
                "agent": "cursor_workhorse",
                "title": title,
            }
        )

    def test_retained_transport_waits_for_real_turn_then_wakes_once(self) -> None:
        parent = FIXTURE["parent_session_id"]
        child = FIXTURE["child_session_id"]
        work_id = FIXTURE["work_id"]
        cursor_id = FIXTURE["cursor_session_id"]
        answer = "# Executive conclusion\n\nRetained lifecycle result."
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {
                "TRIPLE_STAMP_RUN_DIR": value,
                "CURSOR_DATA_DIR": f"{value}/home/.cursor",
            },
            clear=False,
        ), mock.patch.object(lifecycle, "_CURSOR_COMPLETION_STABLE_S", 0.0):
            run = Path(value)
            bridge, transcript = self._paths(
                run,
                child=child,
                cursor_session_id=cursor_id,
            )
            self._dispatch(
                parent=parent,
                child=child,
                work_id=work_id,
                title=FIXTURE["title"],
            )
            inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            runner_app._session_inboxes_ref[parent] = inbox
            runner_app.register_subagent_work(
                parent_session_id=parent,
                child_session_id=child,
                agent="cursor_workhorse",
                title=FIXTURE["title"],
            )
            try:
                transcript.write_text(
                    "\n".join(
                        (
                            self._line(
                                "user",
                                [{"type": "text", "text": "retained request"}],
                            ),
                            self._line(
                                "assistant",
                                [{"type": "tool_use", "name": "WebSearch"}],
                            ),
                        )
                    )
                    + "\n",
                    encoding="utf-8",
                )
                marker_path = bridge / cursor_native_status.TURN_END_FILE
                marker_path.write_text(
                    "\n".join(
                        json.dumps({"generation_id": generation, "ts": timestamp})
                        for generation, timestamp in FIXTURE["stop_markers"]
                    )
                    + "\n",
                    encoding="utf-8",
                )

                # All retained stop hooks, including the minute-one hook, stay
                # behind the transcript lifecycle boundary while tools run.
                self.assertEqual(cursor_native_status.count_turn_ends(bridge), 0)
                with transcript.open("a", encoding="utf-8") as handle:
                    handle.write(
                        self._line(
                            "assistant",
                            [{"type": "tool_use", "name": "WebFetch"}],
                        )
                        + "\n"
                    )
                self.assertEqual(cursor_native_status.count_turn_ends(bridge), 0)
                self.assertTrue(inbox.empty())
                self.assertEqual(runtime_state.read_collections(), [])

                with transcript.open("a", encoding="utf-8") as handle:
                    handle.write(
                        self._line(
                            "assistant",
                            [{"type": "text", "text": answer}],
                        )
                        + "\n"
                    )
                    handle.write(
                        json.dumps({"type": "turn_ended", "status": "success"})
                        + "\n"
                    )
                # First observation records the stable candidate; the next
                # proves it unchanged and releases all eight stop markers once.
                self.assertEqual(cursor_native_status.count_turn_ends(bridge), 0)
                self.assertEqual(
                    cursor_native_status.count_turn_ends(bridge),
                    len(FIXTURE["stop_markers"]),
                )

                client = _LoopbackStatusClient(child)
                asyncio.run(
                    cursor_native_forwarder._post_external_session_status(
                        client,
                        session_id=child,
                        status="idle",
                    )
                )
                cursor_native_status.write_posted_count(
                    bridge,
                    len(FIXTURE["stop_markers"]),
                )
                self.assertEqual(client.posts, 1)
                self.assertEqual(client.wakes, 1)
                self.assertEqual(inbox.qsize(), 1)
                self.assertEqual(inbox._queue[0]["output"], answer)
                persisted = runtime_state.read_cursor_lifecycle(child, work_id)
                self.assertTrue(persisted["delivery_committed"])
                self.assertTrue(persisted["completed_turn_id"])

                # Duplicate stop reports and direct retry cannot redeliver.
                with marker_path.open("a", encoding="utf-8") as handle:
                    handle.write('{"generation_id":"duplicate","ts":1788985220}\n')
                self.assertEqual(
                    cursor_native_status.count_turn_ends(bridge),
                    len(FIXTURE["stop_markers"]),
                )
                asyncio.run(
                    cursor_native_forwarder._post_external_session_status(
                        client,
                        session_id=child,
                        status="idle",
                    )
                )
                self.assertEqual(client.posts, 1)
                self.assertEqual(client.wakes, 1)

                # Recreate runner-local tracking to model a restart. The
                # run-scoped child/work delivery record still deduplicates.
                runner_app.unregister_subagent_work(child)
                runner_app.register_subagent_work(
                    parent_session_id=parent,
                    child_session_id=child,
                    agent="cursor_workhorse",
                    title=FIXTURE["title"],
                )
                asyncio.run(
                    cursor_native_forwarder._post_external_session_status(
                        client,
                        session_id=child,
                        status="idle",
                    )
                )
                self.assertEqual(client.posts, 1)
                self.assertEqual(client.wakes, 1)

                read_started = time.monotonic()
                output = asyncio.run(
                    tool_dispatch._drain_inbox(
                        inbox,
                        server_client=_AllowPolicyClient(),
                        conversation_id=parent,
                    )
                )
                self.assertLess(time.monotonic() - read_started, 0.25)
                self.assertIn(answer, output)
                self.assertEqual(len(runtime_state.read_collections()), 1)
                route = contract._next_route(runtime_state.read_collections())
                self.assertEqual(route.agent, "opus_auditor")
                self.assertEqual(route.title, "audit-cycle-1")
            finally:
                runner_app.unregister_subagent_work(child)
                runner_app._session_inboxes_ref.pop(parent, None)

        self.assertEqual(FIXTURE["duration_seconds"], 11 * 60 + 17)
        self.assertLess(FIXTURE["duration_seconds"], 15 * 60)
        self.assertEqual(FIXTURE["model"], "gpt-5.6-sol-xhigh")

    def test_unknown_opus_completion_is_latched_without_duplicate_record(self) -> None:
        parent = "parent-timeout"
        appended: list[dict[str, object]] = []
        lifecycle._unknown_opus_dispatches.pop(parent, None)
        first = lifecycle.latch_unknown_opus_completion(
            parent,
            title="audit-cycle-1",
            child_session_id="opus-child",
            child_state="task=completed,busy=False",
            append_collection=appended.append,
        )
        second = lifecycle.latch_unknown_opus_completion(
            parent,
            title="audit-cycle-1",
            child_session_id="different-child-must-not-start",
            child_state="task=unknown,busy=True",
            append_collection=appended.append,
        )
        self.assertEqual(first, second)
        self.assertIn("ReadTimeout", first)
        self.assertIn("duplicate paid launch is forbidden", first)
        self.assertIn("opus-child", first)
        self.assertEqual(len(appended), 1)
        self.assertEqual(appended[0]["status"], "failed")
        lifecycle._unknown_opus_dispatches.pop(parent, None)

    def test_legacy_packet_is_leased_and_read_returns_promptly(self) -> None:
        parent = "legacy-parent"
        child = "legacy-child"
        work_id = "legacy-work"
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value},
            clear=False,
        ):
            inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            runner_app._session_inboxes_ref[parent] = inbox
            entry = runner_app.register_subagent_work(
                parent_session_id=parent,
                child_session_id=child,
                agent="cursor_workhorse",
                title="cursor-cycle-1",
            )
            work_id = entry.work_id
            self._dispatch(
                parent=parent,
                child=child,
                work_id=work_id,
                title="cursor-cycle-1",
            )
            entry.status = "completed"
            entry.output = ""
            entry.delivered = True
            inbox.put_nowait(
                {
                    **FIXTURE["native_packet"],
                    "conversation_id": child,
                    "work_id": work_id,
                }
            )
            try:
                started = time.monotonic()
                result = asyncio.run(
                    tool_dispatch._drain_inbox(
                        inbox,
                        server_client=None,
                        conversation_id=parent,
                    )
                )
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 0.25)
                self.assertIn("still active", result)
                self.assertEqual(inbox.qsize(), 1)
                self.assertTrue(inbox._queue[0]["_triple_stamp_pending"])
                self.assertEqual(entry.status, "running")
                self.assertFalse(entry.delivered)
                self.assertEqual(runtime_state.read_collections(), [])
                state = runtime_state.read_cursor_lifecycle(child, work_id)
                self.assertTrue(state["legacy_pending"])
            finally:
                runner_app.unregister_subagent_work(child)
                runner_app._session_inboxes_ref.pop(parent, None)

    def test_caller_cancellation_requeues_leased_complete_packet(self) -> None:
        parent = "cancel-parent"
        payload = {
            "type": "sub_agent",
            "conversation_id": "opus-child",
            "work_id": "opus-work",
            "agent": "opus_auditor",
            "title": "audit-cycle-1",
            "status": "completed",
            "output": '{"verdict":"PASS"}',
        }
        inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        inbox.put_nowait(payload)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked_evaluation(
            packet: dict[str, object],
            **_kwargs: object,
        ) -> object:
            entered.set()
            await release.wait()
            return type("Evaluation", (), {"payload": packet, "retry_original": False})()

        async def scenario() -> None:
            with mock.patch.object(
                tool_dispatch,
                "_evaluate_subagent_inbox_output",
                side_effect=blocked_evaluation,
            ):
                task = asyncio.create_task(
                    tool_dispatch._drain_inbox(
                        inbox,
                        server_client=None,
                        conversation_id=parent,
                    )
                )
                await entered.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        asyncio.run(scenario())
        self.assertEqual(inbox.qsize(), 1)
        self.assertEqual(inbox.get_nowait(), payload)

    def test_every_cursor_stage_type_uses_same_lifecycle_boundary(self) -> None:
        titles = [f"cursor-cycle-{cycle}" for cycle in range(1, 5)]
        titles.extend(
            f"cursor-web-{requester}-{cycle}-{hop}"
            for requester in ("opus", "codex")
            for cycle in range(1, 5)
            for hop in (1, 2)
        )
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {
                "TRIPLE_STAMP_RUN_DIR": value,
                "CURSOR_DATA_DIR": f"{value}/home/.cursor",
            },
            clear=False,
        ), mock.patch.object(lifecycle, "_CURSOR_COMPLETION_STABLE_S", 0.0):
            run = Path(value)
            for index, title in enumerate(titles):
                with self.subTest(title=title):
                    child = f"stage-child-{index}"
                    work_id = f"stage-work-{index}"
                    bridge, transcript = self._paths(
                        run,
                        child=child,
                        cursor_session_id=f"cursor-{index}",
                    )
                    self._dispatch(
                        parent="stage-parent",
                        child=child,
                        work_id=work_id,
                        title=title,
                    )
                    (bridge / cursor_native_status.TURN_END_FILE).write_text(
                        '{"generation_id":"early"}\n',
                        encoding="utf-8",
                    )
                    transcript.write_text(
                        "\n".join(
                            (
                                self._line(
                                    "user",
                                    [{"type": "text", "text": "request"}],
                                ),
                                self._line(
                                    "assistant",
                                    [{"type": "tool_use", "name": "WebSearch"}],
                                ),
                            )
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    self.assertEqual(
                        cursor_native_status.count_turn_ends(bridge),
                        0,
                    )
                    with transcript.open("a", encoding="utf-8") as handle:
                        handle.write(
                            self._line(
                                "assistant",
                                [{"type": "text", "text": f"result for {title}"}],
                            )
                            + "\n"
                        )
                        handle.write(
                            '{"type":"turn_ended","status":"success"}\n'
                        )
                    cursor_native_status.count_turn_ends(bridge)
                    self.assertEqual(
                        cursor_native_status.count_turn_ends(bridge),
                        1,
                    )

    def test_absolute_timeout_emits_one_failure_and_wake(self) -> None:
        parent = "timeout-parent"
        child = "timeout-child"
        work_id = "timeout-work"
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {
                "TRIPLE_STAMP_RUN_DIR": value,
                "CURSOR_DATA_DIR": f"{value}/home/.cursor",
            },
            clear=False,
        ):
            run = Path(value)
            bridge, transcript = self._paths(
                run,
                child=child,
                cursor_session_id="timeout-cursor",
            )
            self._dispatch(
                parent=parent,
                child=child,
                work_id=work_id,
                title="cursor-cycle-1",
            )
            transcript.write_text(
                "\n".join(
                    (
                        self._line(
                            "user",
                            [{"type": "text", "text": "long request"}],
                        ),
                        self._line(
                            "assistant",
                            [{"type": "tool_use", "name": "WebSearch"}],
                        ),
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            old_ns = time.time_ns() - 16 * 60 * 1_000_000_000

            def age(
                generation: dict[str, object],
                _state: dict[str, object],
            ) -> None:
                generation["started_at_ns"] = old_ns
                generation["last_progress_at_ns"] = old_ns

            runtime_state.mutate_cursor_lifecycle(child, work_id, age)
            inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            runner_app._session_inboxes_ref[parent] = inbox
            runner_app.register_subagent_work(
                parent_session_id=parent,
                child_session_id=child,
                agent="cursor_workhorse",
                title="cursor-cycle-1",
            )
            try:
                # No stop marker is required: the forwarder's lifecycle check
                # synthesizes one terminal edge at the absolute stage bound.
                self.assertEqual(cursor_native_status.count_turn_ends(bridge), 1)
                client = _LoopbackStatusClient(child)
                asyncio.run(
                    cursor_native_forwarder._post_external_session_status(
                        client,
                        session_id=child,
                        status="idle",
                    )
                )
                cursor_native_status.write_posted_count(bridge, 1)
                self.assertEqual(client.posts, 1)
                self.assertEqual(client.wakes, 1)
                self.assertIn("CURSOR_WORKER_TIMEOUT: kind=absolute", inbox._queue[0]["output"])
                self.assertEqual(cursor_native_status.count_turn_ends(bridge), 1)
                asyncio.run(
                    cursor_native_forwarder._post_external_session_status(
                        client,
                        session_id=child,
                        status="idle",
                    )
                )
                self.assertEqual(client.posts, 1)
                self.assertEqual(client.wakes, 1)
            finally:
                runner_app.unregister_subagent_work(child)
                runner_app._session_inboxes_ref.pop(parent, None)

    def test_inactivity_timeout_diagnostic_runs_in_forwarder(self) -> None:
        child = "inactive-child"
        work_id = "inactive-work"
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {
                "TRIPLE_STAMP_RUN_DIR": value,
                "CURSOR_DATA_DIR": f"{value}/home/.cursor",
            },
            clear=False,
        ):
            run = Path(value)
            bridge, transcript = self._paths(
                run,
                child=child,
                cursor_session_id="inactive-cursor",
            )
            self._dispatch(
                parent="inactive-parent",
                child=child,
                work_id=work_id,
                title="cursor-web-opus-4-2",
            )
            transcript.write_text(
                self._line(
                    "user",
                    [{"type": "text", "text": "stalled request"}],
                )
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(cursor_native_status.count_turn_ends(bridge), 0)
            old_ns = time.time_ns() - 6 * 60 * 1_000_000_000

            def age(
                generation: dict[str, object],
                _state: dict[str, object],
            ) -> None:
                generation["started_at_ns"] = old_ns
                generation["last_progress_at_ns"] = old_ns

            runtime_state.mutate_cursor_lifecycle(child, work_id, age)
            self.assertEqual(cursor_native_status.count_turn_ends(bridge), 1)
            state = runtime_state.read_cursor_lifecycle(child, work_id)
            self.assertIn(
                "CURSOR_WORKER_TIMEOUT: kind=inactivity",
                state["terminal_output"],
            )

    def test_retained_stale_cycle_has_distinct_cursor_turn_identity(self) -> None:
        fixture = json.loads(
            (ROOT / "tests/fixtures/run-23agmzty-regressions.json").read_text(
                encoding="utf-8"
            )
        )["stale_cursor_cycle"]
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {
                "TRIPLE_STAMP_RUN_DIR": value,
                "CURSOR_DATA_DIR": f"{value}/home/.cursor",
            },
            clear=False,
        ):
            run = Path(value)
            snapshots = []
            for child, cursor_session in (
                (
                    fixture["cycle_1_child"],
                    fixture["cycle_1_cursor_session"],
                ),
                (
                    fixture["cycle_2_child"],
                    fixture["cycle_2_cursor_session"],
                ),
            ):
                bridge, transcript = self._paths(
                    run,
                    child=child,
                    cursor_session_id=cursor_session,
                )
                transcript.write_text(
                    "\n".join(
                        (
                            self._line(
                                "user",
                                [{"type": "text", "text": "same request"}],
                            ),
                            self._line(
                                "assistant",
                                [{"type": "text", "text": "same answer"}],
                            ),
                            json.dumps(
                                {"type": "turn_ended", "status": "success"}
                            ),
                        )
                    )
                    + "\n",
                    encoding="utf-8",
                )
                snapshots.append(
                    lifecycle._cursor_transcript_snapshot(child, bridge)
                )

        self.assertTrue(all(snapshot["complete"] for snapshot in snapshots))
        self.assertNotEqual(snapshots[0]["turn_id"], snapshots[1]["turn_id"])
        self.assertNotEqual(
            snapshots[1]["turn_id"],
            fixture["retained_completed_turn_id"],
        )

    def test_inbox_time_contract_is_far_below_transport_timeout(self) -> None:
        self.assertEqual(lifecycle._CURSOR_STAGE_INACTIVITY_S, 5 * 60)
        self.assertEqual(lifecycle._CURSOR_STAGE_ABSOLUTE_S, 15 * 60)
        contract_seconds = (
            tool_dispatch._drain_inbox.__triple_stamp_inbox_time_contract_s__
        )
        self.assertLess(contract_seconds, 420)

        empty: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        started = time.monotonic()
        self.assertIn(
            "Inbox is empty",
            asyncio.run(
                tool_dispatch._drain_inbox(
                    empty,
                    server_client=None,
                    conversation_id="time-parent",
                )
            ),
        )
        self.assertLess(time.monotonic() - started, 0.25)


if __name__ == "__main__":
    unittest.main()
