"""rc-revive: turn Remote Control back on in Claude Code sessions after the claude.ai account changes.

A Remote Control (RC) session belongs to the claude.ai account that started it. When this machine's login
moves to another account (an account switcher such as cswap, or /login), Claude Code stops RC in every
session that has it ("signed-in claude.ai account or organization changed on this machine — run
/remote-control to start a session for the current account"), and nothing starts it again. rc-revive watches
the sessions, and in each one that lost RC to a switch it types /remote-control and the session's name into
the session's tmux pane, which starts RC under the new account, with the name as its title.

RC lost to a switch: RC went off in a session, and either the machine's account changed while it was on, or
it ran as another account than the machine's current one (a session can notice a switch late). Not when it
was turned off by hand, which Claude Code marks in the transcript (an empty bridge-session entry).

It types only when that is safe:
- the pane shows Claude Code's input box (no dialog, menu or tmux copy mode), the session isn't waiting on a
  permission prompt or a question, and no tmux client on that tmux session had a key press for 15 s;
- a draft on the input line goes to Claude Code's stash first (Ctrl+S), and Claude Code puts it back by
  itself after the command. A suggested prompt (dim text) needs nothing: typing replaces it. A draft with an
  older stash next to it makes it wait, since a second stash would overwrite the first;
- Enter only when the input line reads exactly what it typed; afterwards it checks for a new RC session and
  that the draft is back.
It makes one attempt per session and switch. Whatever goes wrong is logged with the screen, for you.
"""

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

__version__ = "0.1.0"

USAGE = """\
usage:
  {prog} watch              every 5 s, revive the sessions that lost RC to a switch (what the service runs)
  {prog} status             the sessions, their RC state and the account their last RC ran as
  {prog} revive TARGET...   revive now: a pid, tmux pane (%N), tmux session name, or "lost" (every
  {pad}                    session whose last RC ran as an account other than the current one)
  {prog} screen PANE        how the input box of a pane reads (for testing)
  {prog} install-service    run `watch` as a systemd user service, now and after every login
  {prog} uninstall-service  stop that service and remove it

State: ~/.local/state/rc-revive/ (log: one JSON line per event; screens/: the pane at each failure)."""

HOME = Path.home()
SESSIONS = HOME / ".claude/sessions"
PROJECTS = HOME / ".claude/projects"
CONFIG = HOME / ".claude.json"
STATE = Path(os.environ.get("XDG_STATE_HOME") or HOME / ".local/state") / "rc-revive"
LOG = STATE / "log"
SCREENS = STATE / "screens"
UNIT = "rc-revive.service"
COMMAND = "/remote-control"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]*")  # a session name that is safe to type after the command
POLL = 5  # seconds between looks at the sessions
QUIET = 15  # seconds without a key press from a tmux client on the session before typing into it
GIVE_UP = 6 * 3600  # a session that never becomes safe to type into is left alone after this long
CONNECT = 30  # seconds for the session to report its new RC session after the command
SWITCH_SLACK = 60  # an account change this long before RC was last seen on still counts as its cause
SETTLE = 10  # seconds after RC goes off before deciding why: a disconnect by hand writes its transcript entry
ANSI = re.compile(r"\x1b\[([0-9;:?]*)([A-Za-z])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[=>78]")


