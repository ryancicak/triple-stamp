"""Tests for dual Omnigent 0.12/0.14 support.

Covers the compatibility module's version-acceptance policy, the 0.14 module
relocation map, the per-version migration-head expectation, and the launcher's
``_validate_versions`` capability-probe gate (accept 0.12.x/0.14.x, reject
0.13.x and any runtime missing a required surface).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str) -> object:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


compat = _load(
    "triple_stamp_omnigent_compat_under_test",
    ".omnigent/runtime-python/triple_stamp_omnigent_compat.py",
)
launcher = _load("compat_launcher_under_test", ".omnigent/launcher.py")


def _tools() -> object:
    return launcher.Toolchain(
        isaac=None,
        dbcert=None,
        databricks=None,
        uv=Path("/custom/bin/uv"),
        omnigent=Path("/custom/bin/omnigent"),
        omnigent_python=Path("/custom/omnigent/bin/python"),
        cursor_agent=Path("/custom/npm/bin/cursor-agent"),
        sandbox_exec=Path("/usr/bin/sandbox-exec"),
        security=Path("/usr/bin/security"),
        claude=Path("/custom/npm/bin/claude"),
        codex=Path("/custom/npm/bin/codex"),
    )


def _probe_result(**overrides: object) -> object:
    payload = {
        "omnigent_version": "0.12.0",
        "supported": True,
        "surfaces_ok": True,
        "missing_surfaces": [],
    }
    payload.update(overrides)
    return launcher.subprocess.CompletedProcess([], 0, json.dumps(payload), "")


_SDK_OK = launcher.subprocess.CompletedProcess([], 0, "0.2.152\n", "")


class VersionPolicyTests(unittest.TestCase):
    def test_accepts_pinned_stable_line(self) -> None:
        for value in ("0.12.0", "0.12.5", "0.12.99"):
            self.assertTrue(compat.is_supported_omnigent_version(value), value)

    def test_accepts_released_native_line(self) -> None:
        for value in ("0.14.0", "0.14.3", "0.14.99"):
            self.assertTrue(compat.is_supported_omnigent_version(value), value)

    def test_rejects_0_13_and_neighbours(self) -> None:
        for value in ("0.13.0", "0.13.9", "0.11.0", "0.15.0", "1.0.0"):
            self.assertFalse(compat.is_supported_omnigent_version(value), value)

    def test_rejects_malformed_versions(self) -> None:
        for value in ("", "0", "garbage", "0.x.0", None):
            self.assertFalse(compat.is_supported_omnigent_version(value), value)


class RelocationMapTests(unittest.TestCase):
    def test_only_harness_modules_relocate(self) -> None:
        expected = {
            "omnigent.claude_native_bridge": "omnigent.harnesses.claude_native.bridge",
            "omnigent.cursor_native_permissions": "omnigent.harnesses.cursor_native.permissions",
            "omnigent.cursor_native_usage": "omnigent.harnesses.cursor_native.usage",
            "omnigent.cursor_native_forwarder": "omnigent.harnesses.cursor_native.forwarder",
            "omnigent.cursor_native_status": "omnigent.harnesses.cursor_native.status",
        }
        self.assertEqual(compat.MODULE_RELOCATIONS, expected)

    def test_cleanup_tokens_cover_both_spellings(self) -> None:
        self.assertIn("omnigent.claude_native_bridge", compat.CLAUDE_BRIDGE_TOKENS)
        self.assertIn(
            "omnigent.harnesses.claude_native.bridge", compat.CLAUDE_BRIDGE_TOKENS
        )
        self.assertIn("omnigent.cursor_native_usage", compat.CURSOR_USAGE_TOKENS)
        self.assertIn(
            "omnigent.harnesses.cursor_native.usage", compat.CURSOR_USAGE_TOKENS
        )


class MigrationHeadTests(unittest.TestCase):
    def test_head_is_version_specific(self) -> None:
        self.assertEqual(
            compat.expected_migration_heads("0.12.0"), ["ga1b2c3d4e5f"]
        )
        self.assertEqual(
            compat.expected_migration_heads("0.14.0"), ["gg1b2c3d4e5f"]
        )

    def test_unsupported_version_has_no_expected_head(self) -> None:
        self.assertIsNone(compat.expected_migration_heads("0.13.0"))
        self.assertIsNone(compat.expected_migration_heads("garbage"))


class ValidateVersionsGateTests(unittest.TestCase):
    def test_accepts_0_12_runtime(self) -> None:
        with mock.patch.object(
            launcher, "_run", side_effect=[_probe_result(omnigent_version="0.12.0"), _SDK_OK]
        ):
            launcher._validate_versions(_tools(), {}, "direct")

    def test_accepts_0_14_runtime(self) -> None:
        with mock.patch.object(
            launcher, "_run", side_effect=[_probe_result(omnigent_version="0.14.0"), _SDK_OK]
        ):
            launcher._validate_versions(_tools(), {}, "direct")

    def test_rejects_unsupported_version(self) -> None:
        probe = _probe_result(omnigent_version="0.13.0", supported=False)
        with (
            mock.patch.object(launcher, "_run", side_effect=[probe]),
            self.assertRaises(launcher.LaunchError) as raised,
        ):
            launcher._validate_versions(_tools(), {}, "direct")
        self.assertIn("0.12.x or 0.14.x", str(raised.exception))
        self.assertIn("0.13.0", str(raised.exception))

    def test_rejects_missing_surface_fail_closed(self) -> None:
        probe = _probe_result(
            omnigent_version="0.14.0",
            surfaces_ok=False,
            missing_surfaces=["claude_native_bridge"],
        )
        with (
            mock.patch.object(launcher, "_run", side_effect=[probe]),
            self.assertRaises(launcher.LaunchError) as raised,
        ):
            launcher._validate_versions(_tools(), {}, "direct")
        self.assertIn("missing launcher-required surfaces", str(raised.exception))
        self.assertIn("claude_native_bridge", str(raised.exception))

    def test_rejects_unparseable_probe(self) -> None:
        broken = launcher.subprocess.CompletedProcess([], 1, "", "boom")
        with (
            mock.patch.object(launcher, "_run", side_effect=[broken]),
            self.assertRaises(launcher.LaunchError) as raised,
        ):
            launcher._validate_versions(_tools(), {}, "direct")
        self.assertIn("could not determine the Omnigent runtime", str(raised.exception))


class CompatShimTests(unittest.TestCase):
    def test_install_all_reports_aliases_and_shims(self) -> None:
        result = compat.install_all()
        self.assertIn("aliases", result)
        self.assertIn("shims", result)
        self.assertIsInstance(result["aliases"], list)
        self.assertIsInstance(result["shims"], list)

    def test_completed_none_is_awaitable_and_returns_none(self) -> None:
        import asyncio

        async def _use() -> object:
            return await compat._CompletedNone()

        self.assertIsNone(asyncio.run(_use()))

    def test_no_shims_installed_on_0_12(self) -> None:
        # On the pinned stable line the changed-signature shim is a no-op.
        try:
            import omnigent  # noqa: F401
            from importlib.metadata import version

            if not version("omnigent").startswith("0.12"):
                self.skipTest("not running on Omnigent 0.12")
        except Exception:  # noqa: BLE001
            self.skipTest("omnigent is not importable in this environment")
        self.assertEqual(compat.install_compat_shims(), [])


class SelfTestVersionLineTests(unittest.TestCase):
    def test_reports_actual_installed_version(self) -> None:
        for reported in ("0.12.0", "0.14.0"):
            proc = launcher.subprocess.CompletedProcess([], 0, reported + "\n", "")
            with mock.patch.object(launcher, "_run", return_value=proc):
                self.assertEqual(
                    launcher._omnigent_runtime_version("/py", {}), reported
                )

    def test_falls_back_to_unknown_on_probe_failure(self) -> None:
        proc = launcher.subprocess.CompletedProcess([], 1, "", "boom")
        with mock.patch.object(launcher, "_run", return_value=proc):
            self.assertEqual(launcher._omnigent_runtime_version("/py", {}), "unknown")

    def test_self_test_summary_is_not_hardcoded_to_0_12(self) -> None:
        source = (ROOT / ".omnigent/launcher.py").read_text(encoding="utf-8")
        self.assertNotIn("runtime: Omnigent 0.12.0", source)
        self.assertIn(
            "runtime: Omnigent {omnigent_runtime_version}", source
        )


class LiveProbeTests(unittest.TestCase):
    """Exercise the real probe against the interpreter running the suite."""

    def test_installed_runtime_is_accepted_and_complete(self) -> None:
        try:
            import omnigent  # noqa: F401
        except Exception:  # noqa: BLE001
            self.skipTest("omnigent is not importable in this environment")
        status = compat.probe()
        self.assertTrue(status["supported"], status)
        self.assertTrue(status["surfaces_ok"], status.get("missing_surfaces"))
        self.assertEqual(status["missing_surfaces"], [])
        self.assertEqual(
            status["expected_migration_heads"],
            compat.expected_migration_heads(status["omnigent_version"]),
        )


if __name__ == "__main__":
    unittest.main()
