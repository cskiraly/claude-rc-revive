# claude-rc-revive

Turn Claude Code's Remote Control back on after the claude.ai account changes.

A Remote Control (RC) session belongs to the claude.ai account that started it. When the machine's login moves to
another account (an account switcher such as [cswap](https://github.com/realiti4/claude-swap), or `/login`),
Claude Code stops RC in every session that has it:

> signed-in claude.ai account or organization changed on this machine — run /remote-control to start a session for
> the current account

Nothing starts it again, so you have to type `/remote-control` in each session. claude-rc-revive does that for you.
Its command, `rc-revive`, watches the sessions, and in each one that lost RC to a switch it types
`/remote-control <session name>` into the session's tmux pane. That starts RC under the new account, titled with the
session's name.

**Experimental.** It reads Claude Code's screen and session files, which a new Claude Code version can change; it
then waits or fails, logged, rather than typing blind. Tested with Claude Code 2.1.308 and 2.1.310. Not affiliated
with Anthropic.

## Install

Linux with systemd, tmux, Python 3.10 or newer, and Claude Code sessions running in tmux.

```
uv tool install claude-rc-revive  # or: pipx install claude-rc-revive
rc-revive install-service         # a systemd user service that runs `rc-revive watch`
```

To install from GitHub instead: `uv tool install git+https://github.com/cskiraly/claude-rc-revive`.

The service runs while you're logged in. To keep it running without a login session (as tmux sessions often do),
enable lingering once: `sudo loginctl enable-linger $USER`.

## How it works

Every 5 seconds it reads Claude Code's own session records (`~/.claude/sessions/<pid>.json`), which say whether RC
is on and name the session's tmux pane.

**RC lost to a switch:** RC went off in a session, and 10 seconds later the transcript doesn't show it was turned
off by hand (Claude Code writes a disconnect entry then; a switch doesn't). And either the machine's account
(`oauthAccount` in `~/.claude.json`) changed while RC was on, or the session's last RC ran as another account than
the current one: a session can notice a switch late. RC that you turn off yourself stays off.

**Typing**, only when it's safe:

- the pane shows Claude Code's input box: no dialog, menu, permission prompt or question waiting, no tmux copy
  mode;
- nobody pressed a key in that tmux session for 15 seconds;
- a draft on the input line goes to Claude Code's stash first (Ctrl+S), and comes back by itself after the command.
  A suggested prompt (dim text) needs nothing: typing replaces it. A draft with an older stash next to it makes it
  wait, since a second stash would overwrite the first.

It presses Enter only when the input line reads exactly what it typed, then checks the session record for a new RC
session and that the draft came back. While it isn't safe it tries again every 5 seconds, for up to 6 hours. On
anything unexpected it stops, takes back what it typed if it can, and logs the screen.

**Why the name:** after a switch, Claude Code carries nothing of the conversation over to the new account, so a bare
`/remote-control` gets a title like `<hostname>-<adjective>-<noun>` in the Claude app. A name typed with the command
is used as the title. When the session's name has characters the input box could take for something else (only
letters, digits, space, `.`, `_` and `-` are typed), or doesn't fit on the line, it types the command alone.

## Usage

```
rc-revive watch              every 5 s, revive the sessions that lost RC to a switch (what the service runs)
rc-revive status             the sessions, their RC state and the account their last RC ran as
rc-revive revive TARGET...   revive now: a pid, tmux pane (%N), tmux session name, or "lost" (every
                             session whose last RC ran as an account other than the current one)
rc-revive screen PANE        how the input box of a pane reads (for testing)
rc-revive install-service    run `watch` as a systemd user service, now and after every login
rc-revive uninstall-service  stop that service and remove it
```

The log is `~/.local/state/rc-revive/log`, one JSON line per event (`switch`, `rc-off`, `lost-to-switch`,
`left-off`, `wait`, `ok`, `failed`, `give-up`), also in the journal (`journalctl --user -u rc-revive`). The screen
of every failure goes to `~/.local/state/rc-revive/screens/`.

## Caveats

- Only sessions in tmux, and only those using the default config directory (`~/.claude`).
- A session that is waiting on a permission prompt or a question gets RC back only after you answer it there,
  since until then its input box isn't showing. After 6 hours rc-revive gives up on it (`rc-revive revive lost`
  tries again).
- If you type into a session in the second it takes, your keys and its keys mix. The 15-second quiet check makes
  that unlikely.
- In a session with an empty input line and a draft of yours in the stash, Claude Code puts that draft back on the
  input line after the command ("Draft restored"), as it does after any prompt you send.
- RC that goes off for another reason within a minute after a switch also gets revived.

The real fix belongs in Claude Code: after an account change, start RC again for the new account when it was on.

## Tests

- `uv run pytest`: the screen reader on panes captured from Claude Code (empty, suggestion, busy, permission prompt)
  and variants of them, the transcript reader, what it types, the service file, and the watcher over time with
  stubs. Nothing is typed.
- `tests/live-test.sh`: on a throwaway Claude Code session in its own tmux session, a draft goes to the stash, RC
  comes on titled with the session's name, and the draft comes back; a draft plus an older stash is refused. It
  starts and disconnects two RC sessions under your current account (they show up in the Claude app for a moment),
  and sends no prompt to the model. Run it from a directory Claude Code already trusts; `CLAUDE=<binary>` tests
  another Claude Code version.

## Uninstall

```
rc-revive uninstall-service
uv tool uninstall claude-rc-revive
rm -r ~/.local/state/rc-revive
```

## License

MIT
