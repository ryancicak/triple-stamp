"""Read-only customer usage from an add-on: the engine, the launcher login, and wiring.

Every account, table, query, workspace, and figure here is made up.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


server = _module(
    "triple_stamp_usage_mcp_under_test",
    ROOT / ".omnigent/isaac-launcher/triple_stamp_usage_mcp.py",
)
opus_mcp = _module(
    "triple_stamp_opus_mcp_for_usage_tests",
    ROOT / ".omnigent/isaac-launcher/triple_stamp_opus_mcp.py",
)
launcher = _module(
    "triple_stamp_launcher_for_usage_tests",
    ROOT / ".omnigent/launcher.py",
)

TOKEN = "usage-test-token-value-that-must-never-print"
ADDON = {
    "addon": "usage",
    "title": "Metering",
    "host": "https://usage.example.test",
    "databricks_profile": "example-usage",
    "table": "example.billing.usage_daily",
    "preferred_warehouses": ["Shared Warehouse - Stable", "Shared Warehouse"],
    "queries": {
        "monthly": (
            "SELECT DATE_FORMAT(day, 'yyyy-MM') AS month, SUM(dollars) "
            "FROM example.billing.usage_daily WHERE account_name = :account "
            "GROUP BY 1 ORDER BY 1"
        ),
        "product_mix": (
            "SELECT product, SUM(recent), SUM(prior), NULL FROM example.billing.usage_mix "
            "WHERE account_name = :account GROUP BY product"
        ),
        "new_products": (
            "SELECT product, SUM(dollars) FROM example.billing.usage_new "
            "WHERE account_name = :account GROUP BY product"
        ),
        "similar_names": (
            "SELECT DISTINCT account_name FROM example.billing.usage_daily "
            "WHERE LOWER(account_name) LIKE LOWER(:pattern) LIMIT 5"
        ),
    },
}
CONFIG = {
    "host": ADDON["host"],
    "token": TOKEN,
    "expires_at": time.time() + 3600,
    "addon": server.validate_addon(ADDON),
}

WAREHOUSES = [
    # Scores highest by size and width but asks not to be used; a live run
    # stalled on one like it.
    {"id": "busy", "name": "Team Warehouse [PLEASE DO NOT USE]", "state": "RUNNING",
     "enable_serverless_compute": True, "cluster_size": "Large",
     "min_num_clusters": 1, "max_num_clusters": 25},
    {"id": "metrics", "name": "Metric Store", "state": "RUNNING",
     "enable_serverless_compute": True, "cluster_size": "X-Large",
     "min_num_clusters": 1, "max_num_clusters": 16},
    {"id": "shared", "name": "Shared Warehouse - Stable", "state": "RUNNING",
     "enable_serverless_compute": True, "cluster_size": "Small",
     "min_num_clusters": 1, "max_num_clusters": 2},
]


class FakeWorkspace:
    """Records every request and answers like the SQL Statement API."""

    def __init__(
        self,
        usage: set[str],
        *,
        suggestions: tuple[str, ...] = (),
        pending: int = 0,
        fail_first: bool = False,
        fail_suggest: bool = False,
    ):
        self.usage = usage
        self.suggestions = suggestions
        self.fail_suggest = fail_suggest
        self.pending = pending
        self.fail_first = fail_first
        self.requests: list[tuple[str, str, dict | None]] = []

    def __call__(self, method: str, path: str, body: dict | None) -> dict:
        self.requests.append((method, path, body))
        if path == "/api/2.0/sql/warehouses":
            return {"warehouses": WAREHOUSES}
        if method == "GET":
            return self._answer(self._last_statement)
        assert body is not None
        if self.fail_first:
            self.fail_first = False
            return {"status": {"state": "FAILED", "error": {"message": "no permission"}}}
        self._last_statement = body
        if self.pending:
            self.pending -= 1
            return {"statement_id": "stmt-1", "status": {"state": "PENDING"}}
        return self._answer(body)

    def _answer(self, body: dict) -> dict:
        statement = body["statement"]
        value = body["parameters"][0]["value"]
        queries = ADDON["queries"]
        if statement == queries["similar_names"]:
            if self.fail_suggest:
                return {"status": {"state": "FAILED", "error": {"message": "[INSUFFICIENT_PERMISSIONS]"}}}
            rows = [[name] for name in self.suggestions]
        elif value not in self.usage:
            rows = []
        elif statement == queries["monthly"]:
            rows = [["2026-08", "120000"], ["2026-09", "150000"]]
        elif statement == queries["product_mix"]:
            rows = [["SQL", "90000", "80000", "12.5"], ["DATABASE", "4000", None, None]]
        elif statement == queries["new_products"]:
            rows = [["DATABASE", "4000"]]
        else:
            raise AssertionError(f"unexpected statement: {statement}")
        return {"status": {"state": "SUCCEEDED"}, "result": {"data_array": rows}}


class EngineTests(unittest.TestCase):
    def test_the_only_tool_takes_an_account_name_and_is_read_only(self) -> None:
        reply = server.handle({"id": 1, "method": "tools/list"}, Path("/nonexistent"))
        (tool,) = reply["result"]["tools"]
        self.assertEqual(tool["name"], "customer_usage")
        self.assertEqual(list(tool["inputSchema"]["properties"]), ["account_name"])
        self.assertTrue(tool["annotations"]["readOnlyHint"])
        self.assertIn("never belong in a customer answer", tool["description"])

    def test_the_add_on_can_describe_its_own_tool(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = Path(value) / "usage.json"
            addon = server.validate_addon({**ADDON, "description": "Spend from the metering table."})
            path.write_text(json.dumps({**CONFIG, "addon": addon}), encoding="utf-8")
            reply = server.handle({"id": 1, "method": "tools/list"}, path)
        self.assertEqual(reply["result"]["tools"][0]["description"], "Spend from the metering table.")

    def test_usage_binds_the_account_and_never_prints_the_token(self) -> None:
        fake = FakeWorkspace({"Example Corp"})
        text, is_error = server.customer_usage("Example  Corp", config=CONFIG, http=fake)

        self.assertFalse(is_error, text)
        self.assertIn('Metering usage for "Example Corp"', text)
        self.assertIn("https://usage.example.test/explore/data/example/billing/usage_daily", text)
        self.assertIn("- 2026-09: $150,000", text)
        self.assertIn("- SQL: $90,000 (prior $80,000, +12.5%)", text)
        self.assertIn("- DATABASE: $4,000 in the recent period", text)
        self.assertNotIn(TOKEN, text)
        statements = [body for method, path, body in fake.requests if method == "POST"]
        self.assertEqual(len(statements), 3)
        # The add-on's first preferred warehouse, never the "do not use" one.
        self.assertEqual({body["warehouse_id"] for body in statements}, {"shared"})
        for body in statements:
            self.assertNotIn("Example", body["statement"])
            self.assertEqual(
                body["parameters"],
                [{"name": "account", "value": "Example Corp", "type": "STRING"}],
            )

    def test_the_current_month_is_labeled_partial(self) -> None:
        month = server.datetime.now(server.timezone.utc).strftime("%Y-%m")
        text = server._render("Example Corp", CONFIG["addon"], CONFIG["host"], [
            ("monthly", [["2026-01", "10"], [month, "5"]]),
        ])
        self.assertIn("Monthly spend:", text)
        self.assertIn("- 2026-01: $10\n", text + "\n")
        self.assertIn(f"- {month}: $5 (month to date, through ", text)

    def test_unknown_names_get_suggestions_but_no_usage(self) -> None:
        fake = FakeWorkspace(set(), suggestions=("Example Corp", "Example Corp Holdings"))
        text, is_error = server.customer_usage("Example", config=CONFIG, http=fake)
        self.assertFalse(is_error)
        self.assertIn('named exactly "Example"', text)
        self.assertIn("Accounts with similar names: Example Corp; Example Corp Holdings", text)
        # The monthly query, then one suggestion lookup; no more usage queries.
        self.assertEqual(sum(1 for m, _p, _b in fake.requests if m == "POST"), 2)

        text, _ = server.customer_usage("Nobody", config=CONFIG, http=FakeWorkspace(set()))
        self.assertIn("may have no recent usage", text)

    def test_a_legal_suffix_is_dropped_before_giving_up(self) -> None:
        # Live, "<Name> Group" and "<Name>, Inc." both missed "<Name>".
        for spelled in ("Example Group", "Example, Inc.", "Example Holdings LLC"):
            with self.subTest(spelled=spelled):
                fake = FakeWorkspace({"Example"})
                text, is_error = server.customer_usage(spelled, config=CONFIG, http=fake)
                self.assertFalse(is_error, text)
                self.assertIn('Metering usage for "Example"', text)

    def test_a_suggestion_the_login_cannot_run_is_not_an_error(self) -> None:
        fake = FakeWorkspace(set(), fail_suggest=True)
        text, is_error = server.customer_usage("Example", config=CONFIG, http=fake)
        self.assertFalse(is_error)
        self.assertIn('named exactly "Example"', text)
        self.assertIn("Similar names could not be listed", text)

    def test_an_add_on_without_a_suggestion_query_still_explains_a_miss(self) -> None:
        queries = {key: value for key, value in ADDON["queries"].items() if key != "similar_names"}
        config = {**CONFIG, "addon": server.validate_addon({**ADDON, "queries": queries})}
        fake = FakeWorkspace(set())
        text, is_error = server.customer_usage("Example", config=config, http=fake)
        self.assertFalse(is_error)
        self.assertIn("Check the account's exact name.", text)
        self.assertEqual(sum(1 for m, _p, _b in fake.requests if m == "POST"), 1)

    def test_warehouses_that_ask_not_to_be_used_are_never_chosen(self) -> None:
        fake = FakeWorkspace(set())
        preferred = ADDON["preferred_warehouses"]
        self.assertEqual(server._warehouses(fake, preferred, ""), ["shared", "metrics"])
        # Without preferences the widest usable warehouse comes first.
        self.assertEqual(server._warehouses(fake, [], ""), ["metrics", "shared"])
        self.assertEqual(server._warehouses(fake, preferred, "pinned"), ["pinned"])

    def test_unsafe_names_and_expired_logins_fail_before_any_request(self) -> None:
        fake = FakeWorkspace({"Example Corp"})
        for name in ("x'; DROP TABLE t; --", "", "a" * 121, "name_with_underscore"):
            with self.subTest(name=name):
                text, is_error = server.customer_usage(name, config=CONFIG, http=fake)
                self.assertTrue(is_error)
        expired = {**CONFIG, "expires_at": time.time() - 1}
        text, is_error = server.customer_usage("Example Corp", config=expired, http=fake)
        self.assertTrue(is_error)
        self.assertIn("expired", text)
        broken = {**CONFIG, "addon": {**CONFIG["addon"], "queries": {"monthly": "DELETE FROM t"}}}
        text, is_error = server.customer_usage("Example Corp", config=broken, http=fake)
        self.assertTrue(is_error)
        self.assertIn("not usable", text)
        self.assertEqual(fake.requests, [])

    def test_slow_queries_are_polled_and_a_refusing_warehouse_is_skipped(self) -> None:
        fake = FakeWorkspace({"Example Corp"}, pending=1, fail_first=True)
        with mock.patch.object(server.time, "sleep"):
            text, is_error = server.customer_usage("Example Corp", config=CONFIG, http=fake)
        self.assertFalse(is_error, text)
        self.assertTrue(any(m == "GET" and "stmt-1" in p for m, p, _b in fake.requests))
        warehouses = [b["warehouse_id"] for m, _p, b in fake.requests if m == "POST" and b]
        self.assertEqual(warehouses[:2], ["shared", "metrics"])

    def test_the_server_answers_over_stdio_as_opus_launches_it(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            login = Path(value) / "usage.json"
            login.write_text(json.dumps(CONFIG), encoding="utf-8")
            entry = opus_mcp.usage_server(Path(value))
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ]
            result = subprocess.run(
                [entry["command"], *entry["args"]],
                input="".join(json.dumps(message) + "\n" for message in messages),
                capture_output=True,
                text=True,
                timeout=60,
            )
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2], result.stderr)
        self.assertEqual(replies[1]["result"]["tools"][0]["name"], "customer_usage")

    def test_json_rpc_handshake_and_errors(self) -> None:
        path = Path("/nonexistent")
        init = server.handle(
            {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            path,
        )
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertIsNone(server.handle({"method": "notifications/initialized"}, path))
        self.assertEqual(server.handle({"id": 2, "method": "nope"}, path)["error"]["code"], -32601)
        unknown = server.handle(
            {"id": 3, "method": "tools/call", "params": {"name": "run_sql"}}, path
        )
        self.assertEqual(unknown["error"]["code"], -32602)
        # Without a login file the tool reports that usage is not configured.
        call = server.handle(
            {
                "id": 4,
                "method": "tools/call",
                "params": {"name": "customer_usage", "arguments": {"account_name": "Example Corp"}},
            },
            path,
        )
        self.assertTrue(call["result"]["isError"])
        self.assertIn("not configured", call["result"]["content"][0]["text"])


class AddonTests(unittest.TestCase):
    def test_a_valid_add_on_is_cleaned(self) -> None:
        addon = server.validate_addon(ADDON)
        self.assertEqual(addon["title"], "Metering")
        self.assertEqual(addon["databricks_profile"], "example-usage")
        self.assertEqual(
            sorted(addon["queries"]), ["monthly", "new_products", "product_mix", "similar_names"]
        )
        self.assertEqual(addon["titles"]["monthly"], "Monthly spend")
        self.assertEqual(addon["warehouse_id"], "")
        # A cleaned add-on validates again unchanged, as the server re-checks it.
        self.assertEqual(server.validate_addon(addon), addon)

    def test_add_ons_that_could_write_or_escape_are_rejected(self) -> None:
        queries = ADDON["queries"]
        cases = {
            "not a usage add-on": {**ADDON, "addon": "other"},
            "plain http host": {**ADDON, "host": "http://usage.example.test"},
            "host with a path": {**ADDON, "host": "https://usage.example.test/x?y"},
            "unsafe profile": {**ADDON, "databricks_profile": "bad profile!"},
            "two-part table": {**ADDON, "table": "billing.usage"},
            "table with SQL": {**ADDON, "table": "a.b.c; DROP"},
            "missing query": {**ADDON, "queries": {k: v for k, v in queries.items() if k != "product_mix"}},
            "update statement": {**ADDON, "queries": {**queries, "monthly": "UPDATE t SET x = 1 WHERE a = :account"}},
            "second statement": {**ADDON, "queries": {**queries, "monthly": queries["monthly"] + "; DROP TABLE t"}},
            "line comment": {**ADDON, "queries": {**queries, "monthly": queries["monthly"] + " -- note"}},
            "block comment": {**ADDON, "queries": {**queries, "monthly": "SELECT /* x */ 1 FROM t WHERE a = :account"}},
            "write inside a CTE": {**ADDON, "queries": {**queries, "new_products": "WITH x AS (SELECT 1) MERGE INTO t USING x ON a = :account"}},
            "unbound account": {**ADDON, "queries": {**queries, "monthly": "SELECT month, dollars FROM t"}},
            "unbound pattern": {**ADDON, "queries": {**queries, "similar_names": "SELECT name FROM t"}},
            "bad warehouse pin": {**ADDON, "warehouse_id": "not a warehouse"},
            "no title": {**ADDON, "title": ""},
        }
        for label, addon in cases.items():
            with self.subTest(label):
                with self.assertRaises(server.AddonError):
                    server.validate_addon(addon)

    def test_an_unreadable_add_on_file_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = Path(value) / "broken.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(server.AddonError) as raised:
                server.load_addon(path)
        self.assertIn("not readable JSON", str(raised.exception))


class WiringTests(unittest.TestCase):
    def test_opus_gets_the_usage_server_only_with_a_login(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            run = Path(value)
            self.assertIsNone(opus_mcp.usage_server(run))
            (run / "usage.json").write_text("{}", encoding="utf-8")
            with mock.patch.dict(os.environ, {"STABLE_OMNIGENT_PY": "/stable/bin/python"}):
                entry = opus_mcp.usage_server(run)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("STABLE_OMNIGENT_PY", None)
                fallback = opus_mcp.usage_server(run)
        # The same stable interpreter in every process keeps the immutable
        # run-scoped config identical; a symlinked path once failed a launch.
        self.assertEqual(entry["command"], "/stable/bin/python")
        self.assertEqual(fallback["command"], sys.executable)
        self.assertTrue(entry["args"][1].endswith("triple_stamp_usage_mcp.py"))
        self.assertIn("mcp__usage__customer_usage", opus_mcp.READ_ONLY_ALLOWED_TOOLS)

    def test_only_a_person_at_a_terminal_gets_the_browser_login(self) -> None:
        source = (ROOT / ".omnigent/launcher.py").read_text(encoding="utf-8")
        self.assertIn(
            "interactive=sys.stdin.isatty() and not any(arg in SELF_TEST_FLAGS for arg in args)",
            source,
        )


class LauncherTests(unittest.TestCase):
    PROFILE = "[example-usage]\nhost = https://usage.example.test/\nauth_type = databricks-cli\n"
    MINTED = subprocess.CompletedProcess(
        [], 0, json.dumps({"access_token": TOKEN, "expiry": "2099-01-01T00:00:00+00:00"}), ""
    )

    def _run(
        self,
        *,
        addons: dict[str, object] | None,
        env: dict[str, str] | None = None,
        profile: bool = True,
        minted: subprocess.CompletedProcess[str] | None = None,
        interactive: bool = False,
        cli: str | None = "/usr/local/bin/databricks",
        login_succeeds: bool = True,
        in_checkout: bool = False,
    ) -> tuple[dict | None, list[str], list[list[str]]]:
        with tempfile.TemporaryDirectory() as value:
            root, home, run, state = (
                Path(value) / name for name in ("repo", "home", "run", "state")
            )
            for folder in (root, home, run, state):
                folder.mkdir()
            if addons is not None:
                # Add-ons belong in the per-user folder, never in the checkout.
                folder = root / "addons" if in_checkout else state / "addons"
                folder.mkdir()
                for name, content in addons.items():
                    (folder / name).write_text(
                        content if isinstance(content, str) else json.dumps(content),
                        encoding="utf-8",
                    )
            config = home / ".databrickscfg"
            config.write_text(self.PROFILE if profile else "", encoding="utf-8")
            logins: list[list[str]] = []

            def login(databricks: str, host: str, name: str, _env: dict[str, str]) -> bool:
                logins.append([databricks, host, name])
                if login_succeeds:
                    config.write_text(self.PROFILE, encoding="utf-8")
                return login_succeeds

            # Never read the real per-user folder, which may hold a real add-on.
            ambient = {launcher.INTERNAL_SOURCES_ENV, launcher.ADDONS_DIR_ENV, "TRIPLE_STAMP_STATE_DIR"}
            clean = {k: v for k, v in os.environ.items() if k not in ambient}
            clean["TRIPLE_STAMP_STATE_DIR"] = str(state)
            with mock.patch.dict(os.environ, {**clean, **(env or {})}, clear=True), mock.patch.object(
                launcher, "_usage_module", return_value=server
            ), mock.patch.object(launcher.shutil, "which", return_value=cli), mock.patch.object(
                launcher, "_run_quietly", return_value=minted or self.MINTED
            ), mock.patch.object(launcher, "_databricks_login", side_effect=login), mock.patch.object(
                launcher, "_eprint"
            ) as printed:
                launcher._write_usage_login(
                    run, root, home, {"PATH": "/usr/local/bin"}, interactive=interactive
                )
            path = run / "usage.json"
            saved = None
            if path.exists():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                saved = json.loads(path.read_text(encoding="utf-8"))
            messages = [str(call.args[0]) for call in printed.call_args_list]
            return saved, messages, logins

    def test_add_ons_live_outside_every_checkout(self) -> None:
        home = Path("/Users/example")
        cases = {
            "default": ({}, home / ".local/share/triple-stamp/addons"),
            "state folder": ({"TRIPLE_STAMP_STATE_DIR": "/data/ts"}, Path("/data/ts/addons")),
            "named folder": ({launcher.ADDONS_DIR_ENV: "/elsewhere/add-ons"}, Path("/elsewhere/add-ons")),
            "turned off": ({launcher.ADDONS_DIR_ENV: "off"}, None),
        }
        ambient = {launcher.ADDONS_DIR_ENV, "TRIPLE_STAMP_STATE_DIR"}
        clean = {k: v for k, v in os.environ.items() if k not in ambient}
        for label, (env, expected) in cases.items():
            with self.subTest(label), mock.patch.dict(os.environ, {**clean, **env}, clear=True):
                self.assertEqual(launcher._addons_dir(home), expected)

    def test_an_add_on_with_a_login_gives_the_run_private_usage(self) -> None:
        saved, messages, logins = self._run(addons={"metering.json": ADDON})
        self.assertIsNotNone(saved)
        self.assertEqual(saved["host"], "https://usage.example.test")
        self.assertEqual(saved["token"], TOKEN)
        self.assertEqual(saved["addon"], server.validate_addon(ADDON))
        (message,) = messages
        self.assertIn("Metering usage add-on is on (", message)
        self.assertTrue(message.endswith("addons/metering.json)"), message)
        self.assertEqual(logins, [])

    def test_no_add_on_turned_off_and_public_only_runs_stay_silent(self) -> None:
        for label, kwargs in {
            "no addons folder": {"addons": None},
            "empty addons folder": {"addons": {}},
            "turned off": {"addons": {"metering.json": ADDON}, "env": {launcher.ADDONS_DIR_ENV: "off"}},
            "public-only": {"addons": {"metering.json": ADDON}, "env": {launcher.INTERNAL_SOURCES_ENV: "off"}},
            # A file inside the checkout is never an add-on, so no commit can carry one.
            "file in the checkout": {"addons": {"metering.json": ADDON}, "in_checkout": True},
        }.items():
            with self.subTest(label):
                saved, messages, logins = self._run(**kwargs)
                self.assertEqual((saved, messages, logins), (None, [], []))

    def test_a_bad_add_on_is_ignored_with_the_reason(self) -> None:
        bad = {**ADDON, "queries": {**ADDON["queries"], "monthly": "DELETE FROM t WHERE a = :account"}}
        saved, messages, _ = self._run(addons={"bad.json": bad, "notes.json": "{not json"})
        self.assertIsNone(saved)
        (message,) = messages
        self.assertIn("usage add-on ignored", message)
        self.assertIn("bad.json: query 'monthly' must be a single SELECT", message)
        self.assertIn("notes.json: it is not readable JSON", message)

    def test_only_the_first_of_two_add_ons_is_used(self) -> None:
        second = {**ADDON, "title": "Second"}
        saved, messages, _ = self._run(addons={"a.json": ADDON, "b.json": second})
        self.assertEqual(saved["addon"]["title"], "Metering")
        self.assertIn("b.json: only one usage add-on is used, and a.json came first", messages[0])
        self.assertIn("a.json)", messages[-1])

    def test_a_missing_login_opens_the_browser_once_for_a_person(self) -> None:
        saved, messages, logins = self._run(
            addons={"metering.json": ADDON}, profile=False, interactive=True
        )
        self.assertIsNotNone(saved)
        self.assertEqual(logins, [["/usr/local/bin/databricks", "https://usage.example.test", "example-usage"]])
        self.assertIn("logging in to Metering", messages[0])
        self.assertIn("usage add-on is on", messages[-1])

    def test_without_a_login_the_run_starts_and_says_how_to_log_in(self) -> None:
        command = "databricks auth login --host https://usage.example.test --profile example-usage"
        failed = subprocess.CompletedProcess([], 1, "", "expired")
        for label, kwargs in {
            "expired login, no terminal": {"minted": failed},
            "no profile, no terminal": {"profile": False},
            "browser login failed": {"profile": False, "interactive": True, "login_succeeds": False},
        }.items():
            with self.subTest(label):
                saved, messages, logins = self._run(addons={"metering.json": ADDON}, **kwargs)
                self.assertIsNone(saved)
                self.assertIn(f"Metering usage is off for this run; log in once with `{command}`", messages[-1])
                self.assertEqual(len(logins), int(kwargs.get("interactive", False)))

    def test_without_the_databricks_cli_the_run_says_what_to_install(self) -> None:
        saved, messages, _ = self._run(addons={"metering.json": ADDON}, cli=None)
        self.assertIsNone(saved)
        self.assertIn("install the Databricks CLI", messages[0])

    def test_the_run_is_denied_every_add_on_folder(self) -> None:
        home = Path("/Users/example")
        ambient = {launcher.ADDONS_DIR_ENV, "TRIPLE_STAMP_STATE_DIR"}
        clean = {k: v for k, v in os.environ.items() if k not in ambient}
        default = str(home / ".local/share/triple-stamp/addons")
        cases = {
            "default": ({}, [default]),
            # Turned off for a run, the default folder may still hold add-ons.
            "turned off": ({launcher.ADDONS_DIR_ENV: "off"}, [default]),
            "named folder": ({launcher.ADDONS_DIR_ENV: "/elsewhere/add-ons"}, sorted([default, "/elsewhere/add-ons"])),
        }
        for label, (env, expected) in cases.items():
            with self.subTest(label), mock.patch.dict(os.environ, {**clean, **env}, clear=True):
                self.assertEqual(launcher._addons_deny(home).split(os.pathsep), expected)

    def test_the_launch_probe_proves_add_ons_are_unreadable(self) -> None:
        source = (ROOT / ".omnigent/launcher.py").read_text(encoding="utf-8")
        self.assertIn('"TRIPLE_STAMP_ADDONS_DENY": _addons_deny(real_home),', source)
        self.assertIn('raise SystemExit(f"outer Seatbelt allowed reading add-ons: {folder}")', source)

    def test_a_hung_databricks_cli_never_stops_startup(self) -> None:
        hung = launcher._run_quietly(
            [sys.executable, "-c", "import time; time.sleep(10)"], env=dict(os.environ), timeout=0.5
        )
        self.assertIsNone(hung)
        with mock.patch.object(launcher, "_run_quietly", return_value=None):
            self.assertEqual(launcher._databricks_token("databricks", "p", {}), ("", 0.0))


class SandboxTests(unittest.TestCase):
    """Live, the research stage opened an add-on through the ~/.local/share grant."""

    def setUp(self) -> None:
        try:
            self.builder = _module(
                "triple_stamp_outer_seatbelt_for_usage_tests",
                ROOT / ".omnigent/build_outer_seatbelt.py",
            )
        except ImportError as exc:
            self.skipTest(f"omnigent is not importable here: {exc}")

    def test_add_on_folders_are_denied_after_every_grant(self) -> None:
        profile = '(version 1)\n(allow file-read* (subpath "/Users/example/.local/share"))\n'
        folders = os.pathsep.join(["/Users/example/.local/share/triple-stamp/addons", "/tmp/a\"b"])
        denied = self.builder._deny_addons(profile, folders)
        self.assertTrue(denied.startswith(profile.rstrip("\n")))
        self.assertTrue(
            denied.rstrip().endswith(
                '(deny file-read* file-write* (subpath "/Users/example/.local/share/triple-stamp/addons"))\n'
                '(deny file-read* file-write* (subpath "/tmp/a\\"b"))'
            ),
            denied,
        )
        self.assertEqual(self.builder._deny_addons(profile, ""), profile)

    def test_seatbelt_really_blocks_the_add_on_folder(self) -> None:
        if os.environ.get("TRIPLE_STAMP_OUTER_SANDBOX") == "1":
            # Seatbelt cannot nest inside a run (sandbox-exec exits 71), so
            # check the run's real deny instead: no add-on folder is listable.
            for folder in filter(None, os.environ.get("TRIPLE_STAMP_ADDONS_DENY", "").split(os.pathsep)):
                with self.subTest(folder=folder):
                    try:
                        os.listdir(folder)
                    except (PermissionError, FileNotFoundError):
                        continue
                    self.fail(f"the run could list the add-on folder {folder}")
            return
        sandbox_exec = Path("/usr/bin/sandbox-exec")
        if not sandbox_exec.exists():
            self.skipTest("sandbox-exec is not available")
        with tempfile.TemporaryDirectory() as value:
            base = Path(value).resolve()
            (base / "addons").mkdir()
            (base / "addons/usage.json").write_text("{}", encoding="utf-8")
            (base / "notes.txt").write_text("fine", encoding="utf-8")
            profile = self.builder._deny_addons(
                f'(version 1)\n(allow default)\n(allow file-read* (subpath "{base}"))\n',
                str(base / "addons"),
            )
            allowed = subprocess.run(
                [str(sandbox_exec), "-p", profile, "/bin/cat", str(base / "notes.txt")],
                capture_output=True, text=True, timeout=30,
            )
            blocked = subprocess.run(
                [str(sandbox_exec), "-p", profile, "/bin/cat", str(base / "addons/usage.json")],
                capture_output=True, text=True, timeout=30,
            )
        self.assertEqual((allowed.returncode, allowed.stdout), (0, "fine"))
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("Operation not permitted", blocked.stderr)


if __name__ == "__main__":
    unittest.main()
