"""Fail-closed first-run CLI preflight for the vanilla-Mac (`direct`) path.

The public Omnigent wheels install only the ``omni``/``omnigent`` commands. The
three coding CLIs the default ``direct`` provider drives -- Cursor Agent, Claude
Code, and Codex -- are installed and authenticated by the user. This module
checks that each of those CLIs is present and, for every one that is missing,
prints a single copy-paste install + login block that names the pinned model,
then exits non-zero so a first run fails closed with actionable guidance instead
of a bare ``127`` from deep inside the launcher.

Scope and non-goals:

- No Databricks host, proxy, Isaac, or gateway is referenced anywhere here; this
  is the public, outsider path only.
- It never weakens a model pin. A CLI that is installed but whose account lacks
  the pinned entitlement (Cursor Grok 4.6 Extra High, Claude Opus 5, the Codex
  model) still fails closed later in ``auth_preflight``, which names the model
  and the login command. This module only covers the earlier "you have not
  installed / logged in this CLI yet" case.
- It does not validate the Omnigent runtime version (0.12.x / 0.14.x); the
  ``triple-stamp`` entrypoint and ``triple_stamp_omnigent_compat`` own that.

Import-safe under ``python -I``: only the standard library, and no side effects
at import time.
"""

from __future__ import annotations

import os
import signal
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Prerequisite/config failure. Distinct, on purpose, from a bare shell 127: the
# message above it is a full copy-paste remedy, not a mystery "command not found".
EXIT_MISSING_TOOL = 78

# Cursor's model is a hard constant (see cursor_via_login.EXPECTED_ALIAS); it has
# no environment override. Claude/Codex mirror auth_preflight's env-overridable
# defaults so the model this block names is exactly the one preflight will later
# require.
CURSOR_MODEL_ID = "cursor-grok-4.6-xhigh"
CURSOR_MODEL_DISPLAY = "Cursor Grok 4.6 Extra High"
INSTALLER_URLS = {
    "cursor-agent": "https://cursor.com/install",
    "claude": "https://claude.ai/install.sh",
    "codex": "https://chatgpt.com/codex/install.sh",
}
INSTALL_TIMEOUT_SECONDS = 300
LOGIN_TIMEOUT_SECONDS = 300
LOGIN_ARGV = {
    "cursor-agent": ("cursor-agent", "login"),
    "claude": ("claude", "auth", "login"),
    "codex": ("codex", "login"),
}


def _opus_model() -> str:
    return os.environ.get("TRIPLE_STAMP_OPUS_MODEL", "claude-opus-5")


def _codex_model() -> str:
    return os.environ.get("TRIPLE_STAMP_CODEX_MODEL", "gpt-5.6-sol")


def _real_home() -> Path:
    return Path(os.environ.get("TRIPLE_STAMP_REAL_HOME") or Path.home())


@dataclass(frozen=True)
class Tool:
    """A user-installed CLI the ``direct`` provider requires."""

    key: str
    label: str
    binary: str
    search_paths: tuple[Path, ...]
    model_display: str
    model_id: str
    entitlement: str
    install: tuple[str, ...]
    login: str
    login_hint: str = ""


