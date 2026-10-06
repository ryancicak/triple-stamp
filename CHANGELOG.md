# Changelog

## v1.2.6 (2026-10-06)

The audit takes a third of the time it did, with the same findings, and you can watch it think. A model request that stalls is retried after two minutes instead of five.

### Fixes

- **The audit runs at Opus 5.5's xhigh effort instead of max.** Opus 5.5 thinks more per turn than Opus 5 did at the same setting, and its max effort has no ceiling, so after the switch to Opus 5.5 audits took two to three times as long, even on simple questions. One audit took 41 minutes. Replaying the same audit input at both settings, max took 20 to 21 minutes and xhigh 8 to 10, at half the cost, with the same material findings. Anthropic reserves max for work where it measurably helps. The effort check now passes xhigh or higher, and an audit that ran lower still gets a fresh one.
- **The audit researches in a few wide rounds.** Opus plans its internal research first, sends each round's independent searches together, reads only the records that decide a claim, and stops once every claim is settled. Coverage is unchanged: every system the audit must try is still tried. A native Slack search takes about a minute and a half, so Opus makes one narrow one and reads the rest of Slack through Glean. Replayed audits took 6 minutes instead of 8 to 10, with the same findings. A public-only run, which has no tools, is unaffected.
- **You can see what Opus is thinking.** Opus streams short summaries of its thinking, so its terminal shows what it is working on instead of a long silent pause. Thinking and cost are unchanged. Without the summaries, a long final write carried only keep-alive signals, which Claude Code stops trusting after about five minutes, so a write that ran past about ten minutes was cancelled and started over.
- **A stalled model request is retried after two minutes.** A request the model gateway never answered waited five minutes of silence before Claude Code retried it, and one request took 16 minutes that way. Because the thinking summaries keep a healthy stream busy every few seconds, two quiet minutes now mean a stalled request.
- **A slow tool call can no longer cost the audit.** Claude Code moves a tool call that runs past two minutes to the background and delivers its result later as a new message. In one live run, that result arrived two seconds after Opus wrote its audit, started a second turn, and the pipeline collected the empty turn instead of the audit, so an account answer stamped without its Salesforce and usage evidence. Opus now waits for every tool call to finish, which costs a few minutes only when a call is unusually slow.
- **An audit that ran below the expected effort goes back for a fresh audit.** The note added to such an audit asked the judge for a rework by the researcher, while the judge's own rules ask for a fresh audit. Both now ask for a fresh audit.
- **An answer you asked for yourself keeps its internal caveats.** The judge's rendering rules called every answer "customer-facing", even though its audience rule says to write for the requester unless the question asks for something to send a customer. In one live run, an answer the requester asked for themselves left out internal caveats the audit had found. Both rendering rules now write for the reader that the audience rule picks.
- **Setup checks the audit's launch settings.** The startup check now launches Opus at the audit's effort and with its thinking summaries, so a Claude Code that rejects either stops at setup instead of at every audit.

### Verification

- **Replays:** two past questions, each audited from the same input at each setting.
  - At max: 21.0 and 20.1 minutes, $3.69 and $3.41.
  - At xhigh: 8.2 and 9.8 minutes, $1.83 and $1.71.
  - At xhigh with the new research rounds: 6.0 and 5.9 minutes, $1.43 and $1.18.
  - Every replay found the same material internal evidence.
- **Live runs, internal sources:**
  - An account usage question with a voice profile and the usage add-on stamped and passed 18 of 18 checks in 24.1 minutes, against a median of 49.3 minutes across five earlier runs at max. That includes a second audit the judge asked for.
  - An account Lakebase question stamped and passed 17 of 17 checks in 28.8 minutes, with a 7.1-minute audit against an earlier median of 19.1.
- **Live runs, public-only, no voice profile:**
  - The limits question stamped and passed 14 of 14 checks in 27.8 minutes. Its two audits took 5.7 minutes together. The first audit found contradictions in the public docs, so the judge sent the research back once.
  - The internal-plans question ended with its bounded answer in 12.6 minutes, against an earlier median of 14.9.
- **On the wire:** the live Opus ran at xhigh with thinking summaries, its transcripts carry the summaries, and no request was retried.
- **Regression suite:** 520 tests pass on Omnigent 0.14.0 and 0.12.0. Each new test was shown to fail when its fix is undone.

### Known limitations

- The databricks launcher profile keeps Claude Code's five-minute wait for a stalled request, because its gateway path can drop the thinking summaries.
- A judge can still ask for a second audit when internal evidence is incomplete, and a second research cycle when public sources conflict. Each adds a few minutes, now that every audit is shorter.

### For maintainers

