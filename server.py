"""
Penalty Shootout — Multiplayer Server v3
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Fixes applied vs old server:

  ✓ SYNC relay fixed — was pushing both playerIndex 0+1 eagerly when either
    player sent a sync fn. Now correctly pushes only the SENDER'S index to
    both players. Each player accumulates both sides → sync barrier fires
    naturally. This was the root cause of "game won't start".

  ✓ NO eager gamePlay push at connect time — let the SWF send it normally
    via sendGameMessage after PLAYINGSTARTED. Old code double-fired the
    barrier before the game even rendered.

  ✓ Robot mode: the SWF's §_-7f§ has its own robotSendGameMessage / robot AI.
    Server just triggers the mode with correct events; it does NOT generate
    robot AI responses (old server was fighting the SWF's own AI).

  ✓ Robot auto-joins FAST (~6 s / 12 polls) when no other humans in lobby,
    SLOW (~40 s / 80 polls) when other humans are present so invite can happen.

  ✓ Invite system: inviteToTable.php + rejectTableInvite.php + acceptTableInvite.php.
    Invites are queued in the target's per-session inbox and delivered on
    next getMessages poll. Pass targetUID=BOT to invite the robot explicitly.

  ✓ joinTable.php accepts optional tableUID param for invite acceptance.

  ✓ getGameInfo returns ALL tables so the lobby renders every player's avatar.

  ✓ Guest correctly gets JOINTABLERESULT (not OPENTABLERESULT).

  ✓ Random seeds for each PLAYINGSTARTED event.

  ✓ Name persistence: pending_names + IP-address fallback so "Player" default
    name doesn't reappear on lobby rejoin.
"""

import os
import threading
import random
import struct
import urllib.parse
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", 8080))

# ══════════════════════════════════════════════════════════════════════════════
# §_-8d§ serialiser / deserialiser  (unchanged from working original)
# ══════════════════════════════════════════════════════════════════════════════

def _escape(s):
    s = str(s).replace("&", "&amp;").replace('"', "&quot;")
    return s.replace("<", "&lt;").replace(">", "&gt;")


def serialize(val):
    if val is None:
        return "<null />"
    if isinstance(val, bool):
        return '<b v="t"/>' if val else '<b v="f"/>'
    if isinstance(val, int):
        return f'<i v="{val}"/>'
    if isinstance(val, float):
        b = struct.pack("<d", val)
        lo = struct.unpack_from("<I", b, 0)[0]
        hi = struct.unpack_from("<I", b, 4)[0]
        return f'<n v="{hi:08x}{lo:08x}"/>' if hi else f'<n v="{lo:x}"/>'
    if isinstance(val, str):
        return f'<s v="{_escape(val)}"/>'
    if isinstance(val, list):
        return "<a>" + "".join(serialize(v) for v in val) + "</a>"
    if isinstance(val, dict):
        inner = "".join(
            '<k n="' + _escape(k) + '">' + serialize(v) + "</k>"
            for k, v in val.items()
        )
        return f"<o>{inner}</o>"
    return f'<s v="{_escape(str(val))}"/>'


def push_node(fn, *params, sync_string=None, player_index=0):
    """Build one GAMEMESSAGERECEIVED XML node for a single playerIndex."""
    env = {
        "synchronizeString": sync_string,
        "playerIndex": player_index,
        "message": {"functionName": fn, "parameters": list(params)},
    }
    inner = serialize(env).replace('"', "&quot;")
    return f'<GAMEMESSAGERECEIVED message="{inner}" />'


def _parse_node(el):
    t = el.tag
    if t == "s":
        return el.get("v", "")
    if t == "i":
        return int(el.get("v", "0"))
    if t == "n":
        h = el.get("v", "0")
        lo, hi = (
            (int(h[-8:], 16), int(h[:-8], 16)) if len(h) > 8 else (int(h, 16), 0)
        )
        return struct.unpack("<d", struct.pack("<II", lo, hi))[0]
    if t == "b":
        return el.get("v") == "t"
    if t == "a":
        return [_parse_node(c) for c in el]
    if t == "o":
        obj = {}
        for k in el:
            if k.tag == "k":
                ch = list(k)
                obj[k.get("n", "")] = _parse_node(ch[0]) if ch else None
        return obj
    return None


def deserialize(s):
    try:
        return _parse_node(ET.fromstring(s.strip()))
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Global state
# ══════════════════════════════════════════════════════════════════════════════

