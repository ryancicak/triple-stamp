# Changelog

## v1.2.2 (2026-10-02)

Opus 5.5 does the audit, and an audit the model service cancels gets a second try.

### Changes

- **Opus 5.5 does the audit.** The audit stage now runs Claude Opus 5.5 at max effort instead of Opus 5, so your Claude account must include Claude Opus 5.5. Opus 5.5 thinks longer before it writes: on research questions its final write took 7 to 10 minutes, where Opus 5 took about 3, and a whole audit took 10 to 24 minutes.
- **Web research gets 20 minutes instead of 15.** Research usually finishes within 9 minutes, but with several runs at once a few stages took 14 to 15 minutes and two hit the limit, which ended their questions. Research that goes quiet is still stopped after 5 minutes without output.

### Fixes

- **A cancelled audit gets one more try.** When the model service cancels an Opus 5.5 audit (`API Error: 499`), the question used to end there. That audit now gets one fresh retry, the same as an audit whose response broke off mid-stream. A question you stop yourself is never retried.

### Verification

- **Live runs:** 6 real questions in three rounds: with a voice profile and without, with a usage add-on and without, and in public-only mode. 5 stamped. The sixth, a public-only question, finished its first cycle and was stopped during its second when the test machine ran low on disk space. The account question that had ended with the 499 stamped in both rounds that asked it, and no audit needed the retry.
- **Checks:** 3 of the 5 stamped answers passed every check. One account answer had no Salesforce link, named an internal Jira or Slack item without its link, and did not cite the usage lookup Opus ran; Opus 5 also left that lookup uncited on the same question. An arithmetic answer missed the eval's word-overlap topic check.
- **Regression suite:** 481 tests pass on Omnigent 0.14.0 and 0.12.0.
- **Fresh install:** `./triple-stamp --self-test` passes on new clones, one of them in a folder whose path has a space and one cloned from this repository, normally and public-only.

### Known limitations

- With a short-lived model login, a session left open for more than about an hour can lose it, and the question in progress then stops. Type `/quit` and start `./triple-stamp` again. The usage login has the same limit: after about an hour, Opus answers from the other sources.
- A question whose web research is still running after 20 minutes, or whose judge fails to start, ends without an answer instead of retrying. Ask it again.
- An audit gets one retry per cycle. If the model service cancels it twice, the question ends. Ask it again.
- In a folder whose path contains a space, setup installs the newest compatible packages instead of the tested set. The self-test still passed that way.

### For maintainers

- The retry matches only a failed audit whose output or error is exactly the 499 cancellation envelope. A task the user cancelled is never relaunched, and an audit recovered by its retry no longer counts as a failed worker. The contract tests pin all three, and the bundle validator also checks the first two at startup.

## v1.2.1 (2026-09-28)

A run without a usage add-on no longer mentions one.

### Fixes

- **No add-on, no mention.** In a run without a usage add-on, the research stage read Triple-stamp's own docs in the workspace and reported that the add-on was not installed, Opus sometimes listed the missing tool in its audit, and once the judge repeated it in the answer's notes. Cursor now leaves account usage to the audit stage, Opus never mentions a missing usage tool, and without an add-on the judge never mentions the tool or an add-on in the answer or its notes. v1.2.0 has this bug. Runs with an add-on work as before.

### Verification

- **v1.2.0 as published:** from fresh clones of this repository, 12 account usage questions in four rounds, with and without the add-on, each with and without a voice profile. With the add-on, 6 of 6 stamped and passed every check. Without it, 5 of 6 stamped, and the sixth hit the 15-minute research limit. One stamped answer's notes said the add-on was missing, and three of the six questions mentioned it in their stage packets.
- **This fix:** without the add-on, with and without a voice profile, 6 of 6 stamped and passed every check, and no dossier mentions the usage tool or an add-on anywhere, stage packets included. One question whose judge failed to start was asked again. With the add-on and a voice profile, 3 of 3 stamped, and the usage figures came from observed calls. On one question the usage query ran past its time limit, and Opus answered from the other sources and said so.
- **Regression suite:** 480 tests pass on Omnigent 0.14.0 and 0.12.0.
- **Fresh install:** `./triple-stamp --self-test` passes on a new clone, normally and public-only.

