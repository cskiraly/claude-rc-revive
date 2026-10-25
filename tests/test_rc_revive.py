"""rc-revive's logic, with no typing: the screen reader on captured panes, the transcript reader, what it types, the
service file, and the watcher's decisions with the sessions, the account, the clock and the typing replaced by stubs."""
import json
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

import rc_revive as rr

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
    assert read("empty") == {"mode": "prompt", "kind": "empty", "typed": "", "stashed": False}


def test_a_suggested_prompt_is_not_a_draft():
    assert read("suggestion") == {"mode": "prompt", "kind": "suggestion", "typed": "", "stashed": False}


def test_a_busy_session_still_shows_its_input_box():
    assert read("busy")["kind"] == "empty"


def test_a_permission_prompt_has_no_input_box():
    assert read("permission") is None


def test_a_draft():
    assert read("empty", DRAFT) == {"mode": "prompt", "kind": "draft", "typed": "half a sentence, and",
                                    "stashed": False}


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


# --- the service and the command line ---------------------------------------------------------------------------

def test_the_service_runs_this_python(monkeypatch):
    monkeypatch.setattr(rr.sys, "executable", "/home/u/.local/share/uv/tools/rc-revive/bin/python")
    assert "\nExecStart=/home/u/.local/share/uv/tools/rc-revive/bin/python -m rc_revive watch\n" in rr.unit_text()
    monkeypatch.setattr(rr.sys, "executable", "/opt/my tools/bin/python")
    assert '\nExecStart="/opt/my tools/bin/python" -m rc_revive watch\n' in rr.unit_text()


def test_install_and_uninstall_service(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(rr, "HOME", tmp_path)
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
    p = subprocess.run([sys.executable, "-m", "rc_revive", "--version"], capture_output=True, text=True)
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
