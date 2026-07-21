# Configured Agent Defaults Design

## Goal

Allow `workflow.writer` and `workflow.reviewer` in the TOML config to supply
agent choices when their CLI flags are omitted.

## Design

`argparse` will leave `--writer` and `--reviewer` as `None` rather than reject
the command before the config can load. Existing config resolution assigns CLI
values first and config values second. The existing post-config validation
continues to reject either missing value with the current actionable error.

CLI values remain authoritative over config values. Invalid CLI or config
values remain rejected by the existing choices/config behavior.

## Scope

Modify only the two argument definitions and user-facing documentation. Add a
small standard-library regression test that invokes the script with a config
that supplies both agents and `--dry-run`.
