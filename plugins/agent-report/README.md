# Agent Report

This package adds a reporting command to OpenMUX:

```sh
omux agent-report
```

The command analyzes only agents and sessions that OpenMUX already knows about through its Agent Sessions index at `~/.omux/agent-sessions.sqlite`. It then enriches those sessions from each agent's native local store and ranks only rows with strict token totals.

## What it does

- prints a terminal report with top agents, top models, and top sessions
- supports explicit date windows with `--from` and `--to`
- can export the same report as Markdown
- can export a self-contained static HTML dashboard with sortable tables

Default behavior:

- current calendar month through now
- strict token rankings only
- unsupported agents remain visible in the coverage section

## Install

```sh
omux plugins install agent-report
```

Refresh OpenMUX's session index before running the report if needed:

```sh
omux agent-sessions reindex
```

## Usage

```sh
omux agent-report
omux agent-report --from 2026-05-01 --to 2026-05-31
omux agent-report --agent codex --agent opencode
omux agent-report --markdown ~/Desktop/agent-usage.md
omux agent-report --html ~/Desktop/agent-usage.html
```

## Current strict sources

- `codex`: `~/.codex/state_5.sqlite`
- `opencode`: `~/.local/share/opencode/opencode.db`
- `copilot`: `~/.copilot/session-state/<session-id>/events.jsonl`
- `omp`: `~/.omp/agent/sessions/**/*.jsonl`

Agents without a strict token source in the current implementation are excluded from token leaderboards and called out in coverage.

## Development

Run the plugin directly:

```sh
plugins/agent-report/plugin --help
plugins/agent-report/plugin --limit 5
plugins/agent-report/plugin --html /tmp/agent-report.html
```