### Known limitations

- With a short-lived model login, a session left open for more than about an hour can lose it, and the question in progress then stops. Type `/quit` and start `./triple-stamp` again. The usage login has the same limit: after about an hour, Opus answers from the other sources.
- A question whose web research is still running after 15 minutes, or whose judge fails to start, ends without an answer instead of retrying. Ask it again.

### For maintainers

- The contract tests require the three new rules, so a prompt edit cannot drop them silently.

## v1.2.0 (2026-09-28)

Account answers can now start from what the customer actually runs, through a usage add-on that stays on your Mac, and Opus is read-only again on Omnigent 0.14.

### Answers you can send

- **Real usage behind account answers.** With a usage add-on, a question that names a customer also gets the account's recent consumption: monthly spend, the product mix against the period before, and products that just became active. Opus cites the usage source, and the judge accepts a usage figure only when it saw the tool call. The current month is labeled month to date, so a partial month never reads as a drop.
- **Usage stays internal.** An answer for a customer leaves usage, spend, and consumption figures out, along with the other internal details.
- **Account names as people write them.** A question that says "Example Group" or "Example, Inc." still finds the account named "Example", and a name with no match gets similar account names to try.

### Setup

- **One file, nothing else to set.** Copy a usage add-on into `~/.local/share/triple-stamp/addons/` and start `./triple-stamp`. The first start opens your browser once to log in to the add-on's Databricks workspace, and the login renews on its own after that. The folder sits outside every checkout, so an add-on never lands in a commit, and every checkout on your Mac uses it.
- **Checked before every run.** [docs/usage-add-on.md](docs/usage-add-on.md) describes the file, with a made-up example. Every query in it must be a single read-only statement that binds the account name as a parameter; the usage tool can run nothing else and never writes.
- **Sealed off from the run.** The launcher reads the add-on before the run's sandbox is sealed, and the sandbox then denies every read of the add-on folder, so no stage can open the file. The start-up probe proves the folder is unreadable before any model starts.
- **Nothing changes without one.** Without an add-on, and in public-only runs, everything works as before, and the judge never asks for usage the run cannot get. If the add-on's login is missing or the Databricks CLI hangs, the run starts without usage and prints the one command to run. `TRIPLE_STAMP_ADDONS_DIR=off` skips add-ons for a run.

### Fixes

- **Opus is read-only again on Omnigent 0.14.** Omnigent 0.14, the default runtime, launches Claude through a different path than 0.12, so Opus started without Triple-stamp's lockdown: no strict MCP catalog and no read-only tool list. Its tool search offered write tools such as Jira and Slack writes. In our runs, a managed permission policy still refused them, and every audit call we observed was a read. Opus now gets the full lockdown on both runtimes. The self-test checks that the guard is in place and that Omnigent still launches Claude through the path it guards. v1.0.0 and v1.1.0 have this bug on Omnigent 0.14.

### Verification

- **Regression suite:** 479 tests pass on Omnigent 0.14.0 and 0.12.0, and again inside a public-only run.
- **Fresh install:** `./triple-stamp --self-test` passes on a new clone in a folder whose path has spaces, with a usage add-on installed and in public-only mode. The start-up probe proves the add-on folder is unreadable inside the run. With an add-on whose login is missing, the run still starts and prints the one login command.
- **Live runs:** 57 real questions reached a verdict in 24 rounds, with and without a voice profile, with and without a usage add-on, and in public-only mode. 52 stamped. Of the other five, one early run could not start Opus until a config path defect was fixed, one question's research ran past 15 minutes while the Mac also ran three self-tests, and three could not start their research during a brief outage of Cursor's service; those two rounds were stopped and re-run.
- **Final design:** 21 of 21 questions stamped in seven rounds: account usage questions with the add-on and without it, each with and without a voice profile; the regression set without the add-on; and a public-only round with the add-on installed, which the run ignored. The last two rounds ran on the final code. With the add-on, every usage figure came from an observed call. Without it, the judge asked for no usage, nothing the requester reads mentions usage, and the research stage's attempt to open the add-on folder was blocked.
- **Side by side with v1.1.0:** asked how an account's consumption had trended over six months, v1.1.0 said a monthly series was not available and fell back to an older analysis. With the usage add-on, v1.2.0 gave the monthly spend through the current month, labeled the partial month, named the products driving the change, and showed the actual spend of the Lakebase use case the question asked about. On a pilot-status question, both versions did equally well.

