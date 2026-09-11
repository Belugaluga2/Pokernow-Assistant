"""Tests for the players_sessions ledger path, manual import parsers, and Cloudflare handling.
No network access: PokerNowSession is replaced with fakes."""

import json

import pytest

import server
from server import (
    PokerNowBlocked,
    _is_cloudflare_challenge,
    compute_ledger,
    compute_settlements,
    fetch_json,
    ledger_from_players_sessions,
    parse_ledger_csv,
    parse_ledger_import,
)


SAMPLE = {
    "buyInTotal": 650, "inGameTotal": 146, "nitEscrowTotal": 0, "buyOutTotal": 1233, "gameHasRake": False,
    "playersInfos": {
        "LNmGZJoEWx": {"names": ["morbb"], "id": "LNmGZJoEWx", "buyInSum": 50, "buyOutSum": 0,
                       "inGame": 28, "nitEscrow": 0, "net": -22},
        "7cIeaDskH1": {"names": ["Beast", "Beast", "Beastality"], "id": "7cIeaDskH1", "buyInSum": 175,
                       "buyOutSum": 728, "inGame": 3, "nitEscrow": 0, "net": 556},
        "HyLWEUwOW5": {"names": ["hrishaan"], "id": "HyLWEUwOW5", "buyInSum": 75, "buyOutSum": 0,
                       "inGame": 48, "nitEscrow": 0, "net": -27},
        "g2rgxWPDc9": {"names": ["gsbaj"], "id": "g2rgxWPDc9", "buyInSum": 350, "buyOutSum": 505,
                       "inGame": 67, "nitEscrow": 5, "net": 227},
        "ghost00000": {"names": [], "id": "ghost00000", "buyInSum": 0, "buyOutSum": 0,
                       "inGame": 0, "nitEscrow": 0, "net": 0},
    },
}


def _by_name(ledger):
    return {p['name']: p for p in ledger['players']}


# ----- players_sessions -----

def test_players_sessions_nets_and_shape():
    ledger = ledger_from_players_sessions(SAMPLE)
    p = _by_name(ledger)
    assert p['morbb'] == {'name': 'morbb', 'id': 'LNmGZJoEWx', 'buyin': 50, 'cashout': 28, 'net': -22}
    assert p['gsbaj']['cashout'] == 505 + 67 + 5  # buyOut + inGame + nitEscrow
    assert p['gsbaj']['net'] == 227
    assert set(ledger) >= {'players', 'settlements', 'totals'}
    assert ledger['totals'] == {'buyIn': 650, 'buyOut': 1233, 'inGame': 146}


def test_players_sessions_uses_latest_name_and_skips_empty_players():
    ledger = ledger_from_players_sessions(SAMPLE)
    names = [p['name'] for p in ledger['players']]
    assert 'Beastality' in names and 'Beast' not in names
    assert 'ghost00000' not in names
    assert names == sorted(names, key=lambda n: _by_name(ledger)[n]['net'], reverse=True)


def test_players_sessions_settlements_cover_debtors():
    ledger = ledger_from_players_sessions(SAMPLE)
    paid = sum(s['amount'] for s in ledger['settlements'])
    owed = sum(-p['net'] for p in ledger['players'] if p['net'] < 0)
    assert paid == pytest.approx(owed)


def test_players_sessions_rejects_wrong_payload():
    with pytest.raises(ValueError):
        ledger_from_players_sessions({'logs': []})
    with pytest.raises(ValueError):
        ledger_from_players_sessions([])


# ----- ledger CSV -----

CSV = (
    "player_nickname,player_id,session_start_at,session_end_at,buy_in,buy_out,stack,net\n"
    "Alice,a1,2026-09-11T01:00:00Z,2026-09-11T02:00:00Z,100,150,0,50\n"
    "Alice,a1,2026-09-11T02:30:00Z,,50,0,80,30\n"
    "Bob,b2,2026-09-11T01:00:00Z,2026-09-11T03:00:00Z,200,120,0,-80\n"
)


def test_csv_sums_sessions_per_player():
    p = _by_name(parse_ledger_csv(CSV))
    assert p['Alice'] == {'name': 'Alice', 'id': 'a1', 'buyin': 150, 'cashout': 230, 'net': 80}
    assert p['Bob']['net'] == -80


def test_csv_tolerates_bom_reordered_and_extra_columns():
    text = "\ufeffNet,Buy Out,Extra,Player Nickname,Buy In\n" \
           "-10,$40,x,Carol,\"50\"\n" \
           "10,\"1,060\",y,Dave,\"1,050\"\n"
    p = _by_name(parse_ledger_csv(text))
    assert p['Carol'] == {'name': 'Carol', 'id': 'Carol', 'buyin': 50, 'cashout': 40, 'net': -10}
    assert p['Dave']['buyin'] == 1050 and p['Dave']['cashout'] == 1060


