"""
PokerNow Ledger Server

Serves the frontend and processes PokerNow game data.
Uses Socket.IO for instant player stacks + mm=true log for buy-ins/cash-outs.

Usage: python server.py
Then open http://localhost:8000
"""

import http.server
import json
import random
import re
import shutil
import subprocess
import threading
import time

import csv
import io
import requests as req_lib
import socketio

import os
from concurrent.futures import ProcessPoolExecutor

try:
    from curl_cffi import requests as cf_requests  # Chrome TLS/HTTP2 impersonation
except ImportError:  # pragma: no cover
    cf_requests = None

from csv_parser import parse_hand_data
from stats_engine import (
    compute_all_stats, compute_winnings, compute_allin_ev,
    compute_equity, compute_equity_double_board, compute_river_outs, compute_hand_history, compute_biggest_pots, has_eval7,
    extract_hand,
    prebuild_eval5_cache,
)
from equity_categories import list_valid_categories, generate_hands

# Worker pool for CPU-bound MC equity calls. Initialized in __main__ so Windows spawn
# semantics work; falls back to in-process when imported (tests, REPL).
_MC_POOL = None


def _run_mc(fn, *args, **kwargs):
    if _MC_POOL is None:
        return fn(*args, **kwargs)
    return _MC_POOL.submit(fn, *args, **kwargs).result()


def _worker_init():
    """Runs once per pool worker on spawn. Optionally pre-fills the _eval5 cache."""
    if os.environ.get('MC_PREBUILD') == '1':
        prebuild_eval5_cache()

PORT = int(os.environ.get('PORT', 8000))
CURL = shutil.which('curl') or shutil.which('curl.exe') or 'curl'
DELAY = 0.5
BROWSER_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')
GAME_URL = 'https://www.pokernow.com/games/{game_id}'
LEDGER_URL = 'https://www.pokernow.com/games/{game_id}/players_sessions'


# ===== HTTP TO POKERNOW =====
#
# PokerNow sits behind Cloudflare, which challenges datacenter IPs (e.g. Render).
# From a residential IP everything passes, even with a python-requests UA, so
# server-side impersonation is best-effort only. Every failure is classified so
# the frontend can offer the manual import fallback instead of a raw "HTTP 403".

class PokerNowBlocked(Exception):
    """Raised when PokerNow refuses to serve data.
    kind: 'cloudflare' | 'rate_limited' | 'http' | 'bad_json' | 'network'"""

    def __init__(self, kind, message, status=None):
        super().__init__(message)
        self.kind = kind
        self.status = status


_CF_BODY_TOKENS = ('Just a moment', 'challenge-platform', 'cf-chl', 'cf_chl_opt', '_cf_chl_tk')


def _is_cloudflare_challenge(status, headers, body):
    """True when the response is a Cloudflare interstitial rather than PokerNow's own answer."""
    if status not in (403, 503):
        return False
    lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    if 'challenge' in lowered.get('cf-mitigated', '').lower():
        return True
    head = (body or '')[:4000]
    return any(tok in head for tok in _CF_BODY_TOKENS)


class PokerNowSession:
    """One HTTP session per API request, with a shared cookie jar.

    get() tries curl_cffi (Chrome TLS impersonation) -> curl subprocess -> requests.
    A layer only falls through on a *transport* failure; HTTP error statuses are
    returned as-is so fetch_json() can classify them (previously a 403 from curl
    silently fell into a requests retry loop)."""

    def __init__(self):
        self._cf = None
        if cf_requests is not None:
            try:
                self._cf = cf_requests.Session(impersonate='chrome')
            except Exception as e:  # pragma: no cover
                print(f'  curl_cffi unavailable ({e}); using curl/requests')
        self._req = req_lib.Session()
        self._req.headers['User-Agent'] = BROWSER_UA
        self._cookies = {}

    def _remember(self, cookies):
        try:
            self._cookies.update({str(k): str(v) for k, v in dict(cookies).items()})
        except Exception:
            pass

    def cookie_header(self):
        return '; '.join(f'{k}={v}' for k, v in self._cookies.items())

    def get(self, url, timeout=15):
        """Returns (status, headers, text). Raises PokerNowBlocked('network') if every layer fails."""
        errors = []
        if self._cf is not None:
            try:
                resp = self._cf.get(url, timeout=timeout)
                self._remember(resp.cookies)
                return resp.status_code, dict(resp.headers), resp.text
            except Exception as e:
                errors.append(f'curl_cffi: {e}')
        try:
            return self._curl_get(url, timeout)
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired, ValueError) as e:
            errors.append(f'curl: {e}')
        try:
            resp = self._req.get(url, timeout=timeout)
            self._remember(resp.cookies.get_dict())
            return resp.status_code, dict(resp.headers), resp.text
        except req_lib.RequestException as e:
            errors.append(f'requests: {e}')
        raise PokerNowBlocked('network', 'Request failed: ' + '; '.join(errors))

    def _curl_get(self, url, timeout):
        cmd = [CURL, '-s', '-D', '-', '-A', BROWSER_UA, '--max-time', str(timeout), '-w', '\n%{http_code}']
        if self._cookies:
            cmd += ['-b', self.cookie_header()]
        cmd.append(url)
        result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
        output = result.stdout.decode('utf-8', errors='replace')
        payload, _, code = output.rpartition('\n')
        status = int(code.strip() or 0)
        if status == 0:
            raise ValueError(f'curl exit {result.returncode}')
        head, sep, body = payload.partition('\r\n\r\n')
        if not sep:
            head, sep, body = payload.partition('\n\n')
        headers = {}
        for line in head.splitlines()[1:]:
            if ':' not in line:
                continue
            k, v = line.split(':', 1)
            k, v = k.strip().lower(), v.strip()
            headers[k] = v
            if k == 'set-cookie' and '=' in v:
                name, val = v.split(';', 1)[0].split('=', 1)
                self._cookies[name.strip()] = val.strip()
        return status, headers, body


