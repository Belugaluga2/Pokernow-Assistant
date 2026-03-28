"""
PokerNow Ledger Server

Serves the frontend and processes PokerNow game data.
Uses Socket.IO for instant player stacks + mm=true log for buy-ins/cash-outs.

Usage: python server.py
Then open http://localhost:8000
"""

import http.server
import json
import re
import shutil
import subprocess
import threading
import time

import requests as req_lib
import socketio

import os

PORT = int(os.environ.get('PORT', 8000))
CURL = shutil.which('curl') or shutil.which('curl.exe') or 'curl'
DELAY = 0.5


def curl_fetch(url, retries=3):
    """Fetch a URL with retry/backoff for rate limits.
    Tries curl first (bypasses Cloudflare TLS fingerprinting), falls back to requests."""
    for attempt in range(retries):
        # Try curl if available
        try:
            result = subprocess.run(
                [CURL, '-s', '-w', '\n%{http_code}', '--max-time', '15', url],
                capture_output=True, timeout=20,
            )
            output = result.stdout.decode('utf-8', errors='replace')
            lines = output.rsplit('\n', 1)
            body = lines[0] if len(lines) > 1 else output
            status = int(lines[1]) if len(lines) > 1 else 0

            if status == 200:
                return json.loads(body)
            if status == 429 and attempt < retries - 1:
                wait = 2 ** (attempt + 1)
                print(f'  Rate limited (429). Waiting {wait}s...')
                time.sleep(wait)
                continue
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass  # curl not available, fall back to requests

        # Fallback: use requests library
        try:
            resp = req_lib.get(url, timeout=15, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            })
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429 and attempt < retries - 1:
                wait = 2 ** (attempt + 1)
                print(f'  Rate limited (429). Waiting {wait}s...')
                time.sleep(wait)
                continue
            if attempt < retries - 1:
                time.sleep(1)
                continue
            raise Exception(f'HTTP {resp.status_code} from PokerNow')
        except req_lib.RequestException as e:
            if attempt < retries - 1:
                time.sleep(1)
                continue
            raise Exception(f'Request failed: {e}')

    raise Exception('All retries exhausted')


def fetch_all_logs(game_id, money_only=False):
    """Fetch all log pages. money_only=True uses mm=true filter."""
    all_logs = []
    before_at = None
    page = 0

    while True:
        url = f'https://www.pokernow.com/games/{game_id}/log'
        params = []
        if money_only:
            params.append('mm=true')
        if before_at:
            params.append(f'before_at={before_at}')
        if params:
            url += '?' + '&'.join(params)

        data = curl_fetch(url)
        logs = data.get('logs', [])
        if not logs:
            break

        all_logs.extend(logs)
        before_at = logs[-1]['created_at']
        page += 1
        print(f'  [{"money" if money_only else "full"}] Page {page}: {len(logs)} entries')
        time.sleep(DELAY)

    all_logs.reverse()
    return all_logs


# ===== SOCKET.IO: Instant game state =====

def fetch_game_state(game_id, timeout=8):
    """Connect via Socket.IO to get current game state (player stacks) instantly."""
    # Get session cookies
    session = req_lib.Session()
    session.get(f'https://www.pokernow.com/games/{game_id}', timeout=10)
    cookies = session.cookies.get_dict()
    cookie_header = '; '.join(f'{k}={v}' for k, v in cookies.items())

    result = {'state': None, 'error': None}
    event = threading.Event()

    sio = socketio.Client(logger=False, engineio_logger=False)

    @sio.on('connect')
    def on_connect():
        sio.emit('action', {'type': 'RUP'})

    @sio.on('rup')
    def on_rup(data):
        if not result['state']:
            result['state'] = data
            event.set()

    @sio.on('registered')
    def on_registered(data):
        # registered event also has gameState with player stacks
        gs = data.get('gameState') if isinstance(data, dict) else None
        if gs and not result['state']:
            result['state'] = gs
            event.set()

    @sio.on('failed')
    def on_failed(data):
        result['error'] = data
        event.set()

    try:
        sio.connect(
            f'https://www.pokernow.com?gameID={game_id}',
            transports=['websocket'],
            headers={'Cookie': cookie_header},
            wait_timeout=timeout,
        )
        event.wait(timeout=timeout)
    except Exception as e:
        result['error'] = str(e)
    finally:
        try:
            sio.disconnect()
        except Exception:
            pass

    if result['error']:
        raise Exception(f'Socket.IO error: {result["error"]}')
    if not result['state']:
        raise Exception('Timed out waiting for game state')
    return result['state']


