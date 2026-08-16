# Antigravity Agent Sessions Plugin

This package adds local [Antigravity](https://antigravity.google/) trajectories to the OpenMUX Agent Sessions sidebar. It resumes a selected trajectory with `agy --conversation <id>`.

## Data source and privacy

Antigravity stores trajectory summaries in its local VS Code state database:

```text
~/Library/Application Support/Antigravity/User/globalStorage/state.vscdb
```

The database contains a base64-encoded protobuf value rather than a public sessions table. The plugin reads it in read-only mode and emits each available trajectory ID, a truncated initial-prompt excerpt, and its local workspace path when present. As a result, prompt excerpts are visible in the OpenMUX sidebar.

Antigravity does not publish this storage format as a stable API. The adapter indexes valid summaries and skips incompatible records with a diagnostic on stderr.

## Install and use

```sh
omux plugins install agentsessions.antigravity
omux agent-sessions reindex
```

To run discovery directly:

```sh
plugins/agentsessions.antigravity/plugin __omux_agent_sessions discover
```

For testing or nonstandard installations, point the plugin to another state database:

```sh
ANTIGRAVITY_STATE_DB=/tmp/state.vscdb \
  plugins/agentsessions.antigravity/plugin __omux_agent_sessions discover
```

The session resume command is:

```sh
agy --conversation <session-id>
```