lock          = threading.Lock()
pending_names = {}   # uid / '__last__' → name  (checkUser → joinRoom bridge)
ip_names      = {}   # client_ip → last known name  (rejoin name persistence)
sessions      = {}   # uid → {name, table_tid, inbox:[]}
tables        = {}   # tid → table dict

#  Polls before robot auto-joins (each poll ~0.5 s)
ROBOT_WAIT_SOLO  = 12    # ~6 s  — no other humans in lobby
ROBOT_WAIT_LOBBY = 80    # ~40 s — other humans present; give time to invite

#  Game-message function classification
SYNC_FNS = {
    "gamePlay",
    "chooseTeamEnded",
    "startShoot",
    "homeJerseyIsChosen",
    "awayJerseyIsChosen",
}
ASYNC_FNS = {
    "opponentShooterShooted",
    "opponentGoalkeeperJumped",
}

# ══════════════════════════════════════════════════════════════════════════════
# Table helpers
# ══════════════════════════════════════════════════════════════════════════════

ROBOT_UID  = "999999"
ROBOT_NAME = "Robot"

def new_table(tid, host_uid, host_name):
    return dict(
        tid=tid,
        host=host_uid,  host_name=host_name,
        guest=None,     guest_name=None,
        state="open",   # "open" | "playing"
        host_q=[],      guest_q=[],
        wait_polls=0,
        robot_mode=False,
    )

def player_joined_xml(uid, name, table_uid):
    """PLAYERJOINEDTABLE with full player info so the lobby can build playerInfos."""
    return (
        f'<PLAYERJOINEDTABLE playerUID="{uid}" playerName="{_escape(name)}" '
        f'playerGender="1" tableUID="{table_uid}" />'
    )


def q_push(table, uid, node):
    if uid == table["host"]:
        table["host_q"].append(node)
    elif uid == table["guest"]:
        table["guest_q"].append(node)


def q_push_both(table, node):
    table["host_q"].append(node)
    if table["guest"]:
        table["guest_q"].append(node)


def q_pop(table, uid):
    if uid == table["host"]:
        out, table["host_q"] = "".join(table["host_q"]), []
    else:
        out, table["guest_q"] = "".join(table["guest_q"]), []
    return out


def other_uid(table, uid):
    return table["guest"] if uid == table["host"] else table["host"]


def random_seeds():
    """Generate random seeds string for PLAYINGSTARTED."""
    return ",".join(str(random.randint(1, 999999)) for _ in range(2))


def wandering_humans(exclude_uid=None):
    """UIDs of players who have a session but are NOT at any table."""
    return [
        uid for uid, s in sessions.items()
        if not s.get("table_tid") and uid != exclude_uid
    ]


def find_open_table(exclude_uid):
    """Return tid of any open, guest-free, non-robot table."""
    for tid, t in tables.items():
        if t["host"] == exclude_uid or t["guest"] == exclude_uid:
            continue
        if t["state"] == "open" and t["guest"] is None and not t["robot_mode"]:
            return tid
    return None


def leave_table(uid):
    """Remove uid from their current table.  Caller MUST hold lock."""
    sess = sessions.get(uid)
    if not sess:
        return
    tid = sess.get("table_tid")
    if not tid or tid not in tables:
        sess["table_tid"] = None
        return
    t = tables[tid]
    if t["host"] == uid:
        # Host leaves — detach guest then delete table
        if t["guest"]:
            g = sessions.get(t["guest"])
            if g:
                g["table_tid"] = None
        del tables[tid]
    else:
        # Guest leaves — reset table so host can get a new opponent (or robot)
        t["guest"] = t["guest_name"] = None
        t["guest_q"] = []
        t["state"]      = "open"
        t["wait_polls"] = 0
        t["robot_mode"] = False
    sess["table_tid"] = None


# ══════════════════════════════════════════════════════════════════════════════
# HTTP Server
# ══════════════════════════════════════════════════════════════════════════════

