"""Build the inherited triple-stamp Seatbelt profile from Omnigent's baseline."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import yaml
from omnigent.inner.sandbox import SandboxPolicy
from omnigent.inner.seatbelt_sandbox import _build_profile
from omnigent.spec.parser import parse


def _paths(values: set[str]) -> list[Path]:
    return sorted((Path(value).expanduser().resolve(strict=False) for value in values), key=str)


def main() -> None:
    if len(sys.argv) != 8:
        raise SystemExit(
            "usage: build_outer_seatbelt.py "
            "ROOT PROFILE RUNTIME_DIR RUNTIME_BUNDLE REAL_HOME VOICE_PROFILE "
            "CLI_READ_DIRS_JSON"
        )

    root = Path(sys.argv[1]).resolve()
    profile_path = Path(sys.argv[2]).resolve()
    runtime_dir = Path(sys.argv[3]).resolve()
    runtime_bundle = Path(sys.argv[4]).resolve()
    real_home = Path(sys.argv[5]).resolve()
    # Empty means voice rendering is off, so no read grant is needed at all.
    voice_argument = sys.argv[6].strip()
    voice_profile = Path(voice_argument).resolve() if voice_argument else None
    try:
        raw_cli_read_dirs = json.loads(sys.argv[7])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"triple-stamp: CLI read directories are malformed: {exc}"
        ) from exc
    if (
        not isinstance(raw_cli_read_dirs, list)
        or not 1 <= len(raw_cli_read_dirs) <= 3
        or not all(
            isinstance(value, str)
            and value
            and Path(value).is_absolute()
            and Path(value).is_dir()
            for value in raw_cli_read_dirs
        )
    ):
        raise SystemExit("triple-stamp: CLI read directories are invalid")
    cli_read_dirs = {str(Path(value).resolve()) for value in raw_cli_read_dirs}
    if voice_profile is not None and not voice_profile.is_file():
        raise SystemExit(
            f"triple-stamp: configured voice profile is unavailable: {voice_profile}"
        )
    bundle = parse(root)
    agents = (bundle, *bundle.sub_agents)

    # Source files retain an internal Seatbelt so unsupported bare Omnigent
    # launches fail closed. The supported launcher creates one inherited outer
    # profile and only then generates a runtime copy with nested sandboxes off.
    for agent in agents:
        sandbox = agent.os_env.sandbox
        if sandbox is None or sandbox.type != "darwin_seatbelt":
            raise SystemExit(
                f"triple-stamp: {agent.name} source sandbox is not fail-closed Seatbelt"
            )

    stable_tool_root = Path(sys.executable).resolve().parents[1]
    venv_root = Path(sys.prefix)
    invoked_venv = Path(sys.executable).parent.parent
    read_paths: set[str] = {
        str(root),
        str(runtime_dir),
        str(stable_tool_root),
        str(venv_root),
        str(venv_root.resolve(strict=False)),
        str(invoked_venv),
        str(invoked_venv.resolve(strict=False)),
        str(real_home / ".cache/isaac"),
        str(real_home / ".local/bin"),
        str(real_home / ".local/share"),
        str(real_home / ".claude/plugins"),
        str(real_home / ".claude/skills"),
        str(real_home / ".config/llm-cli/hooks"),
        str(real_home / ".vibe/marketplace"),
        str(real_home / "Library/Caches/dbexec"),
        "/Applications/Cursor.app",
    }
    read_paths.update(cli_read_dirs)
    if voice_profile is not None:
        read_paths.add(str(voice_profile))
    write_paths: set[str] = {
        str(root),
        str(runtime_dir),
        f"/tmp/claude-{os.getuid()}",
    }
    harness_tmp = os.environ.get("OMNIGENT_HARNESS_TMP_PARENT", "").strip()
    if harness_tmp:
        harness_path = Path(harness_tmp)
        if harness_path.is_absolute():
            # Keep the short /tmp/ots-* spelling. Cursor bind()s the lexical
            # CURSOR_DATA_DIR path; resolving the symlink would only re-grant
            # runtime_dir, which is already present.
            read_paths.add(str(harness_path))
            write_paths.add(str(harness_path))
    write_files: set[str] = set()

    policy = SandboxPolicy(
        backend_type="darwin_seatbelt",
        active=True,
        read_roots=_paths(read_paths),
        write_roots=_paths(write_paths),
        write_files=_paths(write_files),
        allow_network=True,
        cwd_allow_hidden=[
            ".claude",
            ".codex",
            ".config",
            ".cursor",
            ".databricks",
            ".dbcert",
            ".kube",
            ".local",
            ".omnigent",
            ".venv",
        ],
        cwd_hidden_scan_overflow="error",
        cwd_hidden_scan_recursive=False,
        env_passthrough=[
            "KUBECONFIG",
            "TRIPLE_STAMP_OUTER_SANDBOX",
            "TRIPLE_STAMP_RUN_ID",
            "TRIPLE_STAMP_CURSOR_HOME",
        ],
    )
    provider = os.environ.get("TRIPLE_STAMP_PROVIDER", "direct")
    if provider == "databricks":
        launch_executable = os.environ["ISAAC_BIN"]
    elif provider == "direct":
        launch_executable = os.environ["STABLE_OMNIGENT_BIN"]
    else:
        raise SystemExit(f"triple-stamp: unsupported provider profile {provider!r}")
    profile = _build_profile(policy, root, argv=[launch_executable])
    profile = profile.replace(
        "(allow process-fork)",
        "\n".join(
            (
                "(allow process-fork)",
                ";; Project-owned additions for the inherited process tree.",
                "(allow pseudo-tty)",
                '(allow file-write* (literal "/dev/ptmx"))',
                '(allow file-write* (regex #"^/dev/ttys[0-9]+$"))',
                "(allow signal (target same-sandbox))",
            )
        ),
        1,
    )
    if "(allow pseudo-tty)" not in profile:
        raise SystemExit("triple-stamp: failed to add pseudo-tty to outer profile")
    real_home_literal = str(real_home).replace("\\", "\\\\").replace('"', '\\"')
    if f'(allow file-write* (subpath "{real_home_literal}"))' in profile:
        raise SystemExit("triple-stamp: outer profile unexpectedly grants real-home writes")
    if voice_profile is not None:
        voice_literal = str(voice_profile).replace("\\", "\\\\").replace('"', '\\"')
        if (
            f'(allow file-write* (subpath "{voice_literal}"))' in profile
            or f'(allow file-write* (literal "{voice_literal}"))' in profile
        ):
            raise SystemExit(
                "triple-stamp: outer profile grants voice profile writes"
            )

    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(profile, encoding="utf-8")
    profile_path.chmod(0o600)

    # Keep the checked-in bundle fail-closed for bare launches. Only this
    # generated copy disables nested Seatbelt, and the outer launcher never
    # starts it until the profile's positive and negative probes have passed.
    shutil.rmtree(runtime_bundle, ignore_errors=True)
    shutil.copytree(root / "agents", runtime_bundle / "agents")
    # `tools/` is optional. It held nothing but stale bytecode for a module that
    # no longer exists, so a fresh clone does not carry it at all: git cannot
    # track an empty directory. Copying it unconditionally made every clone fail
    # with FileNotFoundError before the profile was even built.
    if (root / "tools").is_dir():
        shutil.copytree(root / "tools", runtime_bundle / "tools")
    else:
        (runtime_bundle / "tools").mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "config.yaml", runtime_bundle / "config.yaml")
    for config_path in runtime_bundle.glob("**/config.yaml"):
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["os_env"]["cwd"] = str(root)
        config["os_env"]["sandbox"]["type"] = "none"
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    print(profile_path, runtime_bundle)


if __name__ == "__main__":
    main()
