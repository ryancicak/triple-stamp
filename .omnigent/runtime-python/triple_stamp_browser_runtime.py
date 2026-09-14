"""Host-backed browser startup guard for the private triple-stamp runtime."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from triple_stamp_cursor_lifecycle import (
    PARENT_INBOX_PROBE_KEY,
    PARENT_INBOX_PROBE_VALUE,
    PARENT_INBOX_READY,
)

BROWSER_HOST_PREFLIGHT_ENV = "TRIPLE_STAMP_BROWSER_HOST_PREFLIGHT"
BROWSER_WORKSPACE_ROOTS_ENV = "TRIPLE_STAMP_BROWSER_WORKSPACE_ROOTS"
BROWSER_REQUIRED_PATH_ENV = "TRIPLE_STAMP_BROWSER_REQUIRED_PATH"
_PREFLIGHT_TIMEOUT_SECONDS = 8.0
_PREFLIGHT_POLL_SECONDS = 0.1


class BrowserHostPreflightError(RuntimeError):
    """The browser URL would be unusable because no local host is ready."""


@dataclass(frozen=True)
class BrowserReadiness:
    """No-inference API evidence collected before announcing a browser URL."""

    session_id: str
    online_host_ids: tuple[str, ...]
    agent_ids: tuple[str, ...]
    selected_agent_id: str
    host_id: str
    host_workspace_root: str
    candidate_workspace: str
    required_path: str
    launch_cwd: str
    send_prerequisites: bool
    parent_inbox_ready: bool = False


def _rows(payload: object, key: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def online_local_hosts(payload: object) -> list[dict[str, Any]]:
    """Return online user-machine hosts from a ``GET /v1/hosts`` response."""

    return [
        host
        for host in _rows(payload, "hosts")
        if host.get("status") == "online" and host.get("sandbox_provider") is None
    ]


def registered_agent_ids(payload: object) -> tuple[str, ...]:
    """Return every selectable agent id from ``GET /v1/agents``."""

    return tuple(
        str(agent["id"])
        for agent in _rows(payload, "data")
        if isinstance(agent.get("id"), str) and agent["id"]
    )


def _canonical_path(path: str) -> str:
    return str(Path(path).resolve(strict=False))


def workspace_satisfies_required_path(workspace: str, required_path: str) -> bool:
    """Return whether a canonical workspace is at or below an agent boundary."""

    try:
        candidate = Path(workspace).resolve(strict=False)
        boundary = Path(required_path).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False
    return candidate == boundary or candidate.is_relative_to(boundary)


def browser_workspace_roots(
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> tuple[str, ...]:
    """Read the launcher's JSON-encoded browser filesystem allowlist."""

    source = os.environ if environ is None else environ
    raw = source.get(BROWSER_WORKSPACE_ROOTS_ENV, "")
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BrowserHostPreflightError(
            f"browser workspace roots are malformed: {exc}"
        ) from exc
    if not isinstance(values, list) or not values:
        raise BrowserHostPreflightError("browser workspace roots are missing")

    roots: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise BrowserHostPreflightError(
                "browser workspace roots must be non-empty absolute paths"
            )
        canonical = _canonical_path(value)
        if canonical not in roots:
            roots.append(canonical)
    return tuple(roots)


def resolve_browser_host_directory(
    path: str,
    *,
    roots: tuple[str, ...] | None = None,
) -> str:
    """Resolve a picker path while confining browsing to configured roots."""

    allowed = roots or browser_workspace_roots()
    if path == "~":
        candidate = Path(allowed[0])
    elif path.startswith("~/"):
        candidate = Path(allowed[0]) / path[2:]
    else:
        candidate = Path(path)
    if not candidate.is_absolute():
        raise BrowserHostPreflightError(
            f"browser host directory must be absolute: {path!r}"
        )
    canonical = _canonical_path(str(candidate))
    if not any(workspace_satisfies_required_path(canonical, root) for root in allowed):
        raise BrowserHostPreflightError(
            f"browser host directory {path!r} is outside the allowed workspace roots"
        )
    return canonical


