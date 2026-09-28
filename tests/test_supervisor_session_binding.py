from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import triple_stamp_runtime_state as runtime_state
from omnigent.runtime.harnesses import _executor_adapter, _scaffold

ROOT = Path(__file__).resolve().parents[1]
supervisor_spec = importlib.util.spec_from_file_location(
    "triple_stamp_supervisor_binding_under_test",
    ROOT / ".omnigent/runtime-python/triple_stamp_supervisor_runtime.py",
)
assert supervisor_spec is not None and supervisor_spec.loader is not None
supervisor_runtime = importlib.util.module_from_spec(supervisor_spec)
sys.modules[supervisor_spec.name] = supervisor_runtime
supervisor_spec.loader.exec_module(supervisor_runtime)

# The live 2026-09-25 shape: the claude-sdk adapter stamps its random
# per-process key on every message, never the conversation id.
RANDOM_KEY = "e53c7829f57d4439859adec0a521371e"
PARENT = "6a966adeeaef434697965a3a4f28c7bc"


def _unwrapped(value: object) -> object:
    while getattr(value, "__triple_stamp_original__", None) is not None:
        value = value.__triple_stamp_original__  # type: ignore[attr-defined]
    return value


class _State:
    conversation_id = PARENT


class _App:
    state = _State()


class _Request:
    app = _App()


class _Executor:
    pass


class _Adapter:
    def __init__(self) -> None:
        self.executor = _Executor()

    def _ensure_executor(self) -> _Executor:
        return self.executor


class SessionIdResolutionTests(unittest.TestCase):
    def test_bound_conversation_wins_over_random_message_key(self) -> None:
        messages = [{"role": "user", "content": "q", "session_id": RANDOM_KEY}]

        self.assertEqual(
            supervisor_runtime._session_id(messages, bound=PARENT),
            PARENT,
        )
        # Direct executor callers without a binding keep the prior fallback.
        self.assertEqual(supervisor_runtime._session_id(messages), RANDOM_KEY)


class SessionBindingInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.saved_check = _scaffold.HarnessApp._check_conversation_id
        self.saved_run_turn = _executor_adapter.ExecutorAdapter.run_turn
        self.seen: list[str] = []

        async def recording_run_turn(adapter: object, _request: object, _ctx: object) -> None:
            self.seen.append(
                getattr(adapter._ensure_executor(), "_triple_stamp_conversation_id", "")  # type: ignore[attr-defined]
            )

        _scaffold.HarnessApp._check_conversation_id = _unwrapped(self.saved_check)
        _executor_adapter.ExecutorAdapter.run_turn = recording_run_turn

    def tearDown(self) -> None:
        _scaffold.HarnessApp._check_conversation_id = self.saved_check
        _executor_adapter.ExecutorAdapter.run_turn = self.saved_run_turn

    def test_binding_requires_a_triple_stamp_run(self) -> None:
        with mock.patch.dict(os.environ, {"TRIPLE_STAMP_RUN_ID": ""}, clear=False):
            supervisor_runtime.install_supervisor_session_binding()

        self.assertFalse(
            getattr(
                _executor_adapter.ExecutorAdapter.run_turn,
                "__triple_stamp_session_binding__",
                False,
            )
        )

    def test_validated_path_id_reaches_the_executor_on_both_versions(self) -> None:
        with mock.patch.dict(os.environ, {"TRIPLE_STAMP_RUN_ID": "fixture"}, clear=False):
            supervisor_runtime.install_supervisor_session_binding()
            supervisor_runtime.install_supervisor_session_binding()

        adapter = _Adapter()
        _scaffold.HarnessApp._check_conversation_id(adapter, _Request(), PARENT)
        # 0.12 TurnContext has no session_id; 0.14 carries the validated id.
        legacy_ctx = type("LegacyTurnContext", (), {})()
        current_ctx = type("TurnContext", (), {"session_id": PARENT})()
        asyncio.run(
            _executor_adapter.ExecutorAdapter.run_turn(adapter, object(), legacy_ctx)
        )
        asyncio.run(
            _executor_adapter.ExecutorAdapter.run_turn(_Adapter(), object(), current_ctx)
        )

        self.assertEqual(self.seen, [PARENT, PARENT])
        self.assertEqual(adapter._triple_stamp_conversation_id, PARENT)  # type: ignore[attr-defined]
        with self.assertRaises(Exception):
            _scaffold.HarnessApp._check_conversation_id(
                _Adapter(),
                _Request(),
                RANDOM_KEY,
            )


class SessionBindingGuardTests(unittest.TestCase):
    def test_post_stamp_wake_is_suppressed_without_a_model_turn(self) -> None:
        from omnigent.inner import claude_sdk_executor
        from omnigent.inner.executor import TurnComplete

        original = claude_sdk_executor.ClaudeSDKExecutor.run_turn
        model_turns = 0

        async def scripted(*_args: object, **_kwargs: object):
            nonlocal model_turns
            model_turns += 1
            yield TurnComplete(response="2 + 2 = 4.")

        class Supervisor:
            _agent_name = "triple-stamp"
            _triple_stamp_conversation_id = PARENT

        wake = [
            {
                "role": "user",
                "content": (
                    "[System: sub-agent codex_judge/judge-cycle-1 finished "
                    "(completed) — 1 result waiting in inbox. Call "
                    "sys_read_inbox to collect.]"
                ),
                "session_id": RANDOM_KEY,
            }
        ]

        async def collect() -> list[object]:
            return [
                event
                async for event in claude_sdk_executor.ClaudeSDKExecutor.run_turn(
                    Supervisor(),
                    wake,
                    [],
                    "route",
                    None,
                )
            ]

        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {"TRIPLE_STAMP_RUN_DIR": value, "TRIPLE_STAMP_RUN_ID": "fixture"},
            clear=False,
        ):
            runtime_state.activate_parent_attempt(PARENT, "request")
            runtime_state.append_supervisor_continuation(
                {
                    "action": "terminal_stamp_delivered",
                    "attempt": 1,
                    "reason": "attested STAMP TextChunk was consumed",
                    "route": {"status": "success"},
                    "parent_session_id": PARENT,
                }
            )
            saved_check = _scaffold.HarnessApp._check_conversation_id
            saved_adapter_turn = _executor_adapter.ExecutorAdapter.run_turn
            claude_sdk_executor.ClaudeSDKExecutor.run_turn = scripted
            try:
                supervisor_runtime.install_supervisor_continuation_guard()
                events = asyncio.run(collect())
            finally:
                claude_sdk_executor.ClaudeSDKExecutor.run_turn = original
                _scaffold.HarnessApp._check_conversation_id = saved_check
                _executor_adapter.ExecutorAdapter.run_turn = saved_adapter_turn
            actions = [
                row["action"]
                for row in runtime_state.read_supervisor_continuations(PARENT)
            ]
            phantom_attempt = runtime_state._attempt_root(Path(value), RANDOM_KEY)

            self.assertEqual(model_turns, 0)
            self.assertEqual(len(events), 1)
            self.assertIsInstance(events[0], TurnComplete)
            self.assertEqual(events[0].response, "")
            self.assertIn("terminal_stamp_suppressed", actions)
            self.assertFalse(phantom_attempt.exists())
            self.assertEqual(runtime_state.read_supervisor_continuations(RANDOM_KEY), [])


if __name__ == "__main__":
    unittest.main()
