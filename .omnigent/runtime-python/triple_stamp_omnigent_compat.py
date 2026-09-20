"""Dual-version Omnigent compatibility layer for Triple-stamp.

This module lets a single Triple-stamp launcher run on the pinned stable line
(Omnigent ``0.12.x``) **and** the released native line (Omnigent ``0.14.x``)
without adopting the separate ``wip/native-0.14`` rewrite. Omnigent 0.14
relocated a small set of harness modules under ``omnigent.harnesses.*``; this
module aliases the legacy import paths to their relocated targets so the
existing runtime guards, plugin, and bundle attestation keep importing
unchanged. It also owns the version-acceptance policy, the per-version schema
(migration-head) expectation, and a capability probe that the launcher and
``validate_bundle`` use to fail closed when a required surface is absent.

Design constraints:
- Import-safe under ``python -I``; never raises at import time.
- Imports :mod:`omnigent` lazily inside functions only.
- ``install_compat_aliases`` is idempotent and a no-op on 0.12.

The relocation map, the accepted versions, and the migration heads were derived
empirically by installing ``omnigent==0.14.0`` in a disposable Python 3.13 venv
and probing the surfaces the launcher/runtime import; do not edit without a
matching probe.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys

# --- Version acceptance policy ------------------------------------------
#
# Accept the pinned stable line (0.12.x) and the released native line (0.14.x).
# 0.13.x is published on the index but deliberately rejected here: it is
# unproven for this launcher, so it fails closed until it is explicitly
# validated the same way 0.14 was.
SUPPORTED_MINORS = frozenset({(0, 12), (0, 14)})


def _parse_minor(value: str | None) -> tuple[int, int] | None:
    parts = (value or "").strip().split(".")
    try:
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return None


def is_supported_omnigent_version(value: str | None) -> bool:
    """Return ``True`` only for accepted Omnigent versions (0.12.x or 0.14.x)."""

    return _parse_minor(value) in SUPPORTED_MINORS


# --- Module relocations (0.12 legacy path -> 0.14 relocated path) --------
#
# These are the only modules the Triple-stamp launcher / runtime guards /
# plugin import that moved between 0.12 and 0.14. Every other imported surface
# kept its 0.12 path in 0.14 (verified by the capability probe below).
MODULE_RELOCATIONS: dict[str, str] = {
    "omnigent.claude_native_bridge": "omnigent.harnesses.claude_native.bridge",
    "omnigent.cursor_native_permissions": "omnigent.harnesses.cursor_native.permissions",
    "omnigent.cursor_native_usage": "omnigent.harnesses.cursor_native.usage",
    "omnigent.cursor_native_forwarder": "omnigent.harnesses.cursor_native.forwarder",
    "omnigent.cursor_native_status": "omnigent.harnesses.cursor_native.status",
}

# Tokens that may appear in Omnigent-generated ``.cursor`` config for either
# version. The launcher teardown matchers accept either spelling so managed
# entries are still detected and removed on 0.14.
CLAUDE_BRIDGE_TOKENS: tuple[str, ...] = (
    "omnigent.claude_native_bridge",
    "omnigent.harnesses.claude_native.bridge",
)
CURSOR_USAGE_TOKENS: tuple[str, ...] = (
    "omnigent.cursor_native_usage",
    "omnigent.harnesses.cursor_native.usage",
)

# --- Per-version schema attestation (Alembic migration heads) -----------
#
# validate_bundle attests the runtime DB schema by comparing Alembic heads.
# The head differs per Omnigent minor; this is a version gate, not routing.
MIGRATION_HEADS: dict[tuple[int, int], list[str]] = {
    (0, 12): ["ga1b2c3d4e5f"],
    (0, 14): ["gg1b2c3d4e5f"],
}


def expected_migration_heads(value: str | None) -> list[str] | None:
    """Return the accepted Alembic heads for an Omnigent version, or ``None``."""

    return MIGRATION_HEADS.get(_parse_minor(value))


# --- Required capability surfaces ---------------------------------------
#
# logical name -> (candidate modules legacy-first, required attributes).
# The launcher accepts an installed Omnigent version only if every surface here
# resolves (legacy or relocated) with its required attributes present.
REQUIRED_SURFACES: dict[str, tuple[list[str], list[str]]] = {
    "process_reaper": (
        ["omnigent.testing.process_reaper"], ["reap_leaked_omnigent_processes"]),
    "db_compression": (["omnigent.db.compression"], ["decode"]),
    "claude_launcher": (
        ["omnigent.claude_launcher"],
        ["resolve_claude_launch", "CLAUDE_LAUNCHER_ENV_VAR", "_load_launcher"]),
    "tools_manager": (["omnigent.tools.manager"], ["ToolManager"]),
    "claude_sdk_executor": (["omnigent.inner.claude_sdk_executor"], ["_ensure_sdk"]),
    "inner_executor": (
        ["omnigent.inner.executor"],
        ["TextChunk", "ToolCallComplete", "ToolCallRequest", "TurnComplete"]),
    "policies_function": (
        ["omnigent.policies.function"], ["_build_event", "FunctionPolicy"]),
    "policy_engine": (["omnigent.runtime.policies.engine"], ["PolicyEngine"]),
    "runtime_telemetry": (["omnigent.runtime.telemetry"], ["current_session_id"]),
    "chat": (["omnigent.chat"], []),
    "cli": (["omnigent.cli"], []),
    "conversation_browser": (["omnigent.conversation_browser"], []),
    "host_connect": (["omnigent.host.connect"], ["HostProcess"]),
    "host_frames": (
        ["omnigent.host.frames"],
        ["HostCreateDirResultFrame", "HostListDirResultFrame"]),
    "runner_app": (["omnigent.runner.app"], []),
    "runner_tool_dispatch": (["omnigent.runner.tool_dispatch"], []),
    "cursor_native_permissions": (
        ["omnigent.cursor_native_permissions",
         "omnigent.harnesses.cursor_native.permissions"],
        ["_yolo_auto_accept", "_YoloAccept", "_YOLO_ACCEPT_MAX_ATTEMPTS"]),
    "claude_native_bridge": (
        ["omnigent.claude_native_bridge",
         "omnigent.harnesses.claude_native.bridge"],
        ["bridge_dir_for_conversation_id", "read_claude_session_id",
         "read_transcript_path"]),
    "cursor_native_usage": (
        ["omnigent.cursor_native_usage",
         "omnigent.harnesses.cursor_native.usage"], []),
    "cursor_native_forwarder": (
        ["omnigent.cursor_native_forwarder",
         "omnigent.harnesses.cursor_native.forwarder"], []),
    "cursor_native_status": (
        ["omnigent.cursor_native_status",
         "omnigent.harnesses.cursor_native.status"], []),
}


def install_compat_aliases() -> list[str]:
    """Register legacy module names for relocated 0.14 modules (idempotent).

    No-op on Omnigent 0.12 (legacy modules already resolve) and on any error.
    Returns the list of legacy names that were aliased this call. A missing
    relocation target is left silently for the capability probe to report.
    """

    from importlib.metadata import version

    try:
        if _parse_minor(version("omnigent")) == (0, 12):
            return []
    except Exception:  # noqa: BLE001 - metadata read is advisory
        pass

    installed: list[str] = []
    for legacy, relocated in MODULE_RELOCATIONS.items():
        if legacy in sys.modules:
            continue
        try:
            if importlib.util.find_spec(legacy) is not None:
                continue  # legacy path still present -> nothing to alias
        except Exception:  # noqa: BLE001 - treat as absent, try to alias
            pass
        try:
            module = importlib.import_module(relocated)
        except Exception:  # noqa: BLE001 - probe reports the missing surface
            continue
        sys.modules[legacy] = module
        parent_name, _, child = legacy.rpartition(".")
        try:
            parent = importlib.import_module(parent_name)
            setattr(parent, child, module)
        except Exception:  # noqa: BLE001 - attribute binding is best-effort
            pass
        installed.append(legacy)
    return installed


class _CompletedNone:
    """Awaitable that resolves to ``None`` immediately.

    Lets the 0.14 cleanup shim satisfy both the legacy synchronous call site
    (``guarded_drain_inbox`` calls without ``await``) and any residual caller
    that still ``await``s the function.
    """

    __slots__ = ()

    def __await__(self):
        return iter(())


def install_compat_shims() -> list[str]:
    """Adapt Omnigent 0.14 signature/async changes to the 0.12 call convention.

    ``omnigent.runner.tool_dispatch._cleanup_drained_subagent_work`` became an
    ``async`` function with a keyword-only ``server_client`` in 0.14. The
    Triple-stamp inbox guard (``guarded_drain_inbox``) calls it synchronously
    with the 0.12 convention (no ``server_client``, no ``await``). This shim
    runs the 0.14 coroutine to completion with ``server_client=None`` -- which
    the 0.14 body treats as "no server access", performing only the synchronous
    ``unregister_subagent_work`` and skipping the best-effort receipt. That is
    exactly the 0.12 behavior (0.12 has no receipt mechanism at all).

    No-op on 0.12 and on any error. Idempotent.
    """

    from importlib.metadata import version

    try:
        if _parse_minor(version("omnigent")) == (0, 12):
            return []
    except Exception:  # noqa: BLE001 - metadata read is advisory
        pass

    installed: list[str] = []
    try:
        import inspect

        from omnigent.runner import tool_dispatch as _tool_dispatch
    except Exception:  # noqa: BLE001 - probe reports the missing surface
        return installed

    original = getattr(_tool_dispatch, "_cleanup_drained_subagent_work", None)
    if (
        original is not None
        and inspect.iscoroutinefunction(original)
        and not getattr(original, "__triple_stamp_compat__", False)
    ):

        def _cleanup_drained_subagent_work(payload, *, server_client=None):
            # Force server_client=None so the 0.14 coroutine performs no awaits
            # (only the synchronous unregister runs; the receipt is skipped,
            # matching 0.12, which has no receipt). Drive it to completion so
            # the synchronous call site still triggers the cleanup.
            coro = original(payload, server_client=None)
            try:
                coro.send(None)
            except StopIteration:
                pass
            else:  # pragma: no cover - defensive: body should not await
                coro.close()
            return _CompletedNone()

        _cleanup_drained_subagent_work.__triple_stamp_compat__ = True
        _cleanup_drained_subagent_work.__triple_stamp_original__ = original
        _tool_dispatch._cleanup_drained_subagent_work = _cleanup_drained_subagent_work
        installed.append(
            "omnigent.runner.tool_dispatch._cleanup_drained_subagent_work"
        )
    return installed


def install_all() -> dict[str, list[str]]:
    """Install every 0.14 compatibility adaptation (aliases + signature shims).

    Safe to call in every process; a no-op on Omnigent 0.12. This is the single
    entry point wired into ``sitecustomize`` and the launcher's regression
    bootstrap so relocated imports resolve and changed signatures are adapted
    before any guard, plugin, test, or attestation touches them.
    """

    return {
        "aliases": install_compat_aliases(),
        "shims": install_compat_shims(),
    }


def _resolve_surface(candidates: list[str], attrs: list[str]) -> dict[str, object]:
    for candidate in candidates:
        try:
            if importlib.util.find_spec(candidate) is None:
                continue
            module = importlib.import_module(candidate)
        except Exception as exc:  # noqa: BLE001
            return {"module": candidate, "ok": False,
                    "error": f"{type(exc).__name__}: {exc}"}
        missing = [attr for attr in attrs if not hasattr(module, attr)]
        return {
            "module": candidate,
            "relocated": candidate != candidates[0],
            "missing_attrs": missing,
            "ok": not missing,
        }
    return {"module": None, "ok": False, "checked": list(candidates),
            "missing_attrs": list(attrs)}


def probe() -> dict[str, object]:
    """Resolve every required surface and report version/capability status."""

    from importlib.metadata import version

    try:
        omnigent_version = version("omnigent")
    except Exception as exc:  # noqa: BLE001
        return {
            "omnigent_version": None,
            "supported": False,
            "surfaces_ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "surfaces": {},
        }

    # Resolve surfaces first (legacy-first) so relocation reporting is accurate,
    # then exercise the aliaser to confirm it succeeds on this runtime.
    surfaces = {
        name: _resolve_surface(cands, attrs)
        for name, (cands, attrs) in REQUIRED_SURFACES.items()
    }
    surfaces_ok = all(surface["ok"] for surface in surfaces.values())
    missing = sorted(name for name, surface in surfaces.items() if not surface["ok"])
    adaptations = install_all()
    return {
        "omnigent_version": omnigent_version,
        "supported": is_supported_omnigent_version(omnigent_version),
        "surfaces_ok": surfaces_ok,
        "missing_surfaces": missing,
        "relocations_active": sorted(
            name for name, surface in surfaces.items() if surface.get("relocated")
        ),
        "aliases_installed": adaptations["aliases"],
        "shims_installed": adaptations["shims"],
        "expected_migration_heads": expected_migration_heads(omnigent_version),
        "surfaces": surfaces,
    }


def main(argv: list[str] | None = None) -> int:
    print(json.dumps(probe(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