def log_event(**fields):
    STATE.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **fields})
    with open(LOG, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


# --- the screen ---------------------------------------------------------------------------------------

def sgr(params, dim):
    """The dim state after an SGR sequence (ESC [ params m)."""
    ps = [int(p) if p.isdigit() else 0 for p in re.split("[;:]", params)] if params else [0]
    i = 0
    while i < len(ps):
        if ps[i] in (0, 22):
            dim = False
        elif ps[i] == 2:
            dim = True
        elif ps[i] in (38, 48, 58) and i + 1 < len(ps):  # an extended colour: skip its arguments
            i += {5: 2, 2: 4}.get(ps[i + 1], 0)
        i += 1
    return dim


def cells(line, dim=False):
    """The visible characters of one captured line, each with whether it's drawn dim; and the dim state after it."""
    out, pos = [], 0
    for m in ANSI.finditer(line):
        out += [(c, dim) for c in line[pos:m.start()]]
        pos = m.end()
        if m.group(2) == "m":
            dim = sgr(m.group(1), dim)
    out += [(c, dim) for c in line[pos:]]
    return out, dim


def is_rule(text, width):
    """A border of Claude Code's input box: a full-width line of ─, the upper one with the session's name in it."""
    t = text.strip()
    return t[:1] == "─" and t[-1:] == "─" and len(text) >= width - 2 and t.count("─") >= len(t) / 2


def read_screen(text, width):
    """Claude Code's input box at the bottom of a pane, from `tmux capture-pane -p -e`. None when it isn't
    there (a dialog, a menu or another program has the screen); else a dict:
      mode     "prompt" (❯) or "bash" (!)
      kind     "empty", "suggestion" (only dim text: a suggested prompt or a placeholder) or "draft"
      typed    the text on the input line that isn't dim, whitespace collapsed
      stashed  whether Claude Code shows "› stashed" (a draft in its stash)"""
    rows, dim = [], False
    for line in text.split("\n"):
        row, dim = cells(line, dim)
        rows.append(row)
    vis = ["".join(c for c, _ in r).rstrip() for r in rows]
    while vis and not vis[-1]:
        vis.pop()
    rules = [i for i, v in enumerate(vis) if is_rule(v, width)]
    if len(rules) < 2 or rules[-1] < len(vis) - 15 or rules[-1] - rules[-2] < 2:
        return None
    top, bottom = rules[-2], rules[-1]
    first = rows[top + 1]
    start = next((i for i, (c, _) in enumerate(first) if not c.isspace()), None)
    mode = {"❯": "prompt", "!": "bash"}.get(first[start][0] if start is not None else "")
    if mode is None:
        return None
    content = first[start + 1:] + [cell for row in rows[top + 2:bottom] for cell in [(" ", False)] + row]
    ink = [(c, d) for c, d in content if not c.isspace()]
    around = vis[max(0, top - 3):top] + vis[bottom + 1:]
    return {
        "mode": mode,
        "kind": "empty" if not ink else "suggestion" if all(d for _, d in ink) else "draft",
        "typed": " ".join("".join(" " if d else c for c, d in content).split()),
        "stashed": any("› stashed" in v for v in around),
    }


# --- tmux ---------------------------------------------------------------------------------------------

def tmux(*args):
    try:
        p = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def pane_info(pane):
    out = tmux("display-message", "-p", "-t", pane,
               "#{pane_id}\t#{pane_pid}\t#{pane_in_mode}\t#{pane_dead}\t#{session_name}\t#{pane_width}")
    parts = (out or "").rstrip("\n").split("\t")
    if len(parts) != 6 or parts[0] != pane or parts[3] == "1":
        return None
    return {"pid": int(parts[1]), "in_mode": parts[2] == "1", "session": parts[4], "width": int(parts[5])}


def screen(pane, width):
    out = tmux("capture-pane", "-p", "-e", "-t", pane)
    return read_screen(out, width) if out is not None else None


def keys(pane, *args):
    tmux("send-keys", "-t", pane, *args)


def settle(pane, width, done, timeout=3.0):
    """Look at the pane until done(screen) holds or the time is up; the last look."""
    end = time.time() + timeout
    while True:
        time.sleep(0.15)
        s = screen(pane, width)
        if done(s) or time.time() > end:
            return s


def last_key(session):
    """When a tmux client attached to this tmux session last had a key press (0 when none is attached)."""
    out = tmux("list-clients", "-F", "#{client_session}\t#{client_activity}") or ""
    times = [int(a) for s, _, a in (line.partition("\t") for line in out.splitlines()) if s == session and a.isdigit()]
    return max(times, default=0)


# --- sessions and accounts ----------------------------------------------------------------------------

def proc_stat(pid):
    """The fields of /proc/<pid>/stat after the command name (state, ppid, …), or None."""
    try:
        s = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return s[s.rindex(")") + 2:].split()


def alive(rec):
    st = proc_stat(rec.get("pid"))
    return st is not None and str(rec.get("procStart", st[19])) == st[19]  # starttime: not a reused pid


def runs_in(pid, pane_pid):
    for _ in range(8):
        if pid == pane_pid:
            return True
        st = proc_stat(pid)
        if not st:
            return False
        pid = int(st[1])
    return False


def record(pid):
    try:
        return json.loads((SESSIONS / f"{pid}.json").read_text())
    except (OSError, ValueError):
        return None


def records():
    """The live Claude Code sessions on this machine, by pid: their records in ~/.claude/sessions."""
    out = {}
    for f in SESSIONS.glob("*.json"):
        try:
            rec = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict) and isinstance(rec.get("pid"), int) and alive(rec):
            out[rec["pid"]] = rec
    return out