### Known limitations

- With a short-lived model login, a session left open for more than about an hour can lose it, and the question in progress then stops. Type `/quit` and start `./triple-stamp` again. The usage login has the same limit: after about an hour, Opus answers from the other sources.
- A question whose web research is still running after 15 minutes ends without an answer instead of retrying. Ask it again.

### For maintainers

- `tools/triple_stamp_eval.py` scores usage by whether the run has an add-on. With one, `expect_usage` checks that every figure came from an observed and cited call, and `expect_usage_figures` checks that the answer quotes dollar figures. Without one, `usage_quiet` checks that neither the answer nor its notes mention the usage tool.
- The send-ready check no longer mistakes a customer's own "during validation" step for a review note, and `research_on_topic` compares word stems, so research about "blockers" counts for a question about what is "blocking" a pilot.
- The coverage line reports tools outside the five internal systems as `other=`, for example `other=usage=1[...]`, and whether the run has a usage add-on as `usage=installed` or `usage=none`.

## v1.1.0 (2026-09-27)

Account questions now draw on Salesforce, and answers meant for customers leave internal material out.

### Answers you can send

- **Salesforce context without asking.** When a question names a customer or account, Opus also searches the Salesforce records that Glean indexes, such as use cases and support cases, and cites each record with a link to it. Questions that already ask for Salesforce work as before.
- **Customer-ready when you say so.** If the question says the answer is for a customer, the judge keeps internal links, internal plans, and Salesforce details out of it, and the notes list what it left out. Answers for your own team keep their internal links.

### Fixes

- **Stamped answers outside Databricks.** A run with no internal systems, as on a Mac outside Databricks, never got a stamp: the judge kept asking for internal lookups the run could not make, even for `5+5=?`. The judge now knows which internal systems the run has, and it judges a public-only run on its public evidence. v1.0.0 has this bug.
- **Fewer questions lost to an expired login.** A run could start on a cached model login minutes from expiry, and a question then stalled when it expired. At startup, a login with less than 45 minutes left is now renewed.
- **No more infrastructure error from a contradictory judgment.** A "rework" judgment that also set `needs_web: true` failed validation, its format repair could not change it, and the question ended in `PIPELINE_INFRASTRUCTURE_ERROR`. The runtime now matches those flags to the verdict. v1.0.0 has this bug.
- **Salesforce searches stay on topic.** A question that mentions "a customer" only in general no longer needs a Salesforce search, and an audit lists the document-read tool only when it actually ran. An earlier draft of this release triggered a false tool claim that cost an extra audit.
- The Cursor stage's instructions now state the real $150 budget. They still said $50.

### Setup

- Opus searches the internal systems in your Claude Code MCP settings. The README shows how to add a missing one, such as SAFE.
- **Public-only runs.** `TRIPLE_STAMP_INTERNAL_SOURCES=off ./triple-stamp` gives Opus no internal systems, the same view someone outside Databricks gets. Your own Claude settings are not changed.

### Verification

