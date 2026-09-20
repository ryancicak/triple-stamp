"""Pin Cursor's actual model parameters inside the isolated per-run HOME."""

from __future__ import annotations

import hashlib
import json
import os
import pty
import re
import resource
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# RE-PIN CHECKLIST
# 1. Run cursor-agent once in a scratch HOME with the new --model alias, then
#    copy the observed model/selectedModel/modelParameters shape; never guess.
# 2. Update together: agents/cursor_workhorse/config.yaml executor.model,
#    description, and MODEL LOCK; this file's four constants;
#    validate_bundle.py EXPECTED_MODELS["cursor_workhorse"] and AGENTS marker;
#    AGENTS.md model statement; config.yaml header and CURSOR HANDOFF; and
#    triple_stamp_isaac_launcher.py _STAGE_PROJECTIONS plus _conversation_model.
#    Confirm _model_rates still classifies the alias (bare vendor ids may need
#    an explicit rate class).
EXPECTED_ALIAS = "cursor-grok-4.6-xhigh"
BASE_MODEL = "grok-4.6"
DISPLAY_NAME = "Cursor Grok 4.6 Extra High"
EXPECTED_PARAMETERS = (
    {"id": "effort", "value": "xhigh"},
    {"id": "fast", "value": "false"},
)
MANAGEMENT_ARGS = {"--version", "-v", "models", "mcp", "status", "whoami", "about"}
CONFIG_PREFLIGHT_ARGS = {
    "--triple-stamp-config-preflight",
    "--triple-stamp-self-test",
}
STARTUP_PREFLIGHT_ARG = "--triple-stamp-startup-preflight"
STARTUP_ACK_TIMEOUT_SECONDS = 45.0
STARTUP_PREFLIGHT_TIMEOUT_SECONDS = 45.0
INTERACTIVE_STARTUP_ATTEMPTS = 3
_STARTUP_STATUS_FILE = "triple-stamp-startup.json"
_STARTUP_PREFLIGHT_FILE = "cursor-startup-preflight.json"
_CHAT_ID_RE = re.compile(
    r"(?<![0-9a-f])([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})(?![0-9a-f])",
    re.IGNORECASE,
)
_REDACT_PATTERNS = (
    re.compile(r"(?i)\b(authorization)(\s*[:=]\s*bearer\s+)([^\s,;]+)"),
    re.compile(
        r"(?i)\b(authorization|bearer|access[_ -]?token|refresh[_ -]?token|"
        r"api[_ -]?key|password|secret)\b(\s*[:=]\s*|\s+)([^\s,;]+)"
    ),
)
_RUNNER_ONLY_ENV = frozenset(
    {
        "OMNIGENT_CLAUDE_LAUNCHER",
        "OMNIGENT_CODEX_PATH",
        "OMNIGENT_REMOTE_AUTH_TOKEN",
        "OMNIGENT_RUNNER_ENV_PASSTHROUGH",
        "ISAAC_BIN",
        "ISAAC_DEFAULT_UCODE",
        "ISAAC_DISABLE_MAC_MANAGED_SETTINGS_UPDATE",
        "ISAAC_LAUNCH_MODE",
        "ISAAC_OMNIGENT_BIN",
        "ISAAC_OMNI_PY",
        "TRIPLE_STAMP_CODEX_BIN",
        "TRIPLE_STAMP_CODEX_MODEL",
        "TRIPLE_STAMP_OPUS_MODEL",
        "TRIPLE_STAMP_PROVIDER",
        "TRIPLE_STAMP_SUPERVISOR_MODEL",
    }
)


def fail(message: str, code: int = 78) -> int:
    print(f"triple-stamp: {message}", file=sys.stderr, flush=True)
    return code


def _cursor_home() -> Path:
    raw = os.environ.get("TRIPLE_STAMP_CURSOR_HOME")
    # Preserve the launcher's short, run-local symlink spelling. Cursor falls
    # back to /tmp/.cursor when its data path is long, which escapes the
    # per-run Seatbelt write root and makes its worker-server die with EPERM.
    return Path(os.path.abspath(raw)) if raw else Path.home()


def _cursor_env() -> dict[str, str]:
    # Provider routing and the Claude/Codex launch controls are required by
    # Omnigent's runner, not by Cursor. Scrub them here as a final boundary so
    # direct wrapper probes cannot accidentally expose runner-only state.
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in _RUNNER_ONLY_ENV
    }
    home = _cursor_home()
    cursor_root = home / ".cursor"
    env["HOME"] = str(home)
    env["CURSOR_CONFIG_DIR"] = str(cursor_root)
    env["CURSOR_DATA_DIR"] = str(cursor_root)
    return env