def machine_account():
    """The account of this machine's default login, from oauthAccount in ~/.claude.json (cswap rewrites it)."""
    for _ in range(3):  # sessions write this file all the time; a read can catch it half-written
        try:
            a = json.loads(CONFIG.read_text()).get("oauthAccount") or {}
            return {"uuid": a.get("accountUuid"), "org": a.get("organizationUuid"), "email": a.get("emailAddress")}
        except ValueError:
            time.sleep(0.2)
        except OSError:
            return None
    return None


def same(a, b):
    return bool(a and b and a.get("uuid")) and (a["uuid"], a.get("org")) == (b.get("uuid"), b.get("org"))


def transcript(rec):
    sid = rec.get("sessionId") or ""
    path = PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", rec.get("cwd") or "") / f"{sid}.jsonl"
    if path.exists():
        return path
    return next(iter(PROJECTS.glob(f"*/{sid}.jsonl")), None) if sid else None


def last_rc(rec, bridge=None):
    """What the transcript says about the session's RC, from its bridge-session entries near the end:
    {"cleared": True} when the latest entry clears the RC pointer, which a disconnect by hand does (a switch
    leaves it); else the account the latest RC session (or the one with id `bridge`) ran as, {"uuid", "org"};
    None when the transcript has no such entry. The record and the transcript spell an RC session id with
    different prefixes (session_…, cse_…)."""
    path = transcript(rec)
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            f.seek(max(0, f.seek(0, 2) - (4 << 20)))
            tail = f.read()
    except OSError:
        return None
    latest = True
    for line in reversed(tail.split(b"\n")):
        if b'"bridge-session"' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("type") != "bridge-session":
            continue
        b = str(e.get("bridgeSessionId") or "")
        if latest and not b:
            return {"cleared": True}
        latest = False
        if b and (not bridge or b.partition("_")[2] == bridge.partition("_")[2]):
            return {"uuid": e.get("ownerAccountUuid"), "org": e.get("ownerOrganizationUuid")}
    return None


def emails():
    """Account emails by organization uuid, from cswap when it's installed (`status` shows the uuid otherwise)."""
    try:
        p = subprocess.run(["cswap", "list", "--json"], capture_output=True, text=True, timeout=60)
        return {a.get("organizationUuid"): a["email"] for a in json.loads(p.stdout)["accounts"]}
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return {}


# --- reviving -----------------------------------------------------------------------------------------

def failed(pane, why, s):
    SCREENS.mkdir(parents=True, exist_ok=True)
    path = SCREENS / f"{time.strftime('%Y%m%d-%H%M%S')}-{pane.lstrip('%')}.txt"
    path.write_text(tmux("capture-pane", "-p", "-t", pane) or "")
    return "failed", f"{why} (screen: {path}; input box read as {s})"


