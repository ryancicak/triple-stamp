"""Fail-closed lifecycle controller for the triple-stamp bundle."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NoReturn, Self

import yaml

EXIT_AUTH = 77
EXIT_CONFIG = 78
EXIT_BUSY = 75
EXIT_PIPELINE = 70
MIN_AUTH_SECONDS = 3600
MIN_REMOTE_TOKEN_SECONDS = 300
STOP_GRACE_SECONDS = 12.0
SELF_TEST_FLAGS = {"--self-test", "--dry-run"}
BROWSER_HOST_PREFLIGHT_ENV = "TRIPLE_STAMP_BROWSER_HOST_PREFLIGHT"
BROWSER_WORKSPACE_ROOTS_ENV = "TRIPLE_STAMP_BROWSER_WORKSPACE_ROOTS"
BROWSER_REQUIRED_PATH_ENV = "TRIPLE_STAMP_BROWSER_REQUIRED_PATH"
OPUS_READINESS_SMOKE_ENV = "TRIPLE_STAMP_RUN_OPUS_READINESS_SMOKE"
PROFILE_MATRIX_ENV = "TRIPLE_STAMP_PROFILE_MATRIX"
MAX_CYCLES_ENV = "TRIPLE_STAMP_MAX_CYCLES"
VOICE_PROFILE_ENV = "TRIPLE_STAMP_VOICE_PROFILE"
# Databricks-profile control plane. Only read when TRIPLE_STAMP_PROVIDER is
# `databricks`; the default `direct` profile never touches it.
WORKSPACE_PROFILE_ENV = "TRIPLE_STAMP_DATABRICKS_PROFILE"
WORKSPACE_HOST_ENV = "TRIPLE_STAMP_DATABRICKS_HOST"


def _workspace_profile() -> str:
    return os.environ.get(WORKSPACE_PROFILE_ENV, "").strip()


def _workspace_host() -> str:
    """Control-plane host for the databricks profile, without a scheme."""

    raw = os.environ.get(WORKSPACE_HOST_ENV, "").strip()
    return raw.removeprefix("https://").removeprefix("http://").rstrip("/")


def _resolve_voice_profile() -> Path | None:
    """Return the configured voice profile, or ``None`` when there is none.

    Voice rendering is optional. Set `TRIPLE_STAMP_VOICE_PROFILE` to a readable
    markdown file to have Codex render the final answer in that voice; leave it
    unset and the pipeline ships a plain, unstyled answer instead. Every voice
    check downstream is conditioned on this being non-None, so an unset profile
    removes the requirement rather than failing the run.
    """

    configured = os.environ.get(VOICE_PROFILE_ENV, "").strip()
    return Path(configured).expanduser() if configured else None


VOICE_PROFILE = _resolve_voice_profile()
PROVIDER_ENV = "TRIPLE_STAMP_PROVIDER"
PROVIDERS = frozenset({"direct", "databricks"})
MODEL_ENV = {
    "supervisor": "TRIPLE_STAMP_SUPERVISOR_MODEL",
    "opus_auditor": "TRIPLE_STAMP_OPUS_MODEL",
    "codex_judge": "TRIPLE_STAMP_CODEX_MODEL",
}
CLAUDE_MANAGED_SETTINGS = (
    Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
)
SUPERVISOR_PROVIDER = "isaac-databricks-ai-gateway"
SUPERVISOR_AUTH_COMMAND = (
    "jq -r '.access_token' ~/.databricks/model-serving-token.json"
)
PLUGIN_DISTRIBUTION = "triple-stamp-isaac-launcher"
PLUGIN_VERSION = "0.4.0"
PLUGIN_ENTRY_NAME = "isaac"
PLUGIN_ENTRY_VALUE = "triple_stamp_isaac_launcher:IsaacClaudeLauncher"
_PIPELINE_OUTPUTS = (
    "budget-state.json",
    "codex-punch-lists.jsonl",
    "failure-attestation.json",
    "routing-collections.jsonl",
    "routing-dispatches.jsonl",
    "stamp-attestation.json",
    "stamped-answer.bin",
    "supervisor-continuations.jsonl",
    "supervisor-tool-calls.jsonl",
    "terminal-failure.txt",
)


def _max_cycles(value: object | None = None) -> int:
    """Validate the default two-cycle or explicit four-cycle policy."""

    raw = os.environ.get(MAX_CYCLES_ENV, "2") if value is None else value
    text = str(raw)
    if text not in {"2", "4"}:
        _die(f"{MAX_CYCLES_ENV} must be exactly 2 or 4")
    cycles = int(text)
    return cycles


def _route_call_cap(max_cycles: object | None = None) -> int:
    """Match the plugin's exact scoped formula: cycles * 19 * 2 + 8."""

    return _max_cycles(max_cycles) * 19 * 2 + 8


class LaunchError(RuntimeError):
    """A user-facing, pre-model launch failure."""

    def __init__(self, message: str, code: int = EXIT_CONFIG) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Toolchain:
    """Absolute executable paths trusted by the launch."""

    isaac: Path | None
    dbcert: Path | None
    databricks: Path | None
    uv: Path
    omnigent: Path
    omnigent_python: Path
    cursor_agent: Path
    sandbox_exec: Path
    security: Path
    claude: Path = Path("/nonexistent/claude")
    codex: Path = Path("/nonexistent/codex")


def _provider(value: str | None = None) -> str:
    selected = (value if value is not None else os.environ.get(PROVIDER_ENV, "direct")).strip()
    if selected not in PROVIDERS:
        _die(
            f"{PROVIDER_ENV} must be one of direct or databricks; got {selected!r}"
        )
    return selected


def _require_workspace() -> tuple[str, str]:
    """Return (host, profile) for the databricks control plane, or die.

    Checked here rather than in `_provider` because that function also resolves
    which model mapping a profile uses, with no control plane involved. The
    workspace used to be a hardcoded internal hostname; fail with the fix rather
    than building a request against an empty host.
    """

    host, profile = _workspace_host(), _workspace_profile()
    if not host or not profile:
        _die(
            f"{PROVIDER_ENV}=databricks needs both {WORKSPACE_HOST_ENV} and "
            f"{WORKSPACE_PROFILE_ENV}.\n"
            f"  export {WORKSPACE_HOST_ENV}=your-workspace.cloud.databricks.com\n"
            f"  export {WORKSPACE_PROFILE_ENV}=your-cli-profile\n"
            "The default direct profile needs neither.",
            EXIT_CONFIG,
        )
    return host, profile


def _model_config(root: Path) -> dict[str, object]:
    try:
        payload = yaml.safe_load(
            (root / ".omnigent/provider-models.yaml").read_text(encoding="utf-8")
        )
    except (OSError, TypeError, yaml.YAMLError) as exc:
        _die(f"provider model mapping is unavailable: {exc}")
    if not isinstance(payload, dict):
        _die("provider model mapping is not a mapping")
    namespaces = payload.get("namespaces")
    profiles = payload.get("profiles")
    if (
        not isinstance(namespaces, dict)
        or set(namespaces) != {"public_anthropic", "databricks_gateway"}
        or not isinstance(profiles, dict)
        or set(profiles) != PROVIDERS
    ):
        _die("provider model mapping has invalid namespace/profile sections")
    for namespace in ("public_anthropic", "databricks_gateway"):
        values = namespaces.get(namespace)
        if (
            not isinstance(values, dict)
            or set(values) != {"supervisor", "opus_auditor"}
            or not all(isinstance(value, str) and value for value in values.values())
        ):
            _die(f"provider model mapping is invalid for {namespace}")
    for profile in PROVIDERS:
        values = profiles.get(profile)
        if not isinstance(values, dict) or any(
            key not in MODEL_ENV or not isinstance(value, str) or not value
            for key, value in values.items()
        ):
            _die(f"provider model mapping is invalid for {profile}")
    return payload


def _managed_claude_environment(
    paths: tuple[Path, ...] = CLAUDE_MANAGED_SETTINGS,
) -> dict[str, str]:
    """Read only namespace-selection signals from system managed settings."""

    merged: dict[str, str] = {}
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            values = payload.get("env", {})
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(values, dict):
            continue
        for name in ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_GATEWAY"):
            value = values.get(name)
            if isinstance(value, str):
                merged[name] = value
    return merged