class GameServer(BaseHTTPRequestHandler):

    # ── helpers ───────────────────────────────────────────────────────────────

    def _hdrs(self, ct="text/xml"):
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()

    def _uid(self, p):
        return p.get("playerUID") or p.get("uid") or "anon"

    def _client_ip(self):
        fwd = self.headers.get("X-Forwarded-For", "")
        return fwd.split(",")[0].strip() if fwd else self.client_address[0]

    # ── router ────────────────────────────────────────────────────────────────

    def _handle(self, p):
        path = self.path.split("?")[0]
        uid  = self._uid(p)
        ip   = self._client_ip()

        if   "getGameInfo.php"       in path: return self._get_game_info(uid)
        elif "checkUser.php"         in path: return self._check_user(uid, p)
        elif "joinRoom.php"          in path: return self._join_room(uid, p, ip)
        elif "openTable.php"         in path: self._open_table(uid);               return b""
        elif "joinTable.php"         in path: self._join_table(uid, p);            return b""
        elif "robotJoinTable.php"    in path: self._robot_join_table(uid, p);      return b""
        elif "inviteToTable.php"     in path: self._invite_to_table(uid, p);       return b""
        elif "rejectTableInvite.php" in path: self._reject_invite(uid, p);         return b""
        elif "acceptTableInvite.php" in path: self._join_table(uid, p);            return b""
        elif "getMessages.php"       in path: return self._get_messages(uid).encode()
        elif "sendGameMessage.php"   in path: self._game_message(uid, p);          return b""
        elif "leaveTable.php"        in path: self._leave(uid);                    return b""
        elif "leaveRoom.php"         in path: self._leave(uid);                    return b""
        elif "gameEnded.php"         in path: self._game_ended(uid, p);            return b""
        elif "getMyScore.php"        in path: return b"<root><score>0</score></root>"
        elif "getTopTen.php"         in path: return b"<root></root>"
        else:                                 return b""

    # ── getGameInfo ───────────────────────────────────────────────────────────

    def _get_game_info(self, uid):
        """
        Return ALL tables so the lobby can render every player's avatar.
        Open tables appear as joinable seats; playing tables as in-progress.
        Always include at least one TABLEINFO so the SWF doesn't crash.
        """
        with lock:
            n = len(sessions)
            tinfos = []
            for t in tables.values():
                puids = t["host"] + ("," + t["guest"] if t["guest"] else "")
                playing = "true" if t["state"] == "playing" else "false"
                tinfos.append(
                    f'<TABLEINFO tableUID="{t["tid"]}" possibleNoOfPlayers="2" '
                    f'playerUIDs="{puids}" viewerUIDs="" isPlaying="{playing}" />'
                )
            if not tinfos:
                tinfos = [
                    '<TABLEINFO tableUID="101" possibleNoOfPlayers="2" '
                    'playerUIDs="" viewerUIDs="" isPlaying="false" />'
                ]
        return (
            '<?xml version="1.0" encoding="utf-8"?><root>'
            f'<ROOMINFO roomID="1" roomName="Main Room" roomCapacity="100" noOfPlayers="{n}" />'
            + "".join(tinfos)
            + "</root>"
        ).encode()

    # ── checkUser ─────────────────────────────────────────────────────────────

    def _check_user(self, uid, p):
        name = (
            p.get("username") or p.get("userName")
            or p.get("playerName") or "Player"
        )
        with lock:
            pending_names[uid]        = name
            pending_names["__last__"] = name
        print(f'checkUser -> "{name}"', flush=True)
        return (
            '<?xml version="1.0" encoding="utf-8"?><root>'
            "<valid>true</valid>"
            f"<username>{_escape(name)}</username>"
            "<gender>1</gender><email>x@x.com</email>"
            "</root>"
        ).encode()

    # ── joinRoom ──────────────────────────────────────────────────────────────

    def _join_room(self, uid, p, ip):
        """
        Accept every param name the SWF might use for the player name,
        then fall back to the pending_names bridge, then the IP cache,
        then 'Player'.  Store the resolved name in ip_names so the next
        rejoin from the same IP won't fall back to 'Player'.
        """
        name = (
            p.get("playerName") or p.get("playerNameInput")
            or p.get("username") or p.get("userName")
        )
        with lock:
            if not name:
                name = (
                    pending_names.pop(uid, None)
                    or pending_names.pop("__last__", None)
                    or ip_names.get(ip)
                    or "Player"
                )
            else:
                pending_names.pop(uid, None)
                pending_names.pop("__last__", None)

            if name and name != "Player":
                ip_names[ip] = name          # persist for next rejoin

            new_uid = str(random.randint(100000, 999999))
            sessions[new_uid] = {"name": name, "table_tid": None, "inbox": []}

        print(f'-> "{name}" uid={new_uid}', flush=True)
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<root success="true" playerUID="{new_uid}" '
            f'playerName="{_escape(name)}" playerGender="1" '
            'playerNameInput="" playerID="" extraKey="">'
            f'<PLAYERINFO playerUID="{new_uid}" playerName="{_escape(name)}" playerGender="1" />'
            '<TABLEINFO tableUID="101" possibleNoOfPlayers="2" '
            'playerUIDs="" viewerUIDs="" isPlaying="false" />'
            "</root>"
        ).encode()

    # ── openTable ─────────────────────────────────────────────────────────────

    def _open_table(self, uid):
        """Player manually creates a table and waits for an opponent or robot."""
        with lock:
            sess = sessions.get(uid)
            if not sess:
                return
            leave_table(uid)
            tid = uid                        # table keyed by host uid
            tables[tid] = new_table(tid, uid, sess["name"])
            sess["table_tid"] = tid
            t = tables[tid]
            t["host_q"].append(f'<OPENTABLERESULT success="true" tableUID="{tid}" />')
            t["host_q"].append(player_joined_xml(uid, sess["name"], tid))
        print(f'-> "{sess["name"]}" opened table — waiting for opponent', flush=True)

    # ── joinTable ─────────────────────────────────────────────────────────────

    def _join_table(self, uid, p=None):
        """
        Join a table.

        If p contains tableUID (set by invite-acceptance flow),
        try to join that specific table first.
        Otherwise find any open table.
        If none exists, create a solo table; robot joins after delay.
        """
        target_tid = p.get("tableUID") if p else None
        with lock:
            sess = sessions.get(uid)
            if not sess:
                return
            leave_table(uid)

            # 1) Invited to a specific table?
            if target_tid and target_tid in tables:
                t = tables[target_tid]
                if (
                    t["state"] == "open"
                    and t["guest"] is None
                    and t["host"] != uid
                    and not t["robot_mode"]
                ):
                    self._connect_two_players(target_tid, uid, sess)
                    return

            # 2) Any open table in the lobby?
            tid = find_open_table(uid)
            if tid:
                self._connect_two_players(tid, uid, sess)
                return

            # 3) Create solo table; robot joins later via getMessages logic
            print(f'  No open table for "{sess["name"]}" → solo table', flush=True)
            tid = uid
            tables[tid] = new_table(tid, uid, sess["name"])
            sess["table_tid"] = tid
            t = tables[tid]
            t["host_q"].append(f'<OPENTABLERESULT success="true" tableUID="{tid}" />')
            t["host_q"].append(player_joined_xml(uid, sess["name"], tid))

    # ── inviteToTable ─────────────────────────────────────────────────────────

    def _invite_to_table(self, uid, p):
        """
        Host invites another player (or the bot) to their table.

        Expected params (server accepts multiple name variants):
          targetUID / invitedUID / inviteUID / playerUID2 — who to invite
          tableUID — which table (defaults to host's current table)

        Special: targetUID == "BOT", "ROBOT", or "0" → trigger immediate robot join.
        """
        target_uid = (
            p.get("targetUID") or p.get("invitedUID")
            or p.get("inviteUID") or p.get("playerUID2")
        )
        with lock:
            sess = sessions.get(uid)
            if not sess:
                return
            table_uid = p.get("tableUID") or sess.get("table_tid")
            if not table_uid:
                return

            # ── invite the robot ──────────────────────────────────────────
            if target_uid in (None, "BOT", "ROBOT", "0", "bot", "robot"):
                self._robot_join_now(table_uid)
                return

            inviter_name = sess.get("name", "Player")

            # ── invite a human ────────────────────────────────────────────
            target_sess = sessions.get(target_uid)
            if not target_sess:
                print(f'  Invite: target "{target_uid}" not found', flush=True)
                return
            if target_sess.get("table_tid"):
                # Target already in a game
                return

            msg = (
                f'<INVITETOTABLE tableUID="{table_uid}" playerUID="{uid}" '
                f'playerName="{_escape(inviter_name)}" />'
            )
            target_sess.setdefault("inbox", []).append(msg)
            print(
                f'  "{inviter_name}" invited uid={target_uid} to table {table_uid}',
                flush=True,
            )

    # ── rejectTableInvite ─────────────────────────────────────────────────────

    def _reject_invite(self, uid, p):
        """
        Invited player rejects; notify the host via their table queue.
        Params: tableUID (required), hostUID / inviterUID (fallback lookup).
        """
        table_uid = p.get("tableUID")
        host_uid  = p.get("hostUID") or p.get("inviterUID")
        with lock:
            rejecter_name = sessions.get(uid, {}).get("name", "Player")
            notified = False

            if table_uid and table_uid in tables:
                tables[table_uid]["host_q"].append(
                    f'<TABLEINVITEREJECTED playerUID="{uid}" tableUID="{table_uid}" />'
                )
                notified = True

            if not notified and host_uid:
                h_sess = sessions.get(host_uid)
                if h_sess:
                    tid = h_sess.get("table_tid")
                    if tid and tid in tables:
                        tables[tid]["host_q"].append(
                            f'<TABLEINVITEREJECTED playerUID="{uid}" tableUID="{tid}" />'
                        )

        print(f'  "{rejecter_name}" rejected invite to table {table_uid}', flush=True)

    # ── connect two human players ─────────────────────────────────────────────

    def _connect_two_players(self, tid, guest_uid, guest_sess):
        """Wire up a 2-player game.  Caller MUST hold lock."""
        t = tables[tid]
        t["guest"]      = guest_uid
        t["guest_name"] = guest_sess["name"]
        t["state"]      = "playing"
        guest_sess["table_tid"] = tid
        host_uid   = t["host"]
        host_name  = t["host_name"]
        guest_name = guest_sess["name"]
        seeds = random_seeds()

        # Host learns the guest joined, then game starts
        t["host_q"].append(player_joined_xml(guest_uid, guest_name, tid))
        t["host_q"].append('<STARTPLAYINGRESULT success="true" />')
        t["host_q"].append(f'<PLAYINGSTARTED tableUID="{tid}" randomSeeds="{seeds}" />')

        # Guest event sequence:
        #   JOINTABLERESULT  — confirms the join (guest did NOT open the table)
        #   PLAYERJOINEDTABLE(host) — host is playerInfos[0] / playerIndex 0
        #   PLAYERJOINEDTABLE(guest) — guest is playerInfos[1] / playerIndex 1
        #   STARTPLAYINGRESULT + PLAYINGSTARTED — start the game
        #
        # IMPORTANT: do NOT send OPENTABLERESULT to the guest.
        # If the guest receives OPENTABLERESULT it believes it is the host
        # (player 0).  Both players then have playerIndex 0 → the sync
        # barrier waits for index 1 forever → game hangs → sendGameMessage
        # is never called.  JOINTABLERESULT is the correct event here.
        t["guest_q"].append(f'<JOINTABLERESULT success="true" tableUID="{tid}" />')
        t["guest_q"].append(player_joined_xml(host_uid, host_name, tid))
        t["guest_q"].append(player_joined_xml(guest_uid, guest_name, tid))
        t["guest_q"].append('<STARTPLAYINGRESULT success="true" />')
        t["guest_q"].append(f'<PLAYINGSTARTED tableUID="{tid}" randomSeeds="{seeds}" />')

        # ── CRITICAL: do NOT push gamePlay here ──────────────────────────────
        # After PLAYINGSTARTED the SWF sends gamePlay via sendGameMessage.
        # _relay() will push each sender's playerIndex to BOTH players.
        # Each player accumulates index 0 + index 1 → sync barrier fires.
        # Pushing an eager pair here broke the old server.

        print(f'MULTIPLAYER: "{host_name}" vs "{guest_name}"', flush=True)

    # ── robotJoinTable ────────────────────────────────────────────────────────

    def _robot_join_table(self, uid, p):
        """
        Handle POST /robotJoinTable.php

        Called by the SWF in two situations:
          1. Player explicitly invites the robot from the lobby.
          2. The duplicate-instance SharedObject timer (§_-9g§) fires after 5s
             and detects two Flash windows sharing the same local storage.
             The second instance calls this endpoint — but may NOT send
             playerUID in the POST body, so uid may be 'anon'.

        Strategy:
          - Try to find the table via uid → session → table_tid  (normal path)
          - If that fails, try the tableUID param directly
          - If still nothing, scan ALL tables for any playing, non-robot table
            and convert the FIRST one found (there's only ever one when playing solo)
        """
        print(f'  robotJoinTable called: uid={uid!r} params={p}', flush=True)
        with lock:
            tid = None

            # Path 1: uid → session → table
            sess = sessions.get(uid)
            if sess:
                tid = p.get("tableUID") or sess.get("table_tid")

            # Path 2: tableUID param directly
            if not tid:
                tid = p.get("tableUID")

            # Path 3: scan all tables for a playing non-robot table
            if not tid or tid not in tables:
                for candidate_tid, t in tables.items():
                    if t["state"] in ("open", "playing") and not t["robot_mode"]:
                        tid = candidate_tid
                        break

            if not tid or tid not in tables:
                print('  robotJoinTable: no suitable table found', flush=True)
                return

            t = tables[tid]

            if t["state"] == "open" and not t["robot_mode"]:
                # Table waiting, robot joins now
                self._robot_join_now(tid)

            elif t["state"] == "playing" and not t["robot_mode"]:
                # Multiplayer game → convert to robot mode.
                # Disconnect the ghost guest (second Flash window), reset, robot-join.
                guest_uid = t["guest"]
                if guest_uid and guest_uid != ROBOT_UID and guest_uid in sessions:
                    sessions[guest_uid]["table_tid"] = None
                t["guest"]      = None
                t["guest_name"] = None
                t["guest_q"]    = []
                t["state"]      = "open"
                t["wait_polls"] = 0
                t["host_q"]     = []   # clear stale multiplayer messages
                self._robot_join_now(tid)
                print(f'  Multiplayer → robot conversion for table {tid}', flush=True)
            else:
                print(f'  robotJoinTable: table {tid} already in robot mode, ignored', flush=True)

    # ── robot join ────────────────────────────────────────────────────────────

    def _robot_join_now(self, tid):
        """
        Trigger immediate robot join on a specific table.

        The SWF's §_-7f§ has built-in robot AI via robotSendGameMessage.
        Server must:
          1. Send PLAYERJOINEDTABLE with a fake robot UID/name so the lobby
             knows there are 2 players and calls startGame (fixes "2 players needed").
          2. In _relay / robot response, handle SYNC functions by pushing both
             playerIndex 0 and 1 — because there's no real player 1 to do it.
        Caller MUST hold lock.
        """
        if tid not in tables:
            return
        t = tables[tid]
        if t["state"] != "open" or t["robot_mode"]:
            return
        t["robot_mode"] = True
        t["state"]      = "playing"
        t["guest"]      = ROBOT_UID          # treat robot as guest for q routing
        t["guest_name"] = ROBOT_NAME
        seeds = random_seeds()
        # ROBOTJOINTABLERESULT tells the SWF to switch to robot mode
        t["host_q"].append('<ROBOTJOINTABLERESULT success="true" />')
        # PLAYERJOINEDTABLE with robot info so lobby knows seat 2 is filled
        t["host_q"].append(player_joined_xml(ROBOT_UID, ROBOT_NAME, tid))
        t["host_q"].append('<STARTPLAYINGRESULT success="true" />')
        t["host_q"].append(f'<PLAYINGSTARTED tableUID="{tid}" randomSeeds="{seeds}" />')
        print(f'ROBOT joined table {tid}', flush=True)

    # ── getMessages (polling loop) ────────────────────────────────────────────

    def _get_messages(self, uid):
        with lock:
            sess = sessions.get(uid)
            if not sess:
                return ""

            # ── Inbox: invites / rejections for wandering players ──────────
            inbox = sess.get("inbox")
            if inbox:
                out = "".join(inbox)
                sess["inbox"] = []
                return out

            tid = sess.get("table_tid")
            if not tid or tid not in tables:
                return ""
            t = tables[tid]

            # ── Auto robot-join (only the host polls count) ────────────────
            if t["state"] == "open" and not t["robot_mode"] and t["host"] == uid:
                t["wait_polls"] += 1
                wanderers = wandering_humans(exclude_uid=uid)
                limit = ROBOT_WAIT_LOBBY if wanderers else ROBOT_WAIT_SOLO
                if t["wait_polls"] >= limit:
                    self._robot_join_now(tid)

            return q_pop(t, uid)

    # ── sendGameMessage ───────────────────────────────────────────────────────

    def _game_message(self, uid, p):
        raw = p.get("message", "")
        if not raw:
            return
        obj = deserialize(raw)
        if obj is None:
            return

        # Unwrap envelope layers the SWF may add
        if isinstance(obj, dict) and "message" in obj:
            obj = obj["message"]
        fn     = obj.get("functionName", "") if isinstance(obj, dict) else ""
        params = obj.get("parameters", [])   if isinstance(obj, dict) else []
        if not fn and isinstance(obj, list) and obj:
            inner = obj[0]
            if isinstance(inner, dict) and "message" in inner:
                inner = inner["message"]
            fn     = inner.get("functionName", "") if isinstance(inner, dict) else ""
            params = inner.get("parameters", [])   if isinstance(inner, dict) else []
        if not fn:
            return

        with lock:
            sess = sessions.get(uid)
            if not sess:
                return
            tid = sess.get("table_tid")
            if not tid or tid not in tables:
                return
            t = tables[tid]

            if t["robot_mode"]:
                # In robot mode, the SWF's built-in AI handles game moves via
                # robotSendGameMessage (client-side). But SYNC functions need the
                # server to fake the robot's response — because there's no real
                # player 1 to send its side of the sync. We push BOTH playerIndex
                # 0 (human, what they just sent) and playerIndex 1 (robot) so
                # the sync barrier fires immediately.
                if fn in SYNC_FNS:
                    node0 = push_node(fn, *params, sync_string=fn, player_index=0)
                    node1 = push_node(fn, *params, sync_string=fn, player_index=1)
                    t["host_q"].append(node0)
                    t["host_q"].append(node1)
                # ASYNC functions in robot mode: the SWF handles them via
                # robotSendGameMessage internally — server does nothing.
            else:
                self._relay(t, uid, fn, params)

        print(f'  {sessions.get(uid, {}).get("name", "?")} -> {fn}', flush=True)

    # ── relay (2-player game messages) ───────────────────────────────────────

    def _relay(self, t, uid, fn, params):
        """
        Relay game messages correctly between two human players.

        SYNC functions (gamePlay, chooseTeamEnded, startShoot, …):
          The NovelGames sync barrier fires when each player has received
          a message with BOTH playerIndex 0 and playerIndex 1.

          Correct flow:
            Player A sends fn → server pushes playerIndex=0 to BOTH A and B
            Player B sends fn → server pushes playerIndex=1 to BOTH A and B
            Each player now has both indices → barrier fires → game advances

          OLD BROKEN behaviour:
            When either player sent fn → server pushed BOTH indices to BOTH
            players immediately → barrier fired before other player sent fn
            → game state desync / game appeared to hang.

        ASYNC functions (opponentShooterShooted, opponentGoalkeeperJumped):
          One-directional: only the other player receives it.
        """
        sender_idx = 0 if uid == t["host"] else 1
        other = other_uid(t, uid)

        if fn in SYNC_FNS:
            # Push sender's index to BOTH players
            node = push_node(fn, *params, sync_string=fn, player_index=sender_idx)
            q_push_both(t, node)

        elif fn in ASYNC_FNS:
            # Push to the OTHER player only
            if other:
                node = push_node(fn, *params, sync_string=None, player_index=sender_idx)
                q_push(t, other, node)

    # ── leave / game end ──────────────────────────────────────────────────────

    def _leave(self, uid):
        with lock:
            name = sessions.get(uid, {}).get("name", "?")
            leave_table(uid)
            sessions.pop(uid, None)
        print(f'"{name}" left', flush=True)

    def _game_ended(self, uid, p):
        ranks = p.get("ranks", "?")
        name  = sessions.get(uid, {}).get("name", "Player")
        try:
            r = [int(x) for x in ranks.split(",")]
            winner = name if r[0] == 0 else "Opponent/Robot"
            print(f'WINNER: "{winner}"', flush=True)
        except Exception:
            pass
        with lock:
            leave_table(uid)

    # ── HTTP plumbing ─────────────────────────────────────────────────────────

    def do_HEAD(self):
        """Render.com and load balancers send HEAD for health checks."""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()

    def do_OPTIONS(self):
        self._hdrs()

    def do_POST(self):
        n    = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode("utf-8", "replace")
        p    = {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}
        self._hdrs()
        result = self._handle(p)
        self.wfile.write(result if result is not None else b"")

    def do_GET(self):
        path   = self.path.split("?")[0]
        qs_str = self.path.split("?")[1] if "?" in self.path else ""
        p      = {k: v[0] for k, v in urllib.parse.parse_qs(qs_str).items()}

        if path == "/Penalty.swf":
            swf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Penalty.swf")
            if not os.path.exists(swf):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Penalty.swf not found")
                return
            with open(swf, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-shockwave-flash")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
            return

        if "crossdomain.xml" in path:
            self._hdrs()
            self.wfile.write(
                b'<?xml version="1.0"?><cross-domain-policy>'
                b'<allow-access-from domain="*"/></cross-domain-policy>'
            )
            return

        if ".php" in path:
            self._hdrs()
            result = self._handle(p)
            self.wfile.write(result if result is not None else b"")
            return

        # Landing page
        host = self.headers.get("Host", "localhost")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(LANDING.replace("__HOST__", host).encode())

    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} {fmt % args}", flush=True)