def command(rec, width):
    """/remote-control with the session's name, which RC takes as its title in the Claude app. Without a name,
    RC after a switch is titled <hostname>-<adjective>-<noun>: Claude Code carries nothing of the conversation
    over to another account, not even its name. The command alone when the name has characters the input box
    could take for something else (@ opens the file menu), or wouldn't fit on one line."""
    name = " ".join(str(rec.get("name") or "").split())
    if NAME.fullmatch(name) and len(COMMAND) + 1 + len(name) <= width - 6:
        return f"{COMMAND} {name}"
    return COMMAND


def revive(pid, rec):
    """Type /remote-control and the session's name into its pane if that's safe now. Returns (outcome, detail),
    the outcome being "ok", "wait" (not safe yet: try again later) or "failed" (don't try again)."""
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "lock", "w") as lock:  # the service and a manual revive never type at the same time
        fcntl.flock(lock, fcntl.LOCK_EX)
        return revive_now(pid, record(pid) or rec)


def revive_now(pid, rec):
    pane = (rec.get("tmux") or "").rpartition(".")[2]
    if not pane.startswith("%"):
        return "failed", "the session doesn't run in tmux"
    info = pane_info(pane)
    if not info:
        return "failed", f"tmux pane {pane} is gone"
    if not runs_in(pid, info["pid"]):
        return "failed", f"tmux pane {pane} doesn't run this session"
    if rec.get("bridgeSessionId"):
        return "ok", "Remote Control is on already"
    if rec.get("status") == "waiting" or rec.get("waitingFor"):
        return "wait", f"the session waits for a {rec.get('waitingFor') or 'reply'}"
    if info["in_mode"]:
        return "wait", "the pane is in tmux copy mode"
    if time.time() - last_key(info["session"]) < QUIET:
        return "wait", "someone typed in this tmux session in the last 15 s"
    width = info["width"]
    s = screen(pane, width)
    if not s:
        return "wait", "no input box on the screen (a dialog or menu is open)"
    if s["mode"] != "prompt":
        return "wait", f"the input box is in {s['mode']} mode"
    stashed = False
    if s["kind"] == "draft":
        if s["stashed"]:
            return "wait", "a draft on the input line and another one in the stash"
        keys(pane, "C-s")
        s = settle(pane, width, lambda s: s and s["kind"] != "draft" and s["stashed"])
        if not (s and s["kind"] != "draft" and s["stashed"]):
            return failed(pane, "the draft didn't go to the stash", s)
        stashed = True
    cmd = command(rec, width)
    keys(pane, "-l", cmd)
    s = settle(pane, width, lambda s: s and s["typed"] == cmd)
    if not (s and s["typed"] == cmd):
        if s and s["typed"] and cmd.startswith(s["typed"]):  # take back what was typed, and the draft
            keys(pane, *["BSpace"] * len(s["typed"]))
            s = settle(pane, width, lambda s: s and not s["typed"])
            if stashed and s and not s["typed"] and s["stashed"]:
                keys(pane, "C-s")
        return failed(pane, f"the input line didn't read {cmd} after typing it", s)
    time.sleep(0.4)  # let the command menu catch up with the last character
    keys(pane, "Enter")
    end, on = time.time() + CONNECT, None
    while not on and time.time() < end:
        time.sleep(0.5)
        on = (record(pid) or {}).get("bridgeSessionId")
    s = settle(pane, width, lambda s: s and (not stashed or s["kind"] == "draft"))
    if stashed and s and s["kind"] != "draft" and s["stashed"]:  # Claude Code didn't put the draft back
        keys(pane, "C-s")
        s = settle(pane, width, lambda s: s and s["kind"] == "draft")
    draft = "" if not stashed else "; the draft is back" if s and s["kind"] == "draft" else (
        "; the draft is still in the stash (Ctrl+S brings it back)")
    if not on:
        return failed(pane, f"no Remote Control session {CONNECT} s after {cmd}{draft}", s)
    return "ok", f"Remote Control session {on} after {cmd}{draft}"


