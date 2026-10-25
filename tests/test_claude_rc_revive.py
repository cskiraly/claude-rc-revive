"""rc-revive's logic, with no typing: the screen reader on captured panes, the transcript reader, what it types, the
service file, and the watcher's decisions with the sessions, the account, the clock and the typing replaced by stubs."""
import json
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

import claude_rc_revive as rr

HERE = Path(__file__).parent
SCREENS = json.loads((HERE / "screens.json").read_text())  # panes captured with Claude Code 2.1.308, text replaced
A = {"uuid": "a2", "org": "o2"}
B = {"uuid": "a3", "org": "o3"}


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    """The log and screens in a temporary directory, and the log lines not echoed to the test output."""
    monkeypatch.setattr(rr, "STATE", tmp_path / "state")
    monkeypatch.setattr(rr, "LOG", tmp_path / "state" / "log")
    monkeypatch.setattr(rr, "SCREENS", tmp_path / "state" / "screens")
    monkeypatch.setattr(rr, "print", lambda *a, **k: None, raising=False)
    return tmp_path


def read(name, text=None):
    return rr.read_screen(SCREENS[name]["text"] if text is None else text, SCREENS[name]["width"])


EMPTY = SCREENS["empty"]["text"]
DRAFT = EMPTY.replace("❯\xa0", "❯\xa0half a sentence, and")


# --- the screen -----------------------------------------------------------------------------------------------

def test_empty_input_box():
    assert read("empty") == {"mode": "prompt", "kind": "empty", "typed": "", "stashed": False,
                             "insert": False}


def test_a_suggested_prompt_is_not_a_draft():
    assert read("suggestion") == {"mode": "prompt", "kind": "suggestion", "typed": "", "stashed": False,
                                  "insert": False}


def test_a_busy_session_still_shows_its_input_box():
    assert read("busy")["kind"] == "empty"


def test_a_permission_prompt_has_no_input_box():
    assert read("permission") is None


def test_a_draft():
    assert read("empty", DRAFT) == {"mode": "prompt", "kind": "draft", "typed": "half a sentence, and",
                                    "stashed": False, "insert": False}


def test_a_draft_on_two_lines():
    two = DRAFT.replace("half a sentence, and\n", "half a sentence, and\n  a second line\n")
    assert read("empty", two)["typed"] == "half a sentence, and a second line"


def test_the_typed_command_without_its_dim_argument_hint():
    cmd = EMPTY.replace("❯\xa0", "❯\xa0/remote-control\x1b[2m [name]\x1b[0m")
    assert read("empty", cmd)["typed"] == "/remote-control"


def test_a_draft_with_a_stash_next_to_it():
    stash = DRAFT.replace("(shift+tab to cycle) · ← 2 agents", "(shift+tab to cycle) · ← 2 agents · \x1b[2m› stashed\x1b[0m")
    s = read("empty", stash)
    assert s["stashed"] and s["kind"] == "draft"


def test_bash_mode():
    assert read("empty", EMPTY.replace("❯\xa0", "!\xa0"))["mode"] == "bash"


def test_no_bottom_border_no_input_box():
    assert read("empty", EMPTY.rsplit("\n", 2)[0]) is None


def test_sgr_dim_state():
    # colours keep the dim state, 2 sets it, 0 and 22 clear it
    assert rr.sgr("38;5;244", True) and not rr.sgr("0", True) and rr.sgr("2", False) and not rr.sgr("22", True)
    assert rr.sgr("38;2;1;2;22", True)


# --- the transcript: the account an RC session ran as, and disconnects by hand --------------------------------

@pytest.fixture
def transcript(tmp_path, monkeypatch):
    proj = tmp_path / "projects"
    (proj / "-w").mkdir(parents=True)
    monkeypatch.setattr(rr, "PROJECTS", proj)
    path = proj / "-w" / "s1.jsonl"

    def write(*entries):
        with open(path, "w") as f:
            for b, owner in entries:
                e = {"type": "bridge-session", "sessionId": "s1", "bridgeSessionId": b, "lastSequenceNum": 0}
                if owner:
                    e |= {"ownerAccountUuid": owner["uuid"], "ownerOrganizationUuid": owner["org"]}
                f.write(json.dumps({"type": "user", "message": "x"}) + "\n" + json.dumps(e) + "\n")
    write.path = path
    return write


