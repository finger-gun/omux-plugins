# OpenMUX Plugins Registry

This repository is the official OpenMUX plugin registry. It hosts installable plugin packages that can be discovered and copied into a user's local OpenMUX configuration with the `omux` CLI.

OpenMUX is a terminal-first, hackable workspace for developers. The main project lives at:

https://github.com/finger-gun/omux

## How the registry works

The registry root contains `catalog.toml`. Each package entry points to an `omux-plugin.toml` manifest in `plugins/<package-id>/`.

```text
catalog.toml
plugins/
  hello-pane/
    omux-plugin.toml
    plugin
```

Users discover and install packages with:

```sh
omux plugins discover
omux plugins install <plugin-id>
omux plugins update <plugin-id>
omux plugins uninstall <plugin-id>
```

For local testing against a clone of this repository:

```sh
omux plugins discover --registry file://$HOME/projects/omux-plugins
omux plugins install hello-pane --registry file://$HOME/projects/omux-plugins
omux plugins install macos-notify --registry file://$HOME/projects/omux-plugins
```

Installing a package copies its files into `~/.omux/plugins/<command>/`. OpenMUX never runs code from the registry during discovery or install; installed plugins run later as local executable commands.

## Package format

`catalog.toml` lists available packages:

```toml
schema = 1

[packages.hello-pane]
kind = "plugin"
name = "Hello Pane"
description = "Creates a sample extension pane."
version = "0.1.0"
path = "plugins/hello-pane/omux-plugin.toml"
tags = ["demo", "extension-pane"]
```

Each package manifest declares the command, entrypoint, and files to install:

```toml
schema = 1
id = "hello-pane"
name = "Hello Pane"
description = "Creates a sample extension pane."
version = "0.1.0"
license = "Apache-2.0"
kind = "plugin"

[plugin]
command = "hello-pane"
entrypoint = "plugin"

[files.entrypoint]
source = "plugin"
target = "plugin"
executable = true
```

Plugin entrypoints can be Bash, TypeScript, native binaries, or any other executable format with an appropriate shebang. Plugins receive their CLI arguments unchanged and can call the public `omux` CLI to interact with OpenMUX.

## Current plugins

The initial registry packages include a minimal extension-pane example and a small macOS automation helper:

| Package | Command | What it does |
| --- | --- | --- |
| `hello-pane` | `omux hello-pane` | Creates a sample extension pane with local HTML content. |
| `macos-notify` | `omux macos-notify` | Sends a native macOS notification using the built-in notification system. |

OpenMUX also ships a bundled `markdown-preview` plugin and a built-in `omux notify` command in the main app. They are not duplicated here because built-in `omux` commands take precedence over external plugins and cannot be shadowed by registry packages.

## Trust model

Plugins are executable local code. Review package contents before installing packages from any registry you do not control. OpenMUX prints the package source, version, and target paths before installation.