def describe(pid, rec):
    return {"pid": pid, "pane": (rec.get("tmux") or "").rpartition(".")[2], "name": rec.get("name")}


def lost_to_switch(off, changes, last, acct):
    """Why a session's RC went off because of an account switch; None when it didn't.
    off: {"on": when RC was last seen on}; changes: when the machine's account changed; last: last_rc()."""
    if last and last.get("cleared"):
        return None  # turned off by hand
    if any(t >= off["on"] - SWITCH_SLACK for t in changes):
        return "the machine's account changed while RC was on"
    if last and last.get("uuid") and not same(last, acct):  # a session can notice a switch late
        return "RC ran as another account than the machine's current one"
    return None


def watch():
    """Revive the sessions that lose RC to an account switch."""
    seen = {}  # pid -> {"bridge", "on" (when last seen on)}
    pending = {}  # pid -> {"off", "on", "bridge", "decided", "why" (last reason to wait)}
    changes = []  # when the machine's account changed
    acct = machine_account()
    log_event(event="start", account=acct and acct["email"])
    while True:
        try:
            acct = poll(acct, seen, pending, changes)
        except Exception as e:  # one bad look at the sessions mustn't stop the watcher
            log_event(event="error", error=f"{type(e).__name__}: {e}")
        time.sleep(POLL)


def poll(acct, seen, pending, changes):
    """One look at the sessions; the machine's account after it."""
    now = time.time()
    a = machine_account()
    if a and acct and not same(a, acct):
        changes.append(now)
        log_event(event="switch", account=a["email"], previous=acct["email"])
    acct = a or acct
    changes[:] = [t for t in changes if t > now - 86400]
    recs = records()
    for pid in [p for p in list(seen) + list(pending) if p not in recs]:
        seen.pop(pid, None)
        pending.pop(pid, None)
    for pid, rec in recs.items():
        bridge, was = rec.get("bridgeSessionId"), seen.get(pid)
        if bridge:
            if not was or was["bridge"] != bridge:
                log_event(event="rc-on", **describe(pid, rec), bridge=bridge)
            seen[pid] = {"bridge": bridge, "on": now}
            pending.pop(pid, None)
        elif was:
            del seen[pid]
            pending[pid] = {"off": now, "on": was["on"], "bridge": was["bridge"], "decided": False, "why": None}
            log_event(event="rc-off", **describe(pid, rec))
    for pid, p in list(pending.items()):
        rec = recs[pid]
        if not p["decided"]:
            if now - p["off"] < SETTLE:
                continue
            last = last_rc(rec, p["bridge"])
            why = lost_to_switch(p, changes, last, acct)
            log_event(event="lost-to-switch" if why else "left-off", **describe(pid, rec),
                      why=why or ("turned off by hand" if last and last.get("cleared") else "no account switch"))
            if not why:
                del pending[pid]
                continue
            p["decided"] = True
        if now - p["off"] > GIVE_UP:
            log_event(event="give-up", **describe(pid, rec), why=p["why"])
            del pending[pid]
            continue
        try:
            outcome, detail = revive(pid, rec)
        except Exception as e:  # one session's trouble mustn't stop the watcher
            outcome, detail = "failed", f"{type(e).__name__}: {e}"
        if outcome == "wait":
            if detail != p["why"]:
                log_event(event="wait", **describe(pid, rec), why=detail)
            p["why"] = detail
            continue
        log_event(event=outcome, **describe(pid, rec), detail=detail)
        del pending[pid]
    return acct