def tools(home: Path | None = None) -> list[Tool]:
    """Return the required CLIs and how to install + log in to each one."""

    base = home if home is not None else _real_home()
    local = base / ".local/bin"
    return [
        Tool(
            key="cursor-agent",
            label="Cursor Agent (Cursor CLI)",
            binary="cursor-agent",
            search_paths=(
                local / "cursor-agent",
                local / "agent",
                base / ".cursor/bin/cursor-agent",
                Path("/opt/homebrew/bin/cursor-agent"),
                Path("/usr/local/bin/cursor-agent"),
            ),
            model_display=CURSOR_MODEL_DISPLAY,
            model_id=CURSOR_MODEL_ID,
            entitlement="your Cursor account must be entitled to that model",
            install=("curl https://cursor.com/install -fsS | bash",),
            login="cursor-agent login",
        ),
        Tool(
            key="claude",
            label="Claude Code (claude)",
            binary="claude",
            search_paths=(
                local / "claude",
                Path("/opt/homebrew/bin/claude"),
                Path("/usr/local/bin/claude"),
            ),
            model_display="Claude Opus 5",
            model_id=_opus_model(),
            entitlement=(
                "your Claude account must be entitled to it "
                "(Claude Code needs a Pro/Max/Team/Enterprise/Console plan)"
            ),
            install=("curl -fsSL https://claude.ai/install.sh | bash",),
            login="claude",
            login_hint="complete the browser login on first run",
        ),
        Tool(
            key="codex",
            label="Codex (Codex CLI)",
            binary="codex",
            search_paths=(
                Path("/opt/homebrew/bin/codex"),
                Path("/usr/local/bin/codex"),
                local / "codex",
            ),
            model_display="the Codex model",
            model_id=_codex_model(),
            entitlement="your ChatGPT/OpenAI account must be entitled to it",
            install=("curl -fsSL https://chatgpt.com/codex/install.sh | sh",),
            login="codex login",
        ),
    ]


def is_present(tool: Tool) -> bool:
    """True when the CLI is on ``PATH`` or at a known install location.

    Deliberately lenient about *where* the binary lives so this friendly check
    does not fight the launcher's exact-path resolution (which is the strict,
    authoritative gate). If either the user's ``PATH`` or a canonical install
    directory has an executable, we consider the tool installed.
    """

    if shutil.which(tool.binary):
        return True
    return any(
        path.is_file() and os.access(path, os.X_OK) for path in tool.search_paths
    )


def missing_tools(home: Path | None = None) -> list[Tool]:
    return [tool for tool in tools(home) if not is_present(tool)]


def _resolved_tool(tool: Tool) -> Path | None:
    """Resolve an installed CLI without trusting a shell alias or function."""

    discovered = shutil.which(tool.binary)
    if discovered:
        return Path(discovered)
    for path in tool.search_paths:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def _normalize_cursor_alias(home: Path) -> None:
    """Give Cursor's home-local ``agent`` binary the stable launcher name."""

    cursor = home / ".local/bin/cursor-agent"
    agent = home / ".local/bin/agent"
    if cursor.exists() or not agent.is_file() or not os.access(agent, os.X_OK):
        return
    cursor.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    cursor.symlink_to(agent)