def _config_path() -> Path:
    return _cursor_home() / ".cursor/cli-config.json"


def _auth_path() -> Path:
    return _cursor_home() / ".cursor/auth.json"


def _parameters() -> list[dict[str, str]]:
    return [dict(value) for value in EXPECTED_PARAMETERS]


def seed_model_config() -> Path:
    """Atomically seed Cursor's exact observed model parameters."""

    path = _config_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    parameters = _parameters()
    raw.update(
        {
            "model": {
                "modelId": BASE_MODEL,
                "displayModelId": BASE_MODEL,
                "displayName": DISPLAY_NAME,
                "displayNameShort": DISPLAY_NAME,
                "aliases": [],
                "maxMode": False,
            },
            "hasChangedDefaultModel": True,
            "maxMode": False,
            "modelParameters": {BASE_MODEL: parameters},
            "selectedModel": {
                "modelId": BASE_MODEL,
                "parameters": parameters,
            },
            "modelSelectionHistory": [BASE_MODEL],
        }
    )
    fd, temporary = tempfile.mkstemp(prefix="cli-config.json.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(raw, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return path


def model_config_is_exact() -> bool:
    try:
        config = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    parameters = _parameters()
    model = config.get("model")
    selected = config.get("selectedModel")
    all_parameters = config.get("modelParameters")
    return (
        isinstance(model, dict)
        and model.get("modelId") == BASE_MODEL
        and model.get("displayName") == DISPLAY_NAME
        and isinstance(selected, dict)
        and selected.get("modelId") == BASE_MODEL
        and selected.get("parameters") == parameters
        and isinstance(all_parameters, dict)
        and all_parameters.get(BASE_MODEL) == parameters
    )


def _filtered_session_args(args: list[str]) -> list[str] | None:
    filtered: list[str] = []
    found_model = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"--model", "-m"}:
            if found_model or index + 1 >= len(args) or args[index + 1] != EXPECTED_ALIAS:
                return None
            found_model = True
            index += 2
            continue
        if arg.startswith("--model="):
            if found_model or arg.split("=", 1)[1] != EXPECTED_ALIAS:
                return None
            found_model = True
            index += 1
            continue
        filtered.append(arg)
        index += 1
    return filtered if found_model else None


def _check_private_auth() -> bool:
    path = _auth_path()
    return path.is_file() and not (stat.S_IMODE(path.stat().st_mode) & 0o077)


def _sanitize_diagnostic(text: str, *, limit: int = 4000) -> str:
    """Redact credential-shaped values from bounded child diagnostics."""

    value = text[-limit:]
    for pattern in _REDACT_PATTERNS:
        value = pattern.sub(r"\1\2[REDACTED]", value)
    return value


def _resource_limits() -> dict[str, dict[str, int | str]]:
    result: dict[str, dict[str, int | str]] = {}
    for name in ("RLIMIT_NOFILE", "RLIMIT_NPROC", "RLIMIT_AS"):
        limit = getattr(resource, name, None)
        if limit is None:
            continue
        soft, hard = resource.getrlimit(limit)
        result[name] = {
            "soft": "infinity" if soft == resource.RLIM_INFINITY else int(soft),
            "hard": "infinity" if hard == resource.RLIM_INFINITY else int(hard),
        }
    return result


def _return_code_fields(return_code: int | None) -> dict[str, object]:
    if return_code is None:
        return {"return_code": None, "exit_code": None, "signal": None}
    if return_code >= 0:
        return {"return_code": return_code, "exit_code": return_code, "signal": None}
    signum = -return_code
    try:
        signal_name = signal.Signals(signum).name
    except ValueError:
        signal_name = "UNKNOWN"
    return {
        "return_code": return_code,
        "exit_code": None,
        "signal": signum,
        "signal_name": signal_name,
    }


def _safe_runtime_diagnostics() -> dict[str, object]:
    env = _cursor_env()
    return {
        "cwd": os.path.realpath(os.getcwd()),
        "home": env["HOME"],
        "cursor_config_dir": env["CURSOR_CONFIG_DIR"],
        "cursor_data_dir": env["CURSOR_DATA_DIR"],
        "path": env.get("PATH", ""),
        "resource_limits": _resource_limits(),
    }


