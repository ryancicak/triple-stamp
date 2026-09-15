"""Strict, read-only Opus MCP configuration."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

OPUS_MODEL = os.environ.get("TRIPLE_STAMP_OPUS_MODEL", "claude-opus-5")
OPUS_MCP_NAMES = ("glean", "slack", "confluence", "jira", "safe")
# The exact prefix the judge prompt is told to look for. The runtime emits it and
# validate_bundle asserts both halves against this one constant, because the two
# drifting apart is not a hypothetical: rewording the runtime line to
# "voice rendering." while the prompt still said "voice rendering:" made Codex
# miss the marker, report voice as disabled on a voice-enabled run, and lose a
# complete STAMP to a format repair that could not fix it.
VOICE_NOTE_PREFIX = "[System-configured voice rendering:"
OPUS_STARTUP_ENV = {
    # Claude Code 2.1.263 documents these controls. They disable background
    # updates, non-essential traffic, and telemetry without changing model or
    # direct stdio MCP authentication.
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_TELEMETRY": "1",
    "DBEXEC_NO_CERT_REFRESH": "1",
    # MCP schemas are deferred behind ToolSearch, which the audit prompt depends
    # on to discover the exact read tool per family. On this host the flag lives
    # only in Claude's managed settings, and the Opus launch excludes those with
    # `--setting-sources ""`, so it has to be supplied here or Opus starts with
    # no way to reach any internal system.
    "ENABLE_TOOL_SEARCH": "true",
    # Same reason, and the other half of the same bug. These five servers are
    # `dbexec` shims that unpack a pex runtime on first use, which takes far
    # longer than Claude's default 30s connect budget; a cold run then reports
    # every family as `MCP error -32000: Connection closed`, ToolSearch answers
    # "no matching deferred tools", and the audit records zero internal coverage
    # while a warm run on the same code reaches all five. Managed settings carry
    # a 420000ms tool timeout that `--setting-sources ""` discards, and there is
    # no managed value at all for the connect budget, so both are set here.
    "MCP_TIMEOUT": "120000",
    "MCP_TOOL_TIMEOUT": "420000",
}
if os.environ.get("TRIPLE_STAMP_PROVIDER", "direct") == "databricks":
    OPUS_STARTUP_ENV.update(
        {
            "ISAAC_DEFAULT_UCODE": "0",
            "ISAAC_DISABLE_MAC_MANAGED_SETTINGS_UPDATE": "1",
            "ISAAC_LAUNCH_MODE": "omni",
        }
    )

# `dontAsk` denies anything not pre-approved, and the Opus launch passes
# `--setting-sources ""`, so managed settings contribute nothing: not their
# `permissions.allow` list and not their env. This tuple is therefore the whole
# allowlist, not a supplement to a host allowlist, and it has to name every read
# tool the audit protocol in agents/opus_auditor/config.yaml tells Opus to call.
# Listing only the tools managed settings happened to omit left four of the five
# families with no callable tool, so every audit reported zero internal coverage
# and failed mechanical validation. Writes stay blocked by WRITE_TOOLS_DENIED
# because deny beats allow. Keep this in step with that config's selector list.
READ_ONLY_ALLOWED_TOOLS = (
    "ToolSearch",
    "mcp__confluence__get_confluence_page_comments",
    "mcp__confluence__get_confluence_page_content",
    "mcp__confluence__list_confluence_page_versions",
    "mcp__confluence__search_confluence_pages",
    "mcp__glean__get_document_content",
    "mcp__glean__glean_chat",
    "mcp__glean__search",
    "mcp__jira__jira_read_api_call",
    "mcp__safe__safe_read_api_call",
    "mcp__slack__slack_batch_read_api_call",
    "mcp__slack__slack_read_api_call",
)

WRITE_TOOLS_DENIED = (
    "mcp__glean__create_go_link",
    "mcp__slack__slack_write_api_call",
    "mcp__slack__slack_batch_write_api_call",
    "mcp__jira__jira_write_api_call",
    "mcp__confluence__create_confluence_page",
    "mcp__confluence__update_confluence_page",
    "mcp__confluence__reply_to_confluence_comment",
    "mcp__safe__safe_write_api_call",
    "mcp__safe__safe_merge_api_call",
)

_AUTH_BEARER = re.compile(
    r"(?i)(authorization)(\s*[:=]\s*bearer\s+)([^\s,;]+)"
)
_SECRET = re.compile(
    r"(?i)(authorization|bearer|access[_ -]?token|refresh[_ -]?token|"
    r"api[_ -]?key|password|secret|cookie)(\s*[:=]\s*|\s+)([^\s,;]+)"
)
class OpusConfigurationError(RuntimeError):
    """A local strict-MCP configuration error, sanitized for the supervisor."""

    def __init__(self, reason: str) -> None:
        self.reason = _sanitize(reason)
        super().__init__(self.reason)


def _sanitize(value: str, *, limit: int = 800) -> str:
    text = _AUTH_BEARER.sub(
        r"\1\2[REDACTED]", value.replace("\n", " ").strip()
    )
    text = _SECRET.sub(r"\1\2[REDACTED]", text)
    text = re.sub(r"(?i)(https?://[^?\s]+)\?[^\s]+", r"\1?[REDACTED]", text)
    if len(text) > limit:
        text = text[:300] + " … " + text[-(limit - 303) :]
    return text or "no diagnostic"


def configure_opus_startup_environment() -> dict[str, str]:
    """Apply supported Opus-only startup controls to the launching process."""

    for name, value in OPUS_STARTUP_ENV.items():
        os.environ[name] = value
    return dict(OPUS_STARTUP_ENV)


def _run_dir() -> Path:
    raw = os.environ.get("TRIPLE_STAMP_RUN_DIR", "")
    if not raw:
        raise OpusConfigurationError(
            "run directory is missing at the Opus launch boundary"
        )
    return Path(raw).resolve()


def load_generated_mcp_config(home: Path | None = None) -> dict[str, Any]:
    """Copy intended definitions without connecting to any MCP server."""

    path = (home or Path.home()) / ".claude.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        servers = raw["mcpServers"]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OpusConfigurationError(
            f"generated Claude MCP configuration is unreadable: {exc}"
        ) from exc
    selected: dict[str, Any] = {}
    for name in OPUS_MCP_NAMES:
        entry = servers.get(name) if isinstance(servers, dict) else None
        if not isinstance(entry, dict):
            raise OpusConfigurationError(
                f"direct MCP definition {name!r} is absent from generated .claude.json"
            )
        if (
            entry.get("type") != "stdio"
            or entry.get("command") != "dbexec"
            or entry.get("args") != ["repo", "run", "mcp", "start-single", name]
        ):
            raise OpusConfigurationError(
                f"direct MCP definition {name!r} does not match the strict stdio contract"
            )
        selected[name] = entry
    return {"mcpServers": selected}


def materialize_run_config(
    run_dir: Path,
    config: dict[str, Any],
) -> Path:
    """Create one immutable per-run config; perform no server I/O."""

    data = (
        json.dumps(
            config,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    path = run_dir / "opus-mcp.json"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise OpusConfigurationError(
                f"run-scoped MCP config is unreadable: {exc}"
            ) from exc
        if existing != data:
            raise OpusConfigurationError(
                "run-scoped MCP config changed within this run"
            )
    else:
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        path.chmod(0o400)
    if stat.S_IMODE(path.stat().st_mode) != 0o400:
        raise OpusConfigurationError(
            "run-scoped MCP config is not immutable mode 0400"
        )
    return path


def prepare_opus_mcp_config() -> Path:
    """Materialize definitions only; unavailability never gates launch."""

    return materialize_run_config(_run_dir(), load_generated_mcp_config())
