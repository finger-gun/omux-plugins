# Webpage Agent Tool

This package adds the first custom agent tool plugin to the OpenMUX registry.

## What it does

`agenttools.webpage` contributes one agent tool:

- `agenttools.webpage.read-url`

The tool accepts a single text input where the first non-empty line is a URL and any following lines are optional focus instructions. It fetches the page with `curl`, extracts readable text with Python stdlib HTML parsing, and then runs a nested `omux agent -p` pass to return a compact plain-text summary suitable for the small local model context window.

If nested summarization is unavailable, the plugin falls back to deterministic cleaned webpage text with title and source metadata.

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
https://example.com/docs
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
omux agenttools.webpage https://example.com
omux agenttools.webpage https://example.com "Focus on release changes"
```

## Development

Direct CLI smoke test:

```sh
plugins/agenttools.webpage/plugin https://example.com
```

Direct callback smoke test:

```sh
printf '{"input":"https://example.com
Summarize the main point"}'   | plugins/agenttools.webpage/plugin __omux_agent_tool
```
