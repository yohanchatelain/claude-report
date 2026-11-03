# Contributing

Thanks for helping! Bug reports are especially useful when Claude Code changes its file
layout: please include your Claude Code version (`claude --version`) and, if you can, a
redacted excerpt of the transcript lines involved.

## Setup

The project uses [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/yohanchatelain/claude-report
cd claude-report
uv sync                    # creates .venv with the dev tools
uv run claude-report       # run from the checkout
```

## Checks

CI runs all of these on Linux and macOS for Python 3.10 to 3.13; run them before opening a
pull request:

```bash
uv run ruff check .            # lint
uv run ruff format --check .   # formatting (use `uv run ruff format .` to fix)
uv run mypy                    # strict type checking of src/ and tests/
uv run pytest --cov            # tests; coverage must stay at or above 90%
```

## Tests

Tests never touch your real Claude Code data. The `claude` fixture in
`tests/conftest.py` builds a fake layout in a temporary directory and points
`CLAUDE_CONFIG_DIR` and `CLAUDE_REPORT_TMP_ROOT` at it. Use its helpers to describe a
scenario:

```python
def test_example(claude):
    s = claude.session("/work/proj", "session-id", title="Fix the build")
    s.add_task("b1", "make -j8", "Build everything", output="ok\n", status="completed")
    ...
```

When Claude Code's transcript format changes, update the fixture so it writes the new
format, and keep a test for the old one if both remain in the wild.

## Pull requests

- One topic per pull request, with tests for behaviour changes.
- Update `README.md` for user-visible changes and add an entry under *Unreleased* in
  `CHANGELOG.md`.