- **Regression suite:** 442 tests pass on Omnigent 0.14.0 and 0.12.0, and again inside a public-only run.
- **Fresh install:** `./triple-stamp --self-test` passes on a new clone in a folder whose path has spaces, both normally and with `TRIPLE_STAMP_INTERNAL_SOURCES=off`.
- **Live runs:** 33 real questions in 11 rounds, three at a time, with and without a voice profile, inside Databricks and in public-only mode. They covered account questions that cite Salesforce records, customer emails with public links only, internal updates that keep their Jira, Confluence, and Slack links, and the v1.0.0 regression set. 27 stamped. Five exposed the three bugs fixed above. The last was a customer email that ended as a best supported answer: its research turned up an internal incident the email could not mention, so the judge asked for internal review before sending.
- **Final code:** 8 of 9 questions stamped, across public-only, voice, and no-voice rounds. The ninth was that customer email.
- **Side by side with v1.0.0:** on a question that names an account but not Salesforce, v1.1.0 ran a targeted Salesforce search every time and cited more of the account's records. On questions that ask for Salesforce by name, both versions did equally well.

### Known limitations

- With a short-lived model login, a session left open for more than about an hour can lose it, and the question in progress then stops. Type `/quit` and start `./triple-stamp` again. Renewing the login during a session is next.

### For maintainers

- `tools/triple_stamp_eval.py` accepts `expect_salesforce` and `audience: "customer"` per question. It checks that account answers cite Salesforce records and that customer answers carry no internal links.
- Real question sets and eval output are git-ignored: everything in `eval/` except the example, and `out/`. Keep them local, because they can carry customer data.

## v1.0.0 (2026-09-26)

The first stable release. Same three-stage pipeline, same install, sharper answers.

### Answers you can send

- **Send-ready by default.** Review-process notes, such as which internal systems were unavailable, stay out of the answer. They lead the answer's dossier instead.
- **Clickable citations everywhere.** Every source is a link copied from the evidence, including internal Jira, Slack, Confluence, and Glean records.
- **Evidence travels intact.** Each stage receives the exact packets collected before it, never a restatement, so nothing is lost to truncation.
- **Readable in the terminal too.** A bare result is written as a sentence (`5 + 5 = 10.`), because a line like `10.` renders as an empty list in the terminal.

### Reliability

- **Questions asked at the same time keep their own research.** Cursor workers now launch one at a time within a run, each after the previous one has claimed its own chat.
- **No more silent hangs after an audit repair.** A stamp that follows an Opus format repair is now published instead of leaving the chat without an answer.
- **No derailed cycles after a malformed packet.** While a format repair is pending, the runtime refuses any other stage and names the one it needs, so the supervisor cannot skip ahead.
- **No lost packets on long stages.** Inbox reads return before the tool-call deadline, so an abandoned read can never swallow a stage's result.
- **Room for heavy research.** The per-question budget is now $150 (was $50), so a thorough two-cycle answer no longer stops before its final judgment.
- **Stable supervisor identity** without telemetry, and a clean exit whenever every question finished with a verified answer.

### Everyday use

- **Plain progress lines**, such as `Step 2 of 3: auditing the research against internal sources (audit-cycle-1)`, instead of raw JSON.
- **Every answer is kept** the moment it finishes, with a dossier of how it was verified, in `~/.local/share/triple-stamp/answers`. Set `TRIPLE_STAMP_ANSWER_DIR` to move it or turn it off.

### Install and upgrades

- **Reproducible installs.** New private runtimes install the exact package set that passed testing (`.omnigent/runtime-lock/`). Triple-stamp never uses or changes your own Omnigent, so upgrading it cannot break Triple-stamp.

### Verification

- **Regression suite:** 432 tests pass on Omnigent 0.14.0 and 0.12.0.
- **Fresh install:** `./triple-stamp --self-test` passes on a new clone whose private runtime is built from the lock and matches it package for package.
- **Live runs:** real questions with and without a voice profile, three at a time, from a fresh clone and an existing install. Every question in the final round stamped and passed every check: send-ready text, clickable internal links, terminal rendering, research on topic, the exact answer shown in the chat, and the answer saved while the run was live.

### Known limitations

- The $150 per-question budget counts conservative estimates for usage whose cost the provider does not report, so exceptionally heavy research can still reach the cap before the final judgment.

### For maintainers

- `tools/triple_stamp_eval.py` runs a question set through a real launch and writes a scorecard: stamp, send-ready text, clickable internal links, terminal rendering, research on topic, and answers saved while the run is live. It makes paid model calls.
