"""
Penalty Shootout — Public Server
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Real 2-player: shared tables + message relay between players.
Solo fallback: robot joins after ~3s if no opponent appears.

Bug fixes:
  ✓ Names work correctly (read from playerName POST param)
  ✓ Fresh session uid per player at joinRoom — no collision
  ✓ Tables are SHARED — two players play each other, not parallel robots
  ✓ Robot only joins when no human opponent is waiting
"""

import os, threading, random, struct
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse
import xml.etree.ElementTree as ET

PORT = int(os.environ.get('PORT', 8080))

# ══════════════════════════════════════════════════════════════════════════════
# §_-8d§ serializer (unchanged from working v21)
# ══════════════════════════════════════════════════════════════════════════════

def _escape(s):
    s = str(s).replace('&','&amp;').replace('"','&quot;')
    return s.replace('<','&lt;').replace('>','&gt;')

def serialize(val):
    if val is None:           return '<null />'
    if isinstance(val, bool): return '<b v="t"/>' if val else '<b v="f"/>'
    if isinstance(val, int):  return f'<i v="{val}"/>'
    if isinstance(val, float):
        b  = struct.pack('<d', val)
        lo = struct.unpack_from('<I', b, 0)[0]
        hi = struct.unpack_from('<I', b, 4)[0]
        return f'<n v="{hi:08x}{lo:08x}"/>' if hi else f'<n v="{lo:x}"/>'
    if isinstance(val, str):  return f'<s v="{_escape(val)}"/>'
    if isinstance(val, list):
        return '<a>' + ''.join(serialize(v) for v in val) + '</a>'
    if isinstance(val, dict):
        inner = ''.join('<k n="' + _escape(k) + '">' + serialize(v) + '</k>'
                        for k,v in val.items())
        return f'<o>{inner}</o>'
    return f'<s v="{_escape(str(val))}"/>'

def push_node(fn, *params, sync_string=None, player_index=1):
    env   = {'synchronizeString': sync_string, 'playerIndex': player_index,
             'message': {'functionName': fn, 'parameters': list(params)}}
    inner = serialize(env).replace('"', '&quot;')
    return f'<GAMEMESSAGERECEIVED message="{inner}" />'

def sync_push_pair(fn, *params):
    return (push_node(fn, *params, sync_string=fn, player_index=0) +
            push_node(fn, *params, sync_string=fn, player_index=1))

def _parse_node(el):
    t = el.tag
    if t == 's': return el.get('v','')
    if t == 'i': return int(el.get('v','0'))
    if t == 'n':
        h = el.get('v','0')
        lo,hi = (int(h[-8:],16), int(h[:-8],16)) if len(h)>8 else (int(h,16), 0)
        return struct.unpack('<d', struct.pack('<II',lo,hi))[0]
    if t == 'b': return el.get('v') == 't'
    if t == 'a': return [_parse_node(c) for c in el]
    if t == 'o':
        obj = {}
        for k in el:
            if k.tag == 'k':
                ch = list(k)
                obj[k.get('n','')] = _parse_node(ch[0]) if ch else None
        return obj
    return None

def deserialize(s):
    try:    return _parse_node(ET.fromstring(s.strip()))
    except: return None

# ══════════════════════════════════════════════════════════════════════════════
# Global state
# ══════════════════════════════════════════════════════════════════════════════

lock          = threading.Lock()
pending_names = {}   # swf_uid  → name  (bridges checkUser → joinRoom)
sessions      = {}   # uid      → {'name', 'table_tid'}
tables        = {}   # tid      → table dict

ROBOT_WAIT = 100     # polls before robot joins solo game (~20 sec — gives time for human to join)

SYNC_FNS  = {'gamePlay','chooseTeamEnded','startShoot',
             'homeJerseyIsChosen','awayJerseyIsChosen'}
ASYNC_FNS = {'opponentShooterShooted','opponentGoalkeeperJumped'}

# ── table helpers ──────────────────────────────────────────────────────────────

def new_table(tid, host_uid, host_name):
    return dict(
        tid=tid,
        host=host_uid,  host_name=host_name,
        guest=None,     guest_name=None,
        state='open',           # 'open' | 'playing'
        host_q=[], guest_q=[],  # per-player message queues
        wait_polls=0,           # getMessages polls waited without a guest
        robot_mode=False,
        robot_q=[], robot_score=0,
    )

def q_push(table, uid, node):
    if   uid == table['host']:  table['host_q'].append(node)
    elif uid == table['guest']: table['guest_q'].append(node)

