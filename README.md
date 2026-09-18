<p align="center">
  <img src="docs/brand/hero.png" alt="Triple-stamp: three independent models review every answer" width="820">
</p>

<h3 align="center">Three models. Three companies. One answer you can trust.</h3>

<p align="center">
  Only the answer that survives all three ever reaches you.
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
    <td align="center">Researches the public web.</td>
    <td align="center">Audits it against internal systems.</td>
    <td align="center">Judges the result, then writes.</td>
  </tr>
  <tr>
    <td align="center"><sub>No internal access.</sub></td>
    <td align="center"><sub>No web access.</sub></td>
    <td align="center"><sub>No research of its own.</sub></td>
  </tr>
</table>

<br>

<p align="center">
  Two researchers cannot see each other's sources, so neither can cover for the other.<br>
  The final answer is copied from Codex byte for byte.<br>
  You get an answer that passed all three, or an honest failure. Never a guess.
</p>

---

## Install

```bash
uv tool install omnigent==0.12.0   # once
cursor-agent login                 # once
./triple-stamp                     # every time
```

> **Reference implementation.** It expects Databricks-internal infrastructure: the package proxy, Opus's Glean, Jira, Slack, Confluence, and SAFE access, and a Cursor account entitled to Grok 4.6 Extra High. Outside that environment the install will not finish. The full design stays readable in `config.yaml` and `agents/*/config.yaml`.

## Use

```bash
./triple-stamp
```

This is the only command you run. It opens the same run in two places:

- a **browser URL** printed to your terminal, and
- the **interactive terminal** prompt.

Ask your question in either one. Type `/quit` to stop the run and clean up.

<sub>A real run takes about 15 minutes to an hour. Opus's internal audit is slow on purpose. Each stage prints as it starts.</sub>

<br>

<p align="center">
  <img src="docs/brand/stamp-seal.svg" width="112" alt="Triple-stamp seal">
</p>

<p align="center">
  <sub>
    2 rounds by default, 4 in deep mode (<code>TRIPLE_STAMP_MAX_CYCLES=4</code>)
    &nbsp;·&nbsp; hard <b>$50</b> cap
    &nbsp;·&nbsp; answer relayed byte for byte
    &nbsp;·&nbsp; optional voice via <code>TRIPLE_STAMP_VOICE_PROFILE</code>
    &nbsp;·&nbsp; <code>./triple-stamp --self-test</code> checks the install and costs nothing
    &nbsp;·&nbsp; macOS only
  </sub>
</p>