def _download_installer(url: str, destination: Path) -> None:
    try:
        result = subprocess.run(
            [
                "/usr/bin/curl",
                "--fail",
                "--location",
                "--silent",
                "--show-error",
                "--retry",
                "3",
                "--retry-delay",
                "1",
                "--connect-timeout",
                "10",
                "--max-time",
                "60",
                "--proto",
                "=https",
                "--tlsv1.2",
                url,
                "--output",
                str(destination),
            ],
            text=True,
            timeout=75,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("installer download timed out after 75s") from exc
    if result.returncode:
        raise RuntimeError(f"download failed with exit {result.returncode}")
    if not destination.is_file() or destination.stat().st_size < 100:
        raise RuntimeError("downloaded installer was empty or truncated")


def _subprocess_env(home: Path) -> dict[str, str]:
    """Keep install networking settings while withholding ambient credentials."""

    safe_credential_names = {
        "ALL_PROXY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "all_proxy",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
    secret_fragments = ("API_KEY", "CREDENTIAL", "PASSWORD", "SECRET", "TOKEN")
    env = {
        key: value
        for key, value in os.environ.items()
        if key in safe_credential_names
        or not any(fragment in key.upper() for fragment in secret_fragments)
    }
    env["HOME"] = str(home)
    env["PATH"] = f"{home}/.local/bin:{os.environ.get('PATH', '')}"
    return env


def _run_bounded(
    argv: list[str],
    *,
    timeout: int,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command in its own process group and reap every child on timeout."""

    process = subprocess.Popen(
        argv,
        env=env,
        text=True,
        start_new_session=True,
    )
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise
    return subprocess.CompletedProcess(argv, returncode)


def install_tool(tool: Tool, home: Path, stream=sys.stderr) -> Path:
    """Run a tool vendor's official per-user installer and return its binary."""

    if os.environ.get("TRIPLE_STAMP_BOOTSTRAP_OFFLINE") == "1":
        raise RuntimeError("offline mode is enabled")
    url = INSTALLER_URLS[tool.key]
    print(f"triple-stamp setup: installing {tool.label}...", file=stream, flush=True)
    with tempfile.TemporaryDirectory(prefix="triple-stamp-installer-") as value:
        installer = Path(value) / f"{tool.key}-install.sh"
        _download_installer(url, installer)
        env = _subprocess_env(home)
        if tool.key == "codex":
            env["CODEX_INSTALL_DIR"] = str(home / ".local/bin")
        interpreter = "/bin/sh" if tool.key == "codex" else "/bin/bash"
        try:
            result = _run_bounded(
                [interpreter, str(installer)],
                env=env,
                timeout=INSTALL_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"official installer timed out after {INSTALL_TIMEOUT_SECONDS}s"
            ) from exc
        if result.returncode:
            raise RuntimeError(
                f"official installer exited with status {result.returncode}"
            )
    if tool.key == "cursor-agent":
        _normalize_cursor_alias(home)
    resolved = _resolved_tool(tool)
    if resolved is None:
        raise RuntimeError("installer completed but the executable was not found")
    return resolved


def _run_login(argv: list[str], home: Path | None = None) -> None:
    try:
        result = _run_bounded(
            argv,
            env=_subprocess_env(home or _real_home()),
            timeout=LOGIN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"login timed out after {LOGIN_TIMEOUT_SECONDS}s; "
            "rerun interactively or set TRIPLE_STAMP_BOOTSTRAP_SKIP_LOGIN=1"
        ) from exc
    if result.returncode:
        raise RuntimeError(f"login exited with status {result.returncode}")


def _login(
    tool: Tool,
    executable: Path,
    stream=sys.stderr,
    *,
    home: Path | None = None,
) -> None:
    if os.environ.get("TRIPLE_STAMP_BOOTSTRAP_SKIP_LOGIN") == "1":
        return
    argv = list(LOGIN_ARGV[tool.key])
    argv[0] = str(executable)
    print(
        f"triple-stamp setup: sign in to {tool.label} in the browser window...",
        file=stream,
        flush=True,
    )
    _run_login(argv, home)


def _codex_external_auth(home: Path) -> bool:
    if any(
        os.environ.get(name)
        for name in ("OPENAI_API_KEY", "CODEX_API_KEY")
    ):
        return True
    config = home / ".codex/config.toml"
    try:
        text = config.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("model_provider") and "=" in stripped:
            value = stripped.split("=", 1)[1].strip().strip("\"'").lower()
            return bool(value and value not in {"openai", "chatgpt"})
    return False


def auth_ready(tool: Tool, executable: Path, home: Path) -> bool:
    commands = {
        "cursor-agent": [str(executable), "status"],
        "claude": [str(executable), "auth", "status"],
        "codex": [str(executable), "login", "status"],
    }
    try:
        result = subprocess.run(
            commands[tool.key],
            env={
                **os.environ,
                "HOME": str(home),
                "PATH": f"{home}/.local/bin:{os.environ.get('PATH', '')}",
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode == 0:
        return True
    return tool.key == "codex" and _codex_external_auth(home)


def ensure_logins(
    home: Path,
    stream=sys.stderr,
    *,
    skip: frozenset[str] = frozenset(),
) -> int:
    """Open vendor login flows only for CLIs with no usable authentication."""

    if os.environ.get("TRIPLE_STAMP_BOOTSTRAP_SKIP_LOGIN") == "1":
        return 0
    for tool in tools(home):
        if tool.key in skip:
            continue
        executable = _resolved_tool(tool)
        if executable is None:
            return EXIT_MISSING_TOOL
        if auth_ready(tool, executable, home):
            continue
        try:
            _login(tool, executable, stream, home=home)
        except (OSError, RuntimeError) as exc:
            print(
                f"triple-stamp setup: {tool.label} login failed: {exc}",
                file=stream,
            )
            return EXIT_MISSING_TOOL
        if not auth_ready(tool, executable, home):
            print(
                f"triple-stamp setup: {tool.label} login completed but "
                "authentication is still unavailable",
                file=stream,
            )
            return EXIT_MISSING_TOOL
    return 0


def install_missing(home: Path | None = None, stream=sys.stderr) -> int:
    """Install every missing public CLI and complete each new CLI's login."""

    base = home if home is not None else _real_home()
    _normalize_cursor_alias(base)
    missing = missing_tools(base)
    if not missing:
        return ensure_logins(base, stream)
    failures: list[str] = []
    installed: list[tuple[Tool, Path]] = []
    for tool in missing:
        try:
            installed.append((tool, install_tool(tool, base, stream)))
        except (OSError, RuntimeError) as exc:
            failures.append(f"{tool.label}: {exc}")
    if failures:
        print(
            "triple-stamp setup: automatic CLI installation failed:\n  "
            + "\n  ".join(failures),
            file=stream,
        )
        print(_footer(), file=stream, end="")
        return EXIT_MISSING_TOOL
    for tool, executable in installed:
        if auth_ready(tool, executable, base):
            continue
        try:
            _login(tool, executable, stream, home=base)
        except (OSError, RuntimeError) as exc:
            print(
                f"triple-stamp setup: {tool.label} was installed but login failed: {exc}",
                file=stream,
            )
            return EXIT_MISSING_TOOL
        if not auth_ready(tool, executable, base):
            print(
                f"triple-stamp setup: {tool.label} login completed but "
                "authentication is still unavailable",
                file=stream,
            )
            return EXIT_MISSING_TOOL
    remaining = missing_tools(base)
    if remaining:
        print(
            "triple-stamp setup: these CLIs are still unavailable: "
            + ", ".join(tool.label for tool in remaining),
            file=stream,
        )
        return EXIT_MISSING_TOOL
    if ensure_logins(
        base,
        stream,
        skip=frozenset(tool.key for tool, _executable in installed),
    ):
        return EXIT_MISSING_TOOL
    print("triple-stamp setup: required CLIs are installed.", file=stream)
    return 0


def format_block(tool: Tool) -> str:
    """One copy-paste install + login block for a single missing CLI."""

    login_line = tool.login
    if tool.login_hint:
        login_line = f"{tool.login}            # {tool.login_hint}"
    commands = "\n".join(f"    {line}" for line in (*tool.install, login_line))
    return (
        f"[missing] {tool.label} is not installed or not on your PATH.\n"
        f"  Triple-stamp pins {tool.model_display} ({tool.model_id}); "
        f"{tool.entitlement}.\n"
        f"  Install and log in (copy-paste):\n\n"
        f"{commands}\n"
    )


def _header() -> str:
    return (
        "triple-stamp: missing required CLI(s) for the default `direct` provider.\n"
        "Triple-stamp needs Omnigent 0.12 or 0.14 plus Cursor Agent, Claude Code, "
        "and Codex on macOS.\n"
        "The Omnigent wheel installs only `omni`/`omnigent`; you install and log in "
        "to the CLIs below, then run ./triple-stamp again.\n"
    )


def _footer() -> str:
    return (
        "\nAfter installing and logging in, verify the whole install with:\n"
        "    ./triple-stamp --self-test\n"
    )


def report(home: Path | None = None, stream=sys.stderr) -> int:
    """Print a block per missing CLI and return a fail-closed exit code.

    Returns ``0`` when every required CLI is present (nothing printed), otherwise
    ``EXIT_MISSING_TOOL`` after printing all missing-tool blocks at once so the
    user sees everything they need in a single pass.
    """

    missing = missing_tools(home)
    if not missing:
        return 0
    print(_header(), file=stream)
    for tool in missing:
        print(format_block(tool), file=stream)
    print(_footer(), file=stream, end="")
    return EXIT_MISSING_TOOL


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv or [])
    if arguments == ["--install"]:
        return install_missing()
    if arguments:
        print("usage: outsider_preflight.py [--install]", file=sys.stderr)
        return 64
    return report()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