def q_push_both(table, node):
    table['host_q'].append(node)
    if table['guest']: table['guest_q'].append(node)

def q_pop(table, uid):
    if uid == table['host']:
        out, table['host_q']  = ''.join(table['host_q']),  []
    else:
        out, table['guest_q'] = ''.join(table['guest_q']), []
    return out

def other_uid(table, uid):
    return table['guest'] if uid == table['host'] else table['host']

def find_open_table(exclude_uid):
    """Find a table that a new player can join.
    Priority 1: genuinely open (waiting for human opponent).
    Priority 2: robot_mode table where game just started (robot can be replaced).
    """
    robot_fallback = None
    for tid, t in tables.items():
        if t['host'] == exclude_uid or t['guest'] == exclude_uid:
            continue
        if t['state'] == 'open' and t['guest'] is None:
            return tid   # best case — open slot
        if t['robot_mode'] and t['guest'] is None:
            robot_fallback = tid  # robot game, human can take over
    return robot_fallback

def leave_table(uid):
    """Remove uid from their current table. Caller must hold lock."""
    sess = sessions.get(uid)
    if not sess: return
    tid = sess.get('table_tid')
    if not tid or tid not in tables:
        sess['table_tid'] = None
        return
    t = tables[tid]
    if t['host'] == uid:
        # Host left — free any guest's reference then delete table
        if t['guest']:
            g = sessions.get(t['guest'])
            if g: g['table_tid'] = None
        del tables[tid]
    else:
        # Guest left — reset table to open so host can get next opponent (or robot)
        t['guest'] = t['guest_name'] = None
        t['guest_q'] = []
        t['state']      = 'open'
        t['wait_polls'] = 0
        t['robot_mode'] = False
        t['robot_q']    = []
    sess['table_tid'] = None

# ══════════════════════════════════════════════════════════════════════════════
# HTTP Server
# ══════════════════════════════════════════════════════════════════════════════