REC = {"sessionId": "s1", "cwd": "/w"}


def test_owner_of_the_latest_rc_session_or_of_one_by_id(transcript):
    transcript(("cse_X", A), ("cse_Y", B))
    assert rr.last_rc(REC) == B
    assert rr.last_rc(REC, "session_X") == A  # the record and the transcript spell the id differently


def test_a_cleared_pointer_is_a_disconnect_by_hand(transcript):
    transcript(("cse_X", A), ("", None))
    assert rr.last_rc(REC) == {"cleared": True}


def test_no_transcript(transcript):
    assert rr.last_rc(REC) is None


# --- why RC went off --------------------------------------------------------------------------------------------

def test_lost_to_switch():
    assert rr.lost_to_switch({"on": 1000}, [1010], A, B), "the account changed while RC was on"
    assert rr.lost_to_switch({"on": 1000}, [990], None, B), "a change just before the last sighting counts"
    assert not rr.lost_to_switch({"on": 1000}, [100], A, A), "an old change, same account: not a switch"
    assert rr.lost_to_switch({"on": 1000}, [100], A, B), "RC ran as another account (noticed late)"
    assert not rr.lost_to_switch({"on": 1000}, [1010], {"cleared": True}, B), "turned off by hand, even after a switch"


# --- what it types ----------------------------------------------------------------------------------------------

def test_the_command_with_the_sessions_name():
    assert rr.command({"name": "demo-project-85"}, 150) == "/remote-control demo-project-85"
    assert rr.command({"name": " two\t words "}, 150) == "/remote-control two words"


def test_the_command_alone():
    assert rr.command({"name": "see @notes.md"}, 150) == "/remote-control", "@ opens the file menu"
    assert rr.command({"name": None}, 150) == rr.command({}, 150) == "/remote-control"
    assert rr.command({"name": "x" * 40}, 60) == "/remote-control", "too long for the input line"


def test_the_typed_command_with_the_name():
    typed = EMPTY.replace("❯\xa0", "❯\xa0/remote-control demo-project-85")
    assert read("empty", typed)["typed"] == "/remote-control demo-project-85"


def test_a_real_stash_above_the_box():  # Claude Code 2.1.310 shows it above the input box, next to the effort
    assert read("stashed-2.1.310") == {"mode": "prompt", "kind": "draft", "typed": "second draft", "stashed": True,
                                       "insert": False}


def test_the_slash_menu_hides_the_stash():  # the same session, with a stash, while typing /rem
    s = read("slash-menu-2.1.310")
    assert s["typed"] == "/rem" and not s["stashed"], "why a draft that opens a menu isn't stashed"


def test_vim_insert_mode():
    assert read("empty", EMPTY.replace("(shift+tab to cycle)", "(shift+tab to cycle) · -- INSERT --"))["insert"]
    assert not read("empty")["insert"]


def test_versions_checked_for_the_stash():
    assert rr.stash_checked("2.1.308") and rr.stash_checked("2.1.310") and rr.stash_checked("2.1.400")
    assert not rr.stash_checked("2.1.300") and not rr.stash_checked("2.2.0") and not rr.stash_checked("3.0.1")
    assert not rr.stash_checked(None) and not rr.stash_checked("2.1.x") and not rr.stash_checked("2.1")


# --- typing, on a fake tmux pane ----------------------------------------------------------------------------------

RULE = "\x1b[38;5;244m" + "─" * 120


