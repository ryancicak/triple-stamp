"""No-inference authentication preflight inside the real runtime sandbox."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import tomllib

EXPECTED_CURSOR = "cursor-grok-4.6-xhigh - Cursor Grok 4.6 Extra High"
EXPECTED_SUPERVISOR = os.environ.get(
    "TRIPLE_STAMP_SUPERVISOR_MODEL", "claude-sonnet-4-6"
)
EXPECTED_OPUS = os.environ.get("TRIPLE_STAMP_OPUS_MODEL", "claude-opus-5")
EXPECTED_CODEX = os.environ.get("TRIPLE_STAMP_CODEX_MODEL", "gpt-5.6-sol")
EXPECTED_OPUS_STARTUP_ENV = {
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_TELEMETRY": "1",
}
MIN_DBCERT_SECONDS = 3600
MIN_MODEL_TOKEN_SECONDS = 1800
_AUTH_BEARER = re.compile(
    r"(?i)\b(authorization)(\s*[:=]\s*bearer\s+)([^\s,;]+)"
)
_SECRET = re.compile(
    r"(?i)\b(authorization|bearer|access[_ -]?token|refresh[_ -]?token|"
    r"api[_ -]?key|password|secret)\b(\s*[:=]\s*|\s+)([^\s,;]+)"
)
_BASE_URL = re.compile(
    r"(?i)\b(ANTHROPIC_BASE_URL)(\s*[:=]\s*)([^\s,;]+)"
)


class PreflightError(RuntimeError):
    def __init__(self, stage: str, detail: str, remediation: str) -> None:
        super().__init__(detail)
        self.stage = stage
        self.detail = detail
        self.remediation = remediation


def _sanitize(text: str, *, limit: int = 2000) -> str:
    value = text[-limit:].strip()
    value = _AUTH_BEARER.sub(r"\1\2[REDACTED]", value)
    value = _BASE_URL.sub(r"\1\2[REDACTED]", value)
    return _SECRET.sub(r"\1\2[REDACTED]", value)


def _claude_namespace() -> str:
    configured = os.environ.get("TRIPLE_STAMP_CLAUDE_NAMESPACE", "").strip()
    if configured:
        return configured
    models = (EXPECTED_SUPERVISOR, EXPECTED_OPUS)
    if all(model.startswith("system.ai.") for model in models):
        return "databricks_gateway"
    if all(not model.startswith("system.ai.") for model in models):
        return "public_anthropic"
    return "mixed_or_custom"


def _claude_model_failure(
    *,
    stage: str,
    model: str,
    detail: str,
    default_remediation: str,
) -> PreflightError:
    namespace = _claude_namespace()
    gateway_forms = {
        "claude-sonnet-4-6": "system.ai.claude-sonnet-4-6[1m]",
        "claude-opus-5": "system.ai.claude-opus-5[1m]",
    }
    public_forms = {value: key for key, value in gateway_forms.items()}
    if namespace == "databricks_gateway" and model in gateway_forms:
        remediation = (
            f"use gateway model {gateway_forms[model]} for the detected "
            "Databricks gateway namespace; reauthentication will not fix "
            "this model-ID mismatch"
        )
    elif namespace == "public_anthropic" and model in public_forms:
        remediation = (
            f"use public Anthropic model {public_forms[model]} for the detected "
            "public namespace; reauthentication will not fix this model-ID mismatch"
        )
    else:
        remediation = default_remediation
    return PreflightError(
        stage,
        f"model preflight failed; attempted {model!r}; detected namespace "
        f"{namespace}: {detail}",
        remediation,
    )


def _run(argv: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(os.environ),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 127, "", type(exc).__name__)


def _result_detail(result: subprocess.CompletedProcess[str]) -> str:
    return _sanitize(result.stderr or result.stdout) or f"exit {result.returncode}"


def _dbcert_seconds(payload: object) -> int:
    if not isinstance(payload, dict):
        return 0
    try:
        values = (
            payload["x509_certificate"]["validity_duration"],
            payload["ssh_certificate"]["validity_duration"],
        )
        seconds = [int(value[:-1]) for value in values if value.endswith("s")]
    except (KeyError, TypeError, ValueError):
        return 0
    return min(seconds) if len(seconds) == 2 else 0


def _dbcert_status(dbcert: str) -> int:
    result = _run([dbcert, "status", "--output", "json"], timeout=30)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return 0
    return _dbcert_seconds(payload) if result.returncode == 0 else 0


def _preflight_dbcert() -> None:
    dbcert = "/usr/local/bin/dbcert"
    if _dbcert_status(dbcert) >= MIN_DBCERT_SECONDS:
        return
    refresh_env = dict(os.environ)
    refresh_env["NO_OPEN_BROWSER"] = "1"
    try:
        subprocess.run(
            [dbcert, "--force", "--update-kubeconfig=false"],
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
            env=refresh_env,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    if _dbcert_status(dbcert) < MIN_DBCERT_SECONDS:
        raise PreflightError(
            "Opus",
            "dbcert is missing, invalid, or expires in under one hour",
            "/usr/local/bin/dbcert --force --update-kubeconfig=false",
        )


def _model_token_seconds() -> int:
    path = Path.home() / ".databricks/model-serving-token.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw = payload["expires_at"]["__datetime__"]
        expiry = datetime.fromisoformat(raw)
        now = datetime.now(tz=expiry.tzinfo)
        return int((expiry - now).total_seconds())
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return 0


def _refresh_model_token(isaac: str) -> None:
    _run([isaac, "auth", "refresh"], timeout=60)
    if _model_token_seconds() < MIN_MODEL_TOKEN_SECONDS:
        raise PreflightError(
            "Opus",
            "Isaac model-serving OAuth cache is unavailable or expires in under 30 minutes",
            "/usr/local/bin/isaac --claude",
        )


def _real_home() -> Path:
    """The user's actual home, not the per-run sandbox HOME.

    Remediation text is for a human to paste into their own shell, and inside the
    outer Seatbelt `Path.home()` resolves to the run-scoped directory, so the
    printed command pointed at a path that vanishes with the run. The launcher
    already exports the real one.
    """

    return Path(os.environ.get("TRIPLE_STAMP_REAL_HOME") or Path.home())


def _preflight_cursor(root: Path) -> None:
    wrapper = str(root / ".omnigent/cursor-via-login")
    status = _run([wrapper, "status"], timeout=30)
    models = _run([wrapper, "models"], timeout=30)
    config = _run([wrapper, "--triple-stamp-config-preflight"], timeout=30)
    startup = _run([wrapper, "--triple-stamp-startup-preflight"], timeout=30)
    if (
        status.returncode
        or models.returncode
        or config.returncode
        or startup.returncode
        or EXPECTED_CURSOR not in models.stdout
    ):
        detail = next(
            (
                _result_detail(result)
                for result in (status, models, config, startup)
                if result.returncode
            ),
            "Cursor Grok 4.6 Extra High is not in the Cursor account model catalog",
        )
        raise PreflightError(
            "Cursor",
            detail,
            f"{_real_home() / '.local/bin/cursor-agent'} login",
        )


def _preflight_opus(isaac: str) -> None:
    missing = [
        name
        for name, expected in EXPECTED_OPUS_STARTUP_ENV.items()
        if os.environ.get(name) != expected
    ]
    if missing:
        raise PreflightError(
            "Opus",
            "supported no-update/no-telemetry controls were stripped: "
            + ", ".join(missing),
            "restart the triple-stamp server with the current project launcher",
        )
    result = _run(
        [
            isaac,
            "--",
            "--model",
            EXPECTED_OPUS,
            "--effort",
            "max",
            "--version",
        ],
        timeout=90,
    )
    if result.returncode or "Claude Code" not in (result.stdout + result.stderr):
        raise _claude_model_failure(
            stage="Opus",
            model=EXPECTED_OPUS,
            detail=(
                "Isaac could not prepare Claude Code with the pinned selector: "
                + _result_detail(result)
            ),
            default_remediation="/usr/local/bin/isaac --claude",
        )


def _preflight_supervisor_sdk() -> None:
    """Prove the SDK supervisor's Isaac gateway token path without inference."""

    try:
        from omnigent.runtime.workflow import _build_claude_sdk_spawn_env
        from omnigent.spec.parser import parse

        bundle = Path(os.environ["TRIPLE_STAMP_BUNDLE"]).resolve()
        spec = parse(bundle)
        env = _build_claude_sdk_spawn_env(spec, cwd=Path.cwd())
    except Exception as exc:
        raise PreflightError(
            "Supervisor",
            "could not resolve the isolated Claude SDK/Isaac gateway: "
            + _sanitize(str(exc)),
            "/usr/local/bin/isaac --claude",
        ) from exc

    auth_command = env.get("HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND", "")
    base_url = env.get("HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL", "")
    healthy = (
        env.get("HARNESS_CLAUDE_SDK_GATEWAY") == "true"
        and env.get("HARNESS_CLAUDE_SDK_MODEL") == EXPECTED_SUPERVISOR
        and env.get("HARNESS_CLAUDE_SDK_PERMISSION_MODE") == "auto"
        and env.get("HARNESS_CLAUDE_SDK_SKILLS_FILTER") == '"none"'
        and base_url.startswith("https://")
        and "/ai-gateway/anthropic" in base_url
        and bool(auth_command)
    )
    if not healthy:
        raise PreflightError(
            "Supervisor",
            "isolated Claude SDK did not resolve the pinned Isaac Databricks gateway",
            "/usr/local/bin/isaac --claude",
        )
    token = _run(["/bin/sh", "-c", auth_command], timeout=30)
    if token.returncode or len(token.stdout.strip()) < 20:
        raise PreflightError(
            "Supervisor",
            "Claude SDK's Isaac Databricks gateway token helper is not ready",
            "/usr/local/bin/isaac --claude",
        )