class GameServer(BaseHTTPRequestHandler):

    def _hdrs(self, ct='text/xml'):
        self.send_response(200)
        self.send_header('Content-Type', ct)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
        self.end_headers()

    def _uid(self, p):
        return p.get('playerUID') or p.get('uid') or 'anon'

    # ── router ────────────────────────────────────────────────────────────────

    def _handle(self, p):
        path = self.path.split('?')[0]
        uid  = self._uid(p)
        if   'getGameInfo.php'     in path: return self._get_game_info(uid)
        elif 'checkUser.php'       in path: return self._check_user(uid, p)
        elif 'joinRoom.php'        in path: return self._join_room(uid, p)
        elif 'openTable.php'       in path: self._open_table(uid);     return b''
        elif 'joinTable.php'       in path: self._join_table(uid);     return b''
        elif 'getMessages.php'     in path: return self._get_messages(uid).encode()
        elif 'sendGameMessage.php' in path: self._game_message(uid,p); return b''
        elif 'leaveTable.php'      in path: self._leave(uid);          return b''
        elif 'leaveRoom.php'       in path: self._leave(uid);          return b''
        elif 'gameEnded.php'       in path: self._game_ended(uid,p);   return b''
        elif 'getMyScore.php'      in path: return b'<root><score>0</score></root>'
        elif 'getTopTen.php'       in path: return b'<root></root>'
        else: return b''

    # ── endpoints ─────────────────────────────────────────────────────────────

    def _get_game_info(self, uid):
        with lock:
            open_tid  = find_open_table(uid)
            table_uid = open_tid if open_tid else '101'
            host_uid  = tables[open_tid]['host'] if open_tid else ''
        return (
            '<?xml version="1.0" encoding="utf-8"?><root>'
            '<ROOMINFO roomID="1" roomName="Main Room" '
            'roomCapacity="100" noOfPlayers="' + str(len(sessions)) + '" />'
            '<TABLEINFO tableUID="' + table_uid + '" possibleNoOfPlayers="2" '
            'playerUIDs="' + host_uid + '" viewerUIDs="" isPlaying="false" />'
            '</root>'
        ).encode()

    def _check_user(self, uid, p):
        name = (p.get('username') or p.get('userName') or
                p.get('playerName') or 'Player')
        with lock:
            pending_names[uid]        = name   # by specific uid
            pending_names['__last__'] = name   # wildcard — survives uid mismatch
        print(f'checkUser -> "{name}"', flush=True)
        return (
            '<?xml version="1.0" encoding="utf-8"?><root>'
            '<valid>true</valid>'
            '<username>' + name + '</username>'
            '<gender>1</gender><email>x@x.com</email>'
            '</root>'
        ).encode()

    def _join_room(self, uid, p):
        name = (p.get('playerName') or p.get('username') or p.get('userName'))
        with lock:
            if not name:
                # Try exact uid first, then wildcard __last__
                name = (pending_names.pop(uid, None) or
                        pending_names.pop('__last__', None) or
                        'Player')
            else:
                pending_names.pop(uid, None)
                pending_names.pop('__last__', None)
            new_uid = str(random.randint(100000, 999999))
            sessions[new_uid] = {'name': name, 'table_tid': None}
        print(f'-> "{name}" uid={new_uid}', flush=True)
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<root success="true" playerUID="' + new_uid + '" '
            'playerName="' + name + '" playerGender="1" '
            'playerNameInput="" playerID="" extraKey="">'
            '<PLAYERINFO playerUID="' + new_uid + '" playerName="' + name + '" playerGender="1" />'
            '<TABLEINFO tableUID="101" possibleNoOfPlayers="2" '
            'playerUIDs="" viewerUIDs="" isPlaying="false" />'
            '</root>'
        ).encode()

    def _open_table(self, uid):
        with lock:
            sess = sessions.get(uid)
            if not sess: return
            leave_table(uid)
            tid = uid                               # table keyed by host uid
            tables[tid] = new_table(tid, uid, sess['name'])
            sess['table_tid'] = tid
            t = tables[tid]
            t['host_q'].append('<OPENTABLERESULT success="true" tableUID="' + tid + '" />')
            t['host_q'].append('<PLAYERJOINEDTABLE playerUID="' + uid + '" tableUID="' + tid + '" />')
        print(f'-> "{sess["name"]}" opened table — waiting for opponent', flush=True)

    def _join_table(self, uid):
        with lock:
            sess = sessions.get(uid)
            if not sess: return
            leave_table(uid)
            tid = find_open_table(uid)
            if tid and tid in tables:
                t = tables[tid]
                if t['robot_mode']:
                    # Human takes over from robot — reset robot state first
                    print(f'  Human "{sess["name"]}" kicking robot from table {tid}', flush=True)
                    t['robot_mode'] = False
                    t['robot_q']    = []
                    t['state']      = 'open'
                    # Clear any robot-pushed messages in host queue so game resets cleanly
                    t['host_q']     = []
                    # Now connect as normal 2-player game
                    self._connect_two_players(tid, uid, sess)
                else:
                    self._connect_two_players(tid, uid, sess)
            else:
                # No open table — solo game (robot joins after delay)
                print(f'  No table found for "{sess["name"]}" -> solo game', flush=True)
                tid = uid
                tables[tid] = new_table(tid, uid, sess['name'])
                sess['table_tid'] = tid
                t = tables[tid]
                t['host_q'].append('<OPENTABLERESULT success="true" tableUID="' + tid + '" />')
                t['host_q'].append('<PLAYERJOINEDTABLE playerUID="' + uid + '" tableUID="' + tid + '" />')

    def _connect_two_players(self, tid, guest_uid, guest_sess):
        """Wire up a 2-player game. Caller must hold lock."""
        t = tables[tid]
        t['guest']      = guest_uid
        t['guest_name'] = guest_sess['name']
        t['state']      = 'playing'
        guest_sess['table_tid'] = tid
        host_uid   = t['host']
        host_name  = t['host_name']
        guest_name = guest_sess['name']

        # Host: guest joined + game starts
        t['host_q'].append('<PLAYERJOINEDTABLE playerUID="' + guest_uid + '" tableUID="' + tid + '" />')
        t['host_q'].append('<STARTPLAYINGRESULT success="true" />')
        t['host_q'].append('<PLAYINGSTARTED tableUID="' + tid + '" randomSeeds="123,456" />')

        # Guest: see existing host + themselves + game starts
        t['guest_q'].append('<OPENTABLERESULT success="true" tableUID="' + tid + '" />')
        t['guest_q'].append('<PLAYERJOINEDTABLE playerUID="' + host_uid + '" tableUID="' + tid + '" />')
        t['guest_q'].append('<PLAYERJOINEDTABLE playerUID="' + guest_uid + '" tableUID="' + tid + '" />')
        t['guest_q'].append('<STARTPLAYINGRESULT success="true" />')
        t['guest_q'].append('<PLAYINGSTARTED tableUID="' + tid + '" randomSeeds="123,456" />')

        # First sync gamePlay for both players
        pair = sync_push_pair('gamePlay')
        t['host_q'].append(pair)
        t['guest_q'].append(pair)

        print(f'MULTIPLAYER: "{host_name}" vs "{guest_name}"', flush=True)

    def _get_messages(self, uid):
        with lock:
            sess = sessions.get(uid)
            if not sess: return ''
            tid = sess.get('table_tid')
            if not tid or tid not in tables: return ''
            t = tables[tid]

            # Solo: robot joins after ROBOT_WAIT polls
            if t['state'] == 'open' and not t['robot_mode']:
                t['wait_polls'] += 1
                if t['wait_polls'] >= ROBOT_WAIT:
                    t['robot_mode'] = True
                    t['state']      = 'playing'
                    t['host_q'].append('<ROBOTJOINTABLERESULT success="true" />')
                    t['host_q'].append('<ROBOTJOINEDTABLE tableUID="' + tid + '" />')
                    t['host_q'].append('<STARTPLAYINGRESULT success="true" />')
                    t['host_q'].append('<PLAYINGSTARTED tableUID="' + tid + '" randomSeeds="123,456" />')
                    t['robot_q'] = [('sync','gamePlay')]
                    print(f'ROBOT joined table {tid} (solo)', flush=True)

            # Robot AI: one action per poll
            if t['robot_mode'] and t['robot_q']:
                mode, fn = t['robot_q'][0][0], t['robot_q'][0][1]
                args     = t['robot_q'].pop(0)[2:]
                node     = sync_push_pair(fn,*args) if mode=='sync' else \
                           push_node(fn,*args,sync_string=None,player_index=1)
                t['host_q'].append(node)
                print(f'  [{mode}] {fn}', flush=True)

            return q_pop(t, uid)

    def _game_message(self, uid, p):
        raw = p.get('message','')
        if not raw: return
        obj = deserialize(raw)
        if obj is None: return
        # Unwrap envelope
        if isinstance(obj, dict) and 'message' in obj:
            obj = obj['message']
        fn     = obj.get('functionName','') if isinstance(obj,dict) else ''
        params = obj.get('parameters',[])   if isinstance(obj,dict) else []
        if not fn and isinstance(obj, list) and obj:
            inner = obj[0]
            if isinstance(inner,dict) and 'message' in inner: inner = inner['message']
            fn     = inner.get('functionName','') if isinstance(inner,dict) else ''
            params = inner.get('parameters',[])   if isinstance(inner,dict) else []
        if not fn: return

        with lock:
            sess = sessions.get(uid)
            if not sess: return
            tid = sess.get('table_tid')
            if not tid or tid not in tables: return
            t = tables[tid]
            if t['robot_mode']:
                self._robot_respond(t, fn, params)
            else:
                self._relay(t, uid, fn, params)
        print(f'  {sessions.get(uid,{}).get("name","?")} -> {fn}', flush=True)

    # ── 2-player message relay ─────────────────────────────────────────────────

    def _relay(self, t, uid, fn, params):
        other = other_uid(t, uid)
        sender_idx = 0 if uid == t['host'] else 1
        if fn in SYNC_FNS:
            # Both playerIndex 0+1 to BOTH players so sync barrier fires for each
            pair = sync_push_pair(fn, *params)
            q_push_both(t, pair)
        elif fn in ASYNC_FNS:
            # One-directional: relay to OTHER player with sender's index
            if other:
                node = push_node(fn, *params, sync_string=None, player_index=sender_idx)
                q_push(t, other, node)

    # ── robot AI ──────────────────────────────────────────────────────────────

    def _robot_respond(self, t, fn, params):
        if fn == 'gamePlay':
            if not t['robot_q']:
                t['robot_q'].append(('sync','gamePlay'))
        elif fn == 'chooseTeamEnded':
            t0 = int(params[0]) if params else 0
            t1 = int(params[1]) if len(params)>1 else 1
            if t0 == t1:
                t['robot_q'].append(('async','homeJerseyIsChosen'))
        elif fn == 'startShoot':
            t['robot_q'].append(('async','opponentShooterShooted',
                                  random.uniform(250,550), random.uniform(80,200)))
        elif fn == 'opponentShooterShooted':
            t['robot_q'].append(('async','opponentGoalkeeperJumped',
                                  random.uniform(200,600), random.uniform(100,220),
                                  float(random.randint(150,500))))
        elif fn == 'opponentGoal':
            t['robot_score'] = t.get('robot_score',0) + 1
            print(f'  Robot scored ({t["robot_score"]})', flush=True)
        elif fn == 'opponentMissed':
            print('  Robot missed', flush=True)

    # ── leave / end ───────────────────────────────────────────────────────────

    def _leave(self, uid):
        with lock:
            name = sessions.get(uid,{}).get('name','?')
            leave_table(uid)
            sessions.pop(uid, None)
        print(f'"{name}" left', flush=True)

    def _game_ended(self, uid, p):
        ranks = p.get('ranks','?')
        name  = sessions.get(uid,{}).get('name','Player')
        try:
            r = [int(x) for x in ranks.split(',')]
            print(f'WINNER: {"" + name if r[0]==0 else "Opponent/Robot"}', flush=True)
        except: pass
        with lock: leave_table(uid)

    # ── HTTP ─────────────────────────────────────────────────────────────────

    def do_OPTIONS(self): self._hdrs()

    def do_POST(self):
        n    = int(self.headers.get('Content-Length',0))
        body = self.rfile.read(n).decode('utf-8','replace')
        p    = {k: v[0] for k,v in urllib.parse.parse_qs(body).items()}
        self._hdrs()
        self.wfile.write(self._handle(p))

    def do_GET(self):
        path = self.path.split('?')[0]
        qs   = urllib.parse.parse_qs(self.path.split('?')[1] if '?' in self.path else '')
        p    = {k: v[0] for k,v in qs.items()}

        if path == '/Penalty.swf':
            swf = os.path.join(os.path.dirname(os.path.abspath(__file__)),'Penalty.swf')
            if not os.path.exists(swf):
                self.send_response(404); self.end_headers()
                self.wfile.write(b'Penalty.swf not found'); return
            with open(swf,'rb') as f: data = f.read()
            self.send_response(200)
            self.send_header('Content-Type','application/x-shockwave-flash')
            self.send_header('Content-Length',str(len(data)))
            self.send_header('Access-Control-Allow-Origin','*')
            self.end_headers(); self.wfile.write(data); return

        if 'crossdomain.xml' in path:
            self._hdrs()
            self.wfile.write(b'<?xml version="1.0"?><cross-domain-policy>'
                             b'<allow-access-from domain="*"/></cross-domain-policy>'); return

        if '.php' in path:
            self._hdrs(); self.wfile.write(self._handle(p)); return

        # Landing page
        host = self.headers.get('Host','localhost')
        self.send_response(200)
        self.send_header('Content-Type','text/html; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin','*')
        self.end_headers()
        page = LANDING.replace('__HOST__', host)
        self.wfile.write(page.encode())

    def log_message(self, fmt, *args):
        print(f'  {self.address_string()} {fmt%args}', flush=True)