- `OPUS_EFFORT` (`xhigh`) and `EFFORT_LEVELS` in `triple_stamp_runtime_state.py` set the audit effort. The validator, the effort observation, and the dossier labels read them, and `auth_preflight.py` keeps matching copies that a test compares. An observation is compliant when every assistant row ran at that level or higher. Its outcomes are now `all_expected` and `below_expected`.
- Opus launches with `--thinking-display summarized` (`OPUS_THINKING_DISPLAY`), which replaces any caller value. In the direct profile, `OPUS_STARTUP_ENV` adds `CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS=120000`, and the validator fails if the Opus sandbox does not pass every `CLAUDE_*` control through.
- Claude Code 2.1.281 counts at most 30 consecutive keep-alive pings toward its stream watchdog, and its stream idle timeout cannot go below five minutes. On a gateway base URL it skips its first-byte watchdog, so the byte idle timeout is the control that works there.

## v1.2.5 (2026-10-03)

A first start no longer sends a managed Codex to OpenAI's sign-in page, a judge whose Codex does not start gets a fresh one, and a long session keeps its logins.

### Fixes

- **A managed Codex starts without an OpenAI sign-in.** On a Mac where Codex reaches its model through a managed launcher, a first `./triple-stamp` opened OpenAI's email sign-in page if that launcher had never been run there. Setup now runs the launcher's Codex setup once, the way its own `codex` command does, and opens Codex's own sign-in only if Codex still has no login afterwards. v1.2.4 has this bug.
- **A signed-out Cursor gets its sign-in at setup.** `cursor-agent status` prints "Not logged in" and still reports success, so setup treated a signed-out Cursor as signed in and the launch stopped later with a command to run by hand. Setup now reads the answer. A status check that takes longer than 30 seconds no longer opens a sign-in page either; the launcher's own login check decides instead. v1.2.4 has this bug.
- **A judge whose Codex does not start gets one fresh start.** On a busy Mac, the judge's Codex can take longer than Omnigent's 30-second startup window, and the question ended there even though research and the audit had finished. That judge did no work, so it now gets one fresh Codex with the same evidence, and the rest of the cycle continues with it. A second start failure in the same cycle still ends the question. v1.2.4 has this bug.
- **A model gateway refusal no longer leaves the chat idle.** When the gateway answered the coordinator's valid login with 403, the turn that carried a finished audit failed, nothing woke the chat again, and it sat idle. A turn that has not sent any work is now repeated after 30, 60, 120, and 240 seconds, each time on a fresh Claude process that reads the login again. If the gateway still refuses after that, a stage that is still running keeps the question going, and otherwise the question ends with a message to ask again. v1.2.4 has this bug.
- **A long session keeps its logins.** A run kept the model gateway login and the usage login it started with, so a long session could lose them mid-question. While a run is open, the launcher now checks both every five minutes and renews them from your own logins before they expire, without opening a browser. v1.2.4 has this bug.
- **An audit the gateway never answers gets its retry, and the chat no longer closes silently.** A request that went unanswered for 68 minutes ended its audit with "Request timed out", which is now treated like the other transient model-service failures: the audit gets its one fresh retry. Omnigent's runner also stopped after an hour without activity, which closed the chat with no message during such a stall; a run now allows three hours. v1.2.4 has this bug.
- **A question about a customer is answered for you.** The judge wrote one answer about an internal account question as if it were going to the customer, so it left out the Salesforce records you asked about. It now writes for a customer only when the question asks for something to send them. v1.2.4 has this bug.

### Verification

- **Setup:** in a home folder whose Codex had never been set up through its managed launcher, setup set Codex up through the launcher, opened no OpenAI sign-in, and Codex then answered through it.
- **Live runs, recovery paths:** both faults were injected on purpose in test clones.
  - **Judge restart:** the first judge's Codex was held past its 30-second start window. The judge restarted once, and the account question, with a voice profile and the usage add-on, stamped and passed 17 of 17 checks in 24 minutes.
  - **Gateway refusal and login renewal:** the run's gateway login was spoiled before the question. The first two such runs ended after four refusals, because each repeat reused the refused Claude process. With every repeat on a fresh process, the renewer replaced the login five minutes in and the question stamped in 32 minutes, passing 15 of 16 checks. The one miss is the eval's check for unusual routing, which the four deliberate refusals trip.
- **Live runs, normal paths:** public-only, a question the public documentation answers stamped and passed 13 of 13 checks in 41 minutes, after two cycles. With a voice profile, the internal-plans question ended with the bounded answer and its single public-only gap line in 15 minutes, as in v1.2.4; it passed 12 of 13 checks, all but the stamp. The four completed runs cost about $108.
- **Regression suite:** 515 tests pass on Omnigent 0.14.0 and 0.12.0, with and without a voice profile set, and public-only. Each new test was shown to fail when its fix is undone.
- **Fresh install:** `./triple-stamp --self-test` passes from a plain Terminal environment on a new clone that installed its own private runtime and on a clone in a folder whose path has a space, each with and without a voice profile and public-only.

