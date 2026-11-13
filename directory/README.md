# Ghosted: the Claude directory listing

**Make sure you didn't ghost a Claude Code session.**

This folder is the plugin listed in Claude's plugin directory. It holds no code. Its `/ghosted`
skill runs Ghosted through [uv](https://docs.astral.sh/uv/), pinned to a release of this
repository:

```sh
uvx --from git+https://github.com/byndp/ghosted@v0.2.0 ghosted report
```

Claude asks before running it. Ghosted is standard-library Python under the MIT license, and it
makes no network calls of its own. The [main README](https://github.com/byndp/ghosted#readme)
covers what it catches, privacy and every command.

If you'd rather not use uv, install the full plugin straight from GitHub:
`/plugin install ghosted --marketplace byndp/ghosted`.
