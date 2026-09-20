"""Project-owned runtime guards for pinned Omnigent 0.12."""

from __future__ import annotations

import contextvars
import logging
import os
import sys

# Resolve Omnigent 0.14's relocated harness modules to their 0.12 import paths
# before any guard, plugin, or attestation imports them, so those import
# statements keep working unchanged on both runtimes. No-op on 0.12; never
# raises (the launcher capability probe is the fail-closed gate).
try:
    import triple_stamp_omnigent_compat as _omnigent_compat

    _omnigent_compat.install_all()
except Exception:  # noqa: BLE001 - compat install is best-effort; probe gates
    pass

import triple_stamp_cursor_lifecycle as _lifecycle
import triple_stamp_supervisor_runtime as _supervisor_runtime
from triple_stamp_browser_runtime import install_browser_runtime_guard

_CURSOR_COMPLETION_STABLE_S = _lifecycle._CURSOR_COMPLETION_STABLE_S
_CURSOR_STAGE_ABSOLUTE_S = _lifecycle._CURSOR_STAGE_ABSOLUTE_S
_CURSOR_STAGE_INACTIVITY_S = _lifecycle._CURSOR_STAGE_INACTIVITY_S
_cursor_data_root = _lifecycle._cursor_data_root
_install_parent_inbox_guard = _lifecycle.install_parent_inbox_guard


_MODE = "suppress-yolo-false-cards"
_ENV_NAME = "TRIPLE_STAMP_CURSOR_APPROVAL_GUARD"
_PROBE_ENV_NAME = "TRIPLE_STAMP_CURSOR_APPROVAL_GUARD_PROBE"
_runner_entrypoints = {
    "omnigent.runner._entry",
    "omnigent.runner._zygote",
}
_policy_identity: contextvars.ContextVar[dict[str, str] | None] = (
    contextvars.ContextVar("triple_stamp_policy_identity", default=None)
)


def _is_runner_process(argv: object) -> bool:
    return isinstance(argv, (list, tuple)) and any(
        argument in _runner_entrypoints
        for argument in argv
    )


_is_runner = _is_runner_process(getattr(sys, "orig_argv", ()))
_is_probe = os.environ.get(_PROBE_ENV_NAME) == "1"

install_browser_runtime_guard()
_supervisor_runtime.install_headless_pipeline_wait()


def _install_fail_closed_claude_launcher() -> None:
    """Enforce the active provider while preserving Opus's strict MCP args."""

    from omnigent import claude_launcher

    original = claude_launcher.resolve_claude_launch
    if getattr(original, "__triple_stamp_fail_closed__", False):
        return

    def fail_closed(command: str, args: list[str]) -> tuple[str, list[str]]:
        name = os.environ.get(
            claude_launcher.CLAUDE_LAUNCHER_ENV_VAR, ""
        ).strip()
        if not os.environ.get("TRIPLE_STAMP_RUN_ID"):
            return original(command, list(args))
        provider = os.environ.get("TRIPLE_STAMP_PROVIDER", "direct")
        if provider == "direct" and not name:
            from triple_stamp_isaac_launcher import (
                _is_opus_process,
                _triple_stamp_claude_args,
            )
            from triple_stamp_opus_mcp import (
                configure_opus_startup_environment,
                prepare_opus_mcp_config,
            )

            direct_args = list(args)
            if _is_opus_process(direct_args):
                configure_opus_startup_environment()
                direct_args = _triple_stamp_claude_args(
                    direct_args,
                    opus_mcp_config=str(prepare_opus_mcp_config()),
                )
            return original(command, direct_args)
        if provider != "databricks" or name != "isaac":
            raise RuntimeError(
                "PIPELINE_INFRASTRUCTURE_ERROR: Claude provider profile drifted "
                f"(provider={provider!r}, launcher={name!r})"
            )
        launcher = claude_launcher._load_launcher(name)
        if launcher is None:
            raise RuntimeError(
                "PIPELINE_INFRASTRUCTURE_ERROR: required Isaac Claude launcher "
                "is unavailable; direct Claude fallback is forbidden"
            )
        result = launcher.launch(command, list(args))
        if (
            not isinstance(result, tuple)
            or len(result) != 2
            or not isinstance(result[0], str)
            or not result[0]
            or not isinstance(result[1], list)
            or not all(isinstance(value, str) for value in result[1])
        ):
            raise RuntimeError(
                "PIPELINE_INFRASTRUCTURE_ERROR: required Isaac Claude launcher "
                "returned a malformed command; direct Claude fallback is forbidden"
            )
        return result

    fail_closed.__triple_stamp_fail_closed__ = True
    fail_closed.__triple_stamp_original__ = original
    claude_launcher.resolve_claude_launch = fail_closed

    # Replace aliases only in modules imported before this guard. Later imports
    # receive the patched function directly from omnigent.claude_launcher.
    for module_name in (
        "omnigent.claude_native",
        "omnigent.runner.native.orchestration",
    ):
        module = sys.modules.get(module_name)
        if module is not None and getattr(module, "resolve_claude_launch", None) is original:
            module.resolve_claude_launch = fail_closed


