"""
MOCK SERVER v20 — CORRECT SERIALIZER FORMAT
────────────────────────────────────────────────────────────
§_-8d§ wire format (from decompiled serializer):

  String:  <s v="hello"/>
  Int:     <i v="42"/>
  Number:  <n v="3f800000"/>  ← little-endian IEEE 754 hex
  Boolean: <b v="t"/> or <b v="f"/>
  Array:   <a><i v="0"/><i v="1"/></a>
  Object:  <o><k n="key">value_node</k>...</o>
  Null:    <null />

A sendGameMessage message is an object with keys:
  functionName  → <s v="..."/>
  parameters    → <a> of typed values </a>

Full game flow:
  PLAYINGSTARTED → robot pushes gamePlay
  Player sends §_-5W§(chooseTeamEnded, [t0,t1]) batch
  → robot echoes chooseTeamEnded back
  [if same team] → robot pushes homeJerseyIsChosen/awayJerseyIsChosen
  Penalty rounds:
    Player's shoot turn: robot pushes startShoot, then after player kicks
      robot pushes opponentGoalkeeperJumped
    Robot's shoot turn: robot pushes opponentShooterShooted(x,y)
      player dives, sends opponentGoalkeeperJumped
    Results: opponentGoal / opponentMissed auto-resolved by game physics
"""

import os, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse, random, struct
import xml.etree.ElementTree as ET

PORT = int(os.environ.get('PORT', 8080))

# ══════════════════════════════════════════════════════════════════════════════
# §_-8d§ serializer  (Python port)
# ══════════════════════════════════════════════════════════════════════════════

def _escape(s):
    s = str(s).replace('&', '&amp;').replace('"', '&quot;')
    return s.replace('<', '&lt;').replace('>', '&gt;')

def serialize(val):
    """Python value → §_-8d§ XML string."""
    if val is None:
        return '<null />'
    if isinstance(val, bool):
        return f'<b v="{"t" if val else "f"}"/>'
    if isinstance(val, int):
        return f'<i v="{val}"/>'
    if isinstance(val, float):
        # little-endian IEEE 754 double, written as hex
        b = struct.pack('<d', val)
        lo = struct.unpack_from('<I', b, 0)[0]
        hi = struct.unpack_from('<I', b, 4)[0]
        if hi != 0:
            return f'<n v="{hi:08x}{lo:08x}"/>'
        return f'<n v="{lo:x}"/>'
    if isinstance(val, str):
        return f'<s v="{_escape(val)}"/>'
    if isinstance(val, list):
        inner = ''.join(serialize(v) for v in val)
        return f'<a>{inner}</a>'
    if isinstance(val, dict):
        inner = ''.join(f'<k n="{_escape(k)}">{serialize(v)}</k>'
                        for k, v in val.items())
        return f'<o>{inner}</o>'
    return f'<s v="{_escape(str(val))}"/>'

def game_msg(function_name, *params):
    """Build a serialised game message object."""
    obj = {'functionName': function_name, 'parameters': list(params)}
    return serialize(obj)

def push_node(function_name, *params, sync_string=None, player_index=1):
    """Wrap in GAMEMESSAGERECEIVED envelope for getMessages push.

    §_-22§ line 2135-2136:
      _loc2_ = §_-8d§.§_-Gs§(param1.attributes.message)
      addReceivedGameMessage(_loc2_.message, _loc2_.synchronizeString, _loc2_.playerIndex)

    §_-22§.§_-3m§ stores messages by synchronizeString+playerIndex and only
    fires receiveGameMessage once ALL players have contributed their entry.

    For §_-5W§ (synchronised) calls the server must push one node for EACH
    playerIndex (0 and 1) sharing the same synchronizeString.
    For §_-Fh§ (unsynchronised, synchronizeString=None) only one push needed.
    """
    envelope = {
        'synchronizeString': sync_string,   # None → <null/>
        'playerIndex': player_index,
        'message': {'functionName': function_name, 'parameters': list(params)}
    }
    inner = serialize(envelope).replace('"', '&quot;')
    return f'<GAMEMESSAGERECEIVED message="{inner}" />'


