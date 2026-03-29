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

from csv_parser import parse_hand_data
from stats_engine import (
    compute_all_stats, compute_winnings, compute_allin_ev,
    compute_equity, has_eval7,
)

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
                wait = 2 ** attempt
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
                wait = 2 ** attempt
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


def fetch_all_logs(game_id, money_only=False, max_pages=300):
    """Fetch all log pages. money_only=True uses mm=true filter."""
    all_logs = []
    before_at = None
    prev_before_at = None
    page = 0

    while page < max_pages:
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
        prev_before_at = before_at
        before_at = logs[-1]['created_at']
        page += 1
        print(f'  [{"money" if money_only else "full"}] Page {page}: {len(logs)} entries (total: {len(all_logs)})')

        # Detect stuck pagination
        if before_at == prev_before_at:
            print(f'  Pagination cursor unchanged, stopping.')
            break

        if DELAY > 0:
            time.sleep(DELAY)

    if page >= max_pages:
        print(f'  Hit page limit ({max_pages}), returning partial data.')

    all_logs.reverse()
    return all_logs


# ===== SOCKET.IO: Instant game state =====

def fetch_game_state(game_id, timeout=4):
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


# ===== HTTP SERVER =====

def try_fetch_hand_json(game_id):
    """Try to fetch all hand data as JSON in a single request via PokerNow Plus API.
    Works for Plus/Platinum subscribers. Returns list of hand dicts, or None."""
    try:
        # Get session cookies from the game page
        session = req_lib.Session()
        session.get(f'https://www.pokernow.com/games/{game_id}', timeout=10)
        cookies = session.cookies.get_dict()

        # Hit the Plus API for bulk hand data
        url = f'https://plus.pokernow.com/api/game/{game_id}/hands'
        resp = session.get(url, timeout=15, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        })
        if resp.status_code == 200:
            data = resp.json()
            hands = data.get('hands', [])
            if hands:
                print(f'  Plus API: got {len(hands)} hands in one request!')
                return hands
    except Exception as e:
        print(f'  Plus API failed: {e}')
    return None


def _compute_stats_from_hands(hands):
    """Compute all stats from parsed hand list (without EV — that's on-demand)."""
    result = compute_all_stats(hands)
    result['winnings'] = compute_winnings(hands)
    result['ev'] = {'available': has_eval7(), 'computed': False}
    return result


def _compute_stats_from_logs(logs):
    """Convert API logs to CSV format, parse hands, compute all stats."""
    csv_lines = ['entry,at,order']
    for log in reversed(logs):
        entry = log.get('msg', '').replace('"', '""')
        csv_lines.append(f'"{entry}",{log.get("created_at","")},0')
    csv_text = '\n'.join(csv_lines)
    hands, _ = parse_hand_data(csv_text)
    return _compute_stats_from_hands(hands)


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        # GET /api/ledger/{gameId}
        m = re.match(r'^/api/ledger/([a-zA-Z0-9_-]+)$', self.path)
        if m:
            game_id = m.group(1)
            try:
                print(f'  Fetching ledger for {game_id} (parallel)...')

                # Run Socket.IO and log fetch in parallel
                stacks_result = {'stacks': {}, 'error': None}
                logs_result = {'logs': None, 'error': None}

                def _fetch_stacks():
                    try:
                        state = fetch_game_state(game_id)
                        stacks_result['stacks'] = extract_stacks_from_state(state)
                        print(f'  Got stacks for {len(stacks_result["stacks"])} active players via Socket.IO')
                    except Exception as e:
                        stacks_result['error'] = str(e)
                        print(f'  Socket.IO failed ({e}), will use log stacks')

                def _fetch_logs():
                    try:
                        logs_result['logs'] = fetch_all_logs(game_id, money_only=True)
                    except Exception as e:
                        logs_result['error'] = str(e)

                t1 = threading.Thread(target=_fetch_stacks)
                t2 = threading.Thread(target=_fetch_logs)
                t1.start()
                t2.start()
                t1.join()
                t2.join()

                if logs_result['error']:
                    raise Exception(logs_result['error'])

                ledger = compute_ledger(logs_result['logs'], stacks_result['stacks'])
                self._json_response(200, ledger)
            except Exception as e:
                print(f'  ERROR: {e}')
                self._json_response(502, {'error': str(e)})
            return

        # GET /api/stats/{gameId} — live game stats via Plus API
        m = re.match(r'^/api/stats/([a-zA-Z0-9_-]+)$', self.path)
        if m:
            game_id = m.group(1)
            try:
                print(f'  Fetching stats for {game_id}...')
                hands = try_fetch_hand_json(game_id)
                if hands:
                    stats = _compute_stats_from_hands(hands)
                    self._json_response(200, stats)
                else:
                    self._json_response(400, {
                        'error': 'upload_required',
                        'message': 'Bulk hand data requires PokerNow Plus. Download your CSV/JSON from PokerNow and upload it below.',
                    })
            except Exception as e:
                print(f'  ERROR: {e}')
                self._json_response(502, {'error': str(e)})
            return

        super().do_GET()

    def do_POST(self):
        # POST /api/stats/upload or /api/stats/csv — upload CSV or JSON for stats
        if self.path in ('/api/stats/upload', '/api/stats/csv'):
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length).decode('utf-8', errors='replace')

                hands, fmt = parse_hand_data(body)
                print(f'  Parsed {len(hands)} hands from {fmt} upload')

                if not hands:
                    self._json_response(400, {'error': 'No hands found in uploaded data'})
                    return

                result = compute_all_stats(hands)
                result['winnings'] = compute_winnings(hands)
                result['ev'] = {'available': has_eval7(), 'computed': False}
                result['format'] = fmt
                self._json_response(200, result)
            except Exception as e:
                print(f'  ERROR parsing upload: {e}')
                self._json_response(400, {'error': str(e)})
            return

        # POST /api/stats/ev — on-demand all-in EV computation
        if self.path == '/api/stats/ev':
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length).decode('utf-8', errors='replace')

                hands, fmt = parse_hand_data(body)
                print(f'  Computing EV for {len(hands)} hands from {fmt} upload...')

                if not hands:
                    self._json_response(400, {'error': 'No hands found'})
                    return

                if not has_eval7():
                    self._json_response(400, {'error': 'eval7 not available on server'})
                    return

                ev = compute_allin_ev(hands)
                self._json_response(200, ev)
            except Exception as e:
                print(f'  ERROR computing EV: {e}')
                import traceback; traceback.print_exc()
                self._json_response(400, {'error': str(e)})
            return

        # POST /api/equity — equity calculator
        if self.path == '/api/equity':
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length).decode('utf-8'))
                player_hands = body.get('hands', [])
                board = body.get('board', [])
                trials = min(body.get('trials', 100000), 200000)

                if len(player_hands) < 2:
                    self._json_response(400, {'error': 'Need at least 2 hands'})
                    return

                result = compute_equity(player_hands, board, trials)
                self._json_response(200, result)
            except Exception as e:
                print(f'  ERROR equity: {e}')
                self._json_response(400, {'error': str(e)})
            return

        self.send_response(404)
        self.end_headers()

    def do_OPTIONS(self):
        """Handle CORS preflight requests."""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
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


class ThreadedServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


if __name__ == '__main__':
    with ThreadedServer(('0.0.0.0', PORT), Handler) as server:
        print(f'PokerNow Assistant running at http://localhost:{PORT}')
        print(f'  eval7 available: {has_eval7()}')
        print('Press Ctrl+C to stop.\n')
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print('\nStopped.')