def extract_stacks_from_state(state):
    """Extract player stacks from the Socket.IO game state. Stacks are in cents."""
    stacks = {}
    players = state.get('players', {})
    for pid, info in players.items():
        stack_cents = info.get('stack', 0)
        stacks[pid] = round(stack_cents / 100, 2)
    return stacks


# ===== LEDGER COMPUTATION =====

def find_active_players(logs):
    """Quick scan of money logs to find player IDs still at the table."""
    active = set()
    for log in logs:
        msg = log['msg']
        m = re.search(r'The player "(.+?) @ (.+?)" (?:joined|re-joined)', msg)
        if m:
            active.add(m.group(2))
            continue
        m = re.search(r'The player "(.+?) @ (.+?)" quits', msg)
        if m:
            active.discard(m.group(2))
    return list(active)


def compute_ledger(logs, active_stacks):
    """Compute ledger from money-only log messages."""
    players = {}

    def get_player(name, pid):
        if pid not in players:
            players[pid] = {
                'name': name, 'id': pid,
                'buyins': [], 'cashouts': [],
                'lastStack': 0, 'isActive': False,
            }
        players[pid]['name'] = name
        return players[pid]

    for log in logs:
        msg = log['msg']

        m = re.search(r'The player "(.+?) @ (.+?)" (?:joined|re-joined) the game with a stack of (\d+\.?\d*)', msg)
        if m:
            p = get_player(m.group(1), m.group(2))
            p['isActive'] = True
            p['lastStack'] = float(m.group(3))
            continue

        m = re.search(r'The player "(.+?) @ (.+?)" quits the game with a stack of (\d+\.?\d*)', msg)
        if m:
            p = get_player(m.group(1), m.group(2))
            p['cashouts'].append(float(m.group(3)))
            p['isActive'] = False
            p['lastStack'] = 0
            continue

        m = re.search(r'The admin approved the player "(.+?) @ (.+?)" (?:participation|request|adding) .+?(\d+\.\d+)', msg)
        if m:
            p = get_player(m.group(1), m.group(2))
            p['buyins'].append(float(m.group(3)))
            continue

        m = re.search(r'The admin updated the player "(.+?) @ (.+?)" stack from (\d+\.?\d*) to (\d+\.?\d*)', msg)
        if m:
            p = get_player(m.group(1), m.group(2))
            diff = float(m.group(4)) - float(m.group(3))
            if diff > 0:
                p['buyins'].append(diff)
            elif diff < 0:
                p['cashouts'].append(abs(diff))
            continue

        m = re.search(r'"(.+?) @ (.+?)" stand up with the stack of (\d+\.?\d*)', msg)
        if m:
            p = get_player(m.group(1), m.group(2))
            p['lastStack'] = float(m.group(3))
            continue

    # Build results
    results = []
    uncertain_idx = []
    for pid, p in players.items():
        total_buyin = round(sum(p['buyins']), 2)
        if p['isActive']:
            if pid in active_stacks:
                stack = active_stacks[pid]
            else:
                stack = p['lastStack']
                uncertain_idx.append(len(results))
        else:
            stack = 0
        total_cashout = round(sum(p['cashouts']) + stack, 2)
        net = round(total_cashout - total_buyin, 2)
        if total_buyin == 0 and total_cashout == 0:
            continue
        results.append({
            'name': p['name'], 'id': pid,
            'buyin': total_buyin, 'cashout': total_cashout, 'net': net,
        })

    # Zero-sum correction for any remaining inaccuracy
    total_net = round(sum(p['net'] for p in results), 2)
    if abs(total_net) > 0.01 and len(uncertain_idx) == 1:
        idx = uncertain_idx[0]
        results[idx]['net'] = round(results[idx]['net'] - total_net, 2)
        results[idx]['cashout'] = round(results[idx]['cashout'] - total_net, 2)

    results.sort(key=lambda x: x['net'], reverse=True)

    # Settlements
    debtors = [{'name': p['name'], 'r': round(abs(p['net']), 2)} for p in results if p['net'] < -0.005]
    creditors = [{'name': p['name'], 'r': round(p['net'], 2)} for p in results if p['net'] > 0.005]
    debtors.sort(key=lambda x: x['r'], reverse=True)
    creditors.sort(key=lambda x: x['r'], reverse=True)

    settlements = []
    i, j = 0, 0
    while i < len(debtors) and j < len(creditors):
        amount = round(min(debtors[i]['r'], creditors[j]['r']), 2)
        if amount > 0.005:
            settlements.append({
                'from': debtors[i]['name'],
                'to': creditors[j]['name'],
                'amount': amount,
            })
        debtors[i]['r'] -= amount
        creditors[j]['r'] -= amount
        if debtors[i]['r'] < 0.01:
            i += 1
        if creditors[j]['r'] < 0.01:
            j += 1

    return {'players': results, 'settlements': settlements}


