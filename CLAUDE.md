# Ghosted

Lists what Claude Code sessions left open (interrupted sessions, dated promises, decisions waiting on the user).
Pull first: `/ghosted [today|7d|30d|all]` / `ghosted report`; pushes are optional (`ghosted init`).
Called Loose Ends (`le`, `LE-0012`) until 2026-11-11; contract 16.19 lists what was renamed.
Build contract (interfaces, formats, rules): `docs/contract.md`. Read it before changing anything.

## Commands
- Tests (both must pass): `/usr/bin/python3 -m unittest discover -s tests -t .` and `python3 -m unittest discover -s tests -t .`
- Real-session fixtures + benchmark (local only, prints ids/verdicts only): `python3 tests/real/benchmark.py`
- CLI: `ghosted --help` (`bin/ghosted`; `ghosted install --bin` links it, the plugin's `bin/` puts it on PATH). Data lives in `~/.ghosted/`.
- Report: `ghosted report [today|Nd] [--soft] [--json] [--plain]`.
- Plugin check: `claude plugin validate .`. Skill test against synthetic data only:
  `GHOSTED_HOME=<tmp> GHOSTED_PROJECTS_DIR=<tmp> claude -p --plugin-dir . "/ghosted 7d"`.
  Sessions launched from a temp folder are skipped (contract 16.13): synthetic sessions there need
  `[skip] temp_folders = false` in that home's `config.toml`.

## Rules that are easy to break
- **Never print session transcript text** into a terminal, log, test output or commit. `ghosted report` prints quotes BY
  DESIGN (the product, for the user's own eyes): a dev session never runs it on real data except with `--json` (no quotes) piped
  into a counts-only filter, in a throwaway `GHOSTED_HOME` (so the user's first-run 30d report stays theirs). Transcripts hold
  customer data and keys. Real-transcript checks print verdicts, ids and counts only.
- Nothing derived from transcript text is uploaded or stored: interrupted titles are a fixed template; evidence is machine
  facts; report quotes live only in the terminal (never in items/state/digest/measure/--json without --quotes).
- File names reach a terminal only through `ghosted mixed N --session ID --files` (contract 16.11); `touches.json`
  holds repo paths, ints and bools only. A dev session never runs `ghosted mixed --files` on real repos.
- Tiers: waiting = structured signals + direct questions + save offers with unsaved work; soft = polite offers (contract 16.1).
- The plugin ships skills only, never hooks: installing it must have zero side effects.
- Clean room: never open or read the `openloops` project.
- A real-session manifest lives in `~/.ghosted/fixtures/` (contract 16.8), never in the repo: it names real sessions.
- Core is Python 3.9+ stdlib only (`/usr/bin/python3` is 3.9.6).
- No hard-coded home paths; paths come from `~/.ghosted/config.toml`.
- No real names or real session ids in the repo: tests and examples use made-up people, projects and ids (Alex, `boathouse`, `metronome`).
  Owners are `me` and `claude` only (contract 16.14).
- The hook (`ghosted hook`) must never block or print: always exit 0.
- Secrets: `~/.ghosted/.env` (chmod 600). Never committed.
- The phone owns status. The Mac never writes status except an explicit `ghosted done/snooze/dismiss`; the detector never closes
  anything, and reopens an interrupted item only on a flagged ending newer than its close (contract 6; the server applies the same rule).
