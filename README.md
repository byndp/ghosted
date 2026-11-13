# Ghosted

[![tests](https://github.com/byndp/ghosted/actions/workflows/tests.yml/badge.svg)](https://github.com/byndp/ghosted/actions/workflows/tests.yml)

**Make sure you didn't ghost a Claude Code session.**

You hit a usage limit halfway through a task. By the time it resets you're onto a new idea in a
fresh session, and the old one never gets finished. Other times one just gets buried, or your tabs
all close. Ghosted checks every
project for sessions still waiting on you and the unfinished work they left behind, and gives you
the command to resume each one.

![ghosted report lists three sessions waiting on you (a usage limit, an unanswered question, a plan to approve), each with the command to resume it](docs/demo.gif)

## Try it in one line

If you have [uv](https://docs.astral.sh/uv/), this runs it without installing anything for good:

```sh
uvx --from git+https://github.com/byndp/ghosted ghosted report
```
The recording above uses a made-up history (`demo/make_demo.py`); no real session is in it.

## Install

Inside Claude Code:

```
/plugin install ghosted --marketplace byndp/ghosted
```

Then type `/ghosted`. From a terminal, the same install is
`claude plugin install ghosted --marketplace byndp/ghosted`. On older Claude Code:
`/plugin marketplace add byndp/ghosted`, then `/plugin install ghosted@ghosted`.

There's nothing to set up: no account, config file, hook or background job. Ghosted reads the
session history Claude Code already keeps, and the first run looks back 30 days. It makes no LLM
calls and sends nothing to any server. It needs Python 3.9 or newer and nothing else (on macOS,
`python3` comes with the Xcode Command Line Tools). The plugin adds `/ghosted` and puts `ghosted`
on Claude Code's own PATH, and nothing runs until you ask.

To use it from a terminal without the plugin, clone it and run it from the clone:

```sh
git clone https://github.com/byndp/ghosted ~/ghosted
~/ghosted/bin/ghosted report
~/ghosted/bin/ghosted install --bin    # optional: then plain `ghosted` works (via ~/.local/bin)
```

When nothing is waiting, it says so and shows what it checked:

```
GHOSTED · Thu 5 – Wed 11 Nov

✓ You didn't ghost anything in the last 7 days.
18 sessions in 5 projects checked
Looked for: unanswered questions, usage-limit cut-offs, plans waiting for approval, blocked background sessions.
```

## Your receipt

The report quotes your sessions and names your projects, so it's for you. `ghosted receipt` (or
`/ghosted receipt`) prints only the counts, which you can post:

```
GHOSTED · 11 weeks of Claude Code (Fri 28 Aug – Thu 12 Nov)
134 sessions in 12 projects checked
4 ghosted · longest wait 60 days
88 files left uncommitted · 44 commits left unpushed
github.com/byndp/ghosted
```

If you tried it with uvx, the same prefix works here:
`uvx --from git+https://github.com/byndp/ghosted ghosted receipt`.

## What it catches

| Waiting on you | How it knows |
|---|---|
| A question nobody answered | the session ended on a question box or a direct question |
| A usage limit that cut the work off | the session's last entry is the limit, and nobody resumed it |
| A plan waiting for approval | the session ended in plan mode |
| A reminder with a date | Claude registered it with `ghosted add` (an optional rule for your `CLAUDE.md`); counted apart from sessions |
| Unsaved work | a repo your sessions touched has old uncommitted or unpushed changes |
| Two sessions in one folder | it estimates which session changed each uncommitted file, from file times |

Polite sign-offs ("Want me to run the suite?") stay hidden unless you ask for them with `--soft`.
It leaves out sessions that are still running, automated runs and sessions started in a temp
folder.

## Using it

```sh
ghosted report                      # the last 7 days (the very first run looks back 30)
ghosted report today                # or any window: 3d, 30d, 365d
ghosted report all                  # your whole history, older items under their own heading
ghosted report --soft               # also show the polite offers
ghosted report --plain              # plain words instead of git jargon
ghosted report --project boathouse  # only one project, by the name the report shows
```

Every item in the report has a number, which you use to take it off the list:

```sh
ghosted done 7        # you dealt with it
ghosted snooze 7 3d   # hide it for 3 days (or 1d, or mon for next Monday)
ghosted dismiss 7     # it was never a real question
```

To resume a session, paste its `cd ... && claude --resume ...` line into a terminal.

## Privacy

Ghosted sends nothing anywhere. `ghosted report` reads `~/.claude/projects` on your computer and
asks your own `claude` command which sessions are still running. It masks common sensitive
patterns in the questions it quotes (emails, keys, amounts, phone numbers) and never writes those
quotes to its own files. In a terminal the report stays on your screen; through `/ghosted` it
becomes part of your Claude Code conversation, like any other command output. In `~/.ghosted/` it
keeps ids, verdicts, dates, git counts, and the names and folder paths of your projects. If you
turn on phone pushes, they carry neutral titles through the channel you choose.

## More

- [docs/guide.md](docs/guide.md) covers unsaved work, the two `CLAUDE.md` rules, optional phone
  pushes (the Claude app or ntfy), every command and the full privacy model.
- [docs/contract.md](docs/contract.md) is the build contract: interfaces, formats and rules.
- It runs on macOS and Linux. It's built on macOS, and the tests run on Linux (Python 3.9 and
  3.13) and macOS on every change. Windows isn't supported yet.
- License: [MIT](LICENSE).
