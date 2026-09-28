"""Read-only customer usage from a usage add-on, served to Opus over stdio MCP.

A usage add-on is one JSON file in the checkout's ``addons`` folder. It names a
Databricks workspace, the Databricks CLI profile that logs in to it, the table
the figures come from, and the SELECT queries that read one account's recent
consumption. Triple-stamp itself names no organization's tables or queries.

The outer launcher validates the add-on, mints a token from its CLI profile
before the run is sealed off, and writes both to the run's private
``usage.json``. This server offers one tool, ``customer_usage``. It runs only
the add-on's queries, each a single SELECT with the account name bound as a
parameter, so it cannot run any other SQL and never writes. Usage:
``python -I triple_stamp_usage_mcp.py <usage.json>``.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

TOOL = "customer_usage"
QUERY_KEYS = ("monthly", "product_mix", "new_products")
SUGGEST_KEY = "similar_names"
_ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .,&'()/-]{0,119}")
_DEADLINE_S = 150.0
_MAX_WAREHOUSES = 3

DEFAULT_TITLES = {
    "monthly": "Monthly spend",
    "product_mix": "Product mix, recent period against the one before",
    "new_products": "Products newly active in the recent period",
}
DEFAULT_DESCRIPTION = (
    "Read-only consumption for one account, in dollars: monthly spend, the "
    "product mix for a recent period against the one before, and products "
    "newly active in the recent period. Pass the account name exactly as your "
    "CRM spells it. If nothing matches, the reply suggests accounts with "
    "similar names; call again with one of them. These figures are internal "
    "and never belong in a customer answer."
)

# CRM account names rarely carry a legal suffix; live, "<Name> Group" and
# "<Name>, Inc." both missed an account named just "<Name>".
_SUFFIX = re.compile(
    r"(?:,?\s+(?:inc\.?|incorporated|group|holdings|llc|l\.l\.c\.|ltd\.?|limited"
    r"|corp\.?|corporation|co\.?|company|plc|gmbh|ag|s\.a\.))+$",
    re.IGNORECASE,
)

TOOL_SPEC = {
    "name": TOOL,
    "description": DEFAULT_DESCRIPTION,
    "inputSchema": {
        "type": "object",
        "properties": {
            "account_name": {
                "type": "string",
                "description": "Account name, for example \"Example Corp\".",
            }
        },
        "required": ["account_name"],
    },
    "annotations": {"readOnlyHint": True, "openWorldHint": False},
}

Http = Callable[[str, str, "dict[str, Any] | None"], "dict[str, Any]"]


class UsageError(RuntimeError):
    """A failure to report to Opus; never carries the token."""


class AddonError(ValueError):
    """Why an add-on file cannot be used."""


_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_TABLE = re.compile(r"[A-Za-z0-9_]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+")
_WAREHOUSE_ID = re.compile(r"[0-9a-f]{8,32}")
_HOST = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?")
# Statements that write or administer anything. A usage query only reads.
_WRITES = re.compile(
    r"\b(?:insert|update|delete|merge|create|drop|alter|grant|revoke|truncate"
    r"|copy|call|optimize|vacuum|refresh|msck|restore|clone|execute|set)\b",
    re.IGNORECASE,
)


def _label(value: object, field: str, limit: int) -> str:
    text = " ".join(str(value).split()) if isinstance(value, str) else ""
    if not text or len(text) > limit:
        raise AddonError(f"{field} must be text of at most {limit} characters")
    return text


def _statement(key: str, value: object, parameter: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise AddonError(f"query {key!r} is missing")
    if len(text) > 4000:
        raise AddonError(f"query {key!r} is longer than 4000 characters")
    if not re.match(r"(?:select|with)\b", text, re.IGNORECASE):
        raise AddonError(f"query {key!r} must be a single SELECT or WITH statement")
    if ";" in text or "--" in text or "/*" in text:
        raise AddonError(f"query {key!r} may not contain ';' or SQL comments")
    if _WRITES.search(text):
        raise AddonError(f"query {key!r} contains a write or admin keyword")
    if f":{parameter}" not in text:
        raise AddonError(f"query {key!r} must use the :{parameter} parameter")
    return text


def validate_addon(value: object) -> dict[str, Any]:
    """Return a clean usage add-on, or raise AddonError naming the problem."""

    if not isinstance(value, dict) or value.get("addon") != "usage":
        raise AddonError('it is not a usage add-on (it needs "addon": "usage")')
    host = str(value.get("host") or "").strip().rstrip("/")
    if not _HOST.fullmatch(host):
        raise AddonError("host must be an https:// workspace URL")
    profile = str(value.get("databricks_profile") or "").strip()
    if not _PROFILE.fullmatch(profile):
        raise AddonError("databricks_profile must be a Databricks CLI profile name")
    table = str(value.get("table") or "").strip()
    if not _TABLE.fullmatch(table):
        raise AddonError("table must be a catalog.schema.table name")
    queries = value.get("queries")
    if not isinstance(queries, dict):
        raise AddonError("queries must be an object")
    clean = {key: _statement(key, queries.get(key), "account") for key in QUERY_KEYS}
    if queries.get(SUGGEST_KEY) is not None:
        clean[SUGGEST_KEY] = _statement(SUGGEST_KEY, queries[SUGGEST_KEY], "pattern")
    preferred = value.get("preferred_warehouses") or []
    if not isinstance(preferred, list) or not all(
        isinstance(name, str) and 0 < len(name) <= 100 for name in preferred
    ):
        raise AddonError("preferred_warehouses must be a list of warehouse names")
    warehouse_id = str(value.get("warehouse_id") or "").strip()
    if warehouse_id and not _WAREHOUSE_ID.fullmatch(warehouse_id):
        raise AddonError("warehouse_id must be a SQL warehouse ID")
    titles = value.get("titles") or {}
    if not isinstance(titles, dict):
        raise AddonError("titles must be an object")
    return {
        "addon": "usage",
        "title": _label(value.get("title"), "title", 40),
        "host": host,
        "databricks_profile": profile,
        "table": table,
        "queries": clean,
        "preferred_warehouses": list(preferred[:10]),
        "warehouse_id": warehouse_id,
        "titles": {
            key: _label(titles.get(key) or DEFAULT_TITLES[key], f"titles.{key}", 80)
            for key in QUERY_KEYS
        },
        "description": _label(
            value.get("description") or DEFAULT_DESCRIPTION, "description", 1000
        ),
    }


def load_addon(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise AddonError(f"it is not readable JSON ({type(exc).__name__})") from None
    return validate_addon(value)


def _http_for(host: str, token: str) -> Http:
    def call(method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        request = urllib.request.Request(
            host.rstrip("/") + path,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            raise UsageError(f"HTTP {exc.code} from the usage workspace: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise UsageError(f"the usage workspace could not be reached: {exc}") from None
        value = json.loads(raw) if raw else {}
        return value if isinstance(value, dict) else {}

    return call


def _warehouse_score(row: dict[str, Any]) -> int:
    """Prefer serverless, larger, and wider warehouses."""

    size = str(row.get("cluster_size") or "").upper()
    name = str(row.get("name") or "")
    return (
        int(bool(row.get("enable_serverless_compute")))
        + int(bool(size) and size not in {"2X-SMALL", "X-SMALL", "SMALL"})
        + int(bool(name) and not name.upper().startswith("A"))
        + int(row.get("min_num_clusters") or 0)
        + int(0.5 * int(row.get("max_num_clusters") or 0))
    )


_OFF_LIMITS = re.compile(r"do\s*n[o']?t\s+use|deprecated", re.IGNORECASE)


def _warehouses(http: Http, preferred: list[str], pinned: str) -> list[str]:
    """The add-on's preferred warehouses first; never one that asks not to be used.

    Live, the top-scoring warehouse asked in its name not to be used, and the
    queries sent to it stalled while a shared endpoint answered all three in
    under four seconds.
    """

    if pinned:
        return [pinned]
    rows = http("GET", "/api/2.0/sql/warehouses", None).get("warehouses") or []

    def rank(row: dict[str, Any]) -> tuple[int, int, str]:
        name = str(row.get("name") or "")
        preference = next(
            (index for index, prefix in enumerate(preferred) if name.startswith(prefix)),
            len(preferred),
        )
        return (preference, -_warehouse_score(row), name)

    usable = [row for row in rows if not _OFF_LIMITS.search(str(row.get("name") or ""))]
    ids = [str(row["id"]) for row in sorted(usable, key=rank) if row.get("id")]
    if not ids:
        raise UsageError("no SQL warehouse is available to this login")
    return ids[:_MAX_WAREHOUSES]


def _execute(
    http: Http,
    warehouse: str,
    statement: str,
    parameters: list[dict[str, str]],
    deadline: float,
) -> list[list[Any]]:
    response = http(
        "POST",
        "/api/2.0/sql/statements",
        {
            "warehouse_id": warehouse,
            "statement": statement,
            "parameters": parameters,
            "wait_timeout": "30s",
            "on_wait_timeout": "CONTINUE",
            "disposition": "INLINE",
            "format": "JSON_ARRAY",
        },
    )
    while (response.get("status") or {}).get("state") in {"PENDING", "RUNNING"}:
        statement_id = str(response.get("statement_id") or "")
        if time.monotonic() > deadline or not statement_id:
            if statement_id:
                try:
                    http("POST", f"/api/2.0/sql/statements/{statement_id}/cancel", {})
                except UsageError:
                    pass
            raise UsageError("the usage query did not finish in time")
        time.sleep(2)
        response = http("GET", f"/api/2.0/sql/statements/{statement_id}", None)
    status = response.get("status") or {}
    if status.get("state") != "SUCCEEDED":
        message = str((status.get("error") or {}).get("message") or "")[:300]
        raise UsageError(f"the usage query {status.get('state')}: {message}")
    return (response.get("result") or {}).get("data_array") or []


def _money(value: Any) -> str:
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return "n/a"


def _render(
    account: str,
    addon: dict[str, Any],
    host: str,
    sections: list[tuple[str, list[list[Any]]]],
) -> str:
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%MZ")
    # The current month is partial; unlabeled, it reads like a drop in spend.
    current_month = now.strftime("%Y-%m")
    catalog, schema, table = addon["table"].split(".")
    link = f"{host.rstrip('/')}/explore/data/{catalog}/{schema}/{table}"
    lines = [
        f'{addon["title"]} usage for "{account}", in dollars of consumption.',
        f"Source: {link} ({addon['table']}), queried {stamp}.",
    ]
    for key, rows in sections:
        lines += ["", f"{addon['titles'][key]}:"]
        if not rows:
            lines.append("- none")
        for row in rows:
            if key == "monthly":
                partial = f" (month to date, through {now:%Y-%m-%d})" if row[0] == current_month else ""
                lines.append(f"- {row[0]}: {_money(row[1])}{partial}")
            elif key == "product_mix":
                change = f", {float(row[3]):+.1f}%" if row[3] not in (None, "") else ""
                lines.append(
                    f"- {row[0]}: {_money(row[1])} (prior {_money(row[2])}{change})"
                )
            else:
                lines.append(f"- {row[0]}: {_money(row[1])} in the recent period")
    return "\n".join(lines)


def customer_usage(
    account_name: object,
    *,
    config: dict[str, Any],
    http: Http | None = None,
) -> tuple[str, bool]:
    """Return ``(text, is_error)`` for one account's recent consumption."""

    name = " ".join(str(account_name or "").split())
    if not _ACCOUNT.fullmatch(name):
        return (
            "account_name must be an account name made of letters, digits, "
            "spaces, and . , & ' ( ) / - (at most 120 characters).",
            True,
        )
    host, token = str(config.get("host") or ""), str(config.get("token") or "")
    if not host.startswith("https://") or not token:
        return ("Usage is not configured for this run.", True)
    try:
        addon = validate_addon(config.get("addon"))
    except AddonError as exc:
        return (f"The usage add-on for this run is not usable: {exc}.", True)
    if float(config.get("expires_at") or 0) and float(config["expires_at"]) < time.time():
        return (
            "The usage login for this run has expired. Record usage as "
            "unavailable and continue with the other sources.",
            True,
        )
    http = http or _http_for(host, token)
    deadline = time.monotonic() + _DEADLINE_S
    queries = addon["queries"]
    try:
        warehouses = _warehouses(http, addon["preferred_warehouses"], addon["warehouse_id"])

        def monthly_for(candidate: str) -> list[list[Any]]:
            parameters = [{"name": "account", "value": candidate, "type": "STRING"}]
            return _execute(http, warehouse, queries["monthly"], parameters, deadline)

        last: UsageError | None = None
        for warehouse in warehouses:
            try:
                monthly = monthly_for(name)
                break
            except UsageError as exc:
                last = exc
        else:
            raise last or UsageError("no warehouse accepted the query")
        core = _SUFFIX.sub("", name).strip(" ,")
        if not monthly and core and core != name:
            monthly = monthly_for(core)
            if monthly:
                name = core
        if not monthly:
            return (_no_match(http, warehouse, name, queries.get(SUGGEST_KEY), deadline), False)
        account = [{"name": "account", "value": name, "type": "STRING"}]
        sections = [("monthly", monthly)] + [
            (key, _execute(http, warehouse, queries[key], account, deadline))
            for key in QUERY_KEYS[1:]
        ]
    except (UsageError, ValueError, KeyError, IndexError) as exc:
        return (f"Usage could not be queried: {exc}", True)
    return (_render(name, addon, host, sections), False)