# ══════════════════════════════════════════════════════════════════════════════
# Landing page
# ══════════════════════════════════════════════════════════════════════════════

LANDING = """<!DOCTYPE html><html><head>
<meta charset="UTF-8"><title>Penalty Shootout</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#1a1a2e;color:#eee;font-family:monospace;
       display:flex;flex-direction:column;align-items:center;
       justify-content:center;min-height:100vh;gap:24px;padding:24px}
  h1{font-size:2.2rem;color:#00e676}
  .box{background:#0d0d1a;border:1px solid #333;border-radius:10px;
       padding:24px 28px;max-width:600px;width:100%;line-height:1.85}
  .box h2{font-size:1rem;color:#00e676;margin-bottom:12px;letter-spacing:.05em}
  .badge{display:inline-block;background:#00e676;color:#000;font-size:.7rem;
         padding:1px 6px;border-radius:3px;margin-left:6px;vertical-align:middle}
  .url{background:#000;color:#00e676;padding:10px 16px;border-radius:4px;
       font-size:.95rem;word-break:break-all;margin:10px 0;display:block;
       border:1px solid #00e676;user-select:all}
  ol{padding-left:1.4em;margin:8px 0} li{margin:6px 0}
  a{color:#69f0ae;text-decoration:none} a:hover{text-decoration:underline}
  .dl{display:flex;gap:10px;margin:8px 0;flex-wrap:wrap}
  .dl a{background:#1a1a2e;border:1px solid #00e676;color:#00e676;
        padding:6px 14px;border-radius:4px;font-size:.85rem}
  .dl a:hover{background:#00e676;color:#000;text-decoration:none}
  .note{color:#888;font-size:.8rem;margin-top:10px}
  .tip{background:#0a2a0a;border:1px solid #1a7a1a;border-radius:6px;
       padding:12px 16px;margin-top:8px;font-size:.85rem;color:#aaffaa;line-height:1.7}
</style></head><body>
<h1>&#9917; Penalty Shootout</h1>
<div class="box">
  <h2>HOW MULTIPLAYER WORKS</h2>
  <div class="tip">
    &#x2022; Open the game &mdash; you appear as a wandering player in the lobby.<br>
    &#x2022; <b>Click another player&rsquo;s avatar</b> to send them an invite.<br>
    &#x2022; They can <b>accept</b> (game starts) or <b>decline</b> (you can re-invite or invite the bot).<br>
    &#x2022; If you are the <b>only player online</b>, a robot joins automatically in ~6 s.<br>
    &#x2022; If other players are online, the robot waits ~40 s to give you time to invite first.
  </div>
</div>
<div class="box">
  <h2>OPTION 1 &middot; Flash Player Standalone <span class="badge">RECOMMENDED</span></h2>
  <ol>
    <li>Download Flash Player 32:
      <div class="dl">
        <a href="https://github.com/ntkernel/flash/releases/download/32.0.0.238/flashplayer_32_sa.exe">&#8595; Windows (.exe)</a>
        <a href="https://github.com/ntkernel/flash/releases/download/32.0.0.238/flashplayer_32_sa.dmg">&#8595; macOS (.dmg)</a>
      </div>
    </li>
    <li>Open Flash Player &rarr; <b>File &rarr; Open</b></li>
    <li>Paste this URL and press Enter:</li>
  </ol>
  <span class="url">http://__HOST__/Penalty.swf</span>
  <p class="note">No install &mdash; run the .exe / .dmg directly</p>
</div>
<div class="box">
  <h2>OPTION 2 &middot; Pale Moon Browser</h2>
  <ol>
    <li>Download <a href="https://www.palemoon.org/download.shtml" target="_blank">Pale Moon</a></li>
    <li>Enable Flash: Menu &rarr; <b>Add-ons &rarr; Plugins &rarr; Shockwave Flash &rarr; Always Activate</b></li>
    <li>Open in Pale Moon:</li>
  </ol>
  <span class="url">http://__HOST__/Penalty.swf</span>
</div>
</body></html>"""


# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 60, flush=True)
    print("PENALTY SHOOTOUT — MULTIPLAYER SERVER v3", flush=True)
    print(f"http://0.0.0.0:{PORT}", flush=True)
    print("Solo vs robot  |  2-player relay  |  Invite system", flush=True)
    print("=" * 60, flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), GameServer).serve_forever()
