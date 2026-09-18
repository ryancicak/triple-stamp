from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT_HOME = Path(
    "/tmp/omnigent-triple-stamp-502-aecbbf931f556ffc/run-asctomsx/home"
)
RUNTIME_PYTHON = ROOT / ".omnigent/runtime-python"
if str(RUNTIME_PYTHON) not in sys.path:
    sys.path.insert(0, str(RUNTIME_PYTHON))

import triple_stamp_browser_runtime as browser  # noqa: E402
import triple_stamp_cursor_lifecycle as lifecycle  # noqa: E402


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


launcher = _module(
    "triple_stamp_browser_launcher_under_test",
    ROOT / ".omnigent/launcher.py",
)


def _hosts(*rows: dict[str, object]) -> dict[str, object]:
    return {"hosts": list(rows)}


def _agents(*rows: dict[str, object]) -> dict[str, object]:
    return {"object": "list", "data": list(rows)}


def _filesystem(root: str) -> dict[str, object]:
    return {
        "object": "list",
        "data": [
            {
                "name": "config.yaml",
                "path": str(Path(root) / "config.yaml"),
                "type": "file",
            }
        ],
        "has_more": False,
    }


def _session(
    *,
    session_id: str = "session-1",
    agent_id: str = "triple",
    host_id: str = "host-local",
    workspace: str | None = None,
    host_online: bool = True,
    runner_online: bool = True,
) -> dict[str, object]:
    return {
        "id": session_id,
        "agent_id": agent_id,
        "host_id": host_id,
        "host_online": host_online,
        "runner_online": runner_online,
        "workspace": workspace or str(ROOT),
    }


def _inbox_probe_response() -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": "triple-stamp-parent-inbox-preflight",
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": lifecycle.PARENT_INBOX_READY,
                }
            ]
        },
    }


