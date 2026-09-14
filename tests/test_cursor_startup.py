from __future__ import annotations

import errno
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "cursor_via_login_under_test", ROOT / ".omnigent/cursor_via_login.py"
)
assert SPEC is not None and SPEC.loader is not None
cursor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cursor)


class FakeProcess:
    def __init__(self, polls_before_exit: int = 1) -> None:
        self.pid = 4242
        self.stderr = io.StringIO("")
        self.returncode: int | None = None
        self.polls_before_exit = polls_before_exit
        self.poll_count = 0
        self.terminated = False
        self.wait_called = False

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        self.poll_count += 1
        if self.poll_count > self.polls_before_exit:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.wait_called = True
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def send_signal(self, signum: int) -> None:
        del signum


class CursorStartupTests(unittest.TestCase):
    def test_observed_grok_config_is_exact_and_drift_fails(self) -> None:
        fixture = json.loads(
            (
                ROOT
                / "tests/fixtures/cursor-grok-4.6-xhigh-discovery.json"
            ).read_text(encoding="utf-8")
        )
        exact = {
            "model": fixture["model"],
            "selectedModel": fixture["selectedModel"],
            "modelParameters": fixture["modelParameters"],
        }
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ, {"TRIPLE_STAMP_CURSOR_HOME": value}, clear=False
        ):
            config = Path(value) / ".cursor/cli-config.json"
            config.parent.mkdir(parents=True)
            for drift in ("alias", "base", "display", "parameter"):
                candidate = json.loads(json.dumps(exact))
                if drift == "alias":
                    self.assertIsNone(
                        cursor._filtered_session_args(
                            ["--model", "gpt-5.6-sol-xhigh"]
                        )
                    )
                    continue
                if drift == "base":
                    candidate["selectedModel"]["modelId"] = "grok-4.5"
                elif drift == "display":
                    candidate["model"]["displayName"] = "Grok"
                else:
                    candidate["selectedModel"]["parameters"][0]["value"] = "high"
                config.write_text(json.dumps(candidate), encoding="utf-8")
                self.assertFalse(cursor.model_config_is_exact(), drift)
            config.write_text(json.dumps(exact), encoding="utf-8")
            self.assertTrue(cursor.model_config_is_exact())

    def test_only_exact_grok_model_flag_is_accepted(self) -> None:
        self.assertEqual(
            cursor._filtered_session_args(
                ["--model", "cursor-grok-4.6-xhigh", "--print"]
            ),
            ["--print"],
        )
        for args in (
            ["--model", "gpt-5.6-sol-xhigh"],
            ["--model=claude-opus-4-8-max"],
            [
                "--model",
                "cursor-grok-4.6-xhigh",
                "--model",
                "cursor-grok-4.6-xhigh",
            ],
        ):
            self.assertIsNone(cursor._filtered_session_args(args))

    def test_new_chat_store_is_startup_activity(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            chat = root / "chat-1"
            chat.mkdir()
            launch_ms = int(time.time() * 1000)
            (chat / "meta.json").write_text(
                json.dumps({"createdAtMs": launch_ms}), encoding="utf-8"
            )
            (chat / "store.db").write_bytes(b"sqlite")
            self.assertTrue(
                cursor._prompt_was_accepted(
                    root=root,
                    launch_epoch_ms=launch_ms,
                    baseline_mtimes={},
                    resume_chat_id=None,
                )
            )

    def test_zero_activity_startup_is_bounded_failure(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            bridge = Path(value)
            process = FakeProcess(polls_before_exit=10_000)
            with (
                mock.patch.object(cursor, "_bridge_dir_from_workspace", return_value=bridge),
                mock.patch.object(cursor, "_workspace_chat_root", return_value=bridge / "chats"),
                mock.patch.object(cursor, "model_config_is_exact", return_value=True),
                mock.patch.object(cursor, "_prompt_was_accepted", return_value=False),
                mock.patch.object(cursor, "STARTUP_ACK_TIMEOUT_SECONDS", 0.01),
                mock.patch.object(cursor.subprocess, "Popen", return_value=process),
                mock.patch.object(cursor.signal, "signal", return_value=None),
                mock.patch.object(cursor, "fail", return_value=70),
            ):
                result = cursor._run_cursor("/fake/cursor-agent", ["--yolo"])
            self.assertEqual(result, 70)
            self.assertTrue(process.terminated)
            status = json.loads((bridge / "triple-stamp-startup.json").read_text())
            self.assertEqual(status["state"], "failed")
            self.assertIn("did not acknowledge", status["reason"])

    def test_immediate_child_exit_records_code_and_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            bridge = Path(value)
            process = FakeProcess(polls_before_exit=0)
            with (
                mock.patch.object(cursor, "_bridge_dir_from_workspace", return_value=bridge),
                mock.patch.object(cursor, "_workspace_chat_root", return_value=bridge / "chats"),
                mock.patch.object(cursor, "model_config_is_exact", return_value=True),
                mock.patch.object(cursor.subprocess, "Popen", return_value=process),
                mock.patch.object(cursor.signal, "signal", return_value=None),
                mock.patch.object(cursor, "fail", return_value=70),
            ):
                result = cursor._run_cursor("/fake/cursor-agent", ["--yolo"])
            self.assertEqual(result, 70)
            status = json.loads((bridge / "triple-stamp-startup.json").read_text())
            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["exit_code"], 0)
            self.assertIsNone(status["signal"])
            self.assertIn("home", status["runtime"])
            self.assertIn("RLIMIT_NOFILE", status["runtime"]["resource_limits"])

    def test_acknowledged_long_run_blocks_without_completion_polling(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            bridge = Path(value)
            process = FakeProcess(polls_before_exit=12)
            with (
                mock.patch.object(cursor, "_bridge_dir_from_workspace", return_value=bridge),
                mock.patch.object(cursor, "_workspace_chat_root", return_value=bridge / "chats"),
                mock.patch.object(cursor, "model_config_is_exact", return_value=True),
                mock.patch.object(cursor, "_prompt_was_accepted", return_value=True),
                mock.patch.object(cursor, "STARTUP_ACK_TIMEOUT_SECONDS", 0.0),
                mock.patch.object(cursor.subprocess, "Popen", return_value=process),
                mock.patch.object(cursor.signal, "signal", return_value=None),
                mock.patch.object(cursor.time, "sleep", return_value=None),
            ):
                result = cursor._run_cursor("/fake/cursor-agent", ["--yolo"])
            self.assertEqual(result, 0)
            self.assertTrue(process.wait_called)
            self.assertLessEqual(process.poll_count, 3)
            status = json.loads((bridge / "triple-stamp-startup.json").read_text())
            self.assertEqual(status["state"], "exited")

    def test_status_redacts_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            bridge = Path(value)
            cursor._write_startup_status(
                bridge,
                state="failed",
                pid=1,
                reason="failed",
                stderr="Authorization: Bearer abc123 access_token=def456",
            )
            text = (bridge / "triple-stamp-startup.json").read_text()
            self.assertNotIn("abc123", text)
            self.assertNotIn("def456", text)
            self.assertIn("[REDACTED]", text)

    def test_nonpaid_startup_preflight_uses_exact_cursor_environment(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            run = Path(value)
            home = run / "cursor-home"
            (home / ".cursor").mkdir(parents=True)
            real_popen = subprocess.Popen
            captured_env: dict[str, str] = {}

            def spawn(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
                del args
                captured_env.update(kwargs["env"])
                return real_popen(
                    [
                        "/bin/sh",
                        "-c",
                        "printf '01234567-89ab-cdef-0123-456789abcdef\\n'; sleep 60",
                    ],
                    **kwargs,
                )

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "TRIPLE_STAMP_RUN_DIR": str(run),
                        "TRIPLE_STAMP_CURSOR_HOME": str(home),
                        "OMNIGENT_CLAUDE_LAUNCHER": "isaac",
                        "OMNIGENT_CODEX_PATH": "/runner/codex-via-isaac",
                        "OMNIGENT_REMOTE_AUTH_TOKEN": "runner-secret",
                        "OMNIGENT_RUNNER_ENV_PASSTHROUGH": "ISAAC_BIN",
                        "ISAAC_BIN": "/usr/local/bin/isaac",
                        "ISAAC_DEFAULT_UCODE": "0",
                        "ISAAC_LAUNCH_MODE": "omni",
                        "TRIPLE_STAMP_PROVIDER": "databricks",
                        "TRIPLE_STAMP_SUPERVISOR_MODEL": (
                            "system.ai.claude-sonnet-4-6[1m]"
                        ),
                        "TRIPLE_STAMP_OPUS_MODEL": (
                            "system.ai.claude-opus-5[1m]"
                        ),
                        "TRIPLE_STAMP_CODEX_MODEL": "gpt-5.6-sol",
                    },
                    clear=False,
                ),
                mock.patch.object(cursor, "model_config_is_exact", return_value=True),
                mock.patch.object(cursor.subprocess, "Popen", side_effect=spawn),
                mock.patch.object(
                    cursor,
                    "_run_interactive_startup_probe",
                    return_value=(
                        True,
                        "",
                        4343,
                        None,
                        {
                            "interactive_startup_seen": True,
                            "worker_server_seen": True,
                            "worker_socket_isolated": True,
                            "paid_request_seen": False,
                        },
                    ),
                ),
            ):
                result = cursor._run_startup_preflight("/verified/cursor-agent")
            self.assertEqual(result, 0)
            self.assertEqual(captured_env["HOME"], str(home))
            self.assertEqual(captured_env["CURSOR_DATA_DIR"], str(home / ".cursor"))
            self.assertFalse(set(captured_env) & cursor._RUNNER_ONLY_ENV)
            status = json.loads((run / "cursor-startup-preflight.json").read_text())
            self.assertEqual(status["state"], "passed")
            self.assertFalse(status["paid_generation"])
            self.assertEqual(status["ack_kind"], "empty-chat-id")
            self.assertTrue(status["details"]["worker_socket_isolated"])
            self.assertFalse(status["details"]["paid_request_seen"])

    def test_resource_launch_failure_is_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as value, mock.patch.dict(
            os.environ,
            {
                "TRIPLE_STAMP_RUN_DIR": value,
                "TRIPLE_STAMP_CURSOR_HOME": str(Path(value) / "cursor-home"),
            },
            clear=False,
        ), mock.patch.object(
            cursor.subprocess,
            "Popen",
            side_effect=OSError(errno.EMFILE, "Too many open files"),
        ), mock.patch.object(
            cursor, "fail", return_value=77
        ):
            result = cursor._run_startup_preflight("/verified/cursor-agent")
            self.assertEqual(result, 77)
            status = json.loads(
                (Path(value) / "cursor-startup-preflight.json").read_text()
            )
            self.assertEqual(status["state"], "failed")
            self.assertIn("Too many open files", status["reason"])
            self.assertIn("RLIMIT_NOFILE", status["runtime"]["resource_limits"])

    def test_interactive_probe_rejects_global_worker_socket_exit(self) -> None:
        process = FakeProcess(polls_before_exit=10_000)

        def deltas(_root: Path, pattern: str, _baseline: object) -> str:
            if pattern == "*.log":
                return (
                    "global.main.promiseCatch listen EPERM: operation not permitted "
                    "/tmp/.cursor/workspace/worker.sock"
                )
            return "runServer socketPath=/tmp/.cursor/workspace/worker.sock"

        with (
            tempfile.TemporaryDirectory() as value,
            mock.patch.dict(
                os.environ,
                {
                    "TRIPLE_STAMP_CURSOR_HOME": value,
                    "CURSOR_DATA_DIR": str(Path(value) / ".cursor"),
                },
                clear=False,
            ),
            mock.patch.object(cursor.subprocess, "Popen", return_value=process),
            mock.patch.object(cursor, "_file_sizes", return_value={}),
            mock.patch.object(cursor, "_file_deltas", side_effect=deltas),
        ):
            ok, diagnostic, _pid, _code, details = (
                cursor._run_interactive_startup_probe(
                    "/verified/cursor-agent",
                    "01234567-89ab-cdef-0123-456789abcdef",
                )
            )
        self.assertFalse(ok)
        self.assertIn("worker.sock", diagnostic)
        self.assertFalse(details["worker_socket_isolated"])
        self.assertTrue(process.terminated)


if __name__ == "__main__":
    unittest.main()