def _install_minimal_supervisor_tool_surface() -> None:
    """Expose only send/read MCP tools plus Claude SDK ToolSearch."""

    from omnigent.tools.manager import ToolManager

    allowed_mcp = frozenset({"sys_session_send", "sys_read_inbox"})
    original_init = ToolManager.__init__
    if not getattr(original_init, "__triple_stamp_minimal_surface__", False):

        def minimal_init(self: object, *args: object, **kwargs: object) -> None:
            original_init(self, *args, **kwargs)
            spec = getattr(self, "_spec", None)
            if getattr(spec, "name", None) != "triple-stamp":
                return
            tools = getattr(self, "_tools", {})
            if not isinstance(tools, dict):
                raise TypeError("triple-stamp supervisor tool registry is unavailable")
            missing = allowed_mcp.difference(tools)
            if missing:
                raise RuntimeError(
                    "triple-stamp supervisor routing tools are missing: "
                    + ", ".join(sorted(missing))
                )
            self._tools = {  # type: ignore[attr-defined]
                name: tool for name, tool in tools.items() if name in allowed_mcp
            }

        minimal_init.__triple_stamp_minimal_surface__ = True
        minimal_init.__triple_stamp_original__ = original_init
        ToolManager.__init__ = minimal_init

    from omnigent.inner import claude_sdk_executor

    original_ensure_sdk = claude_sdk_executor._ensure_sdk
    if getattr(original_ensure_sdk, "__triple_stamp_minimal_surface__", False):
        return

    def minimal_ensure_sdk() -> object:
        sdk = original_ensure_sdk()
        options_factory = sdk.ClaudeAgentOptions
        if getattr(options_factory, "__triple_stamp_minimal_surface__", False):
            return sdk

        def minimal_options(*args: object, **kwargs: object) -> object:
            kwargs["tools"] = ["ToolSearch"]
            allowed = {
                "mcp__omnigent__sys_session_send",
                "mcp__omnigent__sys_read_inbox",
            }
            kwargs["allowed_tools"] = [
                name
                for name in kwargs.get("allowed_tools", [])
                if isinstance(name, str) and name in allowed
            ]
            return options_factory(*args, **kwargs)

        minimal_options.__triple_stamp_minimal_surface__ = True
        minimal_options.__triple_stamp_original__ = options_factory
        sdk.ClaudeAgentOptions = minimal_options
        return sdk

    minimal_ensure_sdk.__triple_stamp_minimal_surface__ = True
    minimal_ensure_sdk.__triple_stamp_original__ = original_ensure_sdk
    claude_sdk_executor._ensure_sdk = minimal_ensure_sdk