def sync_push_pair(function_name, *params):
    """Return TWO push nodes for a §_-5W§ synchronised call (playerIndex 0 and 1).
    Both share synchronizeString=function_name so §_-I§ fires when both arrive."""
    node0 = push_node(function_name, *params, sync_string=function_name, player_index=0)
    node1 = push_node(function_name, *params, sync_string=function_name, player_index=1)
    return node0 + node1

# ── deserializer ──────────────────────────────────────────────────────────────

def _parse_node(el):
    """§_-8d§ XML element → Python value."""
    tag = el.tag
    if tag == 's':
        return el.get('v', '')
    if tag == 'i':
        return int(el.get('v', '0'))
    if tag == 'n':
        h = el.get('v', '0')
        if len(h) > 8:
            hi = int(h[:-8], 16)
            lo = int(h[-8:], 16)
        else:
            hi = 0
            lo = int(h, 16)
        b = struct.pack('<II', lo, hi)
        return struct.unpack('<d', b)[0]
    if tag == 'b':
        return el.get('v') == 't'
    if tag == 'a':
        return [_parse_node(c) for c in el]
    if tag == 'o':
        obj = {}
        for k_el in el:
            if k_el.tag == 'k':
                key = k_el.get('n', '')
                children = list(k_el)
                obj[key] = _parse_node(children[0]) if children else None
        return obj
    if tag == 'null':
        return None
    return None

def deserialize(xml_str):
    """§_-8d§ XML string → Python object."""
    try:
        root = ET.fromstring(xml_str.strip())
        return _parse_node(root)
    except Exception as e:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Server
# ══════════════════════════════════════════════════════════════════════════════

def new_session():
    return {
        'player_name':   'Guest',
        'player_uid':    'anon',
        'at_table':      False,
        'msg_count':     0,
        'sequence_done': False,
        'in_game':       False,
        'cycle':         0,
        'robot_queue':   [],
        'game_phase':    None,
        'penalty_round': 0,
        'robot_score':   0,
        'player_score':  0,
        'awaiting_player_shot_result': False,
    }

sessions      = {}   # playerUID → state dict
sessions_lock = threading.Lock()
pending_names = {}   # swf_uid → player_name (bridges checkUser → joinRoom)

def get_session(uid):
    with sessions_lock:
        if uid not in sessions:
            sessions[uid] = new_session()
            sessions[uid]['player_uid'] = uid
        return sessions[uid]


