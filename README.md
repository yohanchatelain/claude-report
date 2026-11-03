# claude-report

[![CI](https://github.com/yohanchatelain/claude-report/actions/workflows/ci.yml/badge.svg)](https://github.com/yohanchatelain/claude-report/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

Follow the builds and test runs that a [Claude Code](https://claude.com/claude-code) agent
starts in the background — from any terminal, for any conversation — without hunting for
their output files.

```console
$ claude-report
● Fix the checkout flow in fakeapp  (1 running)
  /home/me/src/fakeapp  ·  3f2a9c1e-7b4d-4e8a-9c0f-1d2e3f4a5b6c
  b4k7xq2mz  failed       45m ago    41KB  Build fakeapp after the dependency bump
  b9p3rt8wd  completed     5m ago    328B  Clean rebuild of fakeapp
  b2n6yh5cs  running       1m ago      0B  Run the full test suite

$ claude-report -f pytest
==> b2n6yh5cs [running] Run the full test suite
    conversation: Fix the checkout flow in fakeapp  (/home/me/src/fakeapp)
    $ uv run pytest -q > .../scratchpad/pytest_run.log 2>&1
    .../scratchpad/pytest_run.log
tests/test_checkout.py ................................................. [ 20%]
........................................................................ [ 41%]
...
==> b2n6yh5cs completed
```

## Why

When Claude Code runs a long command in the background, its output goes to a file named
after a random task id inside a per-session temporary directory. Agents also often redirect
output to their own log file (`pytest > run.log 2>&1`), leaving the task's file empty. To
watch progress you have to find the right session directory, the right task id, and check
whether the command was redirected.

`claude-report` does that for you:

- lists every conversation that has background tasks, by **title**, with each task's
  description, status (running / completed / failed / killed), age and output size;
- **follows** a task like `tail -f`, and **stops when the task ends**, with exit code `0` if
  it completed and `1` otherwise;
- also covers **foreground** commands the agent is waiting on (a long `make` it did not
  send to the background);
- notices `> file`, `&> file` and `| tee file` in the command and follows **that** log
  file, expanding variables set in the command (`S=/tmp/x; make > $S/build.log`);
- picks the running task automatically, or lets you choose one with
  [fzf](https://github.com/junegunn/fzf) (with an output preview).

## Installation

Requires Python 3.10+ on Linux or macOS. No runtime dependencies.

```bash
uv tool install git+https://github.com/yohanchatelain/claude-report
# or
pipx install git+https://github.com/yohanchatelain/claude-report
```

`fzf` is optional; it is used by `--pick` and when several tasks are running at once.

## Usage

```text
claude-report                   recent conversations with background tasks (newest last)
claude-report -f [QUERY]        follow a task until it ends (default: the running one)
claude-report --pick [QUERY]    choose a task with fzf, then follow it
claude-report -p QUERY          print a task's whole output
claude-report --path QUERY      print the path of a task's log file
```

`QUERY` matches a task id exactly, or is a case-insensitive substring of a task's
description, command or conversation title.

| Option | Meaning |
| --- | --- |
| `-s, --session QUERY` | only conversations whose id starts with, or whose title or directory contains, `QUERY` |
| `--here` | only conversations started in the current directory or one of its parents |
| `-n, --lines N` | lines of existing output shown when following; `-1` for all (default 40) |
| `-l, --limit N` | number of conversations listed; `0` for all (default 5) |
| `-V, --version` | print the version |

### Which task does `--follow` choose?

1. Candidates are all tasks matching `QUERY` (all tasks if no query), across the selected
   conversations.
2. If exactly one candidate is running, it is followed.
3. If several are running, you choose with fzf; without fzf or a terminal, the newest wins.
4. If none is running, the newest candidate is shown (and `--follow` returns at once).

### Recipes

```bash
# Notify when the agent's test run finishes
claude-report -f pytest && notify-send "tests passed" || notify-send "tests failed"

# Follow the running task of a given conversation
claude-report -f -s "checkout flow"

# Use your own pager or tools on the log
less +F "$(claude-report --path build)"
```

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success; for `--follow`, the task completed |
| 1 | `--follow`: the task failed or was killed |
| 2 | no matching conversation or task, or invalid arguments |
| 130 | interrupted (Ctrl-C, or fzf cancelled) |

## How it works

`claude-report` only reads files; it never talks to Claude Code or modifies anything.

| Data | Location |
| --- | --- |
| Task output | `$TMPDIR/claude-<uid>/<project-slug>/<session-id>/tasks/<task-id>.output` |
| Transcript | `~/.claude/projects/<project-slug>/<session-id>.jsonl` |

The transcript links each background task id to the tool call that started it
(description and command), records a `<task-notification>` with the final status when the
task ends, and holds the conversation's title and working directory. A task with no
notification yet is reported as `running`.

Foreground commands are also streamed to a task output file while they run, but the
transcript does not name their task id until they return. `claude-report` pairs output files
no background task claims with the Bash calls still waiting for a result (newest with
newest) and marks them `(foreground)`. Their status comes from the tool result: `failed` if
it is flagged as an error (non-zero exit), `completed` otherwise.

Both locations can be overridden, which is also how the test suite works:

| Variable | Default |
| --- | --- |
| `CLAUDE_CONFIG_DIR` | `~/.claude` (the same variable Claude Code uses) |
| `CLAUDE_REPORT_TMP_ROOT` | `$TMPDIR/claude-<uid>` (or `/tmp/claude-<uid>`) |
| `NO_COLOR` | set to disable colours |

### Limitations

- The file layout and transcript format are Claude Code internals, not a public API. They
  may change between Claude Code versions; please open an issue if something breaks.
- A task started less than a second ago may appear as `(unknown command)` until Claude Code
  writes it to the transcript.
- If a Claude Code session dies while a task runs, no notification is written and the task
  stays `running`; stop following with Ctrl-C.
- Redirect detection is a heuristic on the command text: it only considers files that exist,
  expands `$VAR`/`${VAR}` from assignments in the command and then from your environment,
  and resolves relative paths against the conversation's starting directory. It does not
  follow `cd` inside the command.
- Foreground commands are matched to output files by order, which is unambiguous in the
  usual case of one command at a time per conversation.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md).

```bash
uv sync
uv run pytest --cov
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

## License

[MIT](LICENSE)