class BrowserHostRuntimeTests(unittest.TestCase):
    def test_no_host_fails_before_browser_url(self) -> None:
        with self.assertRaisesRegex(
            browser.BrowserHostPreflightError,
            "no online host",
        ):
            browser.assess_browser_readiness(
                session_id="session-1",
                hosts_payload=_hosts(),
                agents_payload=_agents({"id": "triple", "name": "triple-stamp"}),
                session_payload=_session(),
                agent_payload={"id": "triple", "name": "triple-stamp"},
                filesystem_payload=_filesystem(str(ROOT)),
                required_path=str(ROOT),
                launch_cwd=str(ROOT),
            )

    def test_no_host_never_calls_url_announcer(self) -> None:
        announce = mock.Mock()

        def reject(**_kwargs: object) -> browser.BrowserReadiness:
            raise browser.BrowserHostPreflightError("no online host")

        with self.assertRaisesRegex(
            browser.BrowserHostPreflightError,
            "no online host",
        ):
            browser.announce_after_browser_preflight(
                base_url="http://127.0.0.1:6767",
                conversation_id="session-1",
                echo=None,
                announce=announce,
                preflight=reject,
            )
        announce.assert_not_called()

    def test_registered_host_passes_no_inference_api_smoke(self) -> None:
        calls: list[str] = []
        posts: list[tuple[str, object]] = []
        payloads = {
            "/v1/hosts": _hosts(
                {
                    "host_id": "host-local",
                    "status": "online",
                    "sandbox_provider": None,
                }
            ),
            "/v1/agents": _agents(
                {"id": "triple", "name": "triple-stamp"},
                {"id": "cursor", "name": "cursor-native-ui"},
            ),
            "/v1/sessions/session-1": {
                "id": "session-1",
                "agent_id": "triple",
                "host_id": "host-local",
                "host_online": True,
                "runner_online": True,
                "workspace": str(ROOT),
                "items": [],
            },
            "/v1/sessions/session-1/agent": {
                "id": "triple",
                "name": "triple-stamp",
            },
            "/v1/hosts/host-local/filesystem": _filesystem(str(ROOT)),
        }

        def fetch(_base_url: str, path: str) -> object:
            calls.append(path)
            return payloads[path]

        def post(_base_url: str, path: str, payload: object) -> object:
            posts.append((path, payload))
            return _inbox_probe_response()

        readiness = browser.wait_for_browser_readiness(
            base_url="http://127.0.0.1:6767",
            session_id="session-1",
            timeout=0,
            fetch_json=fetch,
            post_json=post,
            required_path=str(ROOT),
            launch_cwd=str(ROOT),
        )
        self.assertEqual(readiness.online_host_ids, ("host-local",))
        self.assertEqual(readiness.agent_ids, ("triple", "cursor"))
        self.assertEqual(readiness.host_id, "host-local")
        self.assertEqual(readiness.selected_agent_id, "triple")
        self.assertEqual(readiness.host_workspace_root, str(ROOT))
        self.assertEqual(readiness.candidate_workspace, str(ROOT))
        self.assertEqual(readiness.required_path, str(ROOT))
        self.assertEqual(readiness.launch_cwd, str(ROOT))
        self.assertTrue(readiness.send_prerequisites)
        self.assertTrue(readiness.parent_inbox_ready)
        self.assertEqual(
            calls,
            [
                "/v1/hosts",
                "/v1/agents",
                "/v1/sessions/session-1",
                "/v1/sessions/session-1/agent",
                "/v1/hosts/host-local/filesystem",
            ],
        )
        self.assertEqual(
            posts,
            [
                (
                    "/v1/sessions/session-1/mcp",
                    browser.parent_inbox_probe_request(),
                )
            ],
        )

    def test_browser_preflight_rejects_missing_parent_inbox(self) -> None:
        payloads = {
            "/v1/hosts": _hosts(
                {
                    "host_id": "host-local",
                    "status": "online",
                    "sandbox_provider": None,
                }
            ),
            "/v1/agents": _agents({"id": "triple"}),
            "/v1/sessions/session-1": _session(),
            "/v1/sessions/session-1/agent": {"id": "triple"},
            "/v1/hosts/host-local/filesystem": _filesystem(str(ROOT)),
        }

        with self.assertRaisesRegex(
            browser.BrowserHostPreflightError,
            "parent session inbox capability probe",
        ):
            browser.wait_for_browser_readiness(
                base_url="http://127.0.0.1:6767",
                session_id="session-1",
                timeout=0,
                fetch_json=lambda _url, path: payloads[path],
                post_json=lambda _url, _path, _payload: {
                    "jsonrpc": "2.0",
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Error: sys_read_inbox requires parent "
                                    "session inbox"
                                ),
                            }
                        ]
                    },
                },
                required_path=str(ROOT),
                launch_cwd=str(ROOT),
            )

    def test_stale_host_is_not_browser_ready(self) -> None:
        with self.assertRaisesRegex(
            browser.BrowserHostPreflightError,
            "no online host",
        ):
            browser.assess_browser_readiness(
                session_id="session-1",
                hosts_payload=_hosts(
                    {
                        "host_id": "host-stale",
                        "status": "offline",
                        "sandbox_provider": None,
                    }
                ),
                agents_payload=_agents({"id": "triple"}),
                session_payload=_session(host_id="host-stale"),
                agent_payload={"id": "triple"},
                filesystem_payload=_filesystem(str(ROOT)),
                required_path=str(ROOT),
                launch_cwd=str(ROOT),
            )

    def test_offline_session_runner_is_not_announced(self) -> None:
        with self.assertRaisesRegex(
            browser.BrowserHostPreflightError,
            "not bound to an online local runner",
        ):
            browser.assess_browser_readiness(
                session_id="session-1",
                hosts_payload=_hosts(
                    {
                        "host_id": "host-local",
                        "status": "online",
                        "sandbox_provider": None,
                    }
                ),
                agents_payload=_agents({"id": "triple"}),
                session_payload=_session(runner_online=False),
                agent_payload={"id": "triple"},
                filesystem_payload=_filesystem(str(ROOT)),
                required_path=str(ROOT),
                launch_cwd=str(ROOT),
            )

    def test_all_registered_agents_remain_selectable(self) -> None:
        payload = _agents(
            {"id": "triple", "name": "triple-stamp"},
            {"id": "cursor", "name": "cursor-native-ui"},
            {"id": "codex", "name": "codex-native-ui"},
        )
        self.assertEqual(
            browser.registered_agent_ids(payload),
            ("triple", "cursor", "codex"),
        )

    def test_send_button_prerequisites(self) -> None:
        ready = {
            "message": "test",
            "agent_id": "triple",
            "host_id": "host-local",
            "workspace": "/workspace",
        }
        self.assertTrue(browser.browser_send_enabled(**ready))
        for missing in ("message", "agent_id", "host_id", "workspace"):
            state = dict(ready)
            state[missing] = "" if missing in {"message", "workspace"} else None
            self.assertFalse(browser.browser_send_enabled(**state), missing)
        self.assertFalse(browser.browser_send_enabled(**ready, starting=True))

    def test_exact_ephemeral_home_browser_regression(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            run_dir = SCREENSHOT_HOME.parent
            env = launcher._runtime_env(
                root=ROOT,
                real_home=Path("/Users/unit"),
                run_dir=run_dir,
                bundle=run_dir / "bundle",
                run_id="unit-run",
                sandbox_token="sandbox",
                cursor_token="cursor",
                omnigent_token="omnigent",
                harness_tmp=Path(value) / "short",
                tools=launcher.Toolchain(
                    isaac=Path("/tool"),
                    dbcert=Path("/tool"),
                    databricks=Path("/tool"),
                    uv=Path("/tool"),
                    omnigent=Path("/tool"),
                    omnigent_python=Path("/tool"),
                    cursor_agent=Path("/tool"),
                    sandbox_exec=Path("/tool"),
                    security=Path("/tool"),
                ),
                managed_python=Path("/managed/python"),
                voice_profile_sha256="0" * 64,
                provider="direct",
                models=launcher._provider_models(ROOT, "direct"),
            )
            launcher._configure_browser_runtime(env, ROOT)

            readiness = browser.assess_browser_readiness(
                session_id="session-1",
                hosts_payload=_hosts(
                    {
                        "host_id": "host-local",
                        "status": "online",
                        "sandbox_provider": None,
                    }
                ),
                agents_payload=_agents({"id": "triple"}),
                session_payload=_session(),
                agent_payload={"id": "triple"},
                filesystem_payload=_filesystem(str(ROOT)),
                required_path=env[browser.BROWSER_REQUIRED_PATH_ENV],
                launch_cwd=env["PWD"],
            )

            self.assertEqual(env["HOME"], str(run_dir / "home"))
            self.assertNotEqual(env["HOME"], env["PWD"])
            self.assertEqual(env["PWD"], str(ROOT))
            passthrough = set(env["OMNIGENT_RUNNER_ENV_PASSTHROUGH"].split(","))
            self.assertTrue(
                {
                    browser.BROWSER_HOST_PREFLIGHT_ENV,
                    browser.BROWSER_WORKSPACE_ROOTS_ENV,
                    browser.BROWSER_REQUIRED_PATH_ENV,
                }.issubset(passthrough)
            )
            self.assertEqual(readiness.host_workspace_root, str(ROOT))
            self.assertEqual(readiness.candidate_workspace, str(ROOT))
            self.assertTrue(readiness.send_prerequisites)

    def test_temp_home_default_is_rejected_before_url(self) -> None:
        temp_home = str(SCREENSHOT_HOME)
        with self.assertRaisesRegex(
            browser.BrowserHostPreflightError,
            "host workspace root .* outside selected agent's required path",
        ):
            browser.assess_browser_readiness(
                session_id="session-1",
                hosts_payload=_hosts(
                    {
                        "host_id": "host-local",
                        "status": "online",
                        "sandbox_provider": None,
                    }
                ),
                agents_payload=_agents({"id": "triple"}),
                session_payload=_session(),
                agent_payload={"id": "triple"},
                filesystem_payload=_filesystem(temp_home),
                required_path=str(ROOT),
                launch_cwd=str(ROOT),
            )

    def test_session_outside_required_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            browser.BrowserHostPreflightError,
            "session workspace .* outside selected agent's required path",
        ):
            browser.assess_browser_readiness(
                session_id="session-1",
                hosts_payload=_hosts(
                    {
                        "host_id": "host-local",
                        "status": "online",
                        "sandbox_provider": None,
                    }
                ),
                agents_payload=_agents({"id": "triple"}),
                session_payload=_session(workspace="/tmp/outside"),
                agent_payload={"id": "triple"},
                filesystem_payload=_filesystem(str(ROOT)),
                required_path=str(ROOT),
                launch_cwd=str(ROOT),
            )

    def test_paths_with_spaces_are_preserved(self) -> None:
        root = "/tmp/Project With Spaces"
        readiness = browser.assess_browser_readiness(
            session_id="session-1",
            hosts_payload=_hosts(
                {
                    "host_id": "host-local",
                    "status": "online",
                    "sandbox_provider": None,
                }
            ),
            agents_payload=_agents({"id": "triple"}),
            session_payload=_session(workspace=root),
            agent_payload={"id": "triple"},
            filesystem_payload=_filesystem(root),
            required_path=root,
            launch_cwd=root,
        )
        self.assertEqual(
            readiness.candidate_workspace,
            str(Path(root).resolve(strict=False)),
        )

    def test_picker_root_aliases_tilde_without_aliasing_home(self) -> None:
        roots = (str(ROOT),)
        self.assertEqual(
            browser.resolve_browser_host_directory("~", roots=roots),
            str(ROOT),
        )
        self.assertEqual(
            browser.resolve_browser_host_directory("~/tests", roots=roots),
            str(ROOT / "tests"),
        )
        with self.assertRaisesRegex(
            browser.BrowserHostPreflightError,
            "outside the allowed workspace roots",
        ):
            browser.resolve_browser_host_directory("/tmp", roots=roots)

    def test_picker_accepts_each_configured_agent_root(self) -> None:
        roots = ("/tmp/agent-one", "/tmp/Agent Two")
        self.assertEqual(
            browser.resolve_browser_host_directory(
                "/tmp/Agent Two/project",
                roots=roots,
            ),
            str(Path("/tmp/Agent Two/project").resolve(strict=False)),
        )

    def test_host_daemon_lists_project_not_isolated_home(self) -> None:
        from omnigent.host.connect import HostProcess
        from omnigent.host.frames import HostListDirFrame

        installed_list_dir = HostProcess._handle_list_dir
        installed_create_dir = HostProcess._handle_create_dir
        original_list_dir = getattr(
            installed_list_dir,
            "__triple_stamp_original__",
            installed_list_dir,
        )
        original_create_dir = getattr(
            installed_create_dir,
            "__triple_stamp_original__",
            installed_create_dir,
        )
        try:
            HostProcess._handle_list_dir = original_list_dir
            HostProcess._handle_create_dir = original_create_dir
            with tempfile.TemporaryDirectory() as value:
                root = Path(value) / "Project Root"
                isolated_home = Path(value) / "run" / "home"
                root.mkdir()
                isolated_home.mkdir(parents=True)
                (root / "README.md").write_text("project", encoding="utf-8")
                (isolated_home / ".omnigent").mkdir()
                with mock.patch.dict(
                    os.environ,
                    {
                        browser.BROWSER_WORKSPACE_ROOTS_ENV: json.dumps([str(root)]),
                        "HOME": str(isolated_home),
                    },
                    clear=False,
                ):
                    browser._install_host_workspace_root_guard()
                    result = HostProcess._handle_list_dir(
                        object.__new__(HostProcess),
                        HostListDirFrame(request_id="root", path="~"),
                    )
                    denied = HostProcess._handle_list_dir(
                        object.__new__(HostProcess),
                        HostListDirFrame(
                            request_id="outside",
                            path=str(isolated_home),
                        ),
                    )
                self.assertEqual([entry.name for entry in result.entries], ["README.md"])
                self.assertEqual(
                    {str(Path(entry.path).parent) for entry in result.entries},
                    {str(root.resolve())},
                )
                self.assertEqual(denied.entries, [])
                self.assertIn("outside ephemeral workspace roots", denied.error or "")
        finally:
            HostProcess._handle_list_dir = installed_list_dir
            HostProcess._handle_create_dir = installed_create_dir

    def test_interactive_no_session_uses_ephemeral_host_architecture(self) -> None:
        runtime_args, browser_mode, translated = launcher._browser_launch_args(
            ["--no-session", "--debug-events"]
        )
        self.assertEqual(runtime_args, ["--debug-events"])
        self.assertTrue(browser_mode)
        self.assertTrue(translated)

    def test_public_help_advertises_only_interactive_surfaces(self) -> None:
        with (
            mock.patch("builtins.print") as output,
            self.assertRaises(SystemExit),
        ):
            launcher._validate_cli_args(["--help"])
        text = output.call_args.args[0]
        self.assertIn("browser UI", text)
        self.assertIn("interactive terminal", text)
        self.assertIn("--self-test", text)
        for hidden in ("-q", "--prompt", "--no-session", "--debug-events"):
            self.assertNotIn(hidden, text)
        self.assertNotRegex(text, r"(^|\s)-p(\s|$)")

    def test_one_shot_flags_point_to_interactive_surfaces(self) -> None:
        for args in (
            ["-q"],
            ["-p", "hello"],
            ["--prompt", "hello"],
            ["--prompt=hello"],
        ):
            with self.subTest(args=args), mock.patch.object(
                launcher,
                "_die",
                side_effect=RuntimeError,
            ) as die, self.assertRaises(RuntimeError):
                launcher._validate_cli_args(args)
            message = die.call_args.args[0]
            self.assertIn("one-shot mode is not a supported", message)
            self.assertIn("browser or interactive terminal prompt", message)

    def test_one_shot_no_session_is_unchanged(self) -> None:
        for args in (
            ["--no-session", "-p", "hello"],
            ["--no-session", "--prompt", "hello"],
            ["--no-session", "--prompt=hello"],
        ):
            with self.subTest(args=args):
                runtime_args, browser_mode, translated = launcher._browser_launch_args(
                    list(args)
                )
                self.assertEqual(runtime_args, args)
                self.assertFalse(browser_mode)
                self.assertFalse(translated)

    def test_cleanup_reaps_host_server_and_runner_data_dir(self) -> None:
        tools = launcher.Toolchain(
            isaac=Path("/tool"),
            dbcert=Path("/tool"),
            databricks=Path("/tool"),
            uv=Path("/tool"),
            omnigent=Path("/tool"),
            omnigent_python=Path("/tool"),
            cursor_agent=Path("/tool"),
            sandbox_exec=Path("/tool"),
            security=Path("/tool"),
        )
        with (
            tempfile.TemporaryDirectory() as value,
            mock.patch.object(
                launcher,
                "_reap_marked_processes",
                return_value=[],
            ) as marked,
            mock.patch.object(launcher, "_reap_data_dir") as data_dir,
        ):
            run_dir = Path(value)
            survivors = launcher._cleanup_runtime_services(
                tools,
                "run-id",
                run_dir,
                {"OMNIGENT_DATA_DIR": str(run_dir / "state")},
            )
        self.assertEqual(survivors, [])
        marked.assert_called_once_with("run-id")
        data_dir.assert_called_once_with(
            tools,
            run_dir / "state",
            {"OMNIGENT_DATA_DIR": str(run_dir / "state")},
        )

    def test_browser_guard_forwards_explicit_runner_environment(self) -> None:
        from omnigent import cli

        original = cli._LOCAL_DAEMON_ENV_ALLOWLIST
        try:
            cli._LOCAL_DAEMON_ENV_ALLOWLIST = frozenset({"HOME"})
            with mock.patch.dict(
                os.environ,
                {
                    "OMNIGENT_RUNNER_ENV_PASSTHROUGH": (
                        "TRIPLE_STAMP_RUN_ID,CURSOR_AUTH_TOKEN,MISSING_VALUE"
                    ),
                    "TRIPLE_STAMP_RUN_ID": "run-id",
                    "CURSOR_AUTH_TOKEN": "secret",
                },
                clear=False,
            ):
                browser._forward_runner_passthrough_into_local_daemon()
            self.assertEqual(
                cli._LOCAL_DAEMON_ENV_ALLOWLIST,
                frozenset(
                    {
                        "HOME",
                        "TRIPLE_STAMP_RUN_ID",
                        "CURSOR_AUTH_TOKEN",
                    }
                ),
            )
        finally:
            cli._LOCAL_DAEMON_ENV_ALLOWLIST = original

    def test_browser_runner_endpoint_provisions_and_collects_without_inference(
        self,
    ) -> None:
        async def scenario() -> None:
            from omnigent.runner import app as runner_app
            from omnigent.runner import tool_dispatch

            lifecycle._install_runner_session_inbox_initialization()
            lifecycle._install_parent_inbox_probe()
            session_id = "browser-no-inference"
            runner_app._session_inboxes_ref.pop(session_id, None)
            server_client = httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(404, json={})
                ),
                base_url="http://server.test",
            )
            application = runner_app.create_runner_app(
                server_client=server_client,
                runner_workspace=ROOT,
                per_session_workspace=False,
            )
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=application),
                    base_url="http://runner.test",
                ) as client:
                    event = await client.post(
                        f"/v1/sessions/{session_id}/events",
                        json={
                            "type": "message",
                            "content": [{"type": "input_text", "text": "unused"}],
                        },
                    )
                    self.assertEqual(event.status_code, 501)
                    inbox = runner_app._session_inboxes_ref[session_id]
                    inbox.put_nowait(
                        {
                            "type": "terminal_idle",
                            "source": "fake-child",
                            "session": "test-session",
                            "content": {
                                "status": "idle",
                                "terminal": "fake-child",
                                "session": "test-session",
                            },
                        }
                    )

                    probe = await client.post(
                        f"/v1/sessions/{session_id}/mcp/execute",
                        json=browser.parent_inbox_probe_request(),
                    )
                    self.assertEqual(probe.status_code, 200)
                    self.assertEqual(
                        probe.json()["result"]["output"],
                        lifecycle.PARENT_INBOX_READY,
                    )
                    self.assertEqual(inbox.qsize(), 1)
                    send_result = await tool_dispatch._execute_subagent_tool(
                        {
                            "agent": "fake-test-child",
                            "title": "no-inference",
                            "args": "do not launch",
                        },
                        server_client=object(),
                        conversation_id=session_id,
                        agent_spec=None,
                        session_inbox=inbox,
                    )
                    self.assertIn("not found in agent spec", send_result)
                    self.assertNotIn("requires parent session inbox", send_result)

                    collected = await client.post(
                        f"/v1/sessions/{session_id}/mcp/execute",
                        json={
                            "jsonrpc": "2.0",
                            "id": "collect",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_read_inbox",
                                "arguments": {},
                            },
                        },
                    )
                    self.assertIn(
                        "fake-child:test-session is idle",
                        collected.json()["result"]["output"],
                    )
                    self.assertTrue(inbox.empty())

                    deleted = await client.delete(f"/v1/sessions/{session_id}")
                    self.assertEqual(deleted.status_code, 200)
                    self.assertNotIn(session_id, runner_app._session_inboxes_ref)
            finally:
                runner_app._session_inboxes_ref.pop(session_id, None)
                await server_client.aclose()

        asyncio.run(scenario())

    def test_browser_inboxes_isolate_concurrency_restart_and_stale_state(
        self,
    ) -> None:
        async def scenario() -> None:
            from omnigent.runner import app as runner_app

            lifecycle._install_runner_session_inbox_initialization()
            session_ids = ("browser-concurrent-a", "browser-concurrent-b")
            for session_id in session_ids:
                runner_app._session_inboxes_ref.pop(session_id, None)
            server_client = httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(404, json={})
                ),
                base_url="http://server.test",
            )
            first_app = runner_app.create_runner_app(server_client=server_client)
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=first_app),
                    base_url="http://runner.test",
                ) as client:
                    await asyncio.gather(
                        *(
                            client.post(
                                f"/v1/sessions/{session_id}/events",
                                json={"type": "message"},
                            )
                            for session_id in session_ids
                        )
                    )
                    first_a = runner_app._session_inboxes_ref[session_ids[0]]
                    first_b = runner_app._session_inboxes_ref[session_ids[1]]
                    self.assertIsNot(first_a, first_b)
                    first_a.put_nowait({"stale": True})
                    lifecycle._parent_inbox_failures[session_ids[0]] = (
                        "terminal test failure"
                    )

                    deleted = await client.delete(
                        f"/v1/sessions/{session_ids[0]}"
                    )
                    self.assertEqual(deleted.status_code, 200)
                    self.assertNotIn(
                        session_ids[0],
                        lifecycle._parent_inbox_failures,
                    )
                    await client.post(
                        f"/v1/sessions/{session_ids[0]}/events",
                        json={"type": "message"},
                    )
                    recreated = runner_app._session_inboxes_ref[session_ids[0]]
                    self.assertIsNot(recreated, first_a)
                    self.assertTrue(recreated.empty())

                runner_app._session_inboxes_ref.clear()
                restarted_app = runner_app.create_runner_app(
                    server_client=server_client
                )
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=restarted_app),
                    base_url="http://runner.test",
                ) as restarted:
                    await restarted.post(
                        f"/v1/sessions/{session_ids[1]}/events",
                        json={"type": "message"},
                    )
                    after_restart = runner_app._session_inboxes_ref[session_ids[1]]
                    self.assertIsNot(after_restart, first_b)
                    self.assertTrue(after_restart.empty())
                    await restarted.delete(f"/v1/sessions/{session_ids[1]}")
            finally:
                for session_id in session_ids:
                    runner_app._session_inboxes_ref.pop(session_id, None)
                await server_client.aclose()

        asyncio.run(scenario())

    def test_rejected_runner_request_does_not_allocate_inbox(self) -> None:
        async def scenario() -> None:
            from omnigent.runner import app as runner_app

            lifecycle._install_runner_session_inbox_initialization()
            session_id = "browser-unauthorized"
            runner_app._session_inboxes_ref.pop(session_id, None)
            server_client = httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(404, json={})
                ),
                base_url="http://server.test",
            )
            application = runner_app.create_runner_app(
                server_client=server_client,
                auth_token="runner-secret",
            )
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=application),
                    base_url="http://runner.test",
                ) as client:
                    rejected = await client.post(
                        f"/v1/sessions/{session_id}/events",
                        json={"type": "message"},
                    )
                    self.assertEqual(rejected.status_code, 401)
                    self.assertNotIn(session_id, runner_app._session_inboxes_ref)
                    accepted = await client.post(
                        f"/v1/sessions/{session_id}/events",
                        headers={"Authorization": "Bearer runner-secret"},
                        json={"type": "message"},
                    )
                    self.assertEqual(accepted.status_code, 501)
                    self.assertIn(session_id, runner_app._session_inboxes_ref)
            finally:
                runner_app._session_inboxes_ref.pop(session_id, None)
                await server_client.aclose()

        asyncio.run(scenario())

    def test_runtime_module_compiles_in_isolation(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "py_compile",
                str(RUNTIME_PYTHON / "triple_stamp_browser_runtime.py"),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