def fetch_json(session, url, retries=4):
    """GET a PokerNow JSON endpoint with 429 backoff (2s, 4s, 8s) and Cloudflare detection.
    Content-Type is ignored on purpose: players_sessions says text/html but is JSON."""
    if os.environ.get('PN_SIMULATE_CLOUDFLARE') == '1':
        raise PokerNowBlocked('cloudflare', 'Simulated Cloudflare challenge (PN_SIMULATE_CLOUDFLARE=1)', 403)
    for attempt in range(retries):
        try:
            status, headers, body = session.get(url)
        except PokerNowBlocked:
            if attempt < retries - 1:
                time.sleep(1)
                continue
            raise
        if _is_cloudflare_challenge(status, headers, body):
            raise PokerNowBlocked('cloudflare', f'PokerNow (Cloudflare) blocked the request (HTTP {status})', status)
        if status == 429:
            if attempt < retries - 1:
                wait = 2 ** (attempt + 1)
                print(f'  Rate limited (429). Waiting {wait}s...')
                time.sleep(wait)
                continue
            raise PokerNowBlocked('rate_limited', 'PokerNow rate limit (429) — wait a minute and try again', 429)
        if status != 200:
            raise PokerNowBlocked('http', f'HTTP {status} from PokerNow', status)
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise PokerNowBlocked('bad_json', 'PokerNow returned non-JSON (probably a block page)', status)
    raise PokerNowBlocked('rate_limited', 'All retries exhausted', 429)


def fetch_all_logs(game_id, money_only=False, max_pages=300, session=None):
    """Fetch all log pages. money_only=True uses mm=true filter."""
    session = session or PokerNowSession()
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

        data = fetch_json(session, url)
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

def fetch_game_state(game_id, timeout=4, cookie_header=None):
    """Connect via Socket.IO to get current game state (player stacks) instantly."""
    if cookie_header is None:
        # Get session cookies from the game page
        session = PokerNowSession()
        session.get(GAME_URL.format(game_id=game_id), timeout=10)
        cookie_header = session.cookie_header()

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
    """Extract player stacks from the Socket.IO game state. Stacks are in cents.
    Includes nit game escrow so the ledger sums to zero."""
    stacks = {}
    players = state.get('players', {})
    nit_escrow = state.get('nit', {}).get('escrowPerPlayer', {})
    for pid, info in players.items():
        stack_cents = info.get('stack', 0) + nit_escrow.get(pid, 0)
        stacks[pid] = round(stack_cents / 100, 2)
    return stacks


