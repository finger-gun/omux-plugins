# KiloCode Agent Sessions Plugin

This package is an Agent Sessions plugin for OpenMUX. It adds KiloCode sessions to the OpenMUX Agent Sessions sidebar without adding KiloCode-specific adapter code to OpenMUX core.

## What it does

KiloCode stores sessions in a local SQLite database:

```text
~/.local/share/kilo/kilo.db
```

The plugin reads the `session` table and emits normalized JSON rows that OpenMUX can index.

## Manifest

```toml
[plugin]
command = "agentsessions.kilocode"
entrypoint = "plugin"

[agent-sessions]
name = "kilocode"
callback = "__omux_agent_sessions"
arguments = ["discover"]
source_kind = "kilocode_sqlite"
resume_command = "kilo -s {session_id}"
```

## Install

Install from the default OpenMUX plugin registry:

```sh
omux plugins install agentsessions.kilocode
```

Then reindex Agent Sessions:

```sh
omux agent-sessions reindex
```

## Configuration

```toml
[agent-sessions.external.kilocode]
enabled = false
```

## Development

```sh
plugins/agentsessions.kilocode/plugin __omux_agent_sessions discover
KILOCODE_DB=/tmp/kilo.db plugins/agentsessions.kilocode/plugin __omux_agent_sessions discover
```