### Known limitations

- A question whose web research is still running after 20 minutes ends without an answer instead of retrying. Ask it again.
- An audit gets one retry per cycle, and so does a judge whose Codex does not start. If either fails twice, the question ends. Ask it again.
- Logins renew only through a login tool that can renew them without a browser. If the model gateway login cannot be renewed, a long question can still stop when it expires.

### For maintainers

- The judge restart is the stage `judge-retry-N-1` (`judge_retry`). It is routed only when a `judge-cycle-N` record failed with Omnigent's "Codex app-server never started a thread (" or "Codex native bridge state is missing" error, never for a Codex that is not signed in or for a cancelled judge. A runtime `[System-required next dispatch: ...]` line names it, `supervisor_contract` reserves it once per cycle and refuses `judge-cycle-N` while it is due, and after a web hop or an internal re-audit the cycle's judge continues as `judge-retry-N-1`. A stamp or a bounded answer from it is attested and accepted at exit like any other judgment.
- The coordinator retry repeats only a turn that sent nothing and failed with Omnigent's "Claude SDK provider authentication failed" error. Before each repeat it closes the session's Claude CLI with `close_session`, because the refused CLI keeps retrying its old request with the token it cached, and a repeat on it only reads that CLI's next stale refusal. The continuation ledger records each try as `provider_refusal_retried`. When the tries run out, a decided stamp or bounded answer is relayed, a stage that is still running is left to wake the chat (`provider_refusal_abstained`), and only otherwise is the question ended.
- The login renewer writes the run's copies of the gateway token file and `usage.json` with an atomic rename, logs each renewal to `login-renewals.log` in the run folder, and prints nothing while the run's terminal UI is live.
- `tools/triple_stamp_eval.py` now waits 150 minutes by default.

## v1.2.4 (2026-10-03)

A public-only run stays public: Opus no longer reaches for internal tools it does not have, a question that needs internal evidence ends without an extra audit, and the judge's Codex starts no MCP servers.

### Fixes

- **Public-only audits stop reaching for internal tools.** With `TRIPLE_STAMP_INTERNAL_SOURCES=off`, Opus was still told to try ToolSearch twice for each of Glean, Jira, Slack, Confluence, and SAFE. A public-only run has no such tools, so Opus wrote the calls out as text instead, and two audits kept repeating them for about 19 minutes until they hit the output limit. Opus is now told that the run has no internal system. It makes no tool call and records each system as not configured. v1.2.3 has this bug.
- **A public-only question that needs internal evidence ends without an extra audit.** When the judge asked for internal evidence in a public-only run, the supervisor started one more Opus audit anyway. That audit had nothing to search. It added about 10 minutes, and in one run it went silent for 10 minutes and failed. That audit is now refused, and the answer arrives right after the judge with a single gap line: it uses public sources only, so it could not check internal plans or records. v1.2.3 has this bug.
- **The judge's Codex starts no MCP servers in a public-only run.** Public-only mode removed the MCP servers only from Opus's settings, so the judge's Codex still started every MCP server in your Codex settings. The run's private copy of those settings now has none. Your own settings are not changed. v1.2.3 has this bug.
- **An answer without the stamp ends with its gap list.** Such an answer used to close with "These gaps are explicit because the completed evidence did not support final quality approval." It now ends with its list of remaining evidence gaps. v1.2.3 has this bug.

### Verification

- **Live runs:** 5 real questions in two rounds from new clones, with a voice profile and without. Public-only, a question the public documentation answers stamped in 16 minutes. A question about internal plans, asked with a voice profile and without, ended in 14 and 15 minutes with the single public-only gap line and cost at most $18.24; in v1.2.3 the same question took up to 49 minutes and $32.52. In every public-only audit, Opus made no tool call and recorded each internal system as not configured, and the judge's Codex had no MCP servers. With internal systems, two account questions stamped and passed every check, one with a voice profile and the usage add-on and one with neither. Their audits searched every configured internal system, and when a judge asked for more internal evidence, a second audit still ran, as before.
- **Regression suite:** 492 tests pass on Omnigent 0.14.0 and 0.12.0, with and without a voice profile set, and public-only.
- **Fresh install:** `./triple-stamp --self-test` passes from a plain Terminal environment on three new clones, one in a folder whose path has a space and one cloned from this repository over HTTPS, each with and without a voice profile and public-only. Each new install got exactly the tested packages.

### Known limitations

