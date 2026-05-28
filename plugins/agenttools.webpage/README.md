# Webpage Agent Tool

This package adds the first custom agent tool plugin to the OpenMUX registry.

## What it does

`agenttools.webpage` contributes one agent tool:

- `agenttools.webpage.read-url`

The tool accepts a single text input where the first non-empty line is a URL and any following lines are optional focus instructions. It fetches the page with `curl`, extracts readable text with Python stdlib HTML parsing, and keeps the extracted chunks in document order.

By default it returns focused extracted content directly. That is also what happens when `omux agent` calls it as a tool, so the active agent can summarize the returned content without spawning another agent. Direct CLI usage can opt into the bounded multi-pass nested `omux agent -p` summarization flow with `--summarize`.

## Manifest

The relevant capability is declared in `omux-plugin.toml`:

```toml
[plugin]
command = "agenttools.webpage"
entrypoint = "plugin"

[agent-tools.read-url]
description = "Fetch a webpage from a URL and return a compact readable summary."
callback = "__omux_agent_tool"
input_hint = "First line: URL. Optional following lines: what to focus on."
```

OpenMUX exposes this to the model as `agenttools.webpage.read-url`.

## Input contract

The tool input is a single string:

```text
https://openmux.fingergun.dev
Summarize the API changes and ignore marketing copy.
```

Rules:

- first non-empty line: `http` or `https` URL
- optional following lines: focus or extraction goals
- unsupported schemes are rejected

## Install

```sh
omux plugins install agenttools.webpage
```

Then start `omux agent` and run `/tools` to confirm the tool is available.

## Disable

Plugin-defined agent tools are enabled by default. Disable only this provider with:

```toml
[agent.external.agenttools.webpage]
enabled = false
```

## CLI usage

The plugin also works as a normal CLI command for local testing:

```sh
omux agenttools.webpage https://openmux.fingergun.dev
omux agenttools.webpage https://openmux.fingergun.dev "Focus on release changes"
omux agenttools.webpage --summarize https://openmux.fingergun.dev "Focus on release changes"
```

The optional extra arguments are joined into one focus string and passed through as guidance for the parent agent or the `--summarize` model prompts. The plugin does not deterministically rank or reorder extracted chunks.

## Development

Direct CLI smoke test:

```sh
plugins/agenttools.webpage/plugin https://openmux.fingergun.dev
```

Default CLI mode and callback mode intentionally do not spawn another `omux agent -p`; nested local model calls can stall while the parent interactive agent is waiting for a tool result.

Direct callback smoke test:

```sh
printf '{"input":"https://openmux.fingergun.dev
Summarize the main point"}'   | plugins/agenttools.webpage/plugin __omux_agent_tool
```