LANDING = """<!DOCTYPE html><html><head>
<meta charset="UTF-8"><title>Penalty Shootout</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#1a1a2e;color:#eee;font-family:monospace;
       display:flex;flex-direction:column;align-items:center;
       justify-content:center;min-height:100vh;gap:24px;padding:24px}
  h1{font-size:2.2rem;color:#00e676}
  .box{background:#0d0d1a;border:1px solid #333;border-radius:10px;
       padding:24px 28px;max-width:580px;width:100%;line-height:1.85}
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
</style></head><body>
<h1>&#9917; Penalty Shootout</h1>
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
  <p class="note">No install needed &mdash; just run the .exe / .dmg directly</p>
</div>
<div class="box">
  <h2>OPTION 2 &middot; Pale Moon Browser</h2>
  <ol>
    <li>Download <a href="https://www.palemoon.org/download.shtml" target="_blank">Pale Moon browser</a> and install it</li>
    <li>Enable Flash: Menu &rarr; <b>Add-ons</b> &rarr; <b>Plugins</b> &rarr; Shockwave Flash &rarr; <b>Always Activate</b></li>
    <li>Open this URL in Pale Moon:</li>
  </ol>
  <span class="url">http://__HOST__/Penalty.swf</span>
  <p class="note">Full guide: <a href="https://andkon.com/arcade/faq.php" target="_blank">andkon.com/arcade/faq.php</a></p>
</div>
</body></html>"""


if __name__ == '__main__':
    print('='*60, flush=True)
    print('PENALTY SHOOTOUT — PUBLIC SERVER', flush=True)
    print(f'http://0.0.0.0:{PORT}', flush=True)
    print('Solo vs robot  |  Real 2-player relay', flush=True)
    print('='*60, flush=True)
    ThreadingHTTPServer(('0.0.0.0', PORT), GameServer).serve_forever()