# ===== LEDGER COMPUTATION =====


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

    _NUM = r'(\d+(?:\.\d+)?)'  # matches "100.00" or "7000" but not trailing period

    def _dollars(raw):
        """Parse a value that may be dollars (with decimal) or cents (without)."""
        val = float(raw)
        if '.' not in raw:
            val /= 100
        return val

    for log in logs:
        msg = log['msg']

        m = re.search(r'The player "(.+?) @ (.+?)" (?:joined|re-joined) the game with a stack of ' + _NUM, msg)
        if m:
            p = get_player(m.group(1), m.group(2))
            p['isActive'] = True
            p['lastStack'] = _dollars(m.group(3))
            continue

        m = re.search(r'The player "(.+?) @ (.+?)" quits the game with a stack of ' + _NUM, msg)
        if m:
            p = get_player(m.group(1), m.group(2))
            p['cashouts'].append(_dollars(m.group(3)))
            p['isActive'] = False
            p['lastStack'] = 0
            continue

        m = re.search(r'The admin approved the player "(.+?) @ (.+?)" (?:participation|request|adding) .+?' + _NUM, msg)
        if m:
            p = get_player(m.group(1), m.group(2))
            p['buyins'].append(_dollars(m.group(3)))
            continue

        m = re.search(r'The admin updated the player "(.+?) @ (.+?)" stack from ' + _NUM + r' to ' + _NUM, msg)
        if m:
            p = get_player(m.group(1), m.group(2))
            diff = _dollars(m.group(4)) - _dollars(m.group(3))
            if diff > 0:
                p['buyins'].append(diff)
            elif diff < 0:
                p['cashouts'].append(abs(diff))
            continue

        m = re.search(r'"(.+?) @ (.+?)" stand up with the stack of ' + _NUM, msg)
        if m:
            p = get_player(m.group(1), m.group(2))
            p['lastStack'] = _dollars(m.group(3))
            continue

    # Build results
    results = []
    uncertain_idx = []
    for pid, p in players.items():
        total_buyin = round(sum(p['buyins']), 2)
        is_active = p['isActive']
        uncertain = False
        if is_active:
            if pid in active_stacks:
                stack = active_stacks[pid]
            else:
                stack = p['lastStack']
                uncertain = True
        else:
            stack = 0
        total_cashout = round(sum(p['cashouts']) + stack, 2)
        net = round(total_cashout - total_buyin, 2)
        if total_buyin == 0 and total_cashout == 0:
            continue
        if uncertain:
            uncertain_idx.append(len(results))
        results.append({
            'name': p['name'], 'id': pid,
            'buyin': total_buyin, 'cashout': total_cashout, 'net': net,
        })

    # Zero-sum correction: if exactly one active player's stack is uncertain,
    # derive it from the constraint that all nets must sum to 0
    total_net = round(sum(p['net'] for p in results), 2)
    if abs(total_net) > 0.01 and len(uncertain_idx) == 1:
        idx = uncertain_idx[0]
        results[idx]['net'] = round(results[idx]['net'] - total_net, 2)
        results[idx]['cashout'] = round(results[idx]['cashout'] - total_net, 2)

    results.sort(key=lambda x: x['net'], reverse=True)
    return {'players': results, 'settlements': compute_settlements(results)}


def compute_settlements(results):
    """Greedy debtor/creditor matching over [{'name', 'net', ...}] -> [{'from', 'to', 'amount'}].
    Largest debtor pays largest creditor first, minimizing the number of transfers."""
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

    return settlements


# ===== LEDGER FROM players_sessions (primary source) =====

def fetch_players_sessions(game_id, session):
    """PokerNow's own ledger endpoint: one request, no pagination, no Socket.IO.
    Shape: {"buyInTotal", "buyOutTotal", "inGameTotal", "nitEscrowTotal", "gameHasRake",
            "playersInfos": {pid: {"names": [...], "id", "buyInSum", "buyOutSum", "inGame", "nitEscrow", "net"}}}"""
    return fetch_json(session, LEDGER_URL.format(game_id=game_id))


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def ledger_from_players_sessions(data):
    """Convert a players_sessions payload into the ledger shape the frontend renders.
    Amounts are used as-is — the same units PokerNow's Ledger view shows. There is no
    zero-sum correction: mid-hand, chips in the pot legitimately make nets sum to nonzero."""
    infos = data.get('playersInfos') if isinstance(data, dict) else None
    if not isinstance(infos, dict):
        raise ValueError('Not a players_sessions payload (missing "playersInfos")')

    results = []
    for pid, info in infos.items():
        if not isinstance(info, dict):
            continue
        names = [n for n in (info.get('names') or []) if n]
        name = names[-1] if names else str(pid)  # last entry = most recent nickname
        buyin = round(_f(info.get('buyInSum')), 2)
        cashout = round(_f(info.get('buyOutSum')) + _f(info.get('inGame')) + _f(info.get('nitEscrow')), 2)
        if buyin == 0 and cashout == 0:
            continue
        results.append({
            'name': name, 'id': info.get('id') or pid,
            'buyin': buyin, 'cashout': cashout, 'net': round(cashout - buyin, 2),
        })

    results.sort(key=lambda x: x['net'], reverse=True)
    return {
        'players': results,
        'settlements': compute_settlements(results),
        'totals': {
            'buyIn': data.get('buyInTotal'),
            'buyOut': data.get('buyOutTotal'),
            'inGame': data.get('inGameTotal'),
        },
    }