- With a short-lived model login, a session left open for more than about an hour can lose it, and the question in progress then stops. Type `/quit` and start `./triple-stamp` again. The usage login has the same limit: after about an hour, Opus answers from the other sources.
- If the model login has less than 45 minutes left at startup and its login tool does not renew it yet, the run starts with a warning, and a long question may stop when the login expires.
- A question whose web research is still running after 20 minutes, or whose judge fails to start, ends without an answer instead of retrying. Ask it again.
- An audit gets one retry per cycle. If the model service cancels it twice, the question ends. Ask it again.

### For maintainers

- In a public-only run, every Opus audit except a format repair gets a `[System-authoritative Opus launch context]` block in its handoff. Coverage receipts accept the new status `not_configured`, with empty `routes`, `tools_called`, and `queries`, only for a system missing from the run's configured list. The judge reads that status the same way, and it never by itself requires NEEDS_INTERNAL.
- `supervisor_contract` denies an `audit-internal-*` dispatch when the run is public-only and `_next_route` already ends the question best-effort with the reason `no internal system is configured for this run`. The supervisor prompt says the same, the refused stage shows no progress line, and the continuation guard then returns the bounded answer instead of an infrastructure error. A fresh audit that repairs an Opus ledger naming tools it never called still runs.
- `_drop_codex_mcp_servers` removes the `mcp_servers` tables and root-level `mcp_servers` keys from the run's copy of `.codex/config.toml`, keeps every other line as written, and stops the launch unless the result parses to the same settings minus `mcp_servers`.

## v1.2.3 (2026-10-02)

`./triple-stamp` starts again after Claude Code's managed login changed, and a new install in a folder with a space gets the tested packages.

### Fixes

- **Starts with any Claude Code login command.** After Claude Code's managed login command changed, `./triple-stamp` stopped at startup with `managed Claude apiKeyHelper is unavailable or has an unexpected shape`, because it accepted only the old command. It now runs whatever login command Claude Code is set to use, the way Claude Code runs it, and a real Claude call inside the run's sandbox still proves the login before any question. If the command fails, Triple-stamp tries one silent token refresh where the login tool has one and never opens a browser. If that does not help, it stops with the command's own reason and the one command that signs you in. A command that hangs is not run again. v1.2.2 has this bug.
- **`--self-test` passes with a voice profile set.** With `TRIPLE_STAMP_VOICE_PROFILE` set, `./triple-stamp --self-test` failed three regression tests that did not expect a voice profile. Only those tests changed; questions were never affected. v1.2.2 has this bug.
- **A folder with a space gets the tested packages.** uv splits a constraint file's path at spaces, so a new install in a folder like `My Projects` got the newest compatible packages instead of the tested set. It now gets the tested set, like every other folder. v1.2.2 has this bug.

### Verification

- **Live runs:** Two rounds of two real questions each, one with a voice profile from a plain Terminal and one without. Both rounds started with the new login command, passed the sandbox's Claude and Codex checks, and finished web research on all four questions. The first answer to finish stamped, in the saved voice; the other three were still in their audits at release time. An existing install updated in place also started with a voice profile.
- **Regression suite:** 486 tests pass on Omnigent 0.14.0 and 0.12.0, with and without a voice profile set, and public-only.
- **Fresh install:** `./triple-stamp --self-test` passes on new clones with and without a voice profile and public-only, both in a folder whose path has a space, now with exactly the tested packages, and from a plain Terminal environment, which finds the model gateway in Claude Code's managed settings. A new Omnigent 0.12.0 install in a folder with a space also gets exactly its tested packages and passes with a voice profile.

### Known limitations

- With a short-lived model login, a session left open for more than about an hour can lose it, and the question in progress then stops. Type `/quit` and start `./triple-stamp` again. The usage login has the same limit: after about an hour, Opus answers from the other sources.
- If the model login has less than 45 minutes left at startup and its login tool does not renew it yet, the run starts with a warning, and a long question may stop when the login expires.
- A question whose web research is still running after 20 minutes, or whose judge fails to start, ends without an answer instead of retrying. Ask it again.
- An audit gets one retry per cycle. If the model service cancels it twice, the question ends. Ask it again.

### For maintainers

- The launcher no longer parses Claude Code's managed `apiKeyHelper`. It runs the command with `/bin/sh -c` and your real home, as Claude Code does, and keeps only a printed token of at least 20 characters. Stand-in login commands in the unit tests cover the managed command of 2026-10-02 character for character, the earlier command in any argument order or found on `PATH`, a path with a space, a pipeline, and commands that fail, hang, or return a nearly expired token.
- `--self-test` runs the suite in your own environment, so the tests that build voice-dependent handoffs now turn the voice profile off for themselves.
- The bootstrap tests' stand-in uv splits a `--constraint` path at spaces, as uv 0.11 does, so the tested-set install is checked from a folder with a space.

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