def candidate_workspace_from_filesystem(payload: object) -> str:
    """Derive the directory the stock Web UI selects from a host root listing."""

    entries = _rows(payload, "data") or _rows(payload, "entries")
    if not entries:
        raise BrowserHostPreflightError(
            "browser preflight failed: the host workspace root is empty or unreadable"
        )
    parents: set[str] = set()
    for entry in entries:
        path = entry.get("path")
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise BrowserHostPreflightError(
                "browser preflight failed: host filesystem returned a non-absolute path"
            )
        parents.add(_canonical_path(str(Path(path).parent)))
    if len(parents) != 1:
        raise BrowserHostPreflightError(
            "browser preflight failed: host filesystem root entries disagree"
        )
    return next(iter(parents))


def browser_send_enabled(
    *,
    message: str,
    agent_id: str | None,
    host_id: str | None,
    workspace: str,
    starting: bool = False,
) -> bool:
    """Mirror the Omnigent 0.12 computer-host composer prerequisites."""

    return bool(
        message.strip() and agent_id and host_id and workspace.strip() and not starting
    )


def parent_inbox_probe_request() -> dict[str, object]:
    """Return the no-inference JSON-RPC request used by browser preflight."""

    return {
        "jsonrpc": "2.0",
        "id": "triple-stamp-parent-inbox-preflight",
        "method": "tools/call",
        "params": {
            "name": "sys_read_inbox",
            "arguments": {
                PARENT_INBOX_PROBE_KEY: PARENT_INBOX_PROBE_VALUE,
            },
        },
    }


def parent_inbox_probe_succeeded(payload: object) -> bool:
    """Recognize only the runner's explicit non-draining inbox attestation."""

    if not isinstance(payload, dict) or payload.get("error") is not None:
        return False
    result = payload.get("result")
    if not isinstance(result, dict):
        return False
    if result.get("output") == PARENT_INBOX_READY:
        return True
    content = result.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("type") == "text"
        and item.get("text") == PARENT_INBOX_READY
        for item in content
    )


def assess_browser_readiness(
    *,
    session_id: str,
    hosts_payload: object,
    agents_payload: object,
    session_payload: object,
    agent_payload: object,
    filesystem_payload: object,
    required_path: str,
    launch_cwd: str,
) -> BrowserReadiness:
    """Validate host, agent, and conversation state without model inference."""

    hosts = online_local_hosts(hosts_payload)
    if not hosts:
        raise BrowserHostPreflightError(
            "browser preflight failed: the local server has no online host; "
            "the new-chat send button would stay disabled"
        )
    agent_ids = registered_agent_ids(agents_payload)
    if not agent_ids:
        raise BrowserHostPreflightError(
            "browser preflight failed: the local server has no selectable agents"
        )
    if not isinstance(session_payload, dict) or session_payload.get("id") != session_id:
        raise BrowserHostPreflightError(
            f"browser preflight failed: session {session_id!r} is not readable"
        )
    selected_agent_id = session_payload.get("agent_id")
    if (
        not isinstance(selected_agent_id, str)
        or not selected_agent_id
        or not isinstance(agent_payload, dict)
        or agent_payload.get("id") != selected_agent_id
    ):
        raise BrowserHostPreflightError(
            f"browser preflight failed: session {session_id!r} has no readable selected agent"
        )
    online_host_ids = tuple(
        str(host["host_id"])
        for host in hosts
        if isinstance(host.get("host_id"), str) and host["host_id"]
    )
    host_id = session_payload.get("host_id")
    if (
        host_id not in online_host_ids
        or session_payload.get("host_online") is not True
        or session_payload.get("runner_online") is not True
    ):
        raise BrowserHostPreflightError(
            f"browser preflight failed: session {session_id!r} is not bound "
            "to an online local runner"
        )
    assert isinstance(host_id, str)

    canonical_required = _canonical_path(required_path)
    canonical_launch_cwd = _canonical_path(launch_cwd)
    session_workspace = session_payload.get("workspace")
    if not isinstance(session_workspace, str) or not workspace_satisfies_required_path(
        session_workspace, canonical_required
    ):
        raise BrowserHostPreflightError(
            f"browser preflight failed: session workspace {session_workspace!r} is "
            f"outside selected agent's required path {canonical_required!r}"
        )
    if not workspace_satisfies_required_path(canonical_launch_cwd, canonical_required):
        raise BrowserHostPreflightError(
            f"browser preflight failed: host cwd {canonical_launch_cwd!r} is "
            f"outside selected agent's required path {canonical_required!r}"
        )

    candidate_workspace = candidate_workspace_from_filesystem(filesystem_payload)
    if not workspace_satisfies_required_path(candidate_workspace, canonical_required):
        raise BrowserHostPreflightError(
            f"browser preflight failed: host workspace root {candidate_workspace!r} is "
            f"outside selected agent's required path {canonical_required!r}"
        )
    send_prerequisites = browser_send_enabled(
        message="browser-preflight",
        agent_id=selected_agent_id,
        host_id=host_id,
        workspace=candidate_workspace,
    )
    if not send_prerequisites:
        raise BrowserHostPreflightError(
            "browser preflight failed: new-chat send prerequisites are incomplete"
        )

    selectable_agents = tuple(dict.fromkeys((*agent_ids, selected_agent_id)))
    return BrowserReadiness(
        session_id=session_id,
        online_host_ids=online_host_ids,
        agent_ids=selectable_agents,
        selected_agent_id=selected_agent_id,
        host_id=host_id,
        host_workspace_root=candidate_workspace,
        candidate_workspace=candidate_workspace,
        required_path=canonical_required,
        launch_cwd=canonical_launch_cwd,
        send_prerequisites=send_prerequisites,
    )