# ===== MANUAL LEDGER IMPORT (pasted players_sessions JSON or downloaded ledger CSV) =====

_CSV_COLS = {
    'name':   ('player_nickname', 'nickname', 'player_name', 'player', 'name'),
    'id':     ('player_id', 'id'),
    'buyin':  ('buy_in', 'buyin', 'buy_ins', 'buyins'),
    'buyout': ('buy_out', 'buyout', 'buy_outs', 'cash_out', 'cashout'),
    'stack':  ('stack', 'in_game', 'ingame', 'current_stack'),
    'net':    ('net', 'profit', 'net_profit'),
}


def _norm_header(h):
    return re.sub(r'[^a-z0-9]+', '_', (h or '').strip().lstrip('\ufeff').lower()).strip('_')


def _num(s):
    s = str(s if s is not None else '').strip().replace('$', '').replace(',', '')
    if not s or s in ('-', 'null', 'None'):
        return 0.0
    return float(s)


def parse_ledger_csv(text):
    """Parse PokerNow's "Download Ledger" CSV (one row per player session) into the ledger shape.
    Header-driven and tolerant of column order/extra columns; sessions are summed per player."""
    reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
    headers = {_norm_header(h): h for h in (reader.fieldnames or [])}
    col = {key: next((headers[c] for c in cands if c in headers), None) for key, cands in _CSV_COLS.items()}
    if not col['name'] or not (col['buyin'] or col['net']):
        raise ValueError('CSV is missing player / buy_in columns — expected PokerNow\'s "Download Ledger" CSV')

    agg = {}
    for row in reader:
        name = (row.get(col['name']) or '').strip()
        pid = (row.get(col['id']) or '').strip() if col['id'] else ''
        key = pid or name
        if not key:
            continue
        p = agg.setdefault(key, {'name': name or key, 'id': key, 'buyin': 0.0, 'buyout': 0.0, 'stack': 0.0, 'net': 0.0})
        if name:
            p['name'] = name
        for k in ('buyin', 'buyout', 'stack', 'net'):
            if col[k]:
                p[k] += _num(row.get(col[k]))

    results = []
    for p in agg.values():
        buyin = round(p['buyin'], 2)
        if col['buyout'] or col['stack']:
            cashout = round(p['buyout'] + p['stack'], 2)
        else:
            cashout = round(buyin + p['net'], 2)  # net-only export
        if buyin == 0 and cashout == 0:
            continue
        results.append({'name': p['name'], 'id': p['id'], 'buyin': buyin, 'cashout': cashout,
                        'net': round(cashout - buyin, 2)})
    if not results:
        raise ValueError('No player rows found in the ledger CSV')

    results.sort(key=lambda x: x['net'], reverse=True)
    return {'players': results, 'settlements': compute_settlements(results)}


def parse_ledger_import(text):
    """Auto-detect a pasted players_sessions JSON or a ledger CSV. Returns (ledger, fmt)."""
    s = (text or '').strip().lstrip('\ufeff')
    if not s:
        raise ValueError('Nothing to import')
    if s.startswith('{'):
        try:
            data = json.loads(s)
        except json.JSONDecodeError as e:
            raise ValueError(f'Invalid JSON: {e}')
        return ledger_from_players_sessions(data), 'json'
    if ',' in s.splitlines()[0]:
        return parse_ledger_csv(s), 'csv'
    raise ValueError('Paste the players_sessions JSON or drop the downloaded ledger CSV')


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
    result['handHistory'] = compute_hand_history(hands)
    result['biggestPots'] = compute_biggest_pots(hands)
    result['ev'] = {'available': has_eval7(), 'computed': False}
    return result