def _bridge_dir_from_workspace() -> Path | None:
    """Resolve Omnigent's managed bridge dir without trusting arbitrary MCP config."""

    try:
        payload = json.loads((Path.cwd() / ".cursor/mcp.json").read_text(encoding="utf-8"))
        server = payload["mcpServers"]["omnigent"]
        args = server["args"]
        index = args.index("--bridge-dir")
        bridge_dir = Path(args[index + 1]).resolve()
        run_dir = Path(os.environ["TRIPLE_STAMP_RUN_DIR"]).resolve()
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    try:
        bridge_dir.relative_to(run_dir)
    except ValueError:
        return None
    return bridge_dir


def _write_startup_status(
    bridge_dir: Path | None,
    *,
    state: str,
    pid: int,
    reason: str | None = None,
    stderr: str = "",
    return_code: int | None = None,
    cursor_session_id: str | None = None,
) -> None:
    """Atomically publish a secret-scrubbed startup handshake for the supervisor."""

    if bridge_dir is None:
        return
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "state": state,
        "pid": pid,
        "model": EXPECTED_ALIAS,
        "updated_at": time.time(),
        "runtime": _safe_runtime_diagnostics(),
        **_return_code_fields(return_code),
    }
    if reason:
        payload["reason"] = reason
    if stderr:
        payload["stderr"] = _sanitize_diagnostic(stderr)
    if cursor_session_id:
        payload["cursor_session_id"] = cursor_session_id
    path = bridge_dir / _STARTUP_STATUS_FILE
    fd, temporary = tempfile.mkstemp(prefix=f"{path.name}.", dir=bridge_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _resume_chat_id(args: list[str]) -> str | None:
    for index, arg in enumerate(args):
        if arg == "--resume" and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith("--resume="):
            return arg.split("=", 1)[1]
    return None


def _workspace_chat_root() -> Path:
    workspace = os.path.realpath(os.getcwd())
    workspace_hash = hashlib.md5(workspace.encode("utf-8")).hexdigest()
    return _cursor_home() / ".cursor/chats" / workspace_hash


def _store_mtimes(root: Path) -> dict[Path, int]:
    result: dict[Path, int] = {}
    for path in root.glob("*/store.db"):
        try:
            result[path] = path.stat().st_mtime_ns
        except OSError:
            continue
    return result


def _prompt_was_accepted(
    *,
    root: Path,
    launch_epoch_ms: int,
    baseline_mtimes: dict[Path, int],
    resume_chat_id: str | None,
) -> bool:
    """Return whether Cursor durably created or changed the active chat store."""

    candidates = [root / resume_chat_id / "store.db"] if resume_chat_id else list(
        root.glob("*/store.db")
    )
    for store in candidates:
        try:
            current_mtime = store.stat().st_mtime_ns
        except OSError:
            continue
        previous_mtime = baseline_mtimes.get(store)
        if previous_mtime is not None:
            if current_mtime > previous_mtime:
                return True
            continue
        try:
            meta = json.loads(store.with_name("meta.json").read_text(encoding="utf-8"))
            created_at_ms = int(meta.get("createdAtMs", 0))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if created_at_ms >= launch_epoch_ms - 2000:
            return True
    return False


def _accepted_chat_id(
    *,
    root: Path,
    launch_epoch_ms: int,
    baseline_mtimes: dict[Path, int],
    resume_chat_id: str | None,
) -> str | None:
    candidates = [root / resume_chat_id / "store.db"] if resume_chat_id else list(
        root.glob("*/store.db")
    )
    accepted: list[tuple[int, str]] = []
    for store in candidates:
        try:
            current_mtime = store.stat().st_mtime_ns
        except OSError:
            continue
        previous_mtime = baseline_mtimes.get(store)
        if previous_mtime is not None:
            if current_mtime > previous_mtime:
                accepted.append((current_mtime, store.parent.name))
            continue
        try:
            meta = json.loads(store.with_name("meta.json").read_text(encoding="utf-8"))
            created_at_ms = int(meta.get("createdAtMs", 0))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if created_at_ms >= launch_epoch_ms - 2000:
            accepted.append((current_mtime, store.parent.name))
    return max(accepted)[1] if accepted else None


def _terminate(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        if process.poll() is not None:
            return
        process.terminate()
    except PermissionError:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        process.wait()


def _reap_process_group(pgid: int) -> None:
    """Terminate Cursor descendants that outlive the group leader."""

    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _write_startup_preflight(
    *,
    state: str,
    pid: int,
    reason: str = "",
    stdout: str = "",
    stderr: str = "",
    return_code: int | None = None,
    chat_id: str | None = None,
    details: dict[str, object] | None = None,
) -> None:
    raw_run_dir = os.environ.get("TRIPLE_STAMP_RUN_DIR", "")
    if not raw_run_dir:
        return
    run_dir = Path(raw_run_dir).resolve()
    payload: dict[str, object] = {
        "state": state,
        "pid": pid,
        "model": EXPECTED_ALIAS,
        "paid_generation": False,
        "model_config_exact": model_config_is_exact(),
        "runtime": _safe_runtime_diagnostics(),
        "updated_at": time.time(),
        **_return_code_fields(return_code),
    }
    if reason:
        payload["reason"] = reason
    if stdout:
        payload["stdout"] = _sanitize_diagnostic(stdout)
    if stderr:
        payload["stderr"] = _sanitize_diagnostic(stderr)
    if chat_id:
        payload["chat_id_sha256"] = hashlib.sha256(chat_id.encode("utf-8")).hexdigest()
        payload["ack_kind"] = "empty-chat-id"
    if details:
        payload["details"] = details
    path = run_dir / _STARTUP_PREFLIGHT_FILE
    fd, temporary = tempfile.mkstemp(prefix=f"{path.name}.", dir=run_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _file_sizes(root: Path, pattern: str) -> dict[Path, int]:
    result: dict[Path, int] = {}
    for path in root.glob(pattern):
        try:
            result[path] = path.stat().st_size
        except OSError:
            continue
    return result


def _isolated_worker_socket(data_root: Path, logged_socket: str) -> str:
    """Prefer a logged socket, then any Unix socket under the isolated data dir."""

    candidates: list[str] = []
    if logged_socket:
        candidates.append(logged_socket)
    try:
        for path in data_root.rglob("*.sock"):
            candidates.append(str(path))
    except OSError:
        pass
    for value in candidates:
        if value and not value.startswith("/tmp/.cursor/"):
            return value
    return logged_socket


def _file_deltas(root: Path, pattern: str, baseline: dict[Path, int]) -> str:
    parts: list[str] = []
    for path in root.glob(pattern):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        parts.append(data[baseline.get(path, 0) :].decode("utf-8", errors="replace"))
    return "\n".join(parts)


def _run_interactive_startup_probe(
    cursor: str,
    chat_id: str,
) -> tuple[bool, str, int, int | None, dict[str, object]]:
    """Start the exact interactive TUI without submitting a paid prompt."""

    env = _cursor_env()
    data_root = Path(env["CURSOR_DATA_DIR"])
    debug_root = Path(env.get("TMPDIR", tempfile.gettempdir())) / (
        f"cursor-agent-logs-{os.getuid()}"
    )
    debug_baseline = _file_sizes(debug_root, "*.log")
    worker_baseline = _file_sizes(data_root / "projects", "**/worker.log")
    master, slave = pty.openpty()
    process: subprocess.Popen[bytes] | None = None
    terminal = bytearray()
    try:
        process = subprocess.Popen(
            [
                cursor,
                "--trust",
                "--model",
                EXPECTED_ALIAS,
            ],
            env=env,
            start_new_session=True,
            stdin=slave,
            stdout=slave,
            stderr=slave,
        )
        os.close(slave)
        slave = -1
        selector = selectors.DefaultSelector()
        selector.register(master, selectors.EVENT_READ)
        deadline = time.monotonic() + STARTUP_PREFLIGHT_TIMEOUT_SECONDS
        startup_seen = False
        worker_seen = False
        worker_socket = ""
        dismissed_mcp_prompt = False
        try:
            while time.monotonic() < deadline:
                for _key, _mask in selector.select(timeout=0.1):
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        chunk = b""
                    if chunk:
                        terminal.extend(chunk)
                        if len(terminal) > 16_000:
                            del terminal[:-8_000]
                screen = terminal.decode("utf-8", errors="replace")
                if (
                    not dismissed_mcp_prompt
                    and "Continue without approval" in screen
                ):
                    try:
                        os.write(master, b"c")
                    except OSError:
                        pass
                    else:
                        dismissed_mcp_prompt = True
                debug_delta = _file_deltas(debug_root, "*.log", debug_baseline)
                worker_delta = _file_deltas(
                    data_root / "projects", "**/worker.log", worker_baseline
                )
                startup_seen = "startup.metrics" in debug_delta
                socket_matches = re.findall(r"runServer socketPath=([^\s]+)", worker_delta)
                if socket_matches:
                    worker_seen = True
                    worker_socket = socket_matches[-1]
                fatal = (
                    "global.main.promiseCatch" in debug_delta
                    or (
                        "worker.sock" in debug_delta
                        and "operation not permitted" in debug_delta.lower()
                    )
                    or "cli.request.create" in debug_delta
                )
                if fatal:
                    break
                if startup_seen and worker_seen:
                    break
                if process.poll() is not None:
                    break
        finally:
            selector.close()
        debug_delta = _file_deltas(debug_root, "*.log", debug_baseline)
        worker_delta = _file_deltas(
            data_root / "projects", "**/worker.log", worker_baseline
        )
        if not worker_socket:
            socket_matches = re.findall(r"runServer socketPath=([^\s]+)", worker_delta)
            if socket_matches:
                worker_socket = socket_matches[-1]
        worker_socket = _isolated_worker_socket(data_root, worker_socket)
        worker_seen = worker_seen or bool(worker_socket)
        startup_seen = startup_seen or "startup.metrics" in debug_delta
        socket_is_isolated = bool(worker_socket) and not worker_socket.startswith(
            "/tmp/.cursor/"
        )
        fatal = (
            "global.main.promiseCatch" in debug_delta
            or (
                "worker.sock" in debug_delta
                and "operation not permitted" in debug_delta.lower()
            )
            or "cli.request.create" in debug_delta
        )
        ok = (
            process.poll() is None
            and startup_seen
            and worker_seen
            and socket_is_isolated
            and not fatal
        )
        diagnostic = _sanitize_diagnostic(
            "\n".join(
                value
                for value in (
                    terminal.decode("utf-8", errors="replace"),
                    debug_delta,
                    worker_delta,
                )
                if value
            ),
            limit=6000,
        )
        details = {
            "interactive_startup_seen": startup_seen,
            "worker_server_seen": worker_seen,
            "worker_socket_isolated": socket_is_isolated,
            "worker_socket_sha256": (
                hashlib.sha256(worker_socket.encode("utf-8")).hexdigest()
                if worker_socket
                else ""
            ),
            "paid_request_seen": "cli.request.create" in debug_delta,
        }
        return ok, diagnostic, process.pid, process.poll(), details
    except OSError as exc:
        return (
            False,
            f"interactive Cursor process launch failed: {type(exc).__name__}: {exc}",
            process.pid if process is not None else 0,
            process.poll() if process is not None else None,
            {
                "interactive_startup_seen": False,
                "worker_server_seen": False,
                "worker_socket_isolated": False,
                "paid_request_seen": False,
            },
        )
    finally:
        if slave >= 0:
            os.close(slave)
        os.close(master)
        if process is not None:
            if process.poll() is None:
                _terminate(process)
            _reap_process_group(process.pid)


def _run_startup_preflight(cursor: str) -> int:
    """Start the real CLI and require its non-inference empty-chat ack."""

    try:
        process = subprocess.Popen(
            [cursor, "create-chat"],
            env=_cursor_env(),
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        reason = f"Cursor process launch failed: {type(exc).__name__}: {exc}"
        _write_startup_preflight(state="failed", pid=0, reason=reason)
        return fail(reason, 77)

    stdout = bytearray()
    stderr = bytearray()
    selector = selectors.DefaultSelector()
    assert process.stdout is not None and process.stderr is not None
    selector.register(process.stdout, selectors.EVENT_READ, stdout)
    selector.register(process.stderr, selectors.EVENT_READ, stderr)
    deadline = time.monotonic() + STARTUP_PREFLIGHT_TIMEOUT_SECONDS
    chat_id: str | None = None
    try:
        while time.monotonic() < deadline:
            for key, _mask in selector.select(timeout=0.1):
                try:
                    chunk = os.read(key.fileobj.fileno(), 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    key.data.extend(chunk)
                else:
                    selector.unregister(key.fileobj)
            match = _CHAT_ID_RE.search(stdout.decode("utf-8", errors="replace"))
            if match:
                chat_id = match.group(1).lower()
                break
            if process.poll() is not None and not selector.get_map():
                break
    finally:
        selector.close()

    if process.poll() is None:
        _terminate(process)
    else:
        process.wait()
    _reap_process_group(process.pid)
    try:
        remaining_stdout, remaining_stderr = process.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        remaining_stdout, remaining_stderr = b"", b""
    stdout.extend(remaining_stdout or b"")
    stderr.extend(remaining_stderr or b"")
    stdout_text = stdout.decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace")

    if chat_id is None:
        match = _CHAT_ID_RE.search(stdout_text)
        chat_id = match.group(1).lower() if match else None
    startup_error = next(
        (
            marker
            for marker in (
                "operation not permitted",
                "resource temporarily unavailable",
                "too many open files",
                "cannot allocate memory",
            )
            if marker in stderr_text.lower()
        ),
        "",
    )
    if chat_id is None or startup_error or not model_config_is_exact():
        reason = (
            "Cursor startup preflight failed before empty-chat acknowledgement"
            + (f": {startup_error}" if startup_error else "")
        )
        _write_startup_preflight(
            state="failed",
            pid=process.pid,
            reason=reason,
            stdout=stdout_text,
            stderr=stderr_text,
            return_code=process.returncode,
        )
        return fail(reason, 77)
    interactive_ok = False
    interactive_diagnostic = ""
    interactive_pid = 0
    interactive_code: int | None = None
    details: dict[str, object] = {}
    for attempt in range(INTERACTIVE_STARTUP_ATTEMPTS):
        (
            interactive_ok,
            interactive_diagnostic,
            interactive_pid,
            interactive_code,
            details,
        ) = _run_interactive_startup_probe(cursor, chat_id)
        if interactive_ok:
            break
        fatal_probe = bool(details.get("paid_request_seen")) or (
            "operation not permitted" in interactive_diagnostic.lower()
            and "worker.sock" in interactive_diagnostic
        )
        if fatal_probe:
            break
        if attempt + 1 < INTERACTIVE_STARTUP_ATTEMPTS:
            time.sleep(1.0)
    if not interactive_ok:
        reason = "Cursor interactive startup preflight failed before prompt-loop readiness"
        _write_startup_preflight(
            state="failed",
            pid=interactive_pid,
            reason=reason,
            stderr=interactive_diagnostic,
            return_code=interactive_code,
            chat_id=chat_id,
            details=details,
        )
        return fail(reason, 77)
    _write_startup_preflight(
        state="passed",
        pid=interactive_pid,
        return_code=interactive_code,
        chat_id=chat_id,
        details=details,
    )
    print("Cursor exact startup/prompt acknowledgement preflight: PASS")
    return 0


def _run_cursor(cursor: str, args: list[str]) -> int:
    bridge_dir = _bridge_dir_from_workspace()
    chat_root = _workspace_chat_root()
    baseline_mtimes = _store_mtimes(chat_root)
    launch_epoch_ms = int(time.time() * 1000)
    resume_chat_id = _resume_chat_id(args)
    process = subprocess.Popen(
        [cursor, "--trust", *args],
        env=_cursor_env(),
        start_new_session=True,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    stderr_parts: list[str] = []

    def relay_stderr() -> None:
        assert process.stderr is not None
        for chunk in process.stderr:
            sys.stderr.write(chunk)
            sys.stderr.flush()
            stderr_parts.append(chunk)
            if sum(map(len, stderr_parts)) > 8000:
                del stderr_parts[:-8]

    stderr_thread = threading.Thread(target=relay_stderr, name="cursor-stderr-relay", daemon=True)
    stderr_thread.start()
    _write_startup_status(bridge_dir, state="starting", pid=process.pid)

    def forward(signum: int, _frame: object) -> None:
        try:
            os.killpg(process.pid, signum)
        except (ProcessLookupError, PermissionError):
            process.send_signal(signum)

    old_handlers = {
        signum: signal.signal(signum, forward)
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }
    drift_count = 0
    startup_deadline = time.monotonic() + STARTUP_ACK_TIMEOUT_SECONDS
    startup_ready = False
    try:
        # Polling is confined to the bounded startup handshake. Once Cursor has
        # durably changed its chat store, block on the child process itself;
        # long web work causes no status/config polling traffic.
        while process.poll() is None:
            if model_config_is_exact():
                drift_count = 0
            else:
                drift_count += 1
                if drift_count >= 4:
                    _terminate(process)
                    stderr_thread.join(timeout=1)
                    reason = (
                        f"Cursor model drifted from {DISPLAY_NAME} "
                        "before startup acknowledgement"
                    )
                    _write_startup_status(
                        bridge_dir,
                        state="failed",
                        pid=process.pid,
                        reason=reason,
                        stderr="".join(stderr_parts),
                    )
                    return fail(
                        f"Cursor model drifted from {DISPLAY_NAME}; "
                        "the worker was stopped"
                    )
            if not startup_ready and _prompt_was_accepted(
                root=chat_root,
                launch_epoch_ms=launch_epoch_ms,
                baseline_mtimes=baseline_mtimes,
                resume_chat_id=resume_chat_id,
            ):
                startup_ready = True
                _write_startup_status(
                    bridge_dir,
                    state="ready",
                    pid=process.pid,
                    cursor_session_id=_accepted_chat_id(
                        root=chat_root,
                        launch_epoch_ms=launch_epoch_ms,
                        baseline_mtimes=baseline_mtimes,
                        resume_chat_id=resume_chat_id,
                    ),
                )
                break
            if not startup_ready and time.monotonic() >= startup_deadline:
                _terminate(process)
                stderr_thread.join(timeout=1)
                reason = (
                    f"Cursor stayed alive but did not acknowledge the injected prompt "
                    f"within {STARTUP_ACK_TIMEOUT_SECONDS:.0f}s"
                )
                _write_startup_status(
                    bridge_dir,
                    state="failed",
                    pid=process.pid,
                    reason=reason,
                    stderr="".join(stderr_parts),
                    return_code=process.returncode,
                )
                return fail(
                    reason
                    + "; the cursor-native bridge did not begin the worker turn",
                    70,
                )
            time.sleep(0.25)
        if startup_ready and process.poll() is None:
            return_code = process.wait()
        else:
            return_code = int(process.returncode or 0)
        stderr_thread.join(timeout=1)
        if not startup_ready:
            reason = f"Cursor exited before startup acknowledgement (exit {return_code})"
            _write_startup_status(
                bridge_dir,
                state="failed",
                pid=process.pid,
                reason=reason,
                stderr="".join(stderr_parts),
                return_code=return_code,
            )
            return fail(reason, return_code or 70)
        _write_startup_status(
            bridge_dir,
            state="exited",
            pid=process.pid,
            reason=f"Cursor exited after startup acknowledgement (exit {return_code})",
            stderr="".join(stderr_parts),
            return_code=return_code,
        )
        return return_code
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
        # cursor-agent can leave MCP/plugin descendants after its leader exits.
        # The dedicated process group is owned by this wrapper, so reap it.
        _reap_process_group(process.pid)


def main() -> int:
    cursor = os.environ.get("CURSOR_AGENT_BIN", "")
    if not cursor or not os.path.isabs(cursor) or not os.access(cursor, os.X_OK):
        return fail("verified Cursor executable is unavailable", 77)

    args = sys.argv[1:]
    if args and args[0] in MANAGEMENT_ARGS:
        return subprocess.call([cursor, *args], env=_cursor_env())
    if os.environ.get("AGENT_CLI_CREDENTIAL_STORE") != "file" or not _check_private_auth():
        return fail("private refreshable Cursor credential store is unavailable", 77)
    if len(args) == 1 and args[0] in CONFIG_PREFLIGHT_ARGS:
        seed_model_config()
        if not model_config_is_exact():
            return fail(f"Cursor {DISPLAY_NAME} config preflight failed")
        print(f"Cursor {DISPLAY_NAME} config preflight: PASS")
        return 0
    if len(args) == 1 and args[0] == STARTUP_PREFLIGHT_ARG:
        seed_model_config()
        if not model_config_is_exact():
            return fail(f"Cursor {DISPLAY_NAME} config preflight failed")
        return _run_startup_preflight(cursor)

    filtered = _filtered_session_args(args)
    if filtered is None:
        return fail(
            f"Cursor session must contain exactly --model {EXPECTED_ALIAS}; "
            "refusing an unpinned or overridden launch"
        )
    seed_model_config()
    if not model_config_is_exact():
        return fail(f"could not seed Cursor's exact {DISPLAY_NAME} configuration")
    return _run_cursor(cursor, filtered)


if __name__ == "__main__":
    raise SystemExit(main())
