# Usage add-on

An add-on is one file that gives Triple-stamp something specific to your
organization without putting it in this repository. Add-ons live outside every
checkout, in your per-user Triple-stamp folder, so no commit can ever carry one.

## Install one

Copy the add-on file into the add-on folder and start `./triple-stamp`:

```sh
mkdir -p ~/.local/share/triple-stamp/addons
cp ~/Downloads/usage.json ~/.local/share/triple-stamp/addons/
```

Every Triple-stamp checkout on your Mac uses it, and the start-up output says
when it is on. If it needs a login, the first start opens your browser once.
Delete the file to remove it, or set `TRIPLE_STAMP_ADDONS_DIR=off` to skip
add-ons for one run. `TRIPLE_STAMP_ADDONS_DIR` can also name another folder.
Share add-ons the way you share any internal file, never through a public
repository.

Only the launcher reads the file, before the run's sandbox is sealed. The
sandbox then denies every read of the add-on folder, so no stage of the run can
open it; the usage tool gets only the run's private login.

## What a usage add-on does

A usage add-on lets Opus read one account's recent consumption from a
Databricks SQL table whenever a question names a customer. Before each run,
Triple-stamp checks the file. Every query must be a single `SELECT` or `WITH`
statement with no semicolons, comments, or write keywords, and must bind the
account name as `:account`. The queries run with your own Databricks login and
never write. Customer-facing answers leave the figures out.

This example uses made-up names:

```json
{
  "addon": "usage",
  "title": "Metering",
  "host": "https://example.cloud.databricks.com",
  "databricks_profile": "metering",
  "table": "example.billing.usage_daily",
  "preferred_warehouses": ["Shared Warehouse"],
  "queries": {
    "monthly": "SELECT DATE_FORMAT(DATE_TRUNC('month', day), 'yyyy-MM'), ROUND(SUM(dollars), 0) FROM example.billing.usage_daily WHERE account_name = :account AND day >= ADD_MONTHS(DATE_TRUNC('month', CURRENT_DATE()), -6) GROUP BY 1 ORDER BY 1",
    "product_mix": "SELECT product, ROUND(SUM(CASE WHEN day >= DATE_SUB(CURRENT_DATE(), 30) THEN dollars ELSE 0 END), 0), ROUND(SUM(CASE WHEN day < DATE_SUB(CURRENT_DATE(), 30) THEN dollars ELSE 0 END), 0), NULL FROM example.billing.usage_daily WHERE account_name = :account AND day >= DATE_SUB(CURRENT_DATE(), 60) GROUP BY product ORDER BY 2 DESC LIMIT 15",
    "new_products": "SELECT product, ROUND(SUM(dollars), 0) FROM example.billing.usage_daily WHERE account_name = :account AND day >= DATE_SUB(CURRENT_DATE(), 30) AND product NOT IN (SELECT product FROM example.billing.usage_daily WHERE account_name = :account AND day < DATE_SUB(CURRENT_DATE(), 30) AND day >= DATE_SUB(CURRENT_DATE(), 60)) GROUP BY product ORDER BY 2 DESC LIMIT 10",
    "similar_names": "SELECT DISTINCT account_name FROM example.billing.usage_daily WHERE day >= DATE_SUB(CURRENT_DATE(), 7) AND LOWER(account_name) LIKE LOWER(:pattern) ORDER BY 1 LIMIT 5"
  }
}
```

| Field | What it is |
| --- | --- |
| `title` | The source name Opus sees and cites. |
| `host` | The Databricks workspace URL. The first-start login uses it. |
| `databricks_profile` | The Databricks CLI profile that holds the login. |
| `table` | The table the figures come from. Its Catalog Explorer page is the cited link. |
| `queries.monthly` | Rows of month (`YYYY-MM`) and dollars. The current month is labeled month to date. |
| `queries.product_mix` | Rows of product, recent dollars, prior dollars, and percent change (or null). |
| `queries.new_products` | Rows of product and recent dollars for newly active products. |
| `queries.similar_names` | Optional. A few account names like `:pattern`, offered when a name has no usage. |
| `preferred_warehouses` | Optional. Warehouse name prefixes to try first. A warehouse whose name says "do not use" or "deprecated" is never used. |
| `warehouse_id` | Optional. Pins one SQL warehouse. |
| `titles`, `description` | Optional. Section titles and the tool description Opus reads. |
