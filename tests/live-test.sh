#!/usr/bin/env bash
# Live test of rc-revive's typing, on a throwaway Claude Code session in its own tmux session (rc-revive-test).
# It starts Remote Control there under the machine's current account (it shows up in the Claude app for a
# moment), then disconnects it and closes the session. No prompt is sent to the model. Run it from a directory
# Claude Code already trusts (the session starts there), or set RC_REVIVE_TEST_DIR.
#
# 1. a draft on the input line: revive must stash it, start RC titled with the session's name, and the draft must
#    come back as it was;
# 1b. a name typed with /remote-control, other than the session's own, must become the RC title (what revive relies
#    on; Claude Code keeps the first 12 hex digits of the title's SHA-256 in ~/.claude.json);
# 2. a draft plus an older stash: revive must refuse to type (it would overwrite the stash).
set -u
here=$(cd "$(dirname "$0")" && pwd)
R() { PYTHONPATH="$here/../src" python3 -m claude_rc_revive "$@"; } # this checkout's rc-revive
CLAUDE=${CLAUDE:-claude}
DIR=${RC_REVIVE_TEST_DIR:-$PWD}
T=rc-revive-test
DRAFT="half a sentence that must not be sent, with a comma, and"
fail() { echo "FAIL: $*"; tmux capture-pane -p -t "$pane" | tail -12; cleanup; exit 1; }
cleanup() {
	[ -n "${pid:-}" ] && [ "$(rec bridgeSessionId)" != "None" ] && disconnect
	tmux kill-session -t "$T" 2>/dev/null
}
rec() { python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get(sys.argv[2]))' "$HOME/.claude/sessions/$pid.json" "$1"; }
scr() { R screen "$pane"; }
clear_input() { # delete what is on the input line, one character at a time
	local n
	n=$(scr | python3 -c 'import json, sys; s = json.load(sys.stdin); print(len(s["typed"]) + 5 if s else 0)')
	[ "$n" -gt 0 ] && tmux send-keys -t "$pane" $(printf 'BSpace %.0s' $(seq "$n"))
	sleep 0.5
}
titled() { # whether the session's RC session has this title
	local id sha
	id=cse_$(rec bridgeSessionId | cut -d_ -f2-)
	for _ in $(seq 20); do
		sha=$(jq -r --arg id "$id" '.replBridgePlaceholders[$id].titleSha // ""' "$HOME/.claude.json")
		[ -n "$sha" ] && break
		sleep 0.5
	done
	[ "$sha" = "$(printf %s "$1" | sha256sum | cut -c1-12)" ]
}
disconnect() { # /remote-control on a connected session opens a menu: Disconnect, QR code, Continue (focused)
	clear_input
	tmux send-keys -t "$pane" -l /remote-control
	sleep 0.8
	tmux send-keys -t "$pane" Enter
	sleep 1.5
	tmux send-keys -t "$pane" Up Up
	sleep 0.5
	tmux capture-pane -p -t "$pane" | grep -q 'Disconnect this session' || { echo "no disconnect menu"; return 1; }
	tmux send-keys -t "$pane" Enter
	for _ in $(seq 20); do [ "$(rec bridgeSessionId)" = "None" ] && echo "  RC disconnected" && return 0; sleep 0.5; done
	echo "  RC still on after the disconnect"
}

tmux kill-session -t "$T" 2>/dev/null
tmux new-session -d -s "$T" -x 150 -y 40 -c "$DIR" \
	"env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT -u CLAUDE_CODE_MESSAGING_SOCKET -u CLAUDE_CODE_MESSAGING_TOKEN -u CLAUDE_CODE_BRIDGE_SESSION_ID -u CLAUDE_CONFIG_DIR $CLAUDE"
pane=$(tmux display -p -t "$T" '#{pane_id}')
pid=
for _ in $(seq 60); do # the session's record names its tmux pane
	pid=$(grep -l "\"tmux\":\"$T:" "$HOME"/.claude/sessions/*.json 2>/dev/null | head -1 | xargs -r basename | cut -d. -f1)
	[ -n "$pid" ] && scr | grep -q '"mode"' && break
	sleep 0.5
done
[ -n "$pid" ] || fail "no session record for $T"
echo "== session $pid in pane $pane: $(scr)"

echo "== 1. a draft on the input line"
tmux send-keys -t "$pane" -l "$DRAFT"
sleep 1
scr | grep -q '"kind": "draft"' || fail "the draft isn't read as a draft: $(scr)"
R revive "$T" || fail "revive exited with an error"
[ "$(rec bridgeSessionId)" != "None" ] || fail "RC isn't on"
echo "  RC on: $(rec bridgeSessionId)"
name=$(rec name)
titled "$name" || fail "the RC session isn't titled with the session's name '$name'"
echo "  titled with the session's name: '$name'"
got=$(scr | python3 -c 'import json, sys; print(json.load(sys.stdin)["typed"])')
[ "$got" = "$DRAFT" ] || fail "the draft didn't come back: '$got'"
echo "  the draft is back: '$got'"
disconnect || fail "couldn't disconnect"

echo "== 1b. a name typed with the command becomes the RC title"
clear_input
tmux send-keys -t "$pane" -l "/remote-control rc-revive-title-check"
sleep 0.8
tmux send-keys -t "$pane" Enter
for _ in $(seq 60); do [ "$(rec bridgeSessionId)" != "None" ] && break; sleep 0.5; done
[ "$(rec bridgeSessionId)" != "None" ] || fail "RC isn't on"
titled rc-revive-title-check || fail "the RC session isn't titled rc-revive-title-check"
echo "  titled rc-revive-title-check (the session's name: '$(rec name)')"
disconnect || fail "couldn't disconnect"

echo "== 2. a draft plus an older stash: no typing"
clear_input
tmux send-keys -t "$pane" -l "first draft"
sleep 0.5
tmux send-keys -t "$pane" C-s
sleep 0.5
tmux send-keys -t "$pane" -l "second draft"
sleep 0.8
echo "  screen: $(scr)"
out=$(PYTHONPATH="$here/../src" timeout 20 python3 -m claude_rc_revive revive "$T" 2>&1 | head -1)
echo "  revive: $out"
case $out in *"another one in the stash"*) echo "  refused, as it should" ;; *) fail "expected a refusal" ;; esac
[ "$(rec bridgeSessionId)" = "None" ] || fail "RC came on although it shouldn't have"

echo "== PASS"
cleanup