def _install_policy_identity_context() -> None:
    """Expose verified current/root IDs to project function policies."""

    from omnigent.policies import function as policy_function
    from omnigent.runtime.policies.engine import PolicyEngine

    original_context = PolicyEngine._context
    if not getattr(
        original_context,
        "__triple_stamp_policy_identity_context__",
        False,
    ):

        def identity_context(self: object) -> dict[str, object]:
            context = dict(original_context(self))
            root_id = getattr(self, "_root_conversation_id", None)
            if isinstance(root_id, str) and root_id:
                context["root_conversation_id"] = root_id
            return context

        identity_context.__triple_stamp_policy_identity_context__ = True
        identity_context.__triple_stamp_original__ = original_context
        PolicyEngine._context = identity_context

    original_build_event = policy_function._build_event
    if not getattr(
        original_build_event,
        "__triple_stamp_policy_identity_context__",
        False,
    ):

        def build_event_with_identity(ctx: object) -> dict[str, object]:
            event = original_build_event(ctx)
            identity = _policy_identity.get()
            event_context = event.get("context")
            if identity is not None and isinstance(event_context, dict):
                event_context.update(identity)
            return event

        build_event_with_identity.__triple_stamp_policy_identity_context__ = True
        build_event_with_identity.__triple_stamp_original__ = original_build_event
        policy_function._build_event = build_event_with_identity

    original_evaluate = policy_function.FunctionPolicy.evaluate
    if getattr(
        original_evaluate,
        "__triple_stamp_policy_identity_context__",
        False,
    ):
        return

    async def evaluate_with_identity(
        self: object,
        ctx: object,
        context: dict[str, object],
    ) -> object:
        conversation_id = context.get("conversation_id")
        root_conversation_id = context.get("root_conversation_id")
        identity = (
            {
                "conversation_id": conversation_id,
                "root_conversation_id": root_conversation_id,
            }
            if (
                isinstance(conversation_id, str)
                and conversation_id
                and isinstance(root_conversation_id, str)
                and root_conversation_id
            )
            else None
        )
        token = _policy_identity.set(identity)
        try:
            return await original_evaluate(self, ctx, context)
        finally:
            _policy_identity.reset(token)

    evaluate_with_identity.__triple_stamp_policy_identity_context__ = True
    evaluate_with_identity.__triple_stamp_original__ = original_evaluate
    policy_function.FunctionPolicy.evaluate = evaluate_with_identity


if os.environ.get(_ENV_NAME) == _MODE:
    try:
        _install_policy_identity_context()
    except Exception as exc:  # noqa: BLE001 - missing identity would weaken the route cap
        print(
            f"triple-stamp: policy identity guard failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        os._exit(78)


if os.environ.get(_ENV_NAME) == _MODE and (_is_runner or _is_probe):
    try:
        from omnigent import cursor_native_permissions as _permissions

        _install_fail_closed_claude_launcher()
        _install_minimal_supervisor_tool_surface()
        _install_parent_inbox_guard()
        _supervisor_runtime.install_supervisor_continuation_guard()
    except Exception as exc:  # noqa: BLE001 - fail closed on any partial guard install
        # Continuing would restore the premature Cursor completion path.
        print(
            f"triple-stamp: runtime guard install failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        os._exit(78)

    _original = _permissions._yolo_auto_accept
    if not getattr(_original, "__triple_stamp_guard__", False):
        _reported: set[str] = set()

        async def _guarded_yolo_auto_accept(*args: object, **kwargs: object):
            """Keep stale Run Everything markers out of approval cards."""

            call = args[0] if args else kwargs.get("call")
            attempts_by_call = kwargs.get("attempts_by_call")
            tool_call_id = getattr(call, "tool_call_id", None)
            if isinstance(attempts_by_call, dict) and isinstance(tool_call_id, str):
                last_attempt_at, attempts = attempts_by_call.get(
                    tool_call_id,
                    (None, 0),
                )
                if attempts >= _permissions._YOLO_ACCEPT_MAX_ATTEMPTS:
                    attempts_by_call[tool_call_id] = (last_attempt_at, 0)

            outcome = await _original(*args, **kwargs)
            if outcome is not _permissions._YoloAccept.SURFACE_CARD:
                return outcome
            if isinstance(attempts_by_call, dict) and isinstance(tool_call_id, str):
                attempts_by_call[tool_call_id] = (kwargs.get("now"), 0)
                if tool_call_id not in _reported:
                    logging.getLogger(__name__).warning(
                        "triple-stamp suppressed a false Cursor approval card for %s",
                        getattr(call, "tool_name", "tool"),
                    )
                    _reported.add(tool_call_id)
            return _permissions._YoloAccept.SKIP

        _guarded_yolo_auto_accept.__triple_stamp_guard__ = True
        _guarded_yolo_auto_accept.__triple_stamp_original__ = _original
        _permissions._yolo_auto_accept = _guarded_yolo_auto_accept