def _codex_config() -> tuple[list[str], str]:
    path = Path.home() / ".codex/config.toml"
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
        provider = payload["model_providers"]["Databricks"]
        auth = provider["auth"]
        command = auth["command"]
        args = auth["args"]
        models = payload["tui"]["model_availability_nux"]
        base_url = provider["base_url"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        return [], ""
    executable = shutil.which(command) if isinstance(command, str) else None
    if (
        not executable
        or not isinstance(args, list)
        or not all(isinstance(value, str) for value in args)
        or EXPECTED_CODEX not in models
        or not isinstance(base_url, str)
        or not base_url.startswith("https://")
    ):
        return [], ""
    return [executable, *args], base_url


def _preflight_codex(root: Path) -> None:
    token_command, _base_url = _codex_config()
    if not token_command:
        raise PreflightError(
            "Codex",
            "isolated Codex Databricks provider/profile configuration is incomplete",
            "/usr/local/bin/isaac codex --no-omni",
        )
    token = _run(token_command, timeout=30)
    if token.returncode or len(token.stdout.strip()) < 20:
        raise PreflightError(
            "Codex",
            "Isaac's Codex Databricks token provider is not ready",
            "/usr/local/bin/isaac codex --no-omni",
        )
    version = _run(
        [
            str(root / ".omnigent/codex-via-isaac"),
            "--model",
            EXPECTED_CODEX,
            "--version",
        ],
        timeout=90,
    )
    if version.returncode or "codex-cli" not in (version.stdout + version.stderr):
        raise PreflightError(
            "Codex",
            "Isaac could not prepare Codex with gpt-5.6-sol/ultra: "
            + _result_detail(version),
            "/usr/local/bin/isaac codex --no-omni",
        )


def _direct_round_trip(root: Path) -> None:
    claude = shutil.which("claude")
    codex_wrapper = os.environ.get(
        "OMNIGENT_CODEX_PATH",
        str(root / ".omnigent/codex-launch"),
    )
    if not claude or not os.access(claude, os.X_OK):
        raise PreflightError(
            "Claude",
            "plain Claude executable is unavailable",
            "install Claude Code and authenticate it",
        )
    if not os.path.isfile(codex_wrapper) or not os.access(codex_wrapper, os.X_OK):
        raise PreflightError(
            "Codex",
            "plain Codex wrapper is unavailable",
            "install Codex CLI and keep .omnigent/codex-launch executable",
        )
    claude_result = _run(
        [
            claude,
            "--model",
            EXPECTED_OPUS,
            "--effort",
            "max",
            "--print",
            "Reply with exactly: DIRECT_CLAUDE_OK",
        ],
        timeout=90,
    )
    if (
        claude_result.returncode
        or claude_result.stdout.strip() != "DIRECT_CLAUDE_OK"
    ):
        raise _claude_model_failure(
            stage="Claude",
            model=EXPECTED_OPUS,
            detail="plain Claude round trip failed: " + _result_detail(claude_result),
            default_remediation=(
                "authenticate Claude Code for the resolved model namespace"
            ),
        )
    codex_result = _run(
        [
            codex_wrapper,
            "--model",
            EXPECTED_CODEX,
            "exec",
            "--skip-git-repo-check",
            "Reply with exactly: DIRECT_CODEX_OK",
        ],
        timeout=120,
    )
    if codex_result.returncode or "DIRECT_CODEX_OK" not in codex_result.stdout:
        raise PreflightError(
            "Codex",
            f"plain Codex round trip failed for {EXPECTED_CODEX}/ultra: "
            + _result_detail(codex_result),
            (
                "authenticate Codex for the configured direct-profile model; "
                "gpt-5.6-sol availability is account-dependent and fails closed"
            ),
        )


def main() -> int:
    root = Path(os.environ.get("TRIPLE_STAMP_ROOT", "")).resolve()
    run_dir = Path(os.environ.get("TRIPLE_STAMP_RUN_DIR", "")).resolve()
    if (
        os.environ.get("TRIPLE_STAMP_OUTER_SANDBOX") != "1"
        or not root.is_dir()
        or not run_dir.is_dir()
        or Path.home().resolve() != (run_dir / "home").resolve()
        or Path(os.environ.get("KUBECONFIG", "")).resolve() != (run_dir / "kube/config").resolve()
    ):
        print("triple-stamp preflight: runtime isolation metadata is invalid", file=sys.stderr)
        return 78
    try:
        _preflight_cursor(root)
        provider = os.environ.get("TRIPLE_STAMP_PROVIDER", "direct")
        if provider == "databricks":
            _preflight_dbcert()
            isaac = os.environ["ISAAC_BIN"]
            _refresh_model_token(isaac)
            _preflight_supervisor_sdk()
            _preflight_opus(isaac)
            _preflight_codex(root)
        elif provider == "direct":
            _direct_round_trip(root)
        else:
            raise PreflightError(
                "Provider",
                f"unsupported provider profile: {provider!r}",
                "set TRIPLE_STAMP_PROVIDER=direct or databricks",
            )
    except PreflightError as exc:
        print(f"triple-stamp preflight [{exc.stage}]: {exc.detail}", file=sys.stderr)
        print(f"Remediation: {exc.remediation}", file=sys.stderr)
        return 77
    except (KeyError, OSError, ValueError) as exc:
        print(
            "triple-stamp preflight: runtime prerequisite failed: "
            + _sanitize(str(exc)),
            file=sys.stderr,
        )
        return 78
    print("triple-stamp auth preflight: PASS")
    print(
        "  Cursor: CLI login + cursor-grok-4.6-xhigh + "
        "non-paid startup acknowledgement"
    )
    if provider == "databricks":
        print("  Supervisor: Claude SDK + Isaac Databricks gateway token")
        print(
            "  Opus: dbcert + model-serving OAuth + selector/config prerequisites; "
            "MCP servers are optional at audit time"
        )
        print("  Codex: Databricks token provider + gpt-5.6-sol/ultra selector")
    else:
        print(f"  Claude: direct round trip + {EXPECTED_OPUS}")
        print(f"  Codex: direct round trip + {EXPECTED_CODEX}/ultra")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
