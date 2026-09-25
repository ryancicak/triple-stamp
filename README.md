<p align="center">
  <img src="docs/brand/hero.png" alt="Triple-stamp: public research, internal evidence, one customer-safe answer" width="820">
</p>

<h3 align="center">Public research. Internal evidence. One customer-safe answer.</h3>

<p align="center">
  Public web and workspace evidence first. Internal systems if you have them. Then a packet judge writes the answer.<br>
  Add a saved voice profile and Codex can make it sound like you, then re-check the claims against the packets.
</p>

<br>

<table align="center" width="100%">
  <tr>
    <td align="center" width="33%"><img src="docs/brand/starfish-pink.svg" width="92" alt="Pink starfish, the Cursor researcher"></td>
    <td align="center" width="33%"><img src="docs/brand/starfish-amber.svg" width="92" alt="Amber starfish, the Opus auditor"></td>
    <td align="center" width="33%"><img src="docs/brand/starfish-teal.svg" width="92" alt="Teal starfish, the Codex judge"></td>
  </tr>
  <tr>
    <td align="center"><b>Cursor</b></td>
    <td align="center"><b>Opus</b></td>
    <td align="center"><b>Codex</b></td>
  </tr>
  <tr>
    <td align="center">Researches the public web and this workspace.</td>
    <td align="center">Attempts Glean, Jira, Slack, Confluence, and SAFE.</td>
    <td align="center">Judges capped packets, then writes.</td>
  </tr>
  <tr>
    <td align="center"><sub>No internal MCP tools.</sub></td>
    <td align="center"><sub>No web access.</sub></td>
    <td align="center"><sub>Instructed not to research; may read your voice-profile file.</sub></td>
  </tr>
</table>

<br>

<p align="center">
  Cursor gathers public and workspace evidence. Opus checks the internal systems it can reach.<br>
  Codex is instructed to judge those handoffs, not start its own research. Triple-stamp relays its final answer byte for byte.
</p>

> **Read it before you send it.** Triple-stamp writes one answer and tries to keep it customer-ready, but it does not split the answer into a customer half and an internal half, and it does not strip internal details out for you. Whatever the judge writes is what you get, word for word. Give it a quick read before you forward it.

---

## Install

Clone it and run it:

```bash
git clone https://github.com/ryancicak/triple-stamp
cd triple-stamp
./triple-stamp
```

That is the whole setup. The first run:

- installs `uv` into your user account if needed;
- uses an existing compatible Omnigent `0.12.x` or `0.14.x`, or installs a
  private Omnigent `0.14` runtime on Python 3.13;
- repairs `claude-agent-sdk` to the required `0.2.152`;
- installs Cursor Agent, Claude Code, and Codex from their official installers;
- opens browser login only for a CLI that is not already authenticated;
- verifies the exact models, sandbox, runtime surfaces, and bundle before any
  research starts; and
- opens the browser UI and interactive terminal.

It never uses `sudo`, replaces another Omnigent installation, or silently
substitutes a cheaper model. Setup is idempotent, so every later run quickly
re-verifies the same invariants and starts.

On a Databricks SA laptop, existing managed Claude/Codex routing and credentials
are detected and preserved. On a vanilla Mac, the three public account logins
are used. Either way, the command is still just `./triple-stamp`.

The account must actually include Cursor Grok 4.6 Extra High, Claude Opus 5, and
the configured Codex model. A script can install software and open login pages;
it cannot grant model entitlements. Missing entitlement therefore fails closed
with the exact account/model that needs attention.

To bootstrap the required tools (if needed) and run live entitlement/model
checks without starting a research run:

```bash
./triple-stamp --self-test
```

Automation escape hatches are intended for CI and troubleshooting:

- `TRIPLE_STAMP_BOOTSTRAP=0` disables first-run installation.
- `TRIPLE_STAMP_BOOTSTRAP_OFFLINE=1` permits only already-installed tools.
- `TRIPLE_STAMP_BOOTSTRAP_OMNIGENT_VERSION=0.12.0` makes a newly created private
  runtime use the other supported line. Existing compatible `0.12` or `0.14`
  runtimes are always accepted after capability verification.

## Use

```bash
./triple-stamp
```

This is the only command you run. It opens the same run in two places:

- a **browser URL** printed to your terminal, and
- the **interactive terminal** prompt.

Ask your question in either one. Type `/quit` to stop the run and clean up.

<sub>A real run takes about 15 minutes to an hour. Opus's internal audit is slow on purpose. Each stage prints as it starts.</sub>

## Current-source research by default

Triple-stamp treats the run date as part of every research request. Cursor
records source publication or last-updated dates, checks the active
product/API/version, and searches specifically for a newer official replacement
when an older page surfaces. Opus applies the same rule to internal documents,
and Codex will not stamp a current claim that rests on unexplained stale or
superseded evidence.

This is quality-aware, not a newest-date-wins rule. Current official guidance
outranks a newer low-quality post. Older sources remain valid when they are
verified as still current or are needed for history, but the evidence packet
must label that use explicitly.

## Make it sound like you

The answer does not have to sound like generic AI prose. Triple-stamp can render it in your saved voice profile: your cadence, level of detail, and preferred phrasing.

```bash
export TRIPLE_STAMP_VOICE_PROFILE="$HOME/path/to/your-voice-profile.md"
./triple-stamp
```

Set the variable to an absolute path to a non-empty UTF-8 Markdown file. Codex reads that exact file on every run. Leave the variable unset for plain professional prose.

The profile changes how the supported answer is written, not what counts as evidence. Codex re-checks the wording against the packets before it can stamp the answer.

Use [`tests/fixtures/example-voice-profile.md`](tests/fixtures/example-voice-profile.md) as a starting point.

## Without Glean

You do not need Glean to run Triple-stamp. Opus still tries every internal system it knows about: Glean, Jira, Slack, Confluence, and SAFE.

There is one catch, and it is on purpose. If a question needs internal evidence and Glean is set up but never answers, the run ends unstamped instead of guessing. You still get the best answer the evidence supports, with the missing pieces named, but it does not carry the stamp.

<br>

<p align="center">
  <img src="docs/brand/stamp-seal.svg" width="132" alt="Triple-stamp seal: public, internal, judge">
</p>

<p align="center">
  <sub>
    answer relayed byte for byte
    &nbsp;·&nbsp; optional saved voice via <code>TRIPLE_STAMP_VOICE_PROFILE</code>
    &nbsp;·&nbsp; <code>./triple-stamp --self-test</code> may install missing tools, open first-time login, and perform live model checks without starting a research run
    &nbsp;·&nbsp; macOS only
  </sub>
</p>
