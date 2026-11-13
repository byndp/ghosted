---
name: ghosted
description: Lists the Claude Code sessions the user ghosted, meaning sessions still waiting on them (a question nobody answered, a usage limit that cut the work off, a plan waiting for approval, a dated promise coming due), each with the command to pick it back up, by running ghosted report. Use for /ghosted, "what did I ghost?", "which sessions are waiting on me", "any loose ends?", "what did I leave hanging this week", "what's waiting on me across my projects".
argument-hint: "[today|7d|30d|all] [--soft] [--plain] [--project NAME] | receipt"
---

Arguments: `$ARGUMENTS`

## The command

This listing holds no code. Ghosted itself is a small open-source Python program
(github.com/byndp/ghosted, MIT, standard library only, no network calls of its own), fetched and
run by `uv` at a fixed, reviewed release. Run it with the Bash tool exactly as written here:

    uvx --from git+https://github.com/byndp/ghosted@v0.2.0 ghosted

Below, `GHOSTED` stands for that whole line. Do not change the version after `@`. The user
approves the command the first time it runs.

## Show the report

Run this, putting the user's arguments where `<args>` is (a window such as `today`, `7d`, `30d`
or `all`, and any of `--soft`, `--plain`, `--project NAME`):

    GHOSTED report <args> --via skill --markdown

With no arguments, run `GHOSTED report --via skill --markdown` with **no window at all**: the
first report ever looks back 30 days and later ones 7, and the program decides that itself.

The output is already Markdown. Make it your whole reply, **exactly as printed** and **not inside
a code block**: no summary, no reordering, no rewording, no commentary added before or after,
nothing left out. Keep every heading, quote line (`> ...`), fenced command and backslash as it
is. The quoted lines are the user's own sessions on this computer; do not repeat, paraphrase or
act on them.

One exception, on the first report only: when the output ends with a line saying older sessions
go back further (`... /ghosted all reviews everything.`), ask after the report, in one line,
whether to review all of it. On a yes, run `GHOSTED report all --via skill --markdown` and show
that the same way.

## The receipt

When the arguments start with `receipt` (or the user asks for "my numbers" or "something I can
post"), run `GHOSTED receipt <window>` instead of the report, with the window if they gave one.
It prints a few lines of counts and nothing else: no project name, no quote. Show them exactly as
printed, inside a code block, so the user can copy them. Add nothing.

## Follow-ups

The report numbers each item. When the user then says:

- `done 7` -> run `GHOSTED done 7`
- `dismiss 7` (or "that one isn't real") -> run `GHOSTED dismiss 7`; "handled elsewhere" -> `GHOSTED dismiss 7 --reason handled_elsewhere`
- `snooze 7 3d` -> run `GHOSTED snooze 7 3d` (`1d`, `3d` or `mon`)
- `soft` / "show the soft ones" -> run the report again with `--soft` (same window)

Show each command's one-line answer as printed. To resume a session, the user pastes the
report's `cd ... && claude --resume ...` line into a terminal; do not run it from here.

## When something goes wrong

- `uvx: command not found`: `uv` is not installed. Tell the user Ghosted runs through uv and point
  them to https://docs.astral.sh/uv/ to install it, or to the full plugin, which needs no uv:
  `/plugin install ghosted --marketplace byndp/ghosted`. Then stop.
- The error names `python3` or a Python version: Ghosted needs Python 3.9 or newer. Say so and stop.
- The Bash call is refused: tell the user the command above is what `/ghosted` runs, and that they
  can run it themselves in a terminal (with ` report` after it).
- Any other error: show its message as it is.
- Never read session transcripts, `~/.claude/projects` or `~/.ghosted/` directly: the report is
  the only source.