class Pane:
    """A tmux pane with Claude Code's input box (as 2.1.310 draws it), driven by the keys rc-revive sends: typing,
    Backspace, Ctrl+S (stash the line, or pop the stash into an empty one), Enter (run the command; Claude Code then
    puts a stashed draft back). The record's RC goes on when Enter submits /remote-control."""

    def __init__(self, st, line="", stash=None, show_stash=True, lag=0, mangle=False, synced=False):
        self.st, self.line, self.stash, self.show_stash = st, line, stash, show_stash
        self.lag, self.mangle, self.synced, self.sent = lag, mangle, synced, []

    def render(self):
        line = self.line[:9] if self.lag > 0 else self.line
        self.lag -= 1
        marks = "◉ xhigh · /effort" + (" · › stashed" if self.stash is not None and self.show_stash else "")
        return "\n".join([" " * 40 + f"\x1b[38;5;246m{marks}\x1b[39m", RULE, f"\x1b[39m❯\xa0{line}", RULE,
                          "\x1b[39m  \x1b[38;5;220m⏵⏵ auto mode on\x1b[38;5;246m (shift+tab to cycle)\x1b[39m"])

    def press(self, keys):
        self.sent.append(keys)
        if keys[0] == "-l":
            self.line += keys[1][:-1] if self.mangle else keys[1]
            return
        for k in keys:
            if k == "BSpace":
                self.line = self.line[:-1]
            elif k == "C-s" and self.line:
                self.stash, self.line = self.line, ""
            elif k == "C-s" and self.stash is not None:
                self.line, self.stash = self.stash, None
            elif k == "Enter" and self.line.startswith("/remote-control"):
                self.st["rec"]["bridgeSessionId"] = "session_NEW"
                self.line = ""
                if self.stash is not None:  # "Draft restored"
                    self.line, self.stash = self.stash, None

    def tmux(self, *args):
        if args[0] == "display-message":
            return f"%9\t4242\t0\t0\tdemo\t120\t{int(self.synced)}\n"
        if args[0] == "capture-pane":
            return self.render()
        if args[0] == "send-keys":
            self.press(list(args[3:]))
            return ""
        if args[0] == "list-clients":
            return f"demo\t{int(self.st['key'])}\n"
        if args[0] == "list-sessions":
            return "demo: 1 windows\n"


@pytest.fixture
def typing(monkeypatch):
    """revive_now on a fake pane: a fake clock, a record that Enter turns RC on in, no vim, a 2.1.310 session."""
    st = {"t": 1000.0, "key": 0, "vim": False,
          "rec": {"pid": 4242, "tmux": "demo:@1.%9", "name": "demo-project", "version": "2.1.310",
                  "bridgeSessionId": None}}

    def sleep(s):
        st["t"] += s
    monkeypatch.setattr(rr, "time", types.SimpleNamespace(time=lambda: st["t"], sleep=sleep, strftime=time.strftime))
    monkeypatch.setattr(rr, "runs_in", lambda pid, pane_pid: True)
    monkeypatch.setattr(rr, "record", lambda pid: st["rec"])
    monkeypatch.setattr(rr, "vim_mode", lambda: st["vim"])

    def run(**kw):
        pane = Pane(st, **kw)
        monkeypatch.setattr(rr, "tmux", pane.tmux)
        return pane, rr.revive_now(4242, st["rec"])
    run.st = st
    return run


def test_an_empty_line(typing):
    pane, (outcome, detail) = typing()
    assert outcome == "ok" and "session_NEW" in detail
    assert ["-l", "/remote-control demo-project"] in pane.sent and pane.sent[-1] == ["Enter"]


def test_a_draft_goes_to_the_stash_and_comes_back(typing):
    pane, (outcome, detail) = typing(line="half a sentence, and")
    assert outcome == "ok" and "the draft is back" in detail and pane.line == "half a sentence, and"
    assert pane.sent[0] == ["C-s"]


def test_a_draft_next_to_a_stash_waits(typing):
    pane, (outcome, detail) = typing(line="new draft", stash="old draft")
    assert outcome == "wait" and not pane.sent and pane.stash == "old draft"


def test_a_draft_with_a_menu_open_waits(typing):
    for draft in ("/rem", "see @no"):
        pane, (outcome, detail) = typing(line=draft)
        assert outcome == "wait" and "menu" in detail and not pane.sent


