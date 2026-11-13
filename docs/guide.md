# Ghosted guide

Everything past the first report: unsaved work, the two CLAUDE.md rules, the full install, phone
pushes, every command and the privacy model. The interfaces and rules are in [contract.md](contract.md).

## Unsaved work, and two sessions in one folder

The report also lists every repo your sessions worked in (the last 30 days; plus any folder you
name under `workspace_roots`) that holds work only this computer has: changes not committed for 3
days, commits on no remote, old unmerged branches, no remote at all. Counts and dates only.

Two sessions in one folder share one set of files and one git index, so the next commit in either
one sweeps up the other's work. Ghosted matches each uncommitted file's save time to the moments
each session worked in that repo (within 15 s) and says so on the row:

```
  5 ! shop · 352 uncommitted since 31 Jul (100d) · from 2 sessions (122 + 39, 191 other)
      → cd ~/code/shop && git status
      ⚠ split before committing: ghosted mixed 5 · cd ~/code && claude --resume 5e0c7a12-… · …
```

`ghosted mixed 5` shows the split; `ghosted mixed 5 --session 5e0c7a12 --files` prints that session's files,
one per line (`--files -0 | xargs -0 git add --` stages exactly them). File names are printed
nowhere else. The split is a timing guess: files under `ambiguous` and `none` are yours to sort.
While two sessions are changing files in one folder, the report's header says so and suggests
`git worktree add ../shop-2`. Ghosted never commits, stashes or creates worktrees itself.

**Two rules for your `~/.claude/CLAUDE.md`.** `ghosted init` offers to add them (each shown and asked
once, written between `<!-- ghosted:begin -->` markers; `ghosted uninstall --claude-md` removes them).
To paste them yourself instead:

```markdown
## Ghosted

Before your last message: if it, a memory note or a handoff doc says something waits on me (a
decision, an answer, a check you promised for a date), run `ghosted add` for each item first:
`ghosted add "<what it is>" --public "<neutral title: no names, amounts or emails>" [--due MM-DD]`,
plus `--owner claude` when the next step is yours. When one is settled, run `ghosted done GS-xxxx`.

## Editing files

Edit files with the Edit/Write tools, not `sed -i`, scripts or heredocs through Bash. Edit refuses
to overwrite a file that changed since you read it (another session may be working in the same
folder), and checkpoints can rewind it. Bash is fine for generated output (builds, scratch files)
and bulk mechanical changes across many files; say so when you do it.
```

## Install