def _get_json(base_url: str, path: str, *, timeout: float = 2.0) -> object:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(
    base_url: str,
    path: str,
    payload: object,
    *,
    timeout: float = 2.0,
) -> object:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_browser_readiness(
    *,
    base_url: str,
    session_id: str,
    timeout: float = _PREFLIGHT_TIMEOUT_SECONDS,
    fetch_json: Callable[[str, str], object] | None = None,
    post_json: Callable[[str, str, object], object] | None = None,
    required_path: str | None = None,
    launch_cwd: str | None = None,
) -> BrowserReadiness:
    """Poll the local APIs until the host-backed conversation is usable."""

    fetch = fetch_json or (lambda url, path: _get_json(url, path))
    post = post_json or (lambda url, path, payload: _post_json(url, path, payload))
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while True:
        try:
            hosts_payload = fetch(base_url, "/v1/hosts")
            if not online_local_hosts(hosts_payload):
                raise BrowserHostPreflightError(
                    "browser preflight failed: the local server has no online host; "
                    "the new-chat send button would stay disabled"
                )
            agents_payload = fetch(base_url, "/v1/agents")
            if not registered_agent_ids(agents_payload):
                raise BrowserHostPreflightError(
                    "browser preflight failed: the local server has no selectable agents"
                )
            session_payload = fetch(base_url, f"/v1/sessions/{session_id}")
            if not isinstance(session_payload, dict):
                raise BrowserHostPreflightError(
                    f"browser preflight failed: session {session_id!r} is not readable"
                )
            host_id = session_payload.get("host_id")
            if not isinstance(host_id, str) or not host_id:
                raise BrowserHostPreflightError(
                    f"browser preflight failed: session {session_id!r} has no host"
                )
            effective_required_path = required_path or os.environ.get(
                BROWSER_REQUIRED_PATH_ENV
            )
            if not effective_required_path:
                raise BrowserHostPreflightError(
                    "browser preflight failed: selected agent required path is unavailable"
                )
            readiness = assess_browser_readiness(
                session_id=session_id,
                hosts_payload=hosts_payload,
                agents_payload=agents_payload,
                session_payload=session_payload,
                agent_payload=fetch(base_url, f"/v1/sessions/{session_id}/agent"),
                filesystem_payload=fetch(
                    base_url,
                    f"/v1/hosts/{urllib.parse.quote(host_id, safe='')}/filesystem",
                ),
                required_path=effective_required_path,
                launch_cwd=launch_cwd or os.getcwd(),
            )
            inbox_payload = post(
                base_url,
                f"/v1/sessions/{session_id}/mcp",
                parent_inbox_probe_request(),
            )
            if not parent_inbox_probe_succeeded(inbox_payload):
                raise BrowserHostPreflightError(
                    "browser preflight failed: parent session inbox "
                    "capability probe did not attest readiness"
                )
            return replace(readiness, parent_inbox_ready=True)
        except (
            BrowserHostPreflightError,
            json.JSONDecodeError,
            OSError,
            urllib.error.URLError,
        ) as exc:
            last_error = exc
        if time.monotonic() >= deadline:
            assert last_error is not None
            raise BrowserHostPreflightError(str(last_error)) from last_error
        time.sleep(_PREFLIGHT_POLL_SECONDS)