def test_a_draft_on_an_unchecked_version_waits(typing):
    typing.st["rec"]["version"] = "2.2.0"
    pane, (outcome, detail) = typing(line="a draft")
    assert outcome == "wait" and "2.2.0" in detail and not pane.sent
    pane, (outcome, detail) = typing()  # an empty line needs no stash
    assert outcome == "ok"


def test_synchronized_panes_wait(typing):
    pane, (outcome, detail) = typing(synced=True)
    assert outcome == "wait" and "synchronize" in detail and not pane.sent


def test_someone_typing_waits(typing):
    typing.st["key"] = typing.st["t"] - 3
    pane, (outcome, detail) = typing()
    assert outcome == "wait" and not pane.sent


def test_vim_mode(typing):
    typing.st["vim"] = True
    pane, (outcome, detail) = typing()
    assert outcome == "wait" and "INSERT" in detail and not pane.sent, "NORMAL mode doesn't show: no typing"


def test_no_stash_seen_pops_the_draft_back(typing):
    pane, (outcome, detail) = typing(line="my draft", show_stash=False)
    assert outcome == "failed" and "back on the input line" in detail
    assert pane.line == "my draft" and pane.stash is None and pane.sent == [["C-s"], ["C-s"]]


def test_a_wrong_line_is_taken_back(typing):
    pane, (outcome, detail) = typing(line="my draft", mangle=True)
    assert outcome == "failed" and ["Enter"] not in pane.sent
    assert pane.line == "my draft" and pane.stash is None, "the typing deleted, the draft popped back"


def test_take_back_when_the_screen_lags(typing, monkeypatch):
    pane = Pane(typing.st, line="/remote-control demo-project", lag=1)  # the first look shows only "/remote-c"
    monkeypatch.setattr(rr, "tmux", pane.tmux)
    rr.take_back("%9", 120, "/remote-control demo-project", False)
    assert pane.line == ""


def test_a_last_look_before_enter(typing, monkeypatch):
    # RC comes on by itself while the command is typed: Enter would open its menu instead
    monkeypatch.setattr(rr, "record", lambda pid: dict(typing.st["rec"], bridgeSessionId="session_OWN"))
    pane, (outcome, detail) = typing(line="my draft")
    assert outcome == "wait" and ["Enter"] not in pane.sent and pane.line == "my draft"


def test_an_unreachable_tmux_server_waits(typing, monkeypatch):
    monkeypatch.setattr(rr, "tmux", lambda *args: None)
    assert rr.revive_now(4242, typing.st["rec"])[0] == "wait"


# --- the service and the command line ---------------------------------------------------------------------------

def test_the_service_runs_this_python(monkeypatch):
    monkeypatch.setattr(rr.shutil, "which", lambda name: "/usr/bin/tmux")
    monkeypatch.delenv("TMUX_TMPDIR", raising=False)
    monkeypatch.setattr(rr.sys, "executable", "/home/u/.local/share/uv/tools/claude-rc-revive/bin/python")
    unit = rr.unit_text()
    assert "\nExecStart=/home/u/.local/share/uv/tools/claude-rc-revive/bin/python -m claude_rc_revive watch\n" in unit
    assert "PATH" not in unit, "tmux is on the system's PATH: nothing to add"
    monkeypatch.setattr(rr.sys, "executable", "/opt/my tools/bin/python")
    assert '\nExecStart="/opt/my tools/bin/python" -m claude_rc_revive watch\n' in rr.unit_text()
    monkeypatch.setattr(rr.sys, "executable", "/opt/50%/$x/python")
    assert "\nExecStart=/opt/50%%/$$x/python -m claude_rc_revive watch\n" in rr.unit_text(), "specifiers, variables"


def test_the_service_reaches_this_tmux(monkeypatch):
    monkeypatch.setattr(rr.shutil, "which", lambda name: "/nix/store/x-tmux/bin/tmux")
    monkeypatch.setenv("TMUX_TMPDIR", "/run/user/1000/tmux")
    unit = rr.unit_text()
    assert '\nEnvironment="PATH=/nix/store/x-tmux/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"\n' in unit
    assert '\nEnvironment="TMUX_TMPDIR=/run/user/1000/tmux"\n' in unit