class GameMockServer(BaseHTTPRequestHandler):

    def _hdrs(self, ct='text/xml'):
        self.send_response(200)
        self.send_header('Content-type', ct)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.end_headers()

    def _reset(self, uid):
        g = get_session(uid)
        g.update({
            'at_table': False, 'msg_count': 0,
            'sequence_done': False, 'in_game': False,
            'game_phase': None, 'robot_queue': [],
            'penalty_round': 0, 'robot_score': 0, 'player_score': 0,
            'awaiting_player_shot_result': False,
        })

    def _uid_from_params(self, p):
        return p.get('playerUID', p.get('uid', 'anon'))

    # ─────────────────────────────────────────────────────────────────────────

    def _handle_request(self, p):
        """Shared handler for both GET and POST — returns response bytes."""
        uid  = self._uid_from_params(p)
        g    = get_session(uid)
        name = g['player_name']
        resp = b''

        if 'getGameInfo.php' in self.path:
            # New session starts here — assign uid from whatever the game sends
            # or generate one if needed
            self._reset(uid)
            resp = (
                '<?xml version="1.0" encoding="utf-8"?><root>'
                '<ROOMINFO roomID="1" roomName="Main Room" '
                'roomCapacity="100" noOfPlayers="1" /></root>'
            ).encode()

        elif 'checkUser.php' in self.path:
            username = p.get('username') or p.get('userName') or 'Player'
            # Save the name keyed by whatever uid the SWF sent —
            # joinRoom will arrive with the same uid and pick it up.
            incoming_uid = uid  # the SWF's own uid (e.g. "1001")
            with sessions_lock:
                pending_names[incoming_uid] = username
            print(f'✓ checkUser → "{username}" (swf_uid={incoming_uid})')
            resp = (
                '<?xml version="1.0" encoding="utf-8"?><root>'
                '<valid>true</valid>'
                f'<username>{username}</username>'
                '<gender>1</gender><email>player@game.com</email>'
                '</root>'
            ).encode()

        elif 'joinRoom.php' in self.path:
            # Look up the name saved by checkUser (same incoming swf uid)
            with sessions_lock:
                username = pending_names.pop(uid, None) or name or 'Player'
            # Always generate a NEW server-side uid so two players with
            # the same default swf uid get completely separate sessions.
            new_uid = str(random.randint(100000, 999999))
            with sessions_lock:
                sessions[new_uid] = new_session()
                sessions[new_uid]['player_name'] = username
                sessions[new_uid]['player_uid']  = new_uid
            uid  = new_uid
            g    = get_session(uid)
            name = username
            print(f'✓ "{name}" joined lobby → uid={uid}')
            resp = (
                '<?xml version="1.0" encoding="utf-8"?>'
                f'<root success="true" playerUID="{uid}" '
                f'playerName="{name}" playerGender="1" '
                'playerNameInput="" playerID="" extraKey="">'
                f'<PLAYERINFO playerUID="{uid}" playerName="{name}" playerGender="1" />'
                '<TABLEINFO tableUID="101" possibleNoOfPlayers="2" '
                'playerUIDs="" viewerUIDs="" isPlaying="false" />'
                '</root>'
            ).encode()

        elif any(x in self.path for x in ['openTable.php', 'joinTable.php']):
            g['at_table']      = True
            g['msg_count']     = 0
            g['sequence_done'] = False
            g['in_game']       = False
            print(f'✓ {name} at table → push sequence starting')

        elif 'robotJoinTable.php' in self.path: pass
        elif 'startPlaying.php'   in self.path: pass

        elif 'getMessages.php' in self.path:
            return self._get_messages(uid).encode()

        elif 'sendGameMessage.php' in self.path:
            self._handle_player_message(p, uid)

        elif 'leaveTable.php' in self.path or 'leaveRoom.php' in self.path:
            self._reset(uid)
            print(f'✓ {name} left')

        elif 'gameEnded.php' in self.path:
            ranks = p.get('ranks', '?')
            try:
                r      = [int(x) for x in ranks.split(',')]
                winner = f'{name} 🎉' if r[0] == 0 else 'ROBOT 🤖'
                print(f'🏆 {winner} wins!  (ranks: player={r[0]} robot={r[1]})')
            except:
                print(f'🏆 gameEnded ranks={ranks}')
            self._reset(uid)

        elif 'getMyScore.php'       in self.path: resp = b'<root><score>0</score></root>'
        elif 'getTopTen.php'        in self.path: resp = b'<root></root>'
        elif 'updateStatistics.php' in self.path: pass
        elif 'sendChatMessage.php'  in self.path: pass
        elif 'invite.php'           in self.path: pass
        elif 'viewTable.php'        in self.path: pass
        else:
            print(f'  (unhandled: {self.path.split("?")[0]})')

        return resp

    def do_OPTIONS(self):
        self._hdrs()

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length).decode('utf-8')
        p      = {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}
        self._hdrs()
        self.wfile.write(self._handle_request(p))

    # ─────────────────────────────────────────────────────────────────────────
    # getMessages: lobby push sequence + in-game robot queue
    # ─────────────────────────────────────────────────────────────────────────

    def _get_messages(self, uid):
        g   = get_session(uid)

        # ── lobby sequence ────────────────────────────────────────────────────
        if not g['sequence_done']:
            if not g['at_table']:
                return ''
            g['msg_count'] += 1
            c = g['msg_count']

            if c == 1:
                print('  Push[1]: OPENTABLERESULT ✓')
                return '<OPENTABLERESULT success="true" tableUID="101" />'
            elif c == 2:
                print('  Push[2]: PLAYERJOINEDTABLE ✓')
                return f'<PLAYERJOINEDTABLE playerUID="{uid}" tableUID="101" />'
            elif c == 4:
                print('  Push[4]: ROBOTJOINTABLERESULT + ROBOTJOINEDTABLE ✓')
                return ('<ROBOTJOINTABLERESULT success="true" />'
                        '<ROBOTJOINEDTABLE tableUID="101" />')
            elif c == 6:
                g['sequence_done'] = True
                g['in_game']       = True
                g['cycle']        += 1
                g['game_phase']    = 'await_gameplay'
                # Robot must echo gamePlay on next poll
                g['robot_queue']   = [('sync', 'gamePlay')]
                print('=' * 60)
                print(f'🎮 Push[6]: PLAYINGSTARTED  [cycle {g["cycle"]}]')
                print('   Queued: robot → gamePlay')
                print('=' * 60)
                return ('<STARTPLAYINGRESULT success="true" />'
                        '<PLAYINGSTARTED tableUID="101" randomSeeds="123,456" />')
            return ''

        # ── in-game: dequeue one robot action per poll ────────────────────────
        if g['robot_queue']:
            entry = g['robot_queue'].pop(0)
            mode  = entry[0]   # 'sync' (§_-5W§) or 'async' (§_-Fh§)
            fn    = entry[1]
            args  = entry[2:]
            if mode == 'sync':
                # §_-5W§: must push playerIndex 0 AND 1 with same synchronizeString
                node = sync_push_pair(fn, *args)
                print(f'  🤖 [sync]  {fn}({", ".join(repr(a) for a in args)})')
            else:
                # §_-Fh§: single push, synchronizeString=None
                node = push_node(fn, *args, sync_string=None, player_index=1)
                print(f'  🤖 [async] {fn}({", ".join(repr(a) for a in args)})')
            return node

        return ''

    # ─────────────────────────────────────────────────────────────────────────
    # sendGameMessage: parse player action and queue robot response
    # ─────────────────────────────────────────────────────────────────────────

    def _handle_player_message(self, p, uid):
        g   = get_session(uid)
        raw = p.get('message', '')
        if not raw:
            return

        obj = deserialize(raw)
        if obj is None:
            print(f'  ⚠ deserialize failed  raw={raw[:80]}')
            return

        # The lobby wraps every message as:
        # { synchronizeString, playerIndex, message: {functionName, parameters} }
        # Unwrap the envelope to get to the real message object.
        if isinstance(obj, dict) and 'message' in obj:
            obj = obj['message']

        fn     = obj.get('functionName', '') if isinstance(obj, dict) else ''
        params = obj.get('parameters', []) if isinstance(obj, dict) else []

        if not fn:
            # §_-5W§ batched format: array of envelope objects — unwrap each
            if isinstance(obj, list):
                inner = obj[0] if obj else {}
                if isinstance(inner, dict) and 'message' in inner:
                    inner = inner['message']
                fn     = inner.get('functionName', '') if isinstance(inner, dict) else ''
                params = inner.get('parameters', []) if isinstance(inner, dict) else []
            if not fn:
                print(f'  ⚠ empty functionName  obj={str(obj)[:80]}')
                return

        print(f'  📤 Player → {fn}({", ".join(repr(a) for a in params)})')
        self._robot_respond(fn, params, uid)

    def _robot_respond(self, fn, params, uid):
        g = get_session(uid)

        # ── gamePlay ──────────────────────────────────────────────────────────
        if fn == 'gamePlay':
            if not g['robot_queue']:
                g['robot_queue'].append(('sync', 'gamePlay'))
            g['game_phase'] = 'await_team'

        elif fn == 'chooseTeamEnded':
            t0 = int(params[0]) if len(params) > 0 else 0
            t1 = int(params[1]) if len(params) > 1 else 1
            print(f'  ── Teams: player={t0} robot={t1}')
            if t0 == t1:
                print('  ── Same team! Robot queues homeJerseyIsChosen')
                # homeJerseyIsChosen is §_-Fh§ (async, no sync barrier)
                g['robot_queue'].append(('async', 'homeJerseyIsChosen'))
            else:
                print('  ── Different teams → penalty rounds start')
            g['game_phase'] = 'playing'

        elif fn in ('homeJerseyIsChosen', 'awayJerseyIsChosen'):
            print(f'  ── Jersey: {fn} → penalty rounds start')
            g['game_phase'] = 'playing'

        elif fn == 'startShoot':
            # robot's shoot turn — clear kick-pending flag (we can't distinguish goal vs save for player)
            g['awaiting_player_shot_result'] = False
            rx = random.uniform(250, 550)
            ry = random.uniform(80, 200)
            print(f'  ── Robot shooting → ({rx:.1f}, {ry:.1f})')
            g['robot_queue'].append(('async', 'opponentShooterShooted', rx, ry))

        elif fn == 'opponentShooterShooted':
            # player kicked — robot dives; result unknown until next round
            g['awaiting_player_shot_result'] = True
            rx = random.uniform(200, 600)
            ry = random.uniform(100, 220)
            dt = float(random.randint(150, 500))
            print(f'  ── Player kicked, robot dives ({rx:.1f}, {ry:.1f}) dt={dt:.0f}ms')
            g['robot_queue'].append(('async', 'opponentGoalkeeperJumped', rx, ry, dt))

        elif fn == 'opponentGoalkeeperJumped':
            print(f'  ── Player dived at ({params[0] if params else "?"}, ...)')

        # ── result notifications ──────────────────────────────────────────────
        elif fn == 'opponentGoal':
            # §_-Em§ phase only: robot scored against the player
            g['awaiting_player_shot_result'] = False
            g['robot_score'] += 1
            print(f'  ⚽ Robot scored! (robot {g["robot_score"]} confirmed goals)')
        elif fn == 'opponentMissed':
            # §_-Em§ phase only: robot missed / player saved
            g['awaiting_player_shot_result'] = False
            print(f'  ✋ Robot missed!')

        else:
            print(f'  ── Unknown from player: {fn}')

    # ─────────────────────────────────────────────────────────────────────────

    def do_GET(self):
        path = self.path.split('?')[0]
        qs   = urllib.parse.parse_qs(self.path.split('?')[1] if '?' in self.path else '')
        p    = {k: v[0] for k, v in qs.items()}

        # ── serve Penalty.swf ─────────────────────────────────────────────────
        if path == '/Penalty.swf':
            swf_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Penalty.swf')
            if not os.path.exists(swf_path):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'Penalty.swf not found')
                return
            with open(swf_path, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'application/x-shockwave-flash')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(data)
            return

        # ── crossdomain.xml ───────────────────────────────────────────────────
        if 'crossdomain.xml' in path:
            self._hdrs()
            self.wfile.write(
                b'<?xml version="1.0"?><cross-domain-policy>'
                b'<allow-access-from domain="*" /></cross-domain-policy>'
            )
            return

        # ── PHP endpoints via GET (Ruffle sends GET for some requests) ────────
        if any(x in path for x in ['.php']):
            self._hdrs()
            self.wfile.write(self._handle_request(p))
            return

        # ── landing page ──────────────────────────────────────────────────────
        host = self.headers.get('Host', 'localhost')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        html = f'''<!DOCTYPE html><html><head>
<meta charset="UTF-8"><title>⚽ Penalty Shootout</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#1a1a2e;color:#eee;font-family:monospace;
       display:flex;flex-direction:column;align-items:center;
       justify-content:center;min-height:100vh;gap:24px;padding:24px}}
  h1{{font-size:2.2rem;color:#00e676}}
  .box{{background:#0d0d1a;border:1px solid #333;border-radius:10px;
        padding:24px 28px;max-width:580px;width:100%;line-height:1.85}}
  .box h2{{font-size:1rem;color:#00e676;margin-bottom:12px;letter-spacing:.05em}}
  .badge{{display:inline-block;background:#00e676;color:#000;font-size:.7rem;
          padding:1px 6px;border-radius:3px;margin-left:6px;vertical-align:middle}}
  .url{{background:#000;color:#00e676;padding:10px 16px;border-radius:4px;
        font-size:.95rem;word-break:break-all;margin:10px 0;display:block;
        border:1px solid #00e676;user-select:all}}
  ol{{padding-left:1.4em;margin:8px 0}}
  li{{margin:6px 0}}
  a{{color:#69f0ae;text-decoration:none}}
  a:hover{{text-decoration:underline}}
  .dl{{display:flex;gap:10px;margin:8px 0;flex-wrap:wrap}}
  .dl a{{background:#1a1a2e;border:1px solid #00e676;color:#00e676;
         padding:6px 14px;border-radius:4px;font-size:.85rem}}
  .dl a:hover{{background:#00e676;color:#000;text-decoration:none}}
  .divider{{color:#333;text-align:center;font-size:.85rem;margin:4px 0}}
  .note{{color:#888;font-size:.8rem;margin-top:10px}}
</style></head><body>
<h1>⚽ Penalty Shootout</h1>

<div class="box">
  <h2>OPTION 1 · Flash Player Standalone <span class="badge">RECOMMENDED</span></h2>
  <ol>
    <li>Download Flash Player 32:
      <div class="dl">
        <a href="https://github.com/ntkernel/flash/releases/download/32.0.0.238/flashplayer_32_sa.exe">⬇ Windows (.exe)</a>
        <a href="https://github.com/ntkernel/flash/releases/download/32.0.0.238/flashplayer_32_sa.dmg">⬇ macOS (.dmg)</a>
      </div>
    </li>
    <li>Open Flash Player → <b>File → Open</b></li>
    <li>Paste this URL and press Enter:</li>
  </ol>
  <span class="url">http://{host}/Penalty.swf</span>
  <p class="note">No install needed — just run the .exe / .dmg directly</p>
</div>

<div class="box">
  <h2>OPTION 2 · Pale Moon Browser</h2>
  <ol>
    <li>Download <a href="https://www.palemoon.org/download.shtml" target="_blank">Pale Moon browser</a> and install it</li>
    <li>Enable Flash in Pale Moon:
      <ul style="padding-left:1.2em;margin-top:4px">
        <li>Menu → <b>Add-ons</b> → <b>Plugins</b></li>
        <li>Find <b>Shockwave Flash</b> → set to <b>Always Activate</b></li>
      </ul>
    </li>
    <li>Open this URL in Pale Moon:</li>
  </ol>
  <span class="url">http://{host}/Penalty.swf</span>
  <p class="note">Full guide: <a href="https://andkon.com/arcade/faq.php" target="_blank">andkon.com/arcade/faq.php</a></p>
</div>

</body></html>'''
        self.wfile.write(html.encode())

    def log_message(self, fmt, *args):
        print(f'  {self.address_string()} {fmt % args}', flush=True)


if __name__ == '__main__':
    print('=' * 60)
    print('⚽  PENALTY SHOOTOUT — PUBLIC SERVER')
    print(f'    http://0.0.0.0:{PORT}')
    print('=' * 60)
    ThreadingHTTPServer(('0.0.0.0', PORT), GameMockServer).serve_forever()
