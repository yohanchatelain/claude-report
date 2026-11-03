# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-03

### Added

- List Claude Code conversations that have background tasks, with titles, task
  descriptions, status, age and output size.
- `--follow` streams a task's output and exits when the task ends (0 if it completed, 1 otherwise).
- `--pick` chooses a task with fzf; fzf is also used when several tasks are running.
- `--print` and `--path` for a task's whole output and its log file path.
- Foreground commands the agent is waiting on are listed and followable, marked
  `(foreground)`; their status comes from the tool result.
- Follow the log file a command redirects to (`>`, `&>`, `| tee`), expanding variables
  assigned in the command or set in the environment.
- Filters: `--session` (id prefix, title or directory) and `--here`.
- `CLAUDE_CONFIG_DIR`, `CLAUDE_REPORT_TMP_ROOT` and `NO_COLOR` environment variables.