# ===== STATS =====

def compute_stats(logs):
    """Compute standard + bomb pot stats from log messages.

    Standard VPIP/PFR: only non-bomb-pot hands, preflop voluntary actions.
    Bomb Pot VPIP: hands that ARE bomb pots — VPIP = player put in additional
    money beyond the forced bomb pot ante (any action on flop/turn/river that
    isn't a check or fold)."""
    players = {}
    hands = []
    current_hand = None

    for log in logs:
        msg = log['msg']

        m = re.search(r'"(.+?) @ (.+?)"', msg)
        if m:
            players[m.group(2)] = m.group(1)

        if re.search(r'-- starting hand #(\d+)', msg):
            current_hand = {
                'players': [],
                'vpip_players': set(),
                'pfr_players': set(),
                'bp_vpip_players': set(),  # bomb pot VPIP (post-flop action)
                'preflop': True,
                'is_bomb_pot': False,
                'postflop': False,
            }
            hands.append(current_hand)
            continue

        if re.search(r'-- ending hand #\d+', msg):
            current_hand = None
            continue

        if not current_hand:
            continue

        if msg.startswith('Player stacks:'):
            for pm in re.finditer(r'"(.+?) @ (.+?)" \([\d.]+\)', msg):
                current_hand['players'].append(pm.group(2))
            continue

        # Detect bomb pot hands from the forced ante
        if 'bomb pot bet' in msg:
            current_hand['is_bomb_pot'] = True

        # Flop starts postflop
        if msg.startswith('Flop:') or msg.startswith('Flop ('):
            current_hand['preflop'] = False
            current_hand['postflop'] = True
            continue

        # --- Standard stats: preflop actions on NON-bomb-pot hands ---
        if current_hand['preflop'] and not current_hand['is_bomb_pot']:
            m = re.search(r'"(.+?) @ (.+?)" raises to', msg)
            if m:
                current_hand['vpip_players'].add(m.group(2))
                current_hand['pfr_players'].add(m.group(2))
                continue

            m = re.search(r'"(.+?) @ (.+?)" calls', msg)
            if m:
                current_hand['vpip_players'].add(m.group(2))
                continue

            m = re.search(r'"(.+?) @ (.+?)" bets', msg)
            if m:
                current_hand['vpip_players'].add(m.group(2))
                current_hand['pfr_players'].add(m.group(2))
                continue

        # --- Bomb pot stats: postflop voluntary money actions ---
        if current_hand['postflop'] and current_hand['is_bomb_pot']:
            # Skip bomb pot forced antes (they happen preflop)
            if 'bomb pot bet' in msg:
                continue

            m = re.search(r'"(.+?) @ (.+?)" (?:bets|raises to|calls)', msg)
            if m:
                current_hand['bp_vpip_players'].add(m.group(2))
                continue

    # Aggregate
    stats = {}
    for hand in hands:
        for pid in hand['players']:
            if pid not in stats:
                stats[pid] = {
                    'hands': 0, 'vpip': 0, 'pfr': 0,
                    'bp_hands': 0, 'bp_vpip': 0,
                }
            if hand['is_bomb_pot']:
                stats[pid]['bp_hands'] += 1
                if pid in hand['bp_vpip_players']:
                    stats[pid]['bp_vpip'] += 1
            else:
                stats[pid]['hands'] += 1
                if pid in hand['vpip_players']:
                    stats[pid]['vpip'] += 1
                if pid in hand['pfr_players']:
                    stats[pid]['pfr'] += 1

    result = []
    for pid, s in stats.items():
        total = s['hands'] + s['bp_hands']
        if total == 0:
            continue
        result.append({
            'name': players.get(pid, pid),
            'handsPlayed': s['hands'],
            'vpip': round(s['vpip'] / s['hands'] * 100, 1) if s['hands'] > 0 else 0,
            'pfr': round(s['pfr'] / s['hands'] * 100, 1) if s['hands'] > 0 else 0,
            'bpHandsPlayed': s['bp_hands'],
            'bpVpip': round(s['bp_vpip'] / s['bp_hands'] * 100, 1) if s['bp_hands'] > 0 else 0,
        })

    result.sort(key=lambda x: x['handsPlayed'] + x['bpHandsPlayed'], reverse=True)
    total_hands = sum(1 for h in hands if not h['is_bomb_pot'])
    total_bp = sum(1 for h in hands if h['is_bomb_pot'])
    return {'stats': result, 'totalHands': total_hands, 'totalBombPots': total_bp}