class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        # GET /api/ledger/{gameId}
        m = re.match(r'^/api/ledger/([a-zA-Z0-9_-]+)$', self.path)
        if m:
            game_id = m.group(1)
            ledger_url = LEDGER_URL.format(game_id=game_id)

            def _fail(e, status):
                kind = {'cloudflare': 'cloudflare_blocked', 'rate_limited': 'rate_limited'}.get(
                    getattr(e, 'kind', None), 'fetch_failed')
                self._json_response(status, {'error': kind, 'message': str(e),
                                             'ledgerUrl': ledger_url, 'gameId': game_id})

            session = PokerNowSession()

            # 1) Primary: PokerNow's own ledger endpoint — one request, no Socket.IO
            try:
                print(f'  Fetching players_sessions for {game_id}...')
                ledger = ledger_from_players_sessions(fetch_players_sessions(game_id, session))
                ledger['source'] = 'players_sessions'
                print(f'  Ledger: {len(ledger["players"])} players via players_sessions')
                self._json_response(200, ledger)
                return
            except PokerNowBlocked as e:
                if e.kind in ('cloudflare', 'rate_limited'):
                    print(f'  BLOCKED: {e}')
                    _fail(e, 503)  # more requests would just be blocked too
                    return
                print(f'  players_sessions failed ({e}); falling back to logs + Socket.IO')
            except Exception as e:
                print(f'  players_sessions unusable ({e}); falling back to logs + Socket.IO')

            # 2) Legacy: money-only log crawl + Socket.IO stacks
            try:
                try:
                    session.get(GAME_URL.format(game_id=game_id), timeout=10)  # cookies for Socket.IO
                except PokerNowBlocked as e:
                    print(f'  Game page bootstrap failed ({e})')
                cookie_header = session.cookie_header()

                stacks_result = {'stacks': {}, 'error': None}
                logs_result = {'logs': None, 'error': None}

                def _fetch_stacks():
                    try:
                        state = fetch_game_state(game_id, cookie_header=cookie_header)
                        stacks_result['stacks'] = extract_stacks_from_state(state)
                        print(f'  Got stacks for {len(stacks_result["stacks"])} active players via Socket.IO')
                    except Exception as e:
                        stacks_result['error'] = str(e)
                        print(f'  Socket.IO failed ({e}), will use log stacks')

                def _fetch_logs():
                    try:
                        logs_result['logs'] = fetch_all_logs(game_id, money_only=True, session=session)
                    except Exception as e:
                        logs_result['error'] = e

                t1 = threading.Thread(target=_fetch_stacks)
                t2 = threading.Thread(target=_fetch_logs)
                t1.start()
                t2.start()
                t1.join()
                t2.join()

                if logs_result['error']:
                    raise logs_result['error']

                ledger = compute_ledger(logs_result['logs'], stacks_result['stacks'])
                ledger['source'] = 'logs'
                self._json_response(200, ledger)
            except PokerNowBlocked as e:
                print(f'  ERROR: {e}')
                _fail(e, 503 if e.kind in ('cloudflare', 'rate_limited') else 502)
            except Exception as e:
                print(f'  ERROR: {e}')
                _fail(e, 502)
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
        # POST /api/ledger/import — pasted players_sessions JSON or downloaded ledger CSV.
        # The user's browser can reach PokerNow when our server is Cloudflare-blocked.
        if self.path == '/api/ledger/import':
            try:
                length = int(self.headers.get('Content-Length', 0))
                if length > 5 * 1024 * 1024:
                    self._json_response(400, {'error': 'parse_failed', 'message': 'Ledger file too large (max 5 MB)'})
                    return
                body = self.rfile.read(length).decode('utf-8', errors='replace')
                ledger, fmt = parse_ledger_import(body)
                ledger['source'] = f'import_{fmt}'
                print(f'  Imported ledger ({fmt}): {len(ledger["players"])} players')
                self._json_response(200, ledger)
            except Exception as e:
                print(f'  ERROR importing ledger: {e}')
                self._json_response(400, {'error': 'parse_failed', 'message': str(e)})
            return

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
                result['handHistory'] = compute_hand_history(hands)
                result['biggestPots'] = compute_biggest_pots(hands)
                result['ev'] = {'available': has_eval7(), 'computed': False}
                result['format'] = fmt
                self._json_response(200, result)
            except Exception as e:
                print(f'  ERROR parsing upload: {e}')
                self._json_response(400, {'error': str(e)})
            return

        # POST /api/stats/ev — on-demand all-in EV computation (SSE streaming)
        ev_path = self.path.split('?')[0]
        if ev_path == '/api/stats/ev':
            # Parse trials from query string
            qs = self.path.split('?', 1)[1] if '?' in self.path else ''
            params = dict(p.split('=', 1) for p in qs.split('&') if '=' in p) if qs else {}
            mc_trials = max(500, min(int(params.get('trials', 33000)), 100000))

            try:
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length).decode('utf-8', errors='replace')

                hands, fmt = parse_hand_data(body)
                print(f'  Computing EV for {len(hands)} hands from {fmt} upload (trials={mc_trials})...')

                if not hands:
                    self._json_response(400, {'error': 'No hands found'})
                    return

                if not has_eval7():
                    self._json_response(400, {'error': 'eval7 not available on server'})
                    return
            except Exception as e:
                print(f'  ERROR parsing for EV: {e}')
                self._json_response(400, {'error': str(e)})
                return

            # Stream progress via SSE
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()

            try:
                def on_progress(current, total):
                    try:
                        msg = json.dumps({'progress': current, 'total': total})
                        self.wfile.write(f'data: {msg}\n\n'.encode())
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass

                ev = compute_allin_ev(hands, mc_trials=mc_trials, progress_callback=on_progress)
                final = json.dumps({'done': True, **ev})
                self.wfile.write(f'data: {final}\n\n'.encode())
                self.wfile.flush()
            except Exception as e:
                print(f'  ERROR computing EV: {e}')
                import traceback; traceback.print_exc()
                try:
                    err = json.dumps({'error': str(e)})
                    self.wfile.write(f'data: {err}\n\n'.encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            return

        # POST /api/hand?n=<number> — return one hand's enriched events for the replayer
        hand_path = self.path.split('?')[0]
        if hand_path == '/api/hand':
            try:
                qs = self.path.split('?', 1)[1] if '?' in self.path else ''
                params = dict(p.split('=', 1) for p in qs.split('&') if '=' in p) if qs else {}
                hand_n = params.get('n')
                if not hand_n:
                    self._json_response(400, {'error': 'Missing ?n=<hand_number>'})
                    return
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length).decode('utf-8', errors='replace')
                hands, fmt = parse_hand_data(body)
                hand = extract_hand(hands, hand_n)
                if not hand:
                    self._json_response(404, {'error': f'Hand #{hand_n} not found'})
                    return
                self._json_response(200, {'hand': hand})
            except Exception as e:
                print(f'  ERROR /api/hand: {e}')
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
                board2 = body.get('board2', []) or []
                dead = body.get('dead', [])
                trials = min(body.get('trials', 100000), 200000)
                committed = body.get('committed') or None
                dead_money = float(body.get('dead_money') or 0.0)

                if len(player_hands) < 2:
                    self._json_response(400, {'error': 'Need at least 2 hands'})
                    return

                if board2 and len(board2) >= 0 and any(c for c in board2):
                    result = _run_mc(
                        compute_equity_double_board,
                        player_hands, board, board2, dead, trials,
                        committed=committed, dead_money=dead_money,
                    )
                else:
                    result = _run_mc(
                        compute_equity,
                        player_hands, board, dead, trials,
                        committed=committed, dead_money=dead_money,
                    )
                self._json_response(200, result)
            except Exception as e:
                print(f'  ERROR equity: {e}')
                self._json_response(400, {'error': str(e)})
            return

        # POST /api/equity/categories — list valid categories for a board
        if self.path == '/api/equity/categories':
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length).decode('utf-8'))
                board = body.get('board', [])
                if len(board) < 3:
                    self._json_response(400, {'error': 'Need at least 3 board cards'})
                    return
                cats = list_valid_categories(board)
                self._json_response(200, {'categories': cats})
            except Exception as e:
                print(f'  ERROR equity/categories: {e}')
                self._json_response(400, {'error': str(e)})
            return

        # POST /api/equity/explore — category-based equity exploration
        if self.path == '/api/equity/explore':
            mode = 'single'
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length).decode('utf-8'))
                board = body.get('board', [])
                players = body.get('players', [])
                mode = body.get('mode', 'single')  # 'single' or 'bulk'

                lock_board = body.get('lock_board', True)

                if len(board) < 3:
                    self._json_response(400, {'error': 'Need at least 3 board cards'})
                    return
                if len(players) < 2:
                    self._json_response(400, {'error': 'Need at least 2 players'})
                    return

                if mode == 'bulk':
                    samples = min(body.get('samples', 500), 5000)
                else:
                    samples = 1

                # Use SSE streaming for bulk mode
                if mode == 'bulk':
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Cache-Control', 'no-cache')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()

                # Fast-fail: check all categories are valid for this board before attempting generation
                if lock_board:
                    valid_cats = {c['name'] for c in list_valid_categories(board) if c.get('possible', True)}
                    for p in players:
                        cat = p.get('category')
                        if cat and cat not in valid_cats:
                            err = f"Category '{cat}' is not possible on this board"
                            if mode == 'bulk':
                                try:
                                    self.wfile.write(f'data: {json.dumps({"error": err})}\n\n'.encode())
                                    self.wfile.flush()
                                except (BrokenPipeError, ConnectionResetError):
                                    pass
                            else:
                                self._json_response(400, {'error': err})
                            return

                all_matchups = []
                all_cards = [r + s for r in '23456789TJQKA' for s in 'shdc']
                board_size = len(board)
                si = 0
                failures = 0
                max_failures = max(samples, 50)  # give up after this many extra failures
                import time as _srv_time
                loop_deadline = _srv_time.time() + max(30, samples * 2)  # ~2s per trial budget
                # Pre-collect which categories need board validation
                needed_cats = [p['category'] for p in players if 'category' in p]
                while len(all_matchups) < samples and failures < max_failures and _srv_time.time() < loop_deadline:
                    # Generate board for this sample
                    if lock_board:
                        sample_board = board
                    else:
                        # Random board of same size, avoiding player cards and locks
                        player_dead = set()
                        for p in players:
                            if 'hand' in p:
                                player_dead.update(p['hand'])
                            for c in p.get('locked', []):
                                if c:
                                    player_dead.add(c)
                        avail = [c for c in all_cards if c not in player_dead]
                        # Keep generating random boards until one supports all categories
                        board_ok = False
                        for _bt in range(200):
                            sample_board = random.sample(avail, board_size)
                            if needed_cats:
                                valid_cats = {c['name'] for c in list_valid_categories(sample_board) if c.get('possible', True)}
                                if all(cat in valid_cats for cat in needed_cats):
                                    board_ok = True
                                    break
                            else:
                                board_ok = True
                                break
                        if not board_ok:
                            failures += 1
                            continue

                    matchup_hands = []
                    dead = list(sample_board)
                    valid = True
                    for p in players:
                        if 'hand' in p:
                            matchup_hands.append(p['hand'])
                            dead.extend(p['hand'])
                        elif 'category' in p:
                            locked = [c for c in p.get('locked', []) if c]
                            if len(locked) == 5:
                                # Fully locked — use as-is if no collisions
                                if any(c in dead for c in locked):
                                    valid = False
                                    break
                                matchup_hands.append(locked)
                                dead.extend(locked)
                            else:
                                # Randomly pick blocker cards for this trial
                                blocker_pool = p.get('blocker_pool', [])
                                blocker_count = p.get('blockers', 0)
                                extra_locked = []
                                avoid = []
                                outs_adj = 0
                                if blocker_pool:
                                    is_flush_draw = p['category'] in ('nut_flush_draw', 'flush_draw', 'combo_draw')
                                    is_wrap = p['category'] in ('gutshot', 'oesd', 'wrap_9', 'wrap_13', 'wrap_16', 'wrap_17', 'wrap_20')
                                    is_structured_wrap = p['category'] in ('wrap_9', 'wrap_13', 'wrap_16', 'wrap_17', 'wrap_20')
                                    available = [c for c in blocker_pool if c not in dead and c not in locked]
                                    if is_structured_wrap and blocker_count > 0:
                                        # Lock entire seed structure with N paired ranks.
                                        # Group available cards by rank
                                        by_rank = {}
                                        for c in available:
                                            by_rank.setdefault(c[0], []).append(c)
                                        seed_ranks = list(by_rank.keys())
                                        random.shuffle(seed_ranks)
                                        # Limit to correct seed count for the wrap type.
                                        # Blocker pool may merge ranks from multiple target
                                        # patterns; locking all of them exceeds 5-card hand size.
                                        seed_count = 3 if p['category'] in ('wrap_9', 'wrap_13', 'wrap_17') else 4
                                        seed_ranks = seed_ranks[:seed_count]
                                        # Pick N ranks to pair (need 2+ cards available)
                                        pair_ranks = [r for r in seed_ranks if len(by_rank[r]) >= 2][:blocker_count]
                                        extra_locked = []
                                        for r in seed_ranks:
                                            cards = by_rank[r]
                                            random.shuffle(cards)
                                            if r in pair_ranks:
                                                extra_locked.extend(cards[:2])  # pair
                                            else:
                                                extra_locked.append(cards[0])  # single
                                        outs_adj = blocker_count
                                    else:
                                        to_pick = blocker_count + (1 if is_flush_draw else 0)
                                        random.shuffle(available)
                                        extra_locked = available[:to_pick]
                                        if is_wrap:
                                            outs_adj = blocker_count
                                    # For flush draws, avoid remaining suited cards to prevent extra flush suit in hand.
                                    # For wraps, do NOT avoid — wraps need out-rank cards as core hand ranks.
                                    if not is_wrap:
                                        avoid = [c for c in available if c not in extra_locked]
                                gen = generate_hands(sample_board, p['category'], count=1, dead=dead + avoid, locked=locked + extra_locked, outs_adjust=outs_adj)
                                if not gen:
                                    valid = False
                                    break
                                matchup_hands.append(gen[0])
                                dead.extend(gen[0])
                        else:
                            valid = False
                            break
                    if not valid:
                        failures += 1
                        continue

                    eq = compute_equity(matchup_hands, sample_board, [])
                    all_matchups.append({
                        'hands': matchup_hands,
                        'board': sample_board,
                        'equity': [e['equity'] for e in eq['equities']],
                    })
                    si += 1

                    # Stream progress for bulk mode every 10 samples
                    if mode == 'bulk' and si % 10 == 0:
                        try:
                            msg = json.dumps({'progress': si, 'total': samples})
                            self.wfile.write(f'data: {msg}\n\n'.encode())
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            return

                if not all_matchups:
                    if mode == 'bulk':
                        try:
                            msg = json.dumps({'error': 'Could not generate valid hands for these categories on this board'})
                            self.wfile.write(f'data: {msg}\n\n'.encode())
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                    else:
                        self._json_response(400, {'error': 'Could not generate valid hands for these categories on this board'})
                    return

                # Aggregate results
                n_players = len(players)
                avg_eq = [0.0] * n_players
                for m in all_matchups:
                    for i, e in enumerate(m['equity']):
                        avg_eq[i] += e
                for i in range(n_players):
                    avg_eq[i] /= len(all_matchups)

                n_matchups = len(all_matchups)
                std_errs = [0.0] * n_players
                if mode == 'bulk' and n_matchups > 1:
                    import math
                    for i in range(n_players):
                        vals = [m['equity'][i] for m in all_matchups]
                        mean = avg_eq[i]
                        std_dev = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5
                        std_errs[i] = round(1.96 * std_dev / math.sqrt(n_matchups), 1)  # 95% confidence interval

                player_results = []
                for i, p in enumerate(players):
                    label = p.get('label', p.get('category', f'Player {i+1}'))
                    entry = {'label': label, 'equity': round(avg_eq[i], 1)}
                    if mode == 'bulk':
                        entry['std_dev'] = std_errs[i]
                    player_results.append(entry)

                response = {
                    'players': player_results,
                    'samples_run': len(all_matchups),
                    'mode': mode,
                    'exact': True,
                    'done': True,
                }
                if mode == 'single' and all_matchups:
                    response['hands'] = all_matchups[0]['hands']
                    response['board'] = all_matchups[0].get('board', board)
                    # Compute river outs for turn boards (4 cards, 1 to come)
                    resp_board = response['board']
                    if len(resp_board) == 4:
                        outs = compute_river_outs(all_matchups[0]['hands'], resp_board)
                        if outs:
                            response['river_outs'] = outs
                            # Override equity from outs so numbers match exactly
                            total = len(outs)
                            n_p = len(players)
                            for i in range(n_p):
                                w = sum(1 for o in outs if o['winner'] == i)
                                t = sum(1 for o in outs if o['winner'] == -1)
                                player_results[i]['equity'] = round((w + t / n_p) / total * 100, 1)

                if mode == 'bulk':
                    try:
                        self.wfile.write(f'data: {json.dumps(response)}\n\n'.encode())
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                else:
                    self._json_response(200, response)
            except Exception as e:
                print(f'  ERROR equity/explore: {e}')
                import traceback; traceback.print_exc()
                if mode == 'bulk':
                    try:
                        self.wfile.write(f'data: {json.dumps({"error": str(e)})}\n\n'.encode())
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                else:
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
    cpu = os.cpu_count() or 2
    workers = max(1, min(int(os.environ.get('MC_WORKERS', cpu - 1)), 8))
    prebuild = os.environ.get('MC_PREBUILD') == '1'
    _MC_POOL = ProcessPoolExecutor(max_workers=workers, initializer=_worker_init)
    with ThreadedServer(('0.0.0.0', PORT), Handler) as server:
        print(f'PokerNow Assistant running at http://localhost:{PORT}')
        print(f'  eval7 available: {has_eval7()}')
        print(f'  MC workers: {workers}{" (prebuilding 5-card cache, ~30-60s on first batch)" if prebuild else ""}')
        print('Press Ctrl+C to stop.\n')
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print('\nStopped.')
        finally:
            _MC_POOL.shutdown(wait=False, cancel_futures=True)