def _detect_claude_namespace(
    environment: dict[str, str] | None = None,
    managed_environment: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Detect the model namespace without exposing endpoint or credential data."""

    ambient = os.environ if environment is None else environment
    managed = (
        _managed_claude_environment()
        if managed_environment is None
        else managed_environment
    )
    for source, values in (("environment", ambient), ("managed settings", managed)):
        if (
            "/ai-gateway/" in values.get("ANTHROPIC_BASE_URL", "")
            or values.get("CLAUDE_CODE_USE_GATEWAY") == "1"
        ):
            return "databricks_gateway", source
    return "public_anthropic", "no gateway routing signal"


def _resolve_models(
    root: Path,
    provider: str,
    *,
    environment: dict[str, str] | None = None,
    managed_environment: dict[str, str] | None = None,
) -> tuple[dict[str, str], str, str]:
    """Resolve explicit env, configured profile mapping, then detected defaults."""

    selected_provider = _provider(provider)
    ambient = os.environ if environment is None else environment
    config = _model_config(root)
    profiles = config["profiles"]
    namespaces = config["namespaces"]
    assert isinstance(profiles, dict) and isinstance(namespaces, dict)
    configured = profiles[selected_provider]
    assert isinstance(configured, dict)
    namespace, detection_reason = _detect_claude_namespace(
        ambient, managed_environment
    )
    detected = namespaces[namespace]
    assert isinstance(detected, dict)
    resolved: dict[str, str] = {}
    for role, env_name in MODEL_ENV.items():
        explicit = ambient.get(env_name, "").strip()
        configured_value = configured.get(role)
        detected_value = (
            "gpt-5.6-sol" if role == "codex_judge" else detected.get(role)
        )
        value = explicit or configured_value or detected_value
        if not isinstance(value, str) or not value:
            _die(f"no model resolved for {role} in {selected_provider}")
        resolved[role] = value
    claude_sources = {
        "explicit environment"
        if ambient.get(MODEL_ENV[role], "").strip()
        else "configured mapping"
        if configured.get(role)
        else f"detected {namespace}"
        for role in ("supervisor", "opus_auditor")
    }
    reason = (
        next(iter(claude_sources))
        if len(claude_sources) == 1
        else "mixed precedence"
    )
    if reason.startswith("detected"):
        reason += f" from {detection_reason}"
    claude_values = (
        resolved["supervisor"],
        resolved["opus_auditor"],
    )
    if all(value.startswith("system.ai.") for value in claude_values):
        chosen_namespace = "databricks_gateway"
    elif all(not value.startswith("system.ai.") for value in claude_values):
        chosen_namespace = "public_anthropic"
    else:
        chosen_namespace = "mixed_or_custom"
    return resolved, chosen_namespace, reason


def _provider_models(root: Path, provider: str) -> dict[str, str]:
    return _resolve_models(root, provider)[0]


def _explicit_model_overrides(
    models: dict[str, str],
    environment: dict[str, str] | None = None,
) -> dict[str, str]:
    ambient = os.environ if environment is None else environment
    resolved = dict(models)
    for role, env_name in MODEL_ENV.items():
        if explicit := ambient.get(env_name, "").strip():
            resolved[role] = explicit
    return resolved


def _resolved_claude_namespace(models: dict[str, str]) -> str:
    values = (models["supervisor"], models["opus_auditor"])
    if all(value.startswith("system.ai.") for value in values):
        return "databricks_gateway"
    if all(not value.startswith("system.ai.") for value in values):
        return "public_anthropic"
    return "mixed_or_custom"


def _eprint(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _die(message: str, code: int = EXIT_CONFIG) -> NoReturn:
    raise LaunchError(f"triple-stamp: {message}", code)


def _check_safe_path(path: Path, label: str) -> Path:
    value = str(path)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        _die(f"{label} contains a control character: {value!r}")
    return path


def _require_executable(path: Path, label: str) -> Path:
    _check_safe_path(path, label)
    if not path.is_file() or not os.access(path, os.X_OK):
        _die(f"required {label} executable not found: {path}", 127)
    # Preserve virtual-environment interpreter paths. Resolving the venv's
    # ``bin/python`` symlink to its base interpreter drops the venv site-packages
    # under ``-I`` and can silently select a different runtime.
    return path.absolute()


def _first_executable(label: str, candidates: list[Path]) -> Path:
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return _require_executable(candidate, label)
    _die(f"required {label} executable not found; checked: {', '.join(map(str, candidates))}", 127)


def _path_candidates(name: str, canonical: list[Path]) -> list[Path]:
    """Use supported canonical locations before falling back to caller PATH."""

    candidates = list(canonical)
    discovered = shutil.which(name, path=os.environ.get("PATH", ""))
    if discovered:
        path = Path(discovered).expanduser()
        candidates.append(path if path.is_absolute() else Path.cwd() / path)
    return list(dict.fromkeys(candidate.absolute() for candidate in candidates))


def _cli_install_prefix(executable: Path) -> Path:
    """Return the narrow read-only install root needed by a resolved CLI."""

    path = executable.absolute()
    candidates = tuple(dict.fromkeys((path, path.resolve(strict=False))))
    for candidate in candidates:
        parts = candidate.parts
        for brew_root in (Path("/opt/homebrew"), Path("/usr/local")):
            try:
                relative = candidate.relative_to(brew_root)
            except ValueError:
                continue
            relative_parts = relative.parts
            if (
                len(relative_parts) >= 4
                and relative_parts[0] == "Cellar"
            ):
                return brew_root.joinpath(*relative_parts[:3])
            return brew_root

        for marker in (".volta", ".npm-global"):
            if marker in parts:
                return Path(*parts[: parts.index(marker) + 1])

        if ".nvm" in parts:
            marker = parts.index(".nvm")
            tail = parts[marker + 1 :]
            if (
                len(tail) >= 3
                and tail[0] == "versions"
                and tail[1] == "node"
            ):
                return Path(*parts[: marker + 4])
            return Path(*parts[: marker + 1])

        if "node-versions" in parts:
            marker = parts.index("node-versions")
            if len(parts) > marker + 2 and parts[marker + 2] == "installation":
                return Path(*parts[: marker + 3])

    return path.parent


def _resolve_toolchain(real_home: Path, provider: str | None = None) -> Toolchain:
    """Resolve required CLIs from PATH and supported macOS locations."""

    selected = _provider(provider)
    invoked_python = Path(sys.executable).absolute()
    omnigent_python = _first_executable(
        "Omnigent Python",
        [
            invoked_python,
            real_home / ".local/share/uv/tools/omnigent/bin/python",
            real_home / ".local/share/uv/tools/omnigent/bin/python3",
        ],
    )
    return Toolchain(
        isaac=(
            _require_executable(Path("/usr/local/bin/isaac"), "Isaac")
            if selected == "databricks"
            else None
        ),
        dbcert=(
            _require_executable(Path("/usr/local/bin/dbcert"), "dbcert")
            if selected == "databricks"
            else None
        ),
        databricks=(
            _first_executable(
                "Databricks CLI",
                [
                    Path("/opt/homebrew/bin/databricks"),
                    Path("/usr/local/bin/databricks"),
                    real_home / ".local/bin/databricks",
                ],
            )
            if selected == "databricks"
            else None
        ),
        uv=_first_executable(
            "uv",
            [Path("/opt/homebrew/bin/uv"), Path("/usr/local/bin/uv"), real_home / ".local/bin/uv"],
        ),
        omnigent=_first_executable(
            "Omnigent",
            [
                invoked_python.parent / "omnigent",
                *_path_candidates(
                    "omnigent",
                    [real_home / ".local/bin/omnigent"],
                ),
            ],
        ),
        omnigent_python=omnigent_python,
        cursor_agent=_first_executable(
            "Cursor Agent",
            _path_candidates(
                "cursor-agent",
                [
                    real_home / ".local/bin/cursor-agent",
                    real_home / ".cursor/bin/cursor-agent",
                    Path("/opt/homebrew/bin/cursor-agent"),
                    Path("/usr/local/bin/cursor-agent"),
                ],
            ),
        ),
        sandbox_exec=_require_executable(Path("/usr/bin/sandbox-exec"), "sandbox-exec"),
        security=_require_executable(Path("/usr/bin/security"), "macOS security"),
        claude=_first_executable(
            "Claude",
            _path_candidates(
                "claude",
                [
                    real_home / ".local/bin/claude",
                    Path("/opt/homebrew/bin/claude"),
                    Path("/usr/local/bin/claude"),
                ],
            ),
        ),
        codex=_first_executable(
            "Codex",
            _path_candidates(
                "codex",
                [
                    Path("/opt/homebrew/bin/codex"),
                    Path("/usr/local/bin/codex"),
                    real_home / ".local/bin/codex",
                ],
            ),
        ),
    )


def _run(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            env=env,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        _die(f"timed out running {argv[0]} after {timeout:g}s")
    except OSError as exc:
        _die(f"could not run {argv[0]}: {exc}", 127)


def _host_env(real_home: Path) -> dict[str, str]:
    """Keep host preflight behavior while removing unrelated secret variables."""

    env = {
        "HOME": str(real_home),
        "USER": os.environ.get("USER", real_home.name),
        "LOGNAME": os.environ.get("LOGNAME", real_home.name),
        "SHELL": os.environ.get("SHELL", "/bin/zsh"),
        "PATH": (
            f"{real_home}/.local/bin:/usr/local/bin:/opt/homebrew/bin:"
            "/usr/bin:/bin:/usr/sbin:/sbin"
        ),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    for name in (
        "LC_ALL",
        "LC_CTYPE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "https_proxy",
        "http_proxy",
        "no_proxy",
        "UV_DEFAULT_INDEX",
        "UV_INDEX",
        "UV_INDEX_URL",
        "UV_EXTRA_INDEX_URL",
        "UV_KEYRING_PROVIDER",
        "UV_NATIVE_TLS",
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
    ):
        if value := os.environ.get(name):
            env[name] = value
    return env


def _validate_cli_args(args: list[str]) -> tuple[list[str], bool]:
    """Allow only launch-shape flags that cannot replace the bundle or its pins."""

    if args == ["--help"] or args == ["-h"]:
        print(
            "Usage: ./triple-stamp               start the browser UI and\n"
            "                                    interactive terminal\n"
            "       ./triple-stamp --self-test   check the install\n\n"
            "Environment (all optional):\n"
            "  TRIPLE_STAMP_VOICE_PROFILE   markdown file to render the answer\n"
            "                               in a specific voice. Unset means a\n"
            "                               plain answer, which is the default.\n"
            "  TRIPLE_STAMP_MAX_CYCLES      2 (default) or 4 for a deeper bound\n"
            "  TRIPLE_STAMP_PROVIDER        direct (default) or databricks\n\n"
            "The launcher intentionally rejects model, harness, server, auth-profile, "
            "resume, and system-prompt overrides."
        )
        raise SystemExit(0)

    cleaned: list[str] = []
    self_test = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in SELF_TEST_FLAGS:
            self_test = True
            index += 1
            continue
        if arg in {"--no-session", "--debug-events", "--log", "--no-log"}:
            cleaned.append(arg)
            index += 1
            continue
        if arg in {"-q", "-p", "--prompt"} or arg.startswith("--prompt="):
            _die(
                "one-shot mode is not a supported Triple-stamp surface; "
                "run ./triple-stamp and ask in the browser or interactive "
                "terminal prompt",
                64,
            )
        _die(
            f"unsupported argument {arg!r}; this launcher rejects options that could "
            "change the bundle, model, harness, server, auth profile, or isolation",
            64,
        )
    if self_test and cleaned:
        _die("--self-test cannot be combined with run arguments", 64)
    return cleaned, self_test


def _prompt_mode(args: list[str]) -> bool:
    """Recognize every internal Omnigent one-shot argument shape."""

    return any(
        arg in {"-p", "--prompt"} or arg.startswith("--prompt=")
        for arg in args
    )


def _browser_launch_args(args: list[str]) -> tuple[list[str], bool, bool]:
    """Select Omnigent's host-backed architecture for interactive browser runs.

    The outer launcher already gives every invocation a private
    ``OMNIGENT_DATA_DIR`` that is deleted on exit.  Passing ``--no-session`` to
    Omnigent as well selects its legacy command-scoped server plus direct runner,
    which deliberately has no host daemon.  That topology can drive the
    terminal REPL, but the root Web UI cannot create a session: ``GET
    /v1/hosts`` is empty.

    For interactive launches, remove only that redundant inner flag so
    Omnigent uses its supported local server + host daemon path.  The run stays
    ephemeral because the outer private data directory and cleanup own its
    lifecycle.  Headless ``-p`` keeps the native ``--no-session`` behavior.

    :param args: Validated Omnigent run arguments.
    :returns: ``(runtime_args, browser_mode, translated_no_session)``.
    """

    prompt_mode = _prompt_mode(args)
    browser_mode = not prompt_mode and not any(arg in SELF_TEST_FLAGS for arg in args)
    translated_no_session = browser_mode and "--no-session" in args
    runtime_args = (
        [arg for arg in args if arg != "--no-session"]
        if translated_no_session
        else list(args)
    )
    return runtime_args, browser_mode, translated_no_session


def _configure_browser_runtime(runtime_env: dict[str, str], root: Path) -> None:
    """Bind browser host discovery to the project without changing HOME."""

    canonical_root = root.resolve()
    runtime_env[BROWSER_HOST_PREFLIGHT_ENV] = "1"
    runtime_env[BROWSER_WORKSPACE_ROOTS_ENV] = json.dumps([str(canonical_root)])
    runtime_env[BROWSER_REQUIRED_PATH_ENV] = str(canonical_root)
    runtime_env["PWD"] = str(canonical_root)


def _validate_versions(
    tools: Toolchain,
    host_env: dict[str, str],
    provider: str,
) -> None:
    # Accept the pinned stable line (0.12.x) or the released native line
    # (0.14.x) only when every launcher/runtime-required surface resolves.
    # The compatibility module owns the version policy, the 0.14 module
    # relocation map, and the capability probe; it fails closed otherwise.
    compat_module = (
        Path(__file__).resolve().parent
        / "runtime-python"
        / "triple_stamp_omnigent_compat.py"
    )
    version_probe = _run(
        [str(tools.omnigent_python), "-I", str(compat_module), "--probe"],
        env=host_env,
    )
    try:
        status = json.loads(version_probe.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError, json.JSONDecodeError):
        status = {}
    if (
        version_probe.returncode
        or not isinstance(status, dict)
        or not status.get("omnigent_version")
    ):
        _die(
            "could not determine the Omnigent runtime version; got "
            f"{version_probe.stdout.strip() or version_probe.stderr.strip() or 'unknown'}",
            127,
        )
    omnigent_version = str(status.get("omnigent_version"))
    if not status.get("supported"):
        _die(
            "the stable runtime must be Omnigent 0.12.x or 0.14.x; "
            f"got {omnigent_version}",
            127,
        )
    if not status.get("surfaces_ok"):
        missing = ", ".join(status.get("missing_surfaces") or []) or "unknown"
        _die(
            f"Omnigent {omnigent_version} is missing launcher-required "
            f"surfaces: {missing}",
            127,
        )
    sdk_check = _run(
        [
            str(tools.omnigent_python),
            "-I",
            "-c",
            "from importlib.metadata import version; print(version('claude-agent-sdk'))",
        ],
        env=host_env,
    )
    if sdk_check.returncode or sdk_check.stdout.strip() != "0.2.152":
        _die(
            "the supervisor runtime must use claude-agent-sdk 0.2.152; "
            f"got {sdk_check.stdout.strip() or sdk_check.stderr.strip() or 'unknown'}",
            127,
        )

    if provider == "databricks":
        assert tools.isaac is not None
        isaac_version = _run([str(tools.isaac), "version"], env=host_env, timeout=30)
        isaac_version_text = isaac_version.stdout + isaac_version.stderr
        if isaac_version.returncode or "Version: 2." not in isaac_version_text:
            _die(
                "Isaac 2.x is required; `isaac version` did not report a 2.x release",
                127,
            )


def _omnigent_runtime_version(stable_python: str, env: dict[str, str]) -> str:
    """Return the installed Omnigent version for the self-test summary line.

    Reports the actual runtime version (0.12.x or 0.14.x) rather than a
    hardcoded string. Falls back to ``"unknown"`` if the probe fails.
    """

    probe = _run(
        [
            str(stable_python),
            "-I",
            "-c",
            "from importlib.metadata import version; print(version('omnigent'))",
        ],
        env=env,
    )
    version = probe.stdout.strip()
    return version if not probe.returncode and version else "unknown"


def _ensure_claude_agent_sdk_pin(
    tools: Toolchain,
    host_env: dict[str, str],
) -> None:
    """Repair uv's newer transitive SDK selection before strict validation."""

    check_argv = [
        str(tools.omnigent_python),
        "-I",
        "-c",
        "from importlib.metadata import version; print(version('claude-agent-sdk'))",
    ]
    check = _run(check_argv, env=host_env)
    if check.returncode == 0 and check.stdout.strip() == "0.2.152":
        return
    with _PluginInstallLock(tools.omnigent_python):
        # Another launcher using this managed interpreter may have repaired it
        # while this process waited for the shared installation lock.
        check = _run(check_argv, env=host_env)
        if check.returncode == 0 and check.stdout.strip() == "0.2.152":
            return
        _eprint(
            "triple-stamp: pinning claude-agent-sdk 0.2.152 in the "
            "Omnigent tool environment..."
        )
        install = _run(
            [
                str(tools.uv),
                "pip",
                "install",
                "--python",
                str(tools.omnigent_python),
                "claude-agent-sdk==0.2.152",
            ],
            env=host_env,
            timeout=180,
        )
        if install.returncode:
            detail = _sanitize_plugin_output(
                install.stderr.strip()
                or install.stdout.strip()
                or "no installer output"
            )
            _die(
                "could not pin claude-agent-sdk 0.2.152 in the Omnigent tool "
                f"environment (uv exited {install.returncode}): {detail}\nRun: "
                f"{shlex.quote(str(tools.uv))} pip install --python "
                f"{shlex.quote(str(tools.omnigent_python))} "
                "claude-agent-sdk==0.2.152",
                127,
            )


def _validate_voice_profile(path: Path | None) -> str:
    """Validate the configured voice profile, if the operator configured one.

    Returns the profile's SHA-256, or "" when voice rendering is off. A
    configured profile is still validated strictly: a run that asks for a voice
    and silently ships an unstyled answer would be worse than failing.
    """

    if path is None:
        return ""
    _check_safe_path(path, "voice profile")
    if path.is_symlink() or not path.is_file():
        _die(
            f"{VOICE_PROFILE_ENV} points at a missing or non-regular file: {path}"
            "\nUnset it to ship answers without voice rendering."
        )
    try:
        data = path.read_bytes()
        data.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _die(f"voice profile is unreadable or not UTF-8: {exc}")
    if not data.strip():
        _die(f"voice profile is empty: {path}")
    return hashlib.sha256(data).hexdigest()


def _ensure_isaac_runtime(tools: Toolchain, host_env: dict[str, str], real_home: Path) -> Path:
    assert tools.isaac is not None
    managed_python = real_home / ".cache/isaac/omni/bin/python"
    if not managed_python.is_file() or not os.access(managed_python, os.X_OK):
        _eprint("triple-stamp: bootstrapping Isaac's managed Omnigent integration...")
        bootstrap_env = dict(host_env)
        bootstrap_env["ISAAC_OMNIGENT_BIN"] = str(tools.omnigent)
        result = _run(
            [str(tools.isaac), "omni", "--", "run", "--help"],
            env=bootstrap_env,
            timeout=180,
        )
        if result.returncode:
            _die(f"Isaac Omnigent bootstrap failed: {result.stderr.strip()}", 127)
    return _require_executable(managed_python, "Isaac managed Python")


_PLUGIN_CHECK = r"""
import json
import os
from importlib.metadata import entry_points
from pathlib import Path

expected = Path(os.environ["TRIPLE_STAMP_PLUGIN_SOURCE"]).resolve()
expected_version = os.environ["TRIPLE_STAMP_PLUGIN_VERSION"]
expected_distribution = os.environ["TRIPLE_STAMP_PLUGIN_DISTRIBUTION"]
expected_value = os.environ["TRIPLE_STAMP_PLUGIN_VALUE"]
matches = [
    entry
    for entry in entry_points(group="omnigent.claude_launcher")
    if entry.name == os.environ["TRIPLE_STAMP_PLUGIN_ENTRY"]
]
entries = []
for entry in matches:
    distribution = getattr(entry, "dist", None)
    entries.append(
        {
            "name": entry.name,
            "value": entry.value,
            "distribution": (
                distribution.metadata.get("Name") if distribution is not None else None
            ),
            "version": distribution.version if distribution is not None else None,
            "metadata_path": str(getattr(distribution, "_path", "")),
        }
    )
module_file = None
import_error = None
load_error = None
try:
    import triple_stamp_isaac_launcher as module
    module_file = str(Path(module.__file__).resolve())
except Exception as exc:
    import_error = f"{type(exc).__name__}: {exc}"
if len(matches) == 1:
    try:
        matches[0].load()
    except Exception as exc:
        load_error = f"{type(exc).__name__}: {exc}"
owned = [
    row for row in entries
    if str(row["distribution"]).lower().replace("_", "-") == expected_distribution
]
unrelated = [row for row in entries if row not in owned]
ok = bool(
    len(matches) == 1
    and len(owned) == 1
    and owned[0]["value"] == expected_value
    and owned[0]["version"] == expected_version
    and module_file == str(expected)
    and import_error is None
    and load_error is None
)
print(
    json.dumps(
        {
            "ok": ok,
            "match_count": len(matches),
            "owned_count": len(owned),
            "unrelated_count": len(unrelated),
            "entries": entries,
            "module_file": module_file,
            "import_error": import_error,
            "load_error": load_error,
        },
        sort_keys=True,
    )
)
raise SystemExit(0 if ok else 1)
"""


class _PluginInstallLock:
    """Serialize package mutations by target managed-Python interpreter."""

    def __init__(self, interpreter: Path) -> None:
        identity = hashlib.sha256(
            str(interpreter.resolve(strict=False)).encode("utf-8")
        ).hexdigest()[:24]
        # Keep the lock inside the per-uid ``/tmp/claude-<uid>`` scratch root,
        # which the outer Seatbelt grants for writes (see build_outer_seatbelt).
        # It stays interpreter-keyed and shared across worktrees that use the
        # same managed Python, but no longer lands on a sandbox-denied path.
        self.path = (
            Path("/tmp")
            / f"claude-{os.getuid()}"
            / "omnigent-triple-stamp-install-locks"
            / f"{identity}.lock"
        )
        self.handle: object | None = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        os.chmod(self.path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        self.handle = handle
        return self

    def __exit__(self, *_: object) -> None:
        if self.handle is not None:
            self.handle.close()  # type: ignore[union-attr]


def _plugin_probe(
    root: Path,
    tools: Toolchain,
    host_env: dict[str, str],
) -> tuple[dict[str, object], subprocess.CompletedProcess[str]]:
    plugin_source = root / ".omnigent/isaac-launcher/triple_stamp_isaac_launcher.py"
    check_env = dict(host_env)
    check_env.update(
        {
            "TRIPLE_STAMP_PLUGIN_SOURCE": str(plugin_source),
            "TRIPLE_STAMP_PLUGIN_VERSION": PLUGIN_VERSION,
            "TRIPLE_STAMP_PLUGIN_DISTRIBUTION": PLUGIN_DISTRIBUTION,
            "TRIPLE_STAMP_PLUGIN_ENTRY": PLUGIN_ENTRY_NAME,
            "TRIPLE_STAMP_PLUGIN_VALUE": PLUGIN_ENTRY_VALUE,
        }
    )
    result = _run(
        [str(tools.omnigent_python), "-I", "-c", _PLUGIN_CHECK],
        env=check_env,
    )
    try:
        status = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError, ValueError):
        status = {
            "ok": False,
            "match_count": "unknown",
            "owned_count": "unknown",
            "unrelated_count": "unknown",
            "entries": [],
            "module_file": None,
            "import_error": (
                result.stderr.strip() or result.stdout.strip() or "probe produced no diagnostics"
            ),
            "load_error": None,
        }
    return status, result


def _plugin_diagnostics(status: dict[str, object]) -> str:
    rows = status.get("entries")
    rendered = []
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                rendered.append(
                    "{name}={value} distribution={distribution} "
                    "version={version} source={metadata_path}".format(**row)
                )
    return (
        f"count={status.get('match_count')} owned={status.get('owned_count')} "
        f"unrelated={status.get('unrelated_count')}; "
        f"module={status.get('module_file')}; "
        f"import_error={status.get('import_error')}; "
        f"load_error={status.get('load_error')}; "
        f"entries=[{'; '.join(rendered) or 'none'}]"
    )


def _sanitize_plugin_output(text: str) -> str:
    sanitized = re.sub(
        r"(?i)(authorization:\s*bearer|bearer|access_token[\"'=:\s]+)\s*[^\s\"']+",
        r"\1 [REDACTED]",
        text,
    )
    sanitized = re.sub(r"https?://[^\s]+", "[REDACTED_URL]", sanitized)
    return sanitized[-4000:]


def _plugin_count(status: dict[str, object], key: str) -> int:
    value = status.get(key)
    return value if isinstance(value, int) and value >= 0 else 0


def _install_plugin(
    root: Path,
    tools: Toolchain,
    host_env: dict[str, str],
    *,
    reinstall: bool,
) -> None:
    args = [
        str(tools.uv),
        "pip",
        "install",
        "--python",
        str(tools.omnigent_python),
    ]
    if reinstall:
        args.append("--reinstall")
    args.extend(
        [
            "--editable",
            str(root / ".omnigent/isaac-launcher"),
            "--no-deps",
        ]
    )
    install = _run(args, env=host_env, timeout=180)
    if install.returncode:
        _die(
            "Isaac launcher plugin install failed: "
            + _sanitize_plugin_output(
                install.stderr.strip() or install.stdout.strip() or "no installer output"
            ),
            127,
        )


def _ensure_plugin(
    root: Path,
    tools: Toolchain,
    host_env: dict[str, str],
) -> None:
    with _PluginInstallLock(tools.omnigent_python):
        status, check = _plugin_probe(root, tools, host_env)
        if check.returncode == 0 and status.get("ok") is True:
            return
        if _plugin_count(status, "unrelated_count") > 0:
            _die(
                "an unrelated package already owns the required "
                f"{PLUGIN_ENTRY_NAME!r} launcher entry; it was not modified.\n"
                + _plugin_diagnostics(status),
                127,
            )
        _eprint(
            "triple-stamp: repairing the project-owned Isaac launcher plugin: "
            + _plugin_diagnostics(status)
        )
        _install_plugin(
            root,
            tools,
            host_env,
            reinstall=_plugin_count(status, "owned_count") > 0,
        )
        status, check = _plugin_probe(root, tools, host_env)
        if check.returncode == 0 and status.get("ok") is True:
            return

        # Duplicate/stale metadata from this exact distribution can survive an
        # interrupted editable reinstall. Remove only that named distribution,
        # then install once. Never uninstall another package sharing the group.
        if _plugin_count(status, "owned_count") > 1:
            uninstall = _run(
                [
                    str(tools.uv),
                    "pip",
                    "uninstall",
                    "--python",
                    str(tools.omnigent_python),
                    PLUGIN_DISTRIBUTION,
                ],
                env=host_env,
                timeout=180,
            )
            if uninstall.returncode:
                _die(
                    "could not remove stale project-owned launcher metadata: "
                    + _sanitize_plugin_output(
                        uninstall.stderr.strip() or uninstall.stdout.strip()
                    )
                    + "\n"
                    + _plugin_diagnostics(status),
                    127,
                )
            _install_plugin(root, tools, host_env, reinstall=False)
            status, check = _plugin_probe(root, tools, host_env)
            if check.returncode == 0 and status.get("ok") is True:
                return
        _die(
            "project-owned Isaac launcher registration is still invalid after repair.\n"
            + _plugin_diagnostics(status),
            127,
        )


def _dbcert_preflight(tools: Toolchain, host_env: dict[str, str]) -> None:
    assert tools.dbcert is not None
    result = _run(
        [str(tools.dbcert), "status", "--output", "json"],
        env=host_env,
        timeout=30,
    )
    try:
        payload = json.loads(result.stdout)
        durations = [
            payload["x509_certificate"]["validity_duration"],
            payload["ssh_certificate"]["validity_duration"],
        ]
        seconds = [int(value[:-1]) for value in durations if value.endswith("s")]
        healthy = result.returncode == 0 and len(seconds) == 2 and min(seconds) >= MIN_AUTH_SECONDS
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        healthy = False
    if not healthy:
        _die(
            "dbcert credentials are missing, invalid, or expire in under one hour.\n"
            "Run: /usr/local/bin/dbcert --force --update-kubeconfig=false\n"
            "This is host authentication, not an agent/model approval.",
            EXIT_AUTH,
        )


def _cursor_token(tools: Toolchain, host_env: dict[str, str]) -> str:
    result = _run(
        [
            str(tools.security),
            "find-generic-password",
            "-a",
            "cursor-user",
            "-s",
            "cursor-access-token",
            "-w",
        ],
        env=host_env,
        timeout=15,
    )
    token = result.stdout.strip()
    if result.returncode or not token:
        _die(
            f"Cursor CLI login is unavailable.\nRun: {tools.cursor_agent} login",
            EXIT_AUTH,
        )
    return token


def _omnigent_remote_auth(
    tools: Toolchain,
    host_env: dict[str, str],
) -> tuple[str, float]:
    """Mint a fresh control-plane token before Seatbelt removes keyring access."""

    assert tools.databricks is not None
    workspace_host, workspace_profile = _require_workspace()
    result = _run(
        [
            str(tools.databricks),
            "auth",
            "token",
            workspace_profile,
            "-o",
            "json",
        ],
        env=host_env,
        timeout=30,
    )
    try:
        payload = json.loads(result.stdout)
        token = str(payload["access_token"])
        expiry = datetime.fromisoformat(str(payload["expiry"])).timestamp()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        token = ""
        expiry = 0.0
    if result.returncode or not token or expiry - time.time() < MIN_REMOTE_TOKEN_SECONDS:
        _die(
            f"cached {workspace_profile!r} OAuth credentials are unavailable or "
            "expire in under five minutes.\nRun: "
            f"{tools.databricks} auth login --host "
            f"https://{workspace_host} --profile {workspace_profile}",
            EXIT_AUTH,
        )
    return token, expiry


def _supervisor_provider(real_home: Path) -> dict[str, object]:
    """Extract the non-secret Isaac Anthropic gateway definition."""

    path = real_home / ".omnigent/config.yaml"
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        provider = config["providers"][SUPERVISOR_PROVIDER]
        family = provider["anthropic"]
        base_url = family["base_url"]
        auth_command = family["auth_command"]
    except (OSError, KeyError, TypeError, yaml.YAMLError):
        _die(
            "Isaac's Claude SDK gateway provider is unavailable.\n"
            "Run: /usr/local/bin/isaac --claude",
            EXIT_AUTH,
        )
    if (
        provider.get("kind") != "gateway"
        or not isinstance(base_url, str)
        or not base_url.startswith("https://")
        or "/ai-gateway/anthropic" not in base_url
        or auth_command != SUPERVISOR_AUTH_COMMAND
    ):
        _die(
            "Isaac's Claude SDK gateway provider is incomplete or uses an "
            "unexpected token helper.\nRun: /usr/local/bin/isaac --claude",
            EXIT_AUTH,
        )
    # Copy only the endpoint and token-helper command. The command reads the
    # short-lived token file copied into the private HOME; no credential value
    # is embedded in this generated config.
    return {
        "kind": "gateway",
        "anthropic": {
            "base_url": base_url,
            "auth_command": auth_command,
        },
    }


def _copy_file(source: Path, destination: Path) -> None:
    if not source.is_file():
        return
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _copy_tree(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)


def _seed_isolated_home(
    root: Path,
    real_home: Path,
    isolated_home: Path,
    stable_python: Path,
) -> None:
    isolated_home.mkdir(mode=0o700, parents=True)
    for relative in (
        ".claude/settings.json",
        ".claude.json",
        ".codex/auth.json",
        ".codex/config.toml",
        ".databrickscfg",
        ".databricks/model-serving-token.json",
        ".databricks/ai-devtools-workspace-oauth.json",
        ".databricks/browser_auth_guard.json",
        ".databricks/browser_auth_guard.json.lock",
        ".databricks/token-cache.json",
        ".claude/plugins/installed_plugins.json",
        ".claude/plugins/known_marketplaces.json",
        ".claude/plugins/managed_plugins.json",
    ):
        _copy_file(real_home / relative, isolated_home / relative)
    for relative in (
        ".dbcert",
        ".kube/databricks",
        ".config/llm-cli",
        ".config/mcp",
    ):
        _copy_tree(real_home / relative, isolated_home / relative)

    for relative in (".claude/plugins", ".omnigent", ".cursor", ".local/bin"):
        (isolated_home / relative).mkdir(mode=0o700, parents=True, exist_ok=True)
    # Ephemeral HOME is always a first launch for Omnigent's TUI. Persist a
    # theme so the startup picker does not block the documented prompt.
    (isolated_home / ".omnigent/config.yaml").write_text(
        "tui:\n  theme: dark\n",
        encoding="utf-8",
    )
    (isolated_home / ".omnigent/config.yaml").chmod(0o600)
    (isolated_home / ".local/share").symlink_to(real_home / ".local/share", target_is_directory=True)
    (isolated_home / ".cache").mkdir(mode=0o700)
    (isolated_home / ".cache/isaac").symlink_to(
        real_home / ".cache/isaac", target_is_directory=True
    )
    # /usr/local/bin/isaac is a dbexec shim. dbexec derives this cache from
    # HOME rather than XDG_CACHE_HOME, so expose the already-installed runtime
    # read-only through the isolated home instead of letting it create a second
    # multi-gigabyte cache in each run.
    (isolated_home / "Library/Caches").mkdir(mode=0o700, parents=True)
    (isolated_home / "Library/Caches/dbexec").symlink_to(
        real_home / "Library/Caches/dbexec", target_is_directory=True
    )
    for name in ("python", "python3"):
        (isolated_home / ".local/bin" / name).symlink_to(stable_python)
    for executable in (real_home / ".local/bin").glob("*"):
        if executable.name in {"cursor-agent", "python", "python3"} or not executable.exists():
            continue
        (isolated_home / ".local/bin" / executable.name).symlink_to(executable)
    (isolated_home / ".local/bin/cursor-agent").symlink_to(
        root / ".omnigent/cursor-via-login"
    )


def _seed_cursor_home(run_dir: Path, harness_tmp: Path | None = None) -> Path:
    """Create a Cursor-only HOME that cannot see Claude/Codex skill trees."""

    cursor_home = run_dir / "cursor-home"
    cursor_home.mkdir(mode=0o700)
    (cursor_home / ".cursor").mkdir(mode=0o700)
    if harness_tmp is not None:
        # Cursor derives a Unix worker socket from its data-dir spelling and
        # falls back to /tmp/.cursor when that path exceeds its internal bound.
        # The canonical run path is long, so give Cursor a short symlink that
        # still resolves inside the private, Seatbelt-writable run directory.
        cursor_alias = harness_tmp / "c"
        cursor_alias.symlink_to(cursor_home, target_is_directory=True)
    return cursor_home


def _write_omnigent_auth(
    isolated_home: Path,
    state_dir: Path,
    token: str,
    expiry: float,
    supervisor_provider: dict[str, object],
) -> None:
    """Seed private control-plane auth so Isaac never opens keyring/browser login."""

    config_path = isolated_home / ".omnigent/config.yaml"
    config_path.write_text(
        "# Isolated per-run config; no host defaults or secrets are inherited.\n"
        + yaml.safe_dump(
            {
                "tui": {"theme": "light"},
                "providers": {SUPERVISOR_PROVIDER: supervisor_provider},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    server = f"https://{_require_workspace()[0]}/api/2.0/omnigent"
    payload = {
        server: {
            "token": token,
            "user_id": "",
            "expires_at": expiry,
        }
    }
    for path in (
        isolated_home / ".omnigent/auth_tokens.json",
        state_dir / "auth_tokens.json",
    ):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        path.chmod(0o600)


def _runtime_env(
    *,
    root: Path,
    real_home: Path,
    run_dir: Path,
    bundle: Path,
    run_id: str,
    sandbox_token: str,
    cursor_token: str,
    omnigent_token: str,
    harness_tmp: Path,
    tools: Toolchain,
    managed_python: Path,
    voice_profile_sha256: str,
    provider: str | None = None,
    models: dict[str, str] | None = None,
) -> dict[str, str]:
    selected_provider = _provider(provider)
    selected_models = _explicit_model_overrides(
        models or _provider_models(root, selected_provider)
    )
    effective_claude_namespace = (
        "databricks_gateway"
        if selected_provider == "databricks"
        else _detect_claude_namespace()[0]
    )
    claude_routing_environment: dict[str, str] = {}
    if (
        selected_provider == "direct"
        and effective_claude_namespace == "databricks_gateway"
    ):
        managed_claude_environment = _managed_claude_environment()
        for name in ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_GATEWAY"):
            value = os.environ.get(name) or managed_claude_environment.get(name)
            if isinstance(value, str) and value:
                claude_routing_environment[name] = value
    isolated_home = run_dir / "home"
    temp_dir = run_dir / "tmp"
    state_dir = run_dir / "state"
    passthrough_names = [
        "CURSOR_AUTH_TOKEN",
        "OMNIGENT_CODEX_PATH",
        "OMNIGENT_CURSOR_PATH",
        "CURSOR_CONFIG_DIR",
        "CURSOR_DATA_DIR",
        "CURSOR_AGENT_BIN",
        "TRIPLE_STAMP_RUN_ID",
        "TRIPLE_STAMP_RUN_DIR",
        "TRIPLE_STAMP_CURSOR_HOME",
        "TRIPLE_STAMP_OUTER_SANDBOX",
        "OMNIGENT_HARNESS_TMP_PARENT",
        "DBEXEC_NO_CERT_REFRESH",
        "DISABLE_AUTOUPDATER",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "DISABLE_TELEMETRY",
        "AGENT_CLI_CREDENTIAL_STORE",
        "TRIPLE_STAMP_VOICE_PROFILE",
        "TRIPLE_STAMP_VOICE_PROFILE_SHA256",
        "PYTHONPATH",
        "PYTHONDONTWRITEBYTECODE",
        "TRIPLE_STAMP_CURSOR_APPROVAL_GUARD",
        "TRIPLE_STAMP_ROOT",
        "STABLE_OMNIGENT_BIN",
        "STABLE_OMNIGENT_PY",
        "TRIPLE_STAMP_AUTH_PREFLIGHT",
        "TRIPLE_STAMP_PROVIDER",
        MAX_CYCLES_ENV,
        "TRIPLE_STAMP_SUPERVISOR_MODEL",
        "TRIPLE_STAMP_OPUS_MODEL",
        "TRIPLE_STAMP_CODEX_MODEL",
        "TRIPLE_STAMP_CLAUDE_NAMESPACE",
        "TRIPLE_STAMP_CODEX_BIN",
        BROWSER_HOST_PREFLIGHT_ENV,
        BROWSER_WORKSPACE_ROOTS_ENV,
        BROWSER_REQUIRED_PATH_ENV,
    ]
    if selected_provider == "databricks":
        passthrough_names.extend(
            [
                "OMNIGENT_CLAUDE_LAUNCHER",
                "ISAAC_BIN",
                "ISAAC_DEFAULT_UCODE",
                "ISAAC_LAUNCH_MODE",
                "ISAAC_DISABLE_MAC_MANAGED_SETTINGS_UPDATE",
            ]
        )
    else:
        passthrough_names.extend(claude_routing_environment)
    passthrough = ",".join(passthrough_names)
    env = {
        "HOME": str(isolated_home),
        "USER": os.environ.get("USER", real_home.name),
        "LOGNAME": os.environ.get("LOGNAME", real_home.name),
        "SHELL": os.environ.get("SHELL", "/bin/zsh"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PWD": str(root),
        "PATH": ":".join(
            dict.fromkeys(
                (
                    str(isolated_home / ".local/bin"),
                    str(tools.cursor_agent.parent),
                    str(tools.claude.parent),
                    str(tools.codex.parent),
                    "/usr/local/bin",
                    "/opt/homebrew/bin",
                    "/usr/bin",
                    "/bin",
                    "/usr/sbin",
                    "/sbin",
                )
            )
        ),
        "TMPDIR": str(temp_dir),
        "PEX_ROOT": str(run_dir / "pex"),
        "KUBECONFIG": str(run_dir / "kube/config"),
        "OMNIGENT_DATA_DIR": str(state_dir),
        "OMNIGENT_HARNESS_TMP_PARENT": str(harness_tmp),
        "OMNIGENT_CODEX_PATH": str(
            root
            / (
                ".omnigent/codex-via-isaac"
                if selected_provider == "databricks"
                else ".omnigent/codex-launch"
            )
        ),
        "OMNIGENT_CURSOR_PATH": str(root / ".omnigent/cursor-via-login"),
        "OMNIGENT_RUNNER_ENV_PASSTHROUGH": passthrough,
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "PYTHONWARNINGS": "ignore:resource_tracker:UserWarning",
        "PYTHONPATH": str(root / ".omnigent/runtime-python"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "TRIPLE_STAMP_CURSOR_APPROVAL_GUARD": "suppress-yolo-false-cards",
        "STABLE_OMNIGENT_BIN": str(tools.omnigent),
        "STABLE_OMNIGENT_PY": str(tools.omnigent_python),
        "TRIPLE_STAMP_CODEX_BIN": str(tools.codex),
        "TRIPLE_STAMP_PROVIDER": selected_provider,
        MAX_CYCLES_ENV: str(_max_cycles()),
        "TRIPLE_STAMP_SUPERVISOR_MODEL": selected_models["supervisor"],
        "TRIPLE_STAMP_OPUS_MODEL": selected_models["opus_auditor"],
        "TRIPLE_STAMP_CODEX_MODEL": selected_models["codex_judge"],
        "TRIPLE_STAMP_CLAUDE_NAMESPACE": effective_claude_namespace,
        "CURSOR_AGENT_BIN": str(tools.cursor_agent),
        "CURSOR_AUTH_TOKEN": cursor_token,
        "CURSOR_CONFIG_DIR": str(harness_tmp / "c/.cursor"),
        "CURSOR_DATA_DIR": str(harness_tmp / "c/.cursor"),
        "AGENT_CLI_CREDENTIAL_STORE": "file",
        "TRIPLE_STAMP_OUTER_SANDBOX": "1",
        "TRIPLE_STAMP_ROOT": str(root),
        "TRIPLE_STAMP_REAL_HOME": str(real_home),
        "TRIPLE_STAMP_RUN_DIR": str(run_dir),
        "TRIPLE_STAMP_CURSOR_HOME": str(harness_tmp / "c"),
        "TRIPLE_STAMP_BUNDLE": str(bundle),
        "TRIPLE_STAMP_RUN_ID": run_id,
        "TRIPLE_STAMP_SANDBOX_TOKEN": sandbox_token,
        # Both empty when voice rendering is off. Every consumer treats an empty
        # path or digest as "no voice profile configured" rather than an error.
        "TRIPLE_STAMP_VOICE_PROFILE": str(VOICE_PROFILE) if VOICE_PROFILE else "",
        "TRIPLE_STAMP_VOICE_PROFILE_SHA256": voice_profile_sha256,
    }
    env.update(claude_routing_environment)
    if selected_provider == "databricks":
        assert tools.isaac is not None
        env.update(
            {
                "OMNIGENT_CLAUDE_LAUNCHER": "isaac",
                "OMNIGENT_REMOTE_AUTH_TOKEN": omnigent_token,
                "ISAAC_OMNIGENT_BIN": str(tools.omnigent),
                "ISAAC_BIN": str(tools.isaac),
                "ISAAC_OMNI_PY": str(managed_python),
                "ISAAC_DEFAULT_UCODE": "0",
                "ISAAC_LAUNCH_MODE": "omni",
                "ISAAC_DISABLE_MAC_MANAGED_SETTINGS_UPDATE": "1",
            }
        )
    if os.environ.get(PROFILE_MATRIX_ENV) == "1":
        env[PROFILE_MATRIX_ENV] = "1"
    for name in (
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "COLORTERM",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
    ):
        if value := os.environ.get(name):
            env[name] = value
    return env


def _build_profile_and_bundle(
    root: Path,
    real_home: Path,
    run_dir: Path,
    tools: Toolchain,
    runtime_env: dict[str, str],
) -> tuple[Path, Path]:
    profile = run_dir / "outer-seatbelt.sb"
    bundle = run_dir / "bundle"
    cli_read_dirs = sorted(
        {
            str(_cli_install_prefix(tools.cursor_agent)),
            str(_cli_install_prefix(tools.claude)),
            str(_cli_install_prefix(tools.codex)),
        }
    )
    build = _run(
        [
            str(tools.omnigent_python),
            "-I",
            str(root / ".omnigent/build_outer_seatbelt.py"),
            str(root),
            str(profile),
            str(run_dir),
            str(bundle),
            str(real_home),
            str(VOICE_PROFILE) if VOICE_PROFILE else "",
            json.dumps(cli_read_dirs, separators=(",", ":")),
        ],
        env=runtime_env,
        timeout=60,
    )
    if build.returncode:
        _die(f"could not build the outer Seatbelt profile: {build.stderr.strip()}")
    if not profile.is_file() or not bundle.joinpath("config.yaml").is_file():
        _die("Seatbelt builder did not produce its profile and runtime bundle")
    return profile, bundle


def _apply_provider_to_bundle(
    bundle: Path,
    provider: str,
    models: dict[str, str],
    max_cycles: object | None = None,
) -> None:
    """Materialize the selected provider's exact models and launch env."""

    cycles = _max_cycles(max_cycles)
    paths = {
        "supervisor": bundle / "config.yaml",
        "opus_auditor": bundle / "agents/opus_auditor/config.yaml",
        "codex_judge": bundle / "agents/codex_judge/config.yaml",
    }
    documents: dict[str, dict[str, object]] = {}
    for name, path in paths.items():
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, yaml.YAMLError) as exc:
            _die(f"could not materialize {provider} profile for {name}: {exc}")
        if not isinstance(document, dict):
            _die(f"runtime config for {name} is not a mapping")
        documents[name] = document

    documents["supervisor"]["executor"]["model"] = models["supervisor"]  # type: ignore[index]
    documents["opus_auditor"]["executor"]["model"] = models["opus_auditor"]  # type: ignore[index]
    documents["codex_judge"]["executor"]["model"] = models["codex_judge"]  # type: ignore[index]

    supervisor_prompt = documents["supervisor"].get("prompt")
    if not isinstance(supervisor_prompt, str):
        _die("runtime supervisor prompt is not text")
    supervisor_prompt = supervisor_prompt.replace(
        (
            "This run permits exactly 2 complete cycles. Cycle 3 is denied.\n"
            "After 2 failed cycles,"
        ),
        (
            f"This run permits exactly {cycles} complete cycles. "
            f"Cycle {cycles + 1} is denied.\n"
            f"After {cycles} failed cycles,"
        ),
    )
    documents["supervisor"]["prompt"] = supervisor_prompt
    try:
        cap_arguments = documents["supervisor"]["guardrails"]["policies"][  # type: ignore[index]
            "cap_calls"
        ]["function"]["arguments"]
    except (KeyError, TypeError):
        _die("runtime supervisor route-call policy is malformed")
    if not isinstance(cap_arguments, dict):
        _die("runtime supervisor route-call arguments are not a mapping")
    cap_arguments["limit"] = _route_call_cap(cycles)

    supervisor_executor = documents["supervisor"]["executor"]
    assert isinstance(supervisor_executor, dict)
    if provider == "databricks":
        supervisor_executor["auth"] = {
            "type": "provider",
            "name": SUPERVISOR_PROVIDER,
        }
    else:
        supervisor_executor.pop("auth", None)

    profile_passthrough = {
        "supervisor": ("ISAAC_BIN",),
        "opus_auditor": (
            "OMNIGENT_CLAUDE_LAUNCHER",
            "ISAAC_BIN",
            "ISAAC_DEFAULT_UCODE",
            "ISAAC_LAUNCH_MODE",
            "ISAAC_DISABLE_MAC_MANAGED_SETTINGS_UPDATE",
        ),
        "codex_judge": ("ISAAC_BIN",),
    }
    for name, document in documents.items():
        sandbox = document["os_env"]["sandbox"]  # type: ignore[index]
        assert isinstance(sandbox, dict)
        passthrough = sandbox.get("env_passthrough")
        if not isinstance(passthrough, list):
            passthrough = []
        values = [str(value) for value in passthrough]
        if MAX_CYCLES_ENV not in values:
            values.append(MAX_CYCLES_ENV)
        for variable in profile_passthrough[name]:
            if provider == "databricks" and variable not in values:
                values.append(variable)
            if provider == "direct":
                values = [value for value in values if value != variable]
        sandbox["env_passthrough"] = values

    for name, path in paths.items():
        path.write_text(
            yaml.safe_dump(documents[name], sort_keys=False),
            encoding="utf-8",
        )


def _sandbox_probe(
    tools: Toolchain,
    profile: Path,
    runtime_env: dict[str, str],
) -> None:
    probe = r"""
import os
import pty
import socket
import hashlib
from pathlib import Path

master, slave = pty.openpty()
os.close(master)
os.close(slave)

positive = Path(os.environ["TRIPLE_STAMP_RUN_DIR"]) / "sandbox-positive"
positive.write_text("ok", encoding="utf-8")
positive.unlink()

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))

configured_voice = os.environ.get("TRIPLE_STAMP_VOICE_PROFILE", "")
if configured_voice:
    voice_profile = Path(configured_voice)
    voice_data = voice_profile.read_bytes()
    if hashlib.sha256(voice_data).hexdigest() != os.environ["TRIPLE_STAMP_VOICE_PROFILE_SHA256"]:
        raise SystemExit("voice profile changed after host preflight")
    try:
        with voice_profile.open("a", encoding="utf-8"):
            pass
    except PermissionError:
        pass
    else:
        raise SystemExit("outer Seatbelt allowed voice profile write")

for path in (
    Path(os.environ["TRIPLE_STAMP_REAL_HOME"]) / "omnigent-seatbelt-deny-probe",
    Path(os.environ["TRIPLE_STAMP_REAL_HOME"]) / ".kube/config",
):
    try:
        with path.open("a", encoding="utf-8"):
            pass
    except PermissionError:
        continue
    if path.name == "omnigent-seatbelt-deny-probe":
        path.unlink(missing_ok=True)
    raise SystemExit(f"outer Seatbelt allowed forbidden write: {path}")
"""
    result = _run(
        [
            str(tools.sandbox_exec),
            "-f",
            str(profile),
            str(tools.omnigent_python),
            "-I",
            "-c",
            probe,
        ],
        env=runtime_env,
        timeout=30,
    )
    if result.returncode:
        _die(
            "outer Seatbelt capability probe failed before any model started: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def _sandboxed_auth_preflight(
    root: Path,
    run_dir: Path,
    run_id: str,
    tools: Toolchain,
    profile: Path,
    runtime_env: dict[str, str],
) -> None:
    """Validate all worker auth paths in the exact sandbox before locking."""

    result = _run(
        [
            str(tools.sandbox_exec),
            "-f",
            str(profile),
            str(tools.omnigent_python),
            "-I",
            str(root / ".omnigent/auth_preflight.py"),
        ],
        env=runtime_env,
        timeout=300,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        preflight_status = run_dir / "cursor-startup-preflight.json"
        try:
            payload = json.loads(preflight_status.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            extras = [
                str(payload.get("reason") or ""),
                str(payload.get("stderr") or "")[:1500],
            ]
            extra = "\n".join(part for part in extras if part and part not in detail)
            if extra:
                detail = f"{detail}\n{extra}"
        raise LaunchError(
            detail,
            EXIT_AUTH if result.returncode == EXIT_AUTH else EXIT_CONFIG,
        )
    marker = run_dir / "auth-preflight-ok"
    marker.write_text(run_id + "\n", encoding="utf-8")
    marker.chmod(0o600)
    runtime_env["TRIPLE_STAMP_AUTH_PREFLIGHT"] = run_id


def _managed_cursor_entry(value: object, run_root: Path | None = None) -> bool:
    if not isinstance(value, dict):
        return False
    args = value.get("args")
    command = value.get("command")
    arg_values = args if isinstance(args, list) else []
    text = " ".join([str(command or ""), *(str(arg) for arg in arg_values)])
    # Match either the 0.12 module path or the 0.14 relocated harness path so
    # managed Cursor entries are still detected for teardown on both runtimes.
    bridge_tokens = (
        "omnigent.claude_native_bridge",
        "omnigent.harnesses.claude_native.bridge",
    )
    if not any(token in text for token in bridge_tokens) or "serve-mcp" not in text:
        return False
    return run_root is None or str(run_root) in text or "/omnigent-triple-stamp-" in text


def _remove_managed_cursor_files(root: Path, run_root: Path | None = None) -> None:
    cursor_dir = root / ".cursor"
    mcp_path = cursor_dir / "mcp.json"
    if mcp_path.is_file():
        try:
            payload = json.loads(mcp_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            servers = payload.get("mcpServers")
            if isinstance(servers, dict) and _managed_cursor_entry(
                servers.get("omnigent"), run_root
            ):
                servers.pop("omnigent", None)
                if not servers:
                    payload.pop("mcpServers", None)
                if payload:
                    mcp_path.write_text(
                        json.dumps(payload, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                else:
                    mcp_path.unlink(missing_ok=True)

    hooks_path = cursor_dir / "hooks.json"
    if hooks_path.is_file():
        try:
            text = hooks_path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        usage_tokens = (
            "omnigent.cursor_native_usage",
            "omnigent.harnesses.cursor_native.usage",
        )
        if (
            any(token in text for token in usage_tokens)
            and (run_root is None or str(run_root) in text or "/omnigent-triple-stamp-" in text)
        ):
            hooks_path.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        cursor_dir.rmdir()


def _snapshot_cursor_file(root: Path, name: str) -> tuple[bool, bytes]:
    path = root / ".cursor" / name
    return (path.exists(), path.read_bytes() if path.is_file() else b"")


def _restore_cursor_file(root: Path, name: str, snapshot: tuple[bool, bytes]) -> None:
    existed, content = snapshot
    path = root / ".cursor" / name
    if existed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    else:
        path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            path.parent.rmdir()


def _processes_with_marker(run_id: str) -> list[object]:
    try:
        import psutil
    except ImportError:
        return []
    matches: list[object] = []
    for process in psutil.process_iter():
        if process.pid == os.getpid():
            continue
        try:
            if process.environ().get("TRIPLE_STAMP_RUN_ID") == run_id:
                matches.append(process)
        except (psutil.Error, OSError):
            continue
    return matches


def _reap_marked_processes(run_id: str) -> list[str]:
    try:
        import psutil
    except ImportError:
        return ["psutil unavailable; marker-based descendant check was skipped"]
    processes = _processes_with_marker(run_id)
    if not processes:
        return []
    for process in processes:
        with contextlib.suppress(psutil.Error, OSError):
            process.terminate()
    _, alive = psutil.wait_procs(processes, timeout=STOP_GRACE_SECONDS)
    for process in alive:
        with contextlib.suppress(psutil.Error, OSError):
            process.kill()
    _, survivors = psutil.wait_procs(alive, timeout=5)
    return [f"PID {process.pid}: {' '.join(process.cmdline())}" for process in survivors]


def _reap_data_dir(tools: Toolchain, data_dir: Path, env: dict[str, str]) -> None:
    code = (
        "from omnigent.testing.process_reaper import reap_leaked_omnigent_processes as r;"
        "import os; reaped,survivors=r(os.environ['TARGET_DATA_DIR']);"
        "print(len(reaped));"
        "raise SystemExit(1 if survivors else 0)"
    )
    reap_env = dict(env)
    reap_env["TARGET_DATA_DIR"] = str(data_dir)
    result = _run(
        [str(tools.omnigent_python), "-I", "-c", code],
        env=reap_env,
        timeout=30,
    )
    if result.returncode:
        _eprint(f"triple-stamp: warning: some processes survived cleanup for {data_dir}")


def _cleanup_runtime_services(
    tools: Toolchain,
    run_id: str,
    run_dir: Path,
    runtime_env: dict[str, str],
) -> list[str]:
    """Stop both marker-owned workers and data-dir-owned browser services."""

    survivors = _reap_marked_processes(run_id)
    _reap_data_dir(tools, run_dir / "state", runtime_env)
    return survivors


def _cleanup_stale_runs(base_dir: Path, *, keep: Path | None = None) -> None:
    if not base_dir.is_dir():
        return
    for run_dir in base_dir.glob("run-*"):
        if keep is not None and run_dir == keep:
            continue
        try:
            owner_pid = int((run_dir / "owner-pid").read_text(encoding="utf-8").strip())
            os.kill(owner_pid, 0)
        except (OSError, TypeError, ValueError):
            owner_pid = 0
        if owner_pid > 0:
            # Another launcher may be running its pre-model auth check without
            # the long-lived workspace lock. Never delete its exact test HOME.
            continue
        harness_link_file = run_dir / "harness-link"
        try:
            harness_link = Path(harness_link_file.read_text(encoding="utf-8").strip())
        except OSError:
            harness_link = None
        marker_path = run_dir / "run-id"
        try:
            run_id = marker_path.read_text(encoding="utf-8").strip()
        except OSError:
            run_id = ""
        if run_id:
            survivors = _reap_marked_processes(run_id)
            if survivors:
                _die("a stale run still owns live processes:\n" + "\n".join(survivors))
        if harness_link is not None and harness_link.is_symlink():
            harness_link.unlink(missing_ok=True)
        shutil.rmtree(run_dir, ignore_errors=True)


class _RunLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: object | None = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            owner = handle.read().strip() or "unknown PID"
            handle.close()
            _die(
                f"another run already owns this workspace ({owner}); "
                "concurrent runs are blocked because Cursor's workspace bridge files are shared",
                EXIT_BUSY,
            )
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self.handle = handle
        return self

    def __exit__(self, *_: object) -> None:
        if self.handle is not None:
            self.handle.close()  # type: ignore[union-attr]
        with contextlib.suppress(OSError):
            self.path.unlink()
        with contextlib.suppress(OSError):
            self.path.parent.rmdir()


def _prepare_runtime(
    root: Path,
    real_home: Path,
    base_dir: Path,
    tools: Toolchain,
    managed_python: Path,
    cursor_token: str,
    omnigent_token: str,
    omnigent_expiry: float,
    supervisor_provider: dict[str, object],
    voice_profile_sha256: str,
    provider: str | None = None,
    models: dict[str, str] | None = None,
) -> tuple[Path, Path, dict[str, str], str]:
    selected_provider = _provider(provider)
    selected_models = _explicit_model_overrides(
        models or _provider_models(root, selected_provider)
    )
    # The Isaac plugin is an editable install into the shared Omnigent
    # environment. Re-bind it to *this* clone before generating Seatbelt,
    # otherwise a leftover install from another checkout is imported from
    # outside the read root and the runtime-guard probe dies with EPERM.
    _ensure_plugin(root, tools, _host_env(real_home))
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=base_dir))
    harness_tmp: Path | None = None
    try:
        max_cycles = _max_cycles()
        run_id = secrets.token_urlsafe(24)
        sandbox_token = secrets.token_urlsafe(32)
        for relative in ("tmp", "pex", "state", "kube"):
            (run_dir / relative).mkdir(mode=0o700)
        (run_dir / "h").mkdir(mode=0o700)
        (run_dir / "run-id").write_text(run_id + "\n", encoding="utf-8")
        cycle_policy = run_dir / "cycle-policy.json"
        cycle_policy.write_text(
            json.dumps(
                {
                    "max_cycles": max_cycles,
                    "allowed_values": [2, 4],
                    "route_call_cap": _route_call_cap(max_cycles),
                    "formula": "cycles*19*2+8",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        cycle_policy.chmod(0o600)
        (run_dir / "owner-pid").write_text(str(os.getpid()) + "\n", encoding="utf-8")
        (run_dir / "sandbox-token").write_text(sandbox_token + "\n", encoding="utf-8")
        (run_dir / "sandbox-token").chmod(0o600)
        harness_tmp = Path("/tmp") / f"ots-{os.getuid()}-{secrets.token_hex(4)}"
        harness_tmp.symlink_to(run_dir / "h", target_is_directory=True)
        (run_dir / "harness-link").write_text(str(harness_tmp) + "\n", encoding="utf-8")
        _seed_isolated_home(
            root,
            real_home,
            run_dir / "home",
            tools.omnigent_python,
        )
        _seed_cursor_home(run_dir, harness_tmp)
        _copy_file(real_home / ".kube/config", run_dir / "kube/config")
        if selected_provider == "databricks":
            _write_omnigent_auth(
                run_dir / "home",
                run_dir / "state",
                omnigent_token,
                omnigent_expiry,
                supervisor_provider,
            )
        bundle = run_dir / "bundle"
        runtime_env = _runtime_env(
            root=root,
            real_home=real_home,
            run_dir=run_dir,
            bundle=bundle,
            run_id=run_id,
            sandbox_token=sandbox_token,
            cursor_token=cursor_token,
            omnigent_token=omnigent_token,
            harness_tmp=harness_tmp,
            tools=tools,
            managed_python=managed_python,
            voice_profile_sha256=voice_profile_sha256,
            provider=selected_provider,
            models=selected_models,
        )
        profile, built_bundle = _build_profile_and_bundle(
            root, real_home, run_dir, tools, runtime_env
        )
        _apply_provider_to_bundle(
            built_bundle,
            selected_provider,
            selected_models,
            max_cycles,
        )
        if built_bundle != bundle:
            _die("Seatbelt builder returned an unexpected runtime bundle path")
        return run_dir, profile, runtime_env, run_id
    except BaseException:
        if harness_tmp is not None and harness_tmp.is_symlink():
            harness_tmp.unlink(missing_ok=True)
        shutil.rmtree(run_dir, ignore_errors=True)
        raise


def _inner_validate(root: Path, args: list[str]) -> int:
    """Prove this process is sandboxed, then validate all mechanical pins."""

    provider = _provider()
    required = [
        "TRIPLE_STAMP_RUN_DIR",
        "TRIPLE_STAMP_CURSOR_HOME",
        "TRIPLE_STAMP_BUNDLE",
        "TRIPLE_STAMP_REAL_HOME",
        "TRIPLE_STAMP_SANDBOX_TOKEN",
        "STABLE_OMNIGENT_PY",
        "CURSOR_AGENT_BIN",
        "TRIPLE_STAMP_AUTH_PREFLIGHT",
        "TRIPLE_STAMP_PROVIDER",
        MAX_CYCLES_ENV,
        "TRIPLE_STAMP_SUPERVISOR_MODEL",
        "TRIPLE_STAMP_OPUS_MODEL",
        "TRIPLE_STAMP_CODEX_MODEL",
        "TRIPLE_STAMP_CLAUDE_NAMESPACE",
    ]
    # Voice rendering is optional, so these two must be exported but are
    # legitimately empty when it is off. Checking them for truthiness the way
    # the rest are checked reads an intentional "off" as a stripped variable.
    required_present = [
        "TRIPLE_STAMP_VOICE_PROFILE",
        "TRIPLE_STAMP_VOICE_PROFILE_SHA256",
    ]
    if provider == "databricks":
        required.append("ISAAC_BIN")
    missing = [name for name in required if not os.environ.get(name)] + [
        name for name in required_present if name not in os.environ
    ]
    if missing:
        _die(f"inherited outer Seatbelt metadata is missing: {', '.join(missing)}")
    run_dir = Path(os.environ["TRIPLE_STAMP_RUN_DIR"]).resolve()
    try:
        cycle_policy = json.loads(
            (run_dir / "cycle-policy.json").read_text(encoding="utf-8")
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        _die(f"run cycle policy is unavailable: {exc}")
    max_cycles = _max_cycles()
    if (
        not isinstance(cycle_policy, dict)
        or cycle_policy.get("max_cycles") != max_cycles
        or cycle_policy.get("allowed_values") != [2, 4]
        or cycle_policy.get("route_call_cap") != _route_call_cap(max_cycles)
        or cycle_policy.get("formula") != "cycles*19*2+8"
    ):
        _die("run cycle policy does not match inherited runtime bounds")
    token_path = run_dir / "sandbox-token"
    try:
        disk_token = token_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        _die(f"could not read the sandbox token: {exc}")
    if not secrets.compare_digest(disk_token, os.environ["TRIPLE_STAMP_SANDBOX_TOKEN"]):
        _die("sandbox token mismatch")
    if stat.S_IMODE(run_dir.stat().st_mode) & 0o077:
        _die(f"runtime directory is not private: {run_dir}")
    preflight_marker = run_dir / "auth-preflight-ok"
    try:
        preflight_run_id = preflight_marker.read_text(encoding="utf-8").strip()
    except OSError as exc:
        _die(f"isolated worker-auth preflight marker is missing: {exc}", EXIT_AUTH)
    if (
        not secrets.compare_digest(preflight_run_id, os.environ["TRIPLE_STAMP_AUTH_PREFLIGHT"])
        or not secrets.compare_digest(preflight_run_id, os.environ["TRIPLE_STAMP_RUN_ID"])
        or stat.S_IMODE(preflight_marker.stat().st_mode) & 0o077
    ):
        _die("isolated worker-auth preflight marker is invalid", EXIT_AUTH)

    forbidden = Path(os.environ["TRIPLE_STAMP_REAL_HOME"]) / (
        f"omnigent-inner-deny-probe-{os.getpid()}"
    )
    try:
        forbidden.write_text("must be denied", encoding="utf-8")
    except PermissionError:
        pass
    else:
        forbidden.unlink(missing_ok=True)
        _die("outer Seatbelt is not active; refusing to disable isolation")

    stable_python = os.environ["STABLE_OMNIGENT_PY"]
    self_test = any(arg in SELF_TEST_FLAGS for arg in args)
    if self_test and os.environ.get(PROFILE_MATRIX_ENV) == "1":
        validation_cases: list[tuple[str, Path, dict[str, str]]] = []
        base_bundle = Path(os.environ["TRIPLE_STAMP_BUNDLE"])
        for matrix_provider in ("direct", "databricks"):
            matrix_bundle = run_dir / f"bundle-{matrix_provider}-validation"
            shutil.copytree(base_bundle, matrix_bundle)
            matrix_models, _matrix_model_namespace, _ = _resolve_models(
                root,
                matrix_provider,
                environment={},
            )
            matrix_namespace = (
                "databricks_gateway"
                if matrix_provider == "databricks"
                else _detect_claude_namespace()[0]
            )
            _apply_provider_to_bundle(
                matrix_bundle,
                matrix_provider,
                matrix_models,
            )
            matrix_env = dict(os.environ)
            matrix_env.update(
                {
                    "TRIPLE_STAMP_BUNDLE": str(matrix_bundle),
                    "TRIPLE_STAMP_PROVIDER": matrix_provider,
                    "TRIPLE_STAMP_SUPERVISOR_MODEL": matrix_models["supervisor"],
                    "TRIPLE_STAMP_OPUS_MODEL": matrix_models["opus_auditor"],
                    "TRIPLE_STAMP_CODEX_MODEL": matrix_models["codex_judge"],
                    "TRIPLE_STAMP_CLAUDE_NAMESPACE": matrix_namespace,
                    "OMNIGENT_CODEX_PATH": str(
                        root
                        / (
                            ".omnigent/codex-via-isaac"
                            if matrix_provider == "databricks"
                            else ".omnigent/codex-launch"
                        )
                    ),
                    OPUS_READINESS_SMOKE_ENV: "1",
                }
            )
            if matrix_provider == "databricks":
                if not matrix_env.get("ISAAC_BIN"):
                    _die(
                        "Databricks provider matrix requires a Databricks "
                        "self-test launch"
                    )
                matrix_env["OMNIGENT_CLAUDE_LAUNCHER"] = "isaac"
            else:
                for name in (
                    "OMNIGENT_CLAUDE_LAUNCHER",
                    "OMNIGENT_REMOTE_AUTH_TOKEN",
                    "ISAAC_BIN",
                    "ISAAC_DEFAULT_UCODE",
                    "ISAAC_DISABLE_MAC_MANAGED_SETTINGS_UPDATE",
                    "ISAAC_LAUNCH_MODE",
                    "ISAAC_OMNIGENT_BIN",
                    "ISAAC_OMNI_PY",
                ):
                    matrix_env.pop(name, None)
            validation_cases.append(
                (matrix_provider, matrix_bundle, matrix_env)
            )
    else:
        selected_validation_env = dict(os.environ)
        if self_test:
            selected_validation_env[OPUS_READINESS_SMOKE_ENV] = "1"
        validation_cases = [
            (
                provider,
                Path(os.environ["TRIPLE_STAMP_BUNDLE"]),
                selected_validation_env,
            )
        ]
    for matrix_provider, matrix_bundle, matrix_env in validation_cases:
        validation = _run(
            [
                stable_python,
                "-I",
                str(root / ".omnigent/validate_bundle.py"),
                str(root),
                str(matrix_bundle),
            ],
            env=matrix_env,
            # Self-test adds exact no-inference wrapper smokes. Normal startup
            # validates only the selected profile's launch contract.
            timeout=180,
        )
        if validation.returncode:
            _die(
                f"{matrix_provider} bundle validation failed: "
                f"{validation.stderr.strip() or validation.stdout.strip() or 'no output'}"
            )

    cursor_home_raw = Path(os.environ["TRIPLE_STAMP_CURSOR_HOME"])
    cursor_home = cursor_home_raw.resolve()
    cursor_config_dir = Path(os.environ.get("CURSOR_CONFIG_DIR", ""))
    cursor_data_dir = Path(os.environ.get("CURSOR_DATA_DIR", ""))
    if (
        cursor_home != (run_dir / "cursor-home").resolve()
        or not cursor_home.is_dir()
        or stat.S_IMODE(cursor_home.stat().st_mode) & 0o077
        or cursor_config_dir.resolve() != cursor_home / ".cursor"
        or cursor_data_dir.resolve() != cursor_home / ".cursor"
        or len(str(cursor_data_dir / "projects")) > 84
    ):
        _die("Cursor-only HOME isolation metadata is invalid")
    cursor_auth_file = cursor_home / ".cursor/auth.json"
    if (
        not cursor_auth_file.is_file()
        or stat.S_IMODE(cursor_auth_file.stat().st_mode) & 0o077
    ):
        _die("Cursor did not create its private mode-0600 refreshable credential store")

    if self_test:
        cursor_version = _run(
            [str(root / ".omnigent/cursor-via-login"), "--version"],
            env=dict(os.environ),
            timeout=30,
        )
        if cursor_version.returncode or not cursor_version.stdout.strip():
            _die(
                "Cursor project wrapper version probe failed: "
                f"{cursor_version.stderr.strip() or 'no output'}"
            )
        cursor_pin = _run(
            [
                str(root / ".omnigent/cursor-via-login"),
                "--triple-stamp-self-test",
            ],
            env=dict(os.environ),
            timeout=30,
        )
        if cursor_pin.returncode:
            _die(
                "Cursor Grok 4.6 Extra High wrapper self-test failed: "
                f"{cursor_pin.stderr.strip() or cursor_pin.stdout.strip() or 'no output'}"
            )
        codex_probe_env = {
            name: value
            for name, value in os.environ.items()
            if name
            in {
                "HOME",
                "USER",
                "LOGNAME",
                "SHELL",
                "PATH",
                "LANG",
                "TMPDIR",
                "PEX_ROOT",
                "KUBECONFIG",
            }
        }
        codex_wrapper = Path(os.environ["OMNIGENT_CODEX_PATH"])
        codex_version = _run(
            [str(codex_wrapper), "--version"],
            env=codex_probe_env,
            timeout=90,
        )
        codex_text = codex_version.stdout + codex_version.stderr
        if codex_version.returncode or "codex-cli" not in codex_text:
            _die(
                f"Codex {provider} wrapper version probe failed: "
                f"{codex_text.strip() or 'no output'}"
            )
        regression_counts: dict[str, str] = {}
        for matrix_provider, _matrix_bundle, matrix_env in validation_cases:
            # Install the 0.14 compatibility layer (relocated-module aliases +
            # changed-signature shims) before test discovery, so tests that
            # import legacy Omnigent paths or call adapted functions behave the
            # same on 0.12 and 0.14. A no-op on 0.12.
            regression_bootstrap = (
                "import sys, unittest;"
                f"sys.path.insert(0, {str(root / '.omnigent/runtime-python')!r});"
                "import triple_stamp_omnigent_compat as _compat;"
                "_compat.install_all();"
                "unittest.main(module=None, argv=["
                "'triple-stamp-regressions', 'discover',"
                f" '-s', {str(root / 'tests')!r}, '-p', 'test_*.py'])"
            )
            regressions = _run(
                [
                    stable_python,
                    "-I",
                    "-c",
                    regression_bootstrap,
                ],
                env=matrix_env,
                timeout=180,
            )
            if regressions.returncode:
                _die(
                    f"{matrix_provider} triple-stamp regression tests failed: "
                    f"{regressions.stderr.strip() or regressions.stdout.strip() or 'no output'}"
                )
            regression_summary = re.search(
                r"\bRan ([0-9]+) tests?\b",
                regressions.stdout + regressions.stderr,
            )
            if regression_summary is None:
                _die(
                    f"{matrix_provider} regression tests returned no "
                    "test-count summary"
                )
            regression_counts[matrix_provider] = regression_summary.group(1)
        omnigent_runtime_version = _omnigent_runtime_version(
            stable_python, dict(os.environ)
        )
        print("triple-stamp self-test: PASS")
        print("  outer Seatbelt: active (positive + negative probes passed)")
        print(
            f"  runtime: Omnigent {omnigent_runtime_version}, per-run HOME/state/tmp"
        )
        for matrix_provider, regression_count in regression_counts.items():
            print(
                f"  regression tests ({matrix_provider}): "
                f"{regression_count} passed"
            )
        print(
            "  bundle validators: "
            + " + ".join(regression_counts)
            + " passed"
        )
        print(f"  provider: {provider}")
        print(f"  supervisor: {os.environ['TRIPLE_STAMP_SUPERVISOR_MODEL']}")
        print("  Cursor: cursor-grok-4.6-xhigh (Cursor Grok 4.6 Extra High)")
        print(f"  Opus: {os.environ['TRIPLE_STAMP_OPUS_MODEL']}, max")
        print(f"  Codex: {os.environ['TRIPLE_STAMP_CODEX_MODEL']}, ultra")
        print(
            f"  voice: profile readable and Seatbelt read-only ({VOICE_PROFILE})"
            if VOICE_PROFILE
            else f"  voice: rendering off; set {VOICE_PROFILE_ENV} to enable it"
        )
        print("  budget: strict $50 cumulative gate; no interactive thresholds")
        max_cycles = _max_cycles()
        print(
            f"  route bound: {max_cycles} cycles; "
            f"{_route_call_cap(max_cycles)} verified root-supervisor "
            "send/read/search/start calls"
        )
        return 0

    if os.environ.get(BROWSER_HOST_PREFLIGHT_ENV) == "1":
        required_path = Path(os.environ.get(BROWSER_REQUIRED_PATH_ENV, "")).resolve(
            strict=False
        )
        try:
            browser_roots = json.loads(os.environ.get(BROWSER_WORKSPACE_ROOTS_ENV, ""))
        except json.JSONDecodeError:
            browser_roots = None
        expected_root = Path(os.environ["TRIPLE_STAMP_ROOT"]).resolve()
        expected_home = run_dir / "home"
        if (
            required_path != expected_root
            or browser_roots != [str(expected_root)]
            or Path.home().resolve() != expected_home
            or Path.cwd().resolve() != expected_root
            or Path(os.environ.get("PWD", "")).resolve(strict=False) != expected_root
        ):
            _die(
                "browser runtime must keep isolated HOME while cwd, PWD, "
                "required path, and host picker root stay on the project"
            )

    _reset_pipeline_outputs(run_dir)
    if provider == "databricks":
        executable = os.environ["ISAAC_BIN"]
        argv = [
            executable,
            "omni",
            "--",
            "run",
            os.environ["TRIPLE_STAMP_BUNDLE"],
            *args,
        ]
    else:
        executable = os.environ["STABLE_OMNIGENT_BIN"]
        argv = [
            executable,
            "run",
            os.environ["TRIPLE_STAMP_BUNDLE"],
            *args,
        ]
    os.execve(executable, argv, dict(os.environ))
    return 127


def _spawn_sandboxed(
    root: Path,
    profile: Path,
    runtime_env: dict[str, str],
    args: list[str],
    tools: Toolchain,
) -> int:
    command = [
        str(tools.sandbox_exec),
        "-f",
        str(profile),
        str(tools.omnigent_python),
        "-I",
        str(root / ".omnigent/launcher.py"),
        *args,
    ]
    capture_stdout = (
        _prompt_mode(args)
        and not any(arg in SELF_TEST_FLAGS for arg in args)
    )
    stdout_handle = None
    if capture_stdout:
        stdout_handle = (Path(runtime_env["TRIPLE_STAMP_RUN_DIR"]) / "child-stdout.bin").open(
            "wb"
        )
    try:
        child = subprocess.Popen(
            command,
            env=runtime_env,
            cwd=root,
            start_new_session=True,
            stdout=stdout_handle,
        )
    finally:
        if stdout_handle is not None:
            stdout_handle.close()
    received_signal: int | None = None
    stop_deadline: float | None = None

    def forward(signum: int, _frame: object) -> None:
        nonlocal received_signal, stop_deadline
        received_signal = signum
        stop_deadline = time.monotonic() + STOP_GRACE_SECONDS
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(child.pid, signum)

    old_handlers = {
        signum: signal.signal(signum, forward)
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }
    try:
        while True:
            try:
                return_code = child.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if stop_deadline is None or time.monotonic() < stop_deadline:
                    continue
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(child.pid, signal.SIGKILL)
                with contextlib.suppress(ProcessLookupError):
                    child.kill()
                return_code = child.wait()
                break
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
    if received_signal is not None:
        _record_external_signal(
            Path(runtime_env["TRIPLE_STAMP_RUN_DIR"]),
            received_signal,
        )
        return 128 + received_signal
    return return_code


def _record_external_signal(run_dir: Path, signum: int) -> None:
    """Persist the outer signal separately from pipeline policy failures."""

    try:
        signal_name = signal.Signals(signum).name
    except ValueError:
        signal_name = f"SIGNAL_{signum}"
    payload = {
        "version": 1,
        "source": "outer_launcher_signal_handler",
        "signal": signal_name,
        "signal_number": int(signum),
    }
    temporary = run_dir / f".external-signal-{os.getpid()}.tmp"
    try:
        temporary.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, run_dir / "external-signal.json")
    finally:
        temporary.unlink(missing_ok=True)


def _reset_pipeline_outputs(run_dir: Path) -> None:
    """Remove validator-only artifacts before the first model can start."""

    for name in _PIPELINE_OUTPUTS:
        (run_dir / name).unlink(missing_ok=True)
    for directory in ("cursor-lifecycle", "packets"):
        shutil.rmtree(run_dir / directory, ignore_errors=True)


def _validated_pipeline_exit(run_dir: Path, child_code: int, args: list[str]) -> int:
    """Return zero only for self-test or an immutable collected STAMP."""

    if any(arg in SELF_TEST_FLAGS for arg in args):
        return child_code

    try:
        attestation = json.loads(
            (run_dir / "stamp-attestation.json").read_text(encoding="utf-8")
        )
        answer = (run_dir / "stamped-answer.bin").read_bytes()
    except (OSError, ValueError, json.JSONDecodeError):
        _ensure_terminal_failure(run_dir, child_code)
        return child_code if child_code != 0 else EXIT_PIPELINE
    if not isinstance(attestation, dict) or attestation.get("verdict") != "STAMP":
        _ensure_terminal_failure(
            run_dir, child_code, "terminal STAMP attestation was malformed"
        )
        return EXIT_PIPELINE
    try:
        # Resolved at call time, not from the import-time constant, so the digest
        # is computed against the profile this run was actually configured with.
        active_profile = _resolve_voice_profile()
        current_profile_digest = (
            hashlib.sha256(active_profile.read_bytes()).hexdigest()
            if active_profile
            else ""
        )
        collections = [
            json.loads(line)
            for line in (run_dir / "routing-collections.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
    except OSError:
        _ensure_terminal_failure(
            run_dir,
            child_code or EXIT_PIPELINE,
            "voice profile or routing ledger became unreadable during "
            "terminal validation",
        )
        return EXIT_PIPELINE
    except (ValueError, json.JSONDecodeError):
        _ensure_terminal_failure(
            run_dir,
            child_code or EXIT_PIPELINE,
            "routing collection ledger was malformed during terminal validation",
        )
        return EXIT_PIPELINE
    evidence = next(
        (
            record.get("output", "")
            for record in reversed(collections)
            if record.get("agent") == "codex_judge"
            and record.get("child_session_id") == attestation.get("child_session_id")
            and record.get("title") == attestation.get("title")
        ),
        "",
    )
    evidence_bytes = evidence.encode("utf-8") if isinstance(evidence, str) else b""
    answer_digest = hashlib.sha256(answer).hexdigest()
    attested_cycle = attestation.get("cycle")
    cursor_seen = False
    required_chain = False
    observed_nonmax_opus_effort = False
    if isinstance(attested_cycle, int) and 1 <= attested_cycle <= 4:
        for record in collections:
            if record.get("status") != "completed":
                continue
            title = str(record.get("title") or "")
            observation = record.get("opus_effort_observation")
            if (
                record.get("agent") == "opus_auditor"
                and re.search(
                    rf"(?:cycle-|repair-|internal-){attested_cycle}(?:-|$)",
                    title,
                )
                and isinstance(observation, dict)
                and observation.get("status") == "observed"
                and observation.get("compliant") is False
            ):
                observed_nonmax_opus_effort = True
            if title == f"cursor-cycle-{attested_cycle}":
                cursor_seen = True
            elif cursor_seen and title in {
                f"audit-cycle-{attested_cycle}",
                f"audit-format-repair-{attested_cycle}",
            } or (
                cursor_seen
                and re.fullmatch(
                    rf"audit-(?:cycle|format-repair)-{attested_cycle}-web-[1-2]",
                    title,
                )
                is not None
            ):
                required_chain = True
                break
    valid = (
        bool(answer)
        and required_chain
        and not observed_nonmax_opus_effort
        and attestation.get("answer_length") == len(answer)
        and isinstance(attestation.get("answer_sha256"), str)
        and secrets.compare_digest(answer_digest, attestation["answer_sha256"])
        and isinstance(attestation.get("voice_profile_sha256"), str)
        # With voice rendering off both sides are "", so this still pins the
        # attestation to the mode the run actually launched in.
        and secrets.compare_digest(
            current_profile_digest, attestation["voice_profile_sha256"]
        )
        and isinstance(attestation.get("evidence_packet_sha256"), str)
        and secrets.compare_digest(
            hashlib.sha256(evidence_bytes).hexdigest(),
            attestation["evidence_packet_sha256"],
        )
        and attestation.get("evidence_packet_length") == len(evidence_bytes)
        and isinstance(attestation.get("child_session_id"), str)
        and bool(attestation["child_session_id"])
    )
    if not valid:
        _ensure_terminal_failure(
            run_dir,
            child_code or EXIT_PIPELINE,
            "STAMP relay failed byte-integrity validation",
        )
        return EXIT_PIPELINE
    return 0


def _pending_dispatch(
    collections: list[dict[str, object]],
    dispatches: list[dict[str, object]],
) -> dict[str, object]:
    """Return the newest dispatch whose matching result was never collected."""

    for dispatch in reversed(dispatches):
        agent = dispatch.get("agent")
        title = dispatch.get("title")
        sent = sum(
            record.get("agent") == agent and record.get("title") == title
            for record in dispatches
        )
        collected = sum(
            record.get("agent") == agent and record.get("title") == title
            for record in collections
        )
        if sent > collected:
            return dispatch
    return {}


def _recover_budget_observation(run_dir: Path) -> dict[str, object]:
    """Recover the retained lower-bound cost when live policy observation abstained."""

    databases = sorted((run_dir / "tmp").glob("ap-chat-data-*/chat.db"))
    if len(databases) != 1:
        return {}
    try:
        from types import SimpleNamespace

        from omnigent.db.compression import decode
        from triple_stamp_isaac_launcher import _cost_snapshot_from_conversations

        connection = sqlite3.connect(databases[0])
        try:
            rows = connection.execute(
                """
                SELECT lower(hex(metadata.id)), metadata.sub_agent_name,
                       metadata.session_usage, conversations.session_overrides,
                       conversations.title
                FROM omnigent_conversation_metadata AS metadata
                JOIN conversations
                  ON conversations.workspace_id = metadata.workspace_id
                 AND conversations.id = metadata.id
                ORDER BY conversations.created_at
                """
            ).fetchall()
        finally:
            connection.close()
        conversations: list[object] = []
        unavailable: list[str] = []
        for session_id, agent, encoded_usage, overrides_text, title in rows:
            usage_text = decode(encoded_usage) if encoded_usage is not None else None
            usage = json.loads(usage_text) if usage_text else None
            overrides = json.loads(overrides_text) if overrides_text else {}
            if usage is None:
                unavailable.append(str(title or session_id))
                continue
            conversations.append(
                SimpleNamespace(
                    id=str(session_id),
                    sub_agent_name=agent,
                    session_usage=usage,
                    reported_model=(
                        overrides.get("reported_model")
                        if isinstance(overrides, dict)
                        else None
                    ),
                    model_override=None,
                )
            )
        snapshot = _cost_snapshot_from_conversations(conversations)
    except (
        ImportError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        sqlite3.Error,
    ):
        return {}
    if not snapshot.get("sessions"):
        return {}
    total = float(snapshot["total_usd"])
    maximum = 50.0
    return {
        "cost_usd": total,
        "reported_usd": float(snapshot["reported_usd"]),
        "estimated_unpriced_usd": float(snapshot["estimated_unpriced_usd"]),
        "remaining_usd": max(0.0, maximum - total),
        "max_cost_usd": maximum,
        "cost_observation_status": "partial" if unavailable else "complete",
        "unavailable_cost_sessions": unavailable,
        "cost_source": "retained_conversation_store",
    }


def _ensure_terminal_failure(
    run_dir: Path, child_code: int, reason_override: str = ""
) -> str:
    """Persist a deterministic sanitized failure when no STAMP exists."""

    failure_path = run_dir / "terminal-failure.txt"
    try:
        return failure_path.read_text(encoding="utf-8")
    except OSError:
        pass
    try:
        collections = [
            json.loads(line)
            for line in (run_dir / "routing-collections.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
    except (OSError, ValueError, json.JSONDecodeError):
        collections = []
    try:
        dispatches = [
            json.loads(line)
            for line in (run_dir / "routing-dispatches.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
    except (OSError, ValueError, json.JSONDecodeError):
        dispatches = []
    last = collections[-1] if collections else {}
    pending = _pending_dispatch(collections, dispatches)
    title = str(pending.get("title") or last.get("title") or "")
    cycle_match = re.search(r"(?:cycle-|-(?:opus|codex)-)([1-4])(?:-|$)", title)
    cycle = int(cycle_match.group(1)) if cycle_match else 0
    if title.startswith("cursor-cycle-"):
        next_stage = f"audit-cycle-{cycle}"
    elif title.startswith("audit-cycle-"):
        next_stage = f"judge-cycle-{cycle}"
    else:
        next_stage = "the next required stage"
    try:
        external_signal = json.loads(
            (run_dir / "external-signal.json").read_text(encoding="utf-8")
        )
        external_signal_name = (
            str(external_signal.get("signal") or "")
            if isinstance(external_signal, dict)
            else ""
        )
    except (OSError, ValueError, json.JSONDecodeError):
        external_signal_name = ""
    if reason_override:
        reason = reason_override
    elif external_signal_name:
        reason = (
            f"external {external_signal_name} interrupted the pipeline before "
            f"{next_stage} after collecting {title or 'no worker stage'}"
        )
    else:
        if pending:
            reason = (
                f"pipeline process exited with {title} in flight after collecting "
                f"{last.get('title') or 'no worker stage'}"
            )
        else:
            reason = (
                f"pipeline process exited before {next_stage} after collecting "
                f"{title or 'no worker stage'}"
            )
    if child_code and not external_signal_name:
        reason += f" (child exit {child_code})"
    budget: object = {}
    try:
        budget = json.loads(
            (run_dir / "budget-state.json").read_text(encoding="utf-8")
        )
        cost = budget.get("cost_usd")
    except (OSError, ValueError, json.JSONDecodeError):
        budget = _recover_budget_observation(run_dir)
        cost = budget.get("cost_usd") if isinstance(budget, dict) else None
    try:
        calls = len(
            (run_dir / "supervisor-tool-calls.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
    except OSError:
        calls = None
    reported = budget.get("reported_usd") if isinstance(budget, dict) else None
    estimated = (
        budget.get("estimated_unpriced_usd") if isinstance(budget, dict) else None
    )
    remaining = budget.get("remaining_usd") if isinstance(budget, dict) else None
    maximum = budget.get("max_cost_usd") if isinstance(budget, dict) else None
    observation_status = (
        str(budget.get("cost_observation_status") or "complete")
        if isinstance(budget, dict)
        else "unavailable"
    )
    unavailable_sessions = (
        budget.get("unavailable_cost_sessions", [])
        if isinstance(budget, dict)
        else []
    )
    missing_count = (
        len(unavailable_sessions)
        if isinstance(unavailable_sessions, list)
        else 0
    )

    def money(value: object) -> str:
        try:
            return f"${max(0.0, float(value)):.2f}"
        except (TypeError, ValueError):
            return "unavailable"

    observation_note = (
        f" observed partial lower bound ({missing_count} session"
        f"{'s' if missing_count != 1 else ''} unavailable);"
        if observation_status == "partial"
        else ";"
    )
    result = (
        f"PIPELINE_INFRASTRUCTURE_ERROR: stage {title or next_stage}, cycle {cycle}; "
        f"provider-reported {money(reported)} + conservative unpriced estimate "
        f"{money(estimated)} = {money(cost)}{observation_note} "
        f"remaining {money(remaining)} of "
        f"{money(maximum)}; continuation denied: {reason}"
    )
    attestation = {
        "version": 1,
        "result": "PIPELINE_INFRASTRUCTURE_ERROR",
        "stage": title,
        "cycle": cycle,
        "reason": reason,
        "cost_usd": cost,
        "reported_usd": reported,
        "estimated_unpriced_usd": estimated,
        "remaining_usd": remaining,
        "max_cost_usd": maximum,
        "calls": calls,
        "cost_observation_status": observation_status,
        "unavailable_cost_sessions": unavailable_sessions,
        "cost_source": (
            budget.get("cost_source")
            if isinstance(budget, dict)
            else None
        ),
    }
    for path, data in (
        (failure_path, result.encode("utf-8")),
        (
            run_dir / "failure-attestation.json",
            (
                json.dumps(attestation, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8"),
        ),
    ):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        except FileExistsError:
            continue
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
    return result


def _emit_attested_answer(run_dir: Path, args: list[str]) -> None:
    """Emit the exact attested bytes once for terminal one-shot mode."""

    if not _prompt_mode(args):
        return
    answer = (run_dir / "stamped-answer.bin").read_bytes()
    sys.stdout.buffer.write(answer)
    sys.stdout.buffer.flush()


def _emit_terminal_failure(run_dir: Path, args: list[str]) -> None:
    """Emit one exact sanitized failure for terminal one-shot mode."""

    if not _prompt_mode(args):
        return
    data = (run_dir / "terminal-failure.txt").read_bytes()
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _retain_runtime_diagnostics(
    *,
    models_started: bool,
    keep_runtime: bool,
    return_code: int | None,
) -> bool:
    del models_started
    return keep_runtime or return_code != 0


def _outer_main(root: Path, args: list[str]) -> int:
    if sys.platform != "darwin":
        _die("this bundle requires macOS Seatbelt")
    os.umask(0o077)
    provider = _provider()
    models, model_namespace, model_reason = _resolve_models(root, provider)
    _eprint(
        "triple-stamp: Claude model namespace "
        f"{model_namespace} ({model_reason}); launcher profile {provider}"
    )
    real_home = Path.home().resolve()
    voice_profile_sha256 = _validate_voice_profile(VOICE_PROFILE)
    tools = _resolve_toolchain(real_home, provider)
    host_env = _host_env(real_home)
    _ensure_claude_agent_sdk_pin(tools, host_env)
    _validate_versions(tools, host_env, provider)
    managed_python = (
        _ensure_isaac_runtime(tools, host_env, real_home)
        if provider == "databricks"
        else tools.omnigent_python
    )
    _ensure_plugin(root, tools, host_env)
    cursor_token = _cursor_token(tools, host_env)
    if provider == "databricks":
        omnigent_token, omnigent_expiry = _omnigent_remote_auth(tools, host_env)
        supervisor_provider = _supervisor_provider(real_home)
    else:
        omnigent_token, omnigent_expiry = "", 0.0
        supervisor_provider = {}

    root_hash = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    base_dir = Path("/tmp") / f"omnigent-triple-stamp-{os.getuid()}-{root_hash}"
    base_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = base_dir / "run.lock"
    run_dir, profile, runtime_env, run_id = _prepare_runtime(
        root,
        real_home,
        base_dir,
        tools,
        managed_python,
        cursor_token,
        omnigent_token,
        omnigent_expiry,
        supervisor_provider,
        voice_profile_sha256,
        provider,
        models,
    )
    runtime_args, browser_mode, translated_no_session = _browser_launch_args(args)
    if browser_mode:
        _configure_browser_runtime(runtime_env, root)
    if translated_no_session:
        _eprint(
            "triple-stamp: browser mode is ephemeral and host-backed; "
            "the private UI, host, and sessions will be removed on exit"
        )
    models_started = False
    run_return_code: int | None = None
    try:
        _sandbox_probe(tools, profile, runtime_env)
        # Workspace Cursor configuration is shared state. Hold the run lock
        # before removing stale managed entries or probing Cursor so a rejected
        # concurrent invocation cannot disrupt the active run.
        with _RunLock(lock_path):
            # Stale managed MCP entries make Cursor's TUI wait on
            # "Approve all servers" and prevent prompt-loop readiness.
            _remove_managed_cursor_files(root)
            _sandboxed_auth_preflight(
                root,
                run_dir,
                run_id,
                tools,
                profile,
                runtime_env,
            )
            _cleanup_stale_runs(base_dir, keep=run_dir)
            _remove_managed_cursor_files(root)
            hooks_snapshot = _snapshot_cursor_file(root, "hooks.json")
            mcp_snapshot = _snapshot_cursor_file(root, "mcp.json")
            try:
                models_started = True
                child_code = _spawn_sandboxed(
                    root,
                    profile,
                    runtime_env,
                    runtime_args,
                    tools,
                )
                run_return_code = _validated_pipeline_exit(run_dir, child_code, args)
                if run_return_code == 0 and not any(
                    arg in SELF_TEST_FLAGS for arg in args
                ):
                    _emit_attested_answer(run_dir, args)
                elif run_return_code != 0 and not any(
                    arg in SELF_TEST_FLAGS for arg in args
                ):
                    _emit_terminal_failure(run_dir, args)
                return run_return_code
            finally:
                survivors = _cleanup_runtime_services(
                    tools,
                    run_id,
                    run_dir,
                    runtime_env,
                )
                if survivors:
                    _eprint(
                        "triple-stamp: warning: processes survived marker cleanup:\n"
                        + "\n".join(survivors)
                    )
                _remove_managed_cursor_files(root, run_dir)
                _restore_cursor_file(root, "hooks.json", hooks_snapshot)
                _restore_cursor_file(root, "mcp.json", mcp_snapshot)
    finally:
        harness_tmp_value = runtime_env.get("OMNIGENT_HARNESS_TMP_PARENT")
        if harness_tmp_value:
            harness_tmp = Path(harness_tmp_value)
            if harness_tmp.is_symlink():
                harness_tmp.unlink(missing_ok=True)
        keep_runtime = os.environ.get("TRIPLE_STAMP_KEEP_RUNTIME") == "1"
        if _retain_runtime_diagnostics(
            models_started=models_started,
            keep_runtime=keep_runtime,
            return_code=run_return_code,
        ):
            _eprint(f"triple-stamp: retained private diagnostics at {run_dir}")
        else:
            shutil.rmtree(run_dir, ignore_errors=True)
    return 1


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    _check_safe_path(root, "bundle path")
    raw_args = sys.argv[1:]
    try:
        if os.environ.get("TRIPLE_STAMP_OUTER_SANDBOX") == "1":
            return _inner_validate(root, raw_args)
        args, self_test = _validate_cli_args(raw_args)
        return _outer_main(root, ["--self-test"] if self_test else args)
    except LaunchError as exc:
        _eprint(str(exc))
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