The plugin (`/plugin install ghosted --marketplace byndp/ghosted`) adds `/ghosted` and
puts `ghosted` on the PATH of Claude Code's own Bash tool, not your terminal's. The optional guided setup, `ghosted init` (a live record of how each session
ends, and pushes to your phone or Mac), asks questions, so it needs a terminal: run from inside
Claude Code it only writes a default `config.toml`. A plugin install has no `ghosted` in Terminal, so
get one from a clone first (the plugin's own folder is versioned and changes on every update, so
don't link that), then run `ghosted init` in Terminal.

**Not technical? Paste this into Claude Code:** *Install Ghosted from
github.com/byndp/ghosted and show me what I ghosted.*

> **For Claude, following that request:** (1) ask the user to type
> `/plugin install ghosted --marketplace byndp/ghosted` (slash commands are theirs to
> run), or, if they prefer, clone the repo and run `bin/ghosted install --skill`; (2) use the `ghosted`
> skill and show its output exactly as printed, without summarising it; (3) explain
> that `ghosted done N` / `ghosted dismiss N` clear an item and that pasting a `cd ... && claude --resume ...`
> line into a terminal reopens that session. Do not run `ghosted init`, `ghosted install --hook` or
> `ghosted install --launchd` unless the user asks.

The full install, from a clone:

```sh
git clone https://github.com/byndp/ghosted ~/ghosted && cd ~/ghosted
bin/ghosted init                     # writes ~/.ghosted/config.toml (add --adapter ntfy for pushes)
bin/ghosted install --all --dry-run  # shows what would change; touches nothing
bin/ghosted install --all            # ~/.local/bin/ghosted, the /ghosted skill, the Stop hook, the hourly job (macOS)
bin/ghosted doctor
```

`ghosted install` is idempotent and backs up `~/.claude/settings.json` to `~/.ghosted/backups/`
before editing it; `ghosted uninstall --all` removes only what it added (the `~/.claude/CLAUDE.md`
rules too; `--claude-md` removes just those). Pick parts with `--bin`, `--skill`, `--hook`, `--launchd`. Pushes: `claude-app` or `ntfy` (below),
`pushover`, or `macos` (a banner on the Mac itself). On Linux, `install --all` leaves out the hourly
job (it uses launchd, macOS only for now).

### Phone pushes: the Claude app or ntfy

`ghosted init` shows both with their trade-offs and you pick one (contract 16.12):

| | Claude app (`claude-app`) | ntfy (`ntfy`) |
|---|---|---|
| On the phone | the Claude app you already have | the free ntfy app |
| Account | your claude.ai sign-in (not an API key or Bedrock) | none |
| How | a small background Claude Code session named "Ghosted" calls the PushNotification tool | one HTTPS request to ntfy.sh |
| Cost | one short Haiku turn per push (about 10 s) on your Claude plan; a usage limit stops it | free |
| Privacy | pushes stay within Anthropic | pushes pass through ntfy.sh; anyone with your topic can read them (titles are kept neutral) |
| Stability | relies on undocumented Claude Code behaviour, which an update can change | documented |
| Setup | trust `~/.ghosted/notifier` once (`cd ~/.ghosted/notifier && claude`, accept, `/exit`); one "Remote control enabled" push when it starts | subscribe to your topic |

Claude Code's answer to a push is not proof that it arrived. So when the Claude app can't confirm
one, and an ntfy topic is set, the push also goes through ntfy (`[notify.claude-app] fallback = "ntfy"`,
the default; `"none"` turns that off). A duplicate beats a lost nudge. Under an iPhone Focus, pushes
land silently unless Claude is allowed in it. The notifier's own sessions never show up in
`ghosted report`.

The hourly job fires at :02 every hour and works 07:00-23:59 in the configured `timezone`
(`ghosted run` skips the other hours), so a Mac on another zone still reports during your day.
`ghosted install --launchd` refuses while `config.toml` uses TOML that `/usr/bin/python3` (3.9, the
job's interpreter) cannot read, and, on the machine that holds the real-session fixtures
(`~/.ghosted/fixtures/`), until they are labelled (`--force` installs anyway).

## Commands

| command | what it does |
|---|---|
| `ghosted add "<local>" --public "<neutral>" [--due 10-11]` | register a promise (with `--due`) or a decision |
| `ghosted report [today\|Nd\|all]` / `/ghosted` | what is waiting on you, with resume lines |
| `ghosted receipt [today\|Nd\|all]` / `/ghosted receipt` | your numbers only (no names, no quotes), safe to post |
| `ghosted done ID` · `ghosted snooze ID 1d\|3d\|mon` · `ghosted dismiss ID [--reason not_real\|handled_elsewhere]` | close or park an item (`12` = `GS-0012`, the number `ghosted report` shows) |
| `ghosted mixed N [--session ID --files [-0]]` | which session changed which uncommitted files in a repo (the only command that prints file names) |
| `ghosted list [--all] [--json]` | open items, grouped like the phone |
| `ghosted sweep` | scan now and print the v1 digest |
| `ghosted run` | the hourly job (launchd): scan, digest, notify or sync |
| `ghosted test-push` | a test push through the channel you picked |
| `ghosted sync` · `ghosted resync` · `ghosted pair` | for a self-hosted phone server (`--adapter remote`), which is not in this repo yet |
| `ghosted doctor` · `ghosted init` · `ghosted install` · `ghosted uninstall` · `ghosted version` | setup |

## Privacy model

- Transcripts in `~/.claude/projects` are read locally and never copied or uploaded. The report
  quotes the last question of a waiting session (common sensitive patterns masked) on screen only,
  never in Ghosted's files. Run through `/ghosted`, the report is command output in your Claude
  Code conversation, so it goes wherever that conversation goes.
- Only neutral public titles leave your computer. `--public` is required and the sanitizer refuses
  (never edits) titles with emails, amounts, keys, phone numbers or street addresses. The full
  local title stays in `~/.ghosted/items.json`.
- Interrupted sessions get a fixed title (`proj: session 10-01 ended on usage limit`) and
  machine-fact evidence (`91 uncommitted, 2 unpushed`), never text from the session.
- The hook (`ghosted hook`) records only a verdict per turn end, never message text; it is async,
  prints nothing and always exits 0.
- Secrets live in `~/.ghosted/.env` (chmod 600); `ghosted doctor` shows them as set or missing.