def test_csv_net_only_export():
    p = _by_name(parse_ledger_csv("player_nickname,buy_in,net\nErin,100,25\n"))
    assert p['Erin']['cashout'] == 125


def test_csv_missing_columns_raises():
    with pytest.raises(ValueError):
        parse_ledger_csv("foo,bar\n1,2\n")


# ----- import auto-detection -----

def test_import_detects_json_and_csv():
    ledger, fmt = parse_ledger_import('  \n' + json.dumps(SAMPLE))
    assert fmt == 'json' and len(ledger['players']) == 4
    ledger, fmt = parse_ledger_import('\ufeff' + CSV)
    assert fmt == 'csv' and len(ledger['players']) == 2


def test_import_rejects_garbage():
    with pytest.raises(ValueError):
        parse_ledger_import('')
    with pytest.raises(ValueError):
        parse_ledger_import('hello world')
    with pytest.raises(ValueError):
        parse_ledger_import('{not json')


# ----- settlements shared between both ledger paths -----

def test_compute_settlements_greedy():
    results = [{'name': 'A', 'net': 30}, {'name': 'B', 'net': -10}, {'name': 'C', 'net': -20}]
    assert compute_settlements(results) == [
        {'from': 'C', 'to': 'A', 'amount': 20},
        {'from': 'B', 'to': 'A', 'amount': 10},
    ]


def test_compute_ledger_still_uses_shared_settlements():
    logs = [
        {'msg': 'The admin approved the player "A @ a1" participation with a stack of 100.00'},
        {'msg': 'The player "A @ a1" joined the game with a stack of 100.00'},
        {'msg': 'The admin approved the player "B @ b2" participation with a stack of 100.00'},
        {'msg': 'The player "B @ b2" joined the game with a stack of 100.00'},
        {'msg': 'The player "A @ a1" quits the game with a stack of 150.00'},
        {'msg': 'The player "B @ b2" quits the game with a stack of 50.00'},
    ]
    ledger = compute_ledger(logs, {})
    assert ledger['settlements'] == compute_settlements(ledger['players'])
    assert ledger['settlements'] == [{'from': 'B', 'to': 'A', 'amount': 50}]


# ----- Cloudflare handling -----

def test_cloudflare_detection():
    assert _is_cloudflare_challenge(403, {'CF-Mitigated': 'challenge'}, '')
    assert _is_cloudflare_challenge(503, {}, '<html><title>Just a moment...</title>')
    assert not _is_cloudflare_challenge(403, {}, '{"error":"Invalid or missing token"}')
    assert not _is_cloudflare_challenge(200, {'cf-mitigated': 'challenge'}, 'Just a moment')


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, timeout=15):
        self.calls += 1
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(server.time, 'sleep', lambda s: None)
    monkeypatch.delenv('PN_SIMULATE_CLOUDFLARE', raising=False)


def test_fetch_json_retries_429_then_succeeds():
    s = FakeSession([(429, {}, 'Too many'), (200, {'content-type': 'text/html'}, '{"ok": 1}')])
    assert fetch_json(s, 'u') == {'ok': 1}
    assert s.calls == 2


def test_fetch_json_classifies_failures():
    with pytest.raises(PokerNowBlocked) as e:
        fetch_json(FakeSession([(403, {'cf-mitigated': 'challenge'}, '')]), 'u')
    assert e.value.kind == 'cloudflare'

    with pytest.raises(PokerNowBlocked) as e:
        fetch_json(FakeSession([(200, {}, '<html>block page</html>')]), 'u')
    assert e.value.kind == 'bad_json'

    with pytest.raises(PokerNowBlocked) as e:
        fetch_json(FakeSession([(404, {}, 'Not Found')]), 'u')
    assert e.value.kind == 'http' and e.value.status == 404

    with pytest.raises(PokerNowBlocked) as e:
        fetch_json(FakeSession([(429, {}, '')] * 4), 'u')
    assert e.value.kind == 'rate_limited'


def test_fetch_json_simulated_block(monkeypatch):
    monkeypatch.setenv('PN_SIMULATE_CLOUDFLARE', '1')
    s = FakeSession([(200, {}, '{}')])
    with pytest.raises(PokerNowBlocked) as e:
        fetch_json(s, 'u')
    assert e.value.kind == 'cloudflare' and s.calls == 0