# ===== HTTP SERVER =====

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        # GET /api/ledger/{gameId}
        m = re.match(r'^/api/ledger/([a-zA-Z0-9_-]+)$', self.path)
        if m:
            game_id = m.group(1)
            try:
                # Step 1: Get current stacks instantly via Socket.IO
                print(f'  Connecting to game {game_id}...')
                try:
                    state = fetch_game_state(game_id)
                    active_stacks = extract_stacks_from_state(state)
                    print(f'  Got stacks for {len(active_stacks)} active players via Socket.IO')
                except Exception as e:
                    print(f'  Socket.IO failed ({e}), falling back to log search')
                    active_stacks = {}

                # Step 2: Fetch money messages for buy-ins/cash-outs
                print(f'  Fetching money messages...')
                logs = fetch_all_logs(game_id, money_only=True)

                # Step 3: Compute ledger
                ledger = compute_ledger(logs, active_stacks)
                self._json_response(200, ledger)
            except Exception as e:
                print(f'  ERROR: {e}')
                self._json_response(502, {'error': str(e)})
            return

        # GET /api/stats/{gameId}
        m = re.match(r'^/api/stats/([a-zA-Z0-9_-]+)$', self.path)
        if m:
            game_id = m.group(1)
            try:
                print(f'  Fetching stats for {game_id}...')
                logs = fetch_all_logs(game_id, money_only=False)
                stats = compute_stats(logs)
                self._json_response(200, stats)
            except Exception as e:
                print(f'  ERROR: {e}')
                self._json_response(502, {'error': str(e)})
            return

        super().do_GET()

    def do_POST(self):
        # POST /api/stats/csv — upload a PokerNow CSV log for instant stats
        if self.path == '/api/stats/csv':
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length).decode('utf-8', errors='replace')

                # Parse CSV: each row is entry,at,order
                import csv
                import io
                reader = csv.DictReader(io.StringIO(body))
                logs = []
                for row in reader:
                    entry = row.get('entry', '')
                    # CSV wraps quotes as "", unescape them
                    logs.append({'msg': entry})

                # Reverse to chronological order (CSV is newest-first)
                logs.reverse()

                print(f'  Parsed {len(logs)} entries from CSV upload')
                stats = compute_stats(logs)
                self._json_response(200, stats)
            except Exception as e:
                print(f'  ERROR parsing CSV: {e}')
                self._json_response(400, {'error': str(e)})
            return

        self.send_response(404)
        self.end_headers()

    def _json_response(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f'  {args[0]}')


if __name__ == '__main__':
    with http.server.HTTPServer(('0.0.0.0', PORT), Handler) as server:
        print(f'PokerNow Ledger running at http://localhost:{PORT}')
        print('Press Ctrl+C to stop.\n')
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print('\nStopped.')