def targets(args, recs):
    acct = machine_account()
    out = []
    for t in args:
        if t == "lost":
            out += [pid for pid, rec in recs.items() if "bridgeSessionId" in rec and not rec["bridgeSessionId"]
                    and (last := last_rc(rec)) and last.get("uuid") and not same(last, acct)]
        else:
            out += [pid for pid, rec in recs.items()
                    if t in (str(pid), (rec.get("tmux") or "").rpartition(".")[2], (rec.get("tmux") or "").split(":")[0])]
    return list(dict.fromkeys(out))


def unit_text():
    """The service: `watch`, run by this Python, which can import this package (the environment of a uv tool or
    pipx install, or cswap's when it came with its rc-revive extra)."""
    exe = f'"{sys.executable}"' if " " in sys.executable else sys.executable
    return ("[Unit]\nDescription=Turn Remote Control back on in Claude Code sessions after account switches "
            f"(rc-revive)\n\n[Service]\nExecStart={exe} -m rc_revive watch\nEnvironment=PYTHONUNBUFFERED=1\n"
            "Restart=always\nRestartSec=30\n\n[Install]\nWantedBy=default.target\n")


def systemctl(*args, check=True):
    p = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        sys.exit(f"systemctl --user {' '.join(args)} failed: {p.stderr.strip()}")


def install_service():
    path = HOME / ".config/systemd/user" / UNIT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_text())
    systemctl("daemon-reload")
    systemctl("enable", UNIT)
    systemctl("restart", UNIT)  # after an upgrade, a reinstall runs the new code
    print(f"installed {path} and started it.\nlog: {LOG} (also journalctl --user -u {UNIT})")


def uninstall_service():
    path = HOME / ".config/systemd/user" / UNIT
    systemctl("disable", "--now", UNIT, check=False)  # not an error when it isn't installed
    path.unlink(missing_ok=True)
    systemctl("daemon-reload")
    print(f"stopped and removed {UNIT}; its log stays in {STATE}")


def main(argv=None, prog="rc-revive"):
    """The command line, argv without the program's name. `prog` names the command in the usage (cswap runs this
    as `cswap rc-revive`)."""
    argv = sys.argv[1:] if argv is None else list(argv)
    cmd, args = (argv[0], argv[1:]) if argv else ("", [])
    if cmd == "watch" and not args:
        watch()
    elif cmd == "status" and not args:
        acct, names = machine_account(), emails()
        print(f"machine account: {acct and acct['email']}")
        for pid, rec in sorted(records().items(), key=lambda kv: kv[1].get("tmux") or ""):
            last = last_rc(rec) if "bridgeSessionId" in rec else None
            rc = ("on" if rec.get("bridgeSessionId") else "never on" if "bridgeSessionId" not in rec
                  else "off by hand" if last and last.get("cleared") else "off")
            print(f"{(rec.get('tmux') or '-'):<45} {pid:>7} {rec.get('status') or '-':<8} RC {rc:<12}"
                  f" last RC as: {last and last.get('org') and (names.get(last['org']) or last['org']) or '-'}")
    elif cmd == "revive" and args:
        recs = records()
        pids = targets(args, recs)
        if not pids:
            sys.exit("no such session")
        for pid in pids:
            end = time.time() + 120
            while True:
                outcome, detail = revive(pid, record(pid) or recs[pid])
                if outcome != "wait" or time.time() > end:
                    break
                print(f"{pid}: waiting: {detail}", flush=True)
                time.sleep(POLL)
            log_event(event=outcome, **describe(pid, recs[pid]), detail=detail, manual=True)
    elif cmd == "screen" and len(args) == 1:
        info = pane_info(args[0])
        print(json.dumps(info and screen(args[0], info["width"])))
    elif cmd == "install-service" and not args:
        install_service()
    elif cmd == "uninstall-service" and not args:
        uninstall_service()
    elif cmd in ("-h", "--help", "help"):
        print(USAGE.format(prog=prog, pad=" " * len(prog)))
    elif cmd in ("-V", "--version"):
        print(f"rc-revive {__version__}")
    else:
        sys.exit(USAGE.format(prog=prog, pad=" " * len(prog)))
    return 0