def _no_match(
    http: Http, warehouse: str, name: str, suggest: str | None, deadline: float
) -> str:
    """Explain a miss and offer similar account names, if the add-on can."""

    message = f'No recent usage for the account named exactly "{name}".'
    if not suggest:
        return f"{message} Check the account's exact name."
    pattern = [{"name": "pattern", "value": f"%{_SUFFIX.sub('', name).strip(' ,')}%", "type": "STRING"}]
    try:
        rows = _execute(http, warehouse, suggest, pattern, deadline)
    except UsageError as exc:
        return f"{message} Similar names could not be listed ({exc})."
    others = [str(row[0]) for row in rows if row and row[0] and str(row[0]) != name]
    if not others:
        return f"{message} The account may have no recent usage."
    return (
        f"{message} Accounts with similar names: "
        + "; ".join(others)
        + ". Call again with the exact name."
    )


def _load(config_path: Path) -> dict[str, Any]:
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def handle(
    message: dict[str, Any],
    config_path: Path,
    http: Http | None = None,
) -> dict[str, Any] | None:
    """Answer one JSON-RPC message; notifications get no reply."""

    ident = message.get("id")
    if ident is None:
        return None
    method = message.get("method")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    def result(value: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": ident, "result": value}

    if method == "initialize":
        return result(
            {
                "protocolVersion": str(params.get("protocolVersion") or "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "triple-stamp-usage", "version": "1.1"},
            }
        )
    if method == "ping":
        return result({})
    if method == "tools/list":
        spec = dict(TOOL_SPEC)
        try:
            spec["description"] = validate_addon(_load(config_path).get("addon"))["description"]
        except AddonError:
            pass
        return result({"tools": [spec]})
    if method == "tools/call":
        if params.get("name") != TOOL:
            return {
                "jsonrpc": "2.0",
                "id": ident,
                "error": {"code": -32602, "message": f"unknown tool {params.get('name')!r}"},
            }
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        text, is_error = customer_usage(
            arguments.get("account_name"), config=_load(config_path), http=http
        )
        return result({"content": [{"type": "text", "text": text}], "isError": is_error})
    return {
        "jsonrpc": "2.0",
        "id": ident,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def serve(config_path: Path, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
    for line in stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        reply = handle(message, config_path)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: triple_stamp_usage_mcp.py <usage.json>")
    serve(Path(sys.argv[1]))