def _forward_runner_passthrough_into_local_daemon() -> None:
    """Preserve the launcher's explicit runner env across CLI -> daemon."""

    from omnigent import cli

    names = {
        name.strip()
        for name in os.environ.get("OMNIGENT_RUNNER_ENV_PASSTHROUGH", "").split(",")
        if name.strip() and name.strip() in os.environ
    }
    cli._LOCAL_DAEMON_ENV_ALLOWLIST = frozenset(  # type: ignore[attr-defined]
        set(cli._LOCAL_DAEMON_ENV_ALLOWLIST) | names  # type: ignore[attr-defined]
    )


def _install_host_workspace_root_guard() -> None:
    """Make the browser picker root independent from the credential HOME."""

    roots = browser_workspace_roots()
    from omnigent.host.connect import HostProcess
    from omnigent.host.frames import (
        HostCreateDirResultFrame,
        HostListDirResultFrame,
    )

    original_list_dir = HostProcess._handle_list_dir
    if not getattr(original_list_dir, "__triple_stamp_workspace_root__", False):

        def guarded_list_dir(self: object, frame: Any) -> object:
            try:
                path = resolve_browser_host_directory(
                    frame.path,
                    roots=roots,
                )
            except (AttributeError, BrowserHostPreflightError):
                return HostListDirResultFrame(
                    request_id=getattr(frame, "request_id", ""),
                    status="ok",
                    error="permission denied outside ephemeral workspace roots",
                )
            return original_list_dir(self, replace(frame, path=path))

        guarded_list_dir.__triple_stamp_workspace_root__ = True
        guarded_list_dir.__triple_stamp_original__ = original_list_dir
        HostProcess._handle_list_dir = guarded_list_dir

    original_create_dir = HostProcess._handle_create_dir
    if not getattr(original_create_dir, "__triple_stamp_workspace_root__", False):

        def guarded_create_dir(self: object, frame: Any) -> object:
            try:
                path = resolve_browser_host_directory(
                    frame.path,
                    roots=roots,
                )
            except (AttributeError, BrowserHostPreflightError):
                return HostCreateDirResultFrame(
                    request_id=getattr(frame, "request_id", ""),
                    status="ok",
                    error="permission denied outside ephemeral workspace roots",
                )
            return original_create_dir(self, replace(frame, path=path))

        guarded_create_dir.__triple_stamp_workspace_root__ = True
        guarded_create_dir.__triple_stamp_original__ = original_create_dir
        HostProcess._handle_create_dir = guarded_create_dir


def announce_after_browser_preflight(
    *,
    base_url: str,
    conversation_id: str,
    echo: Callable[[str], None] | None,
    announce: Callable[..., str],
    preflight: Callable[..., BrowserReadiness] = wait_for_browser_readiness,
) -> str:
    """Announce only after the no-inference browser checks succeed."""

    preflight(base_url=base_url, session_id=conversation_id)
    return announce(
        base_url=base_url,
        conversation_id=conversation_id,
        echo=echo,
    )


def install_browser_runtime_guard() -> None:
    """Install daemon env propagation and URL-announcement preflight once."""

    if os.environ.get(BROWSER_HOST_PREFLIGHT_ENV) != "1":
        return

    _forward_runner_passthrough_into_local_daemon()
    # A pre-fix browser process can still be alive while this source changes
    # during development. It has the original preflight flag but no root
    # contract; leave that already-announced run untouched until `/quit`.
    if os.environ.get(BROWSER_WORKSPACE_ROOTS_ENV):
        _install_host_workspace_root_guard()

    from omnigent import conversation_browser

    original = conversation_browser.announce_conversation_url
    if getattr(original, "__triple_stamp_browser_preflight__", False):
        return

    def checked_announce(
        *,
        base_url: str,
        conversation_id: str,
        echo: Callable[[str], None] | None = None,
    ) -> str:
        return announce_after_browser_preflight(
            base_url=base_url,
            conversation_id=conversation_id,
            echo=echo,
            announce=original,
        )

    checked_announce.__triple_stamp_browser_preflight__ = True
    checked_announce.__triple_stamp_original__ = original
    conversation_browser.announce_conversation_url = checked_announce