def test_no_service_from_a_temporary_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(rr, "HOME", tmp_path)
    monkeypatch.setattr(rr.sys, "executable", "/home/u/.cache/uv/builds-v0/.tmpAbc/bin/python")
    with pytest.raises(SystemExit) as e:
        rr.install_service()
    assert "temporary environment" in str(e.value.code) and not (tmp_path / ".config").exists()


def test_install_and_uninstall_service(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(rr, "HOME", tmp_path)
    monkeypatch.setattr(rr.sys, "executable", "/home/u/.local/share/uv/tools/claude-rc-revive/bin/python")
    monkeypatch.setattr(rr.subprocess, "run", lambda args, **kw: calls.append(args) or
                        subprocess.CompletedProcess(args, 0, "", ""))
    unit = tmp_path / ".config/systemd/user/rc-revive.service"
    rr.install_service()
    assert unit.read_text() == rr.unit_text()
    assert calls == [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", rr.UNIT],
                     ["systemctl", "--user", "restart", rr.UNIT]]
    calls.clear()
    rr.uninstall_service()
    assert not unit.exists()
    assert calls == [["systemctl", "--user", "disable", "--now", rr.UNIT], ["systemctl", "--user", "daemon-reload"]]


def test_usage_names_the_command_it_runs_as():
    with pytest.raises(SystemExit) as e:
        rr.main(["bogus"], prog="cswap rc-revive")
    assert "cswap rc-revive watch" in str(e.value.code)
    with pytest.raises(SystemExit) as e:
        rr.main(["watch", "extra"])
    assert "rc-revive install-service" in str(e.value.code)


def test_the_package_runs_as_a_module():
    p = subprocess.run([sys.executable, "-m", "claude_rc_revive", "--version"], capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout.strip() == f"rc-revive {rr.__version__}"


# --- the watcher, with stubs for the sessions, the account, the transcript, the clock and the typing -----------

def test_watcher(monkeypatch):
    st = {"t": 10000.0, "acct": dict(A, email="two"), "recs": {}, "last": {}, "revived": [], "outcome": ("ok", "on")}
    monkeypatch.setattr(rr, "time", types.SimpleNamespace(time=lambda: st["t"], sleep=lambda s: None,
                                                          strftime=time.strftime))
    monkeypatch.setattr(rr, "machine_account", lambda: st["acct"])
    monkeypatch.setattr(rr, "records", lambda: st["recs"])
    monkeypatch.setattr(rr, "last_rc", lambda rec, bridge=None: st["last"].get(rec["pid"]))

    def revive(pid, rec):
        st["revived"].append(pid)
        return st["outcome"]
    monkeypatch.setattr(rr, "revive", revive)
    seen, pending, changes = {}, {}, []
    watcher = {"acct": st["acct"]}  # the account the watcher saw last

    def tick(dt=5):
        st["t"] += dt
        watcher["acct"] = rr.poll(watcher["acct"], seen, pending, changes)

    def session(pid, bridge):
        st["recs"][pid] = {"pid": pid, "tmux": f"s{pid}:@1.%{pid}", "name": f"s{pid}", "bridgeSessionId": bridge}

    session(1, "session_A1"); session(2, "session_A2"); session(3, None); session(4, "session_A4")
    tick()
    assert set(seen) == {1, 2, 4} and not pending, "sessions with RC on are tracked"
    st["acct"] = dict(B, email="three")  # the account switches
    st["recs"][4]["bridgeSessionId"] = None  # session 4 was turned off by hand, just before
    st["last"][4] = {"cleared": True}
    tick()
    session(1, None); session(2, None); st["last"][1] = A; st["last"][2] = A  # RC stops in 1 and 2
    tick()
    assert set(pending) == {1, 2, 4} and not st["revived"], "nothing is typed in the first 10 s after RC goes off"
    tick(); tick()
    assert sorted(st["revived"]) == [1, 2] and not pending, "after 10 s: 1 and 2 revived, 4 (off by hand) left alone"
    st["revived"].clear()

    tick(120)  # well after the switch
    session(5, "session_B5"); tick(); session(5, None); st["last"][5] = B; tick(); tick(); tick()
    assert not st["revived"] and 5 not in pending, "RC off with no switch since: left alone"

    session(6, "session_B6"); tick()
    st["acct"] = dict(A, email="two"); tick()
    st["outcome"] = ("wait", "someone typed in this tmux session in the last 15 s")
    session(6, None); st["last"][6] = B; tick(); tick(); tick()
    assert st["revived"] == [6] and pending[6]["why"].startswith("someone typed"), "not safe yet: it waits"
    st["outcome"] = ("ok", "on"); tick()
    assert st["revived"] == [6, 6] and not pending, "and tries again at the next look"

    session(7, "session_A7"); tick(); st["acct"] = dict(B, email="three"); tick()
    session(7, None); st["last"][7] = A; tick(); tick()
    st["outcome"] = ("wait", "no input box on the screen (a dialog or menu is open)"); tick()
    del st["recs"][7]; tick()
    assert 7 not in pending and 7 not in seen, "a session that ends is forgotten"

    session(8, "session_B8"); tick(); st["acct"] = dict(A, email="two"); tick()
    session(8, None); st["last"][8] = B; tick(); tick(); tick()
    st["t"] += rr.GIVE_UP; tick()
    assert 8 not in pending, "a session that never gets safe is given up after 6 h"

    st["revived"].clear(); st["outcome"] = ("ok", "on")
    session(9, "session_A9"); tick(); st["acct"] = dict(B, email="three"); tick()
    session(9, None); tick(); tick(); tick()
    assert st["revived"] == [9], "a switch while RC was on is enough, with nothing in the transcript"

    events = {json.loads(line)["event"] for line in open(rr.LOG)}
    assert {"switch", "rc-on", "rc-off", "lost-to-switch", "left-off", "ok", "wait", "give-up"} <= events


def test_a_session_waiting_for_an_answer_is_not_given_up(monkeypatch):
    st = {"t": 50000.0, "acct": dict(A, email="two")}
    monkeypatch.setattr(rr, "time", types.SimpleNamespace(time=lambda: st["t"], sleep=lambda s: None,
                                                          strftime=time.strftime))
    monkeypatch.setattr(rr, "machine_account", lambda: st["acct"])
    rec = {"pid": 7, "tmux": "s7:@1.%7", "name": "s7", "bridgeSessionId": None, "status": "waiting",
           "waitingFor": "input needed"}
    monkeypatch.setattr(rr, "records", lambda: {7: rec})
    monkeypatch.setattr(rr, "revive", lambda pid, r: ("wait", "the session is waiting (input needed)"))
    pending = {7: {"off": st["t"], "on": st["t"], "bridge": "session_A7", "decided": True, "why": None,
                   "since": st["t"]}}
    st["t"] += rr.GIVE_UP + 3600
    rr.poll(st["acct"], {}, pending, [])
    assert 7 in pending, "it waited for an answer for 7 h: still pending"
    rec.update(status="idle", waitingFor=None)
    st["t"] += 60
    rr.poll(st["acct"], {}, pending, [])
    assert 7 in pending, "answered a minute ago: the clock started then"
    st["t"] += rr.GIVE_UP
    rr.poll(st["acct"], {}, pending, [])
    assert 7 not in pending, "6 h after the answer, still not safe: given up"


def test_revive_exits_1_unless_revived(monkeypatch):
    monkeypatch.setattr(rr, "records", lambda: {7: {"pid": 7, "tmux": "s7:@1.%7", "name": "s7"}})
    monkeypatch.setattr(rr, "record", lambda pid: None)
    monkeypatch.setattr(rr, "revive", lambda pid, rec: ("failed", "tmux pane %7 is gone"))
    assert rr.main(["revive", "7"]) == 1
    monkeypatch.setattr(rr, "revive", lambda pid, rec: ("ok", "Remote Control session session_X"))
    assert rr.main(["revive", "7"]) == 0
