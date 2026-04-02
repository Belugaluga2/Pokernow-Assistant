"""
Unified poker statistics engine.

Computes all stats in a single pass per hand:
  Preflop:  VPIP, PFR, 3-Bet%, 4-Bet%, Fold-to-3Bet, Fold-to-4Bet, Steal
  Postflop: C-Bet, C-Bet Turn, Fold-to-CBet, Triple Barrel, Donk Bet
  Showdown: WTSD%, W$SD%
  Aggression: AF, AFq
  Positional: Fold-to-Steal (SB/BB)
  Bomb Pot: BP Hands, BP VPIP

Also: cumulative winnings, all-in EV (optional, requires eval7), equity calculator.

Event type constants (verified from PokerNow JSON):
  CHECK=0, ANTE=1, BIG_BLIND=2, SMALL_BLIND=3, POSTED_BB=4, POSTED_SB_DEAD=5,
  CALL=7, BET_RAISE=8, COMMUNITY=9, PAYOUT=10, FOLD=11, SHOW_MUCK=12,
  ALLIN_APPROVAL=14, END_OF_HAND=15, REFUND=16, BOUNTIES=18
"""

import itertools
import random
from collections import defaultdict

# ── Event type constants ──────────────────────────────────────────────────

CHECK = 0
ANTE = 1
BIG_BLIND = 2
SMALL_BLIND = 3
POSTED_BB = 4
POSTED_SB_DEAD = 5
CALL = 7
BET_RAISE = 8
COMMUNITY = 9
PAYOUT = 10
FOLD = 11
SHOW_MUCK = 12
ALLIN_APPROVAL = 14
END_OF_HAND = 15
REFUND = 16
BOUNTIES = 18

# ── Position derivation ──────────────────────────────────────────────────

# Standard position names by number of players remaining after BTN
_POSITION_NAMES = {
    2: ['BTN', 'BB'],           # Heads-up: BTN=SB
    3: ['BTN', 'SB', 'BB'],
    4: ['BTN', 'SB', 'BB', 'UTG'],
    5: ['BTN', 'SB', 'BB', 'UTG', 'CO'],
    6: ['BTN', 'SB', 'BB', 'UTG', 'MP', 'CO'],
    7: ['BTN', 'SB', 'BB', 'UTG', 'UTG+1', 'MP', 'CO'],
    8: ['BTN', 'SB', 'BB', 'UTG', 'UTG+1', 'MP', 'HJ', 'CO'],
    9: ['BTN', 'SB', 'BB', 'UTG', 'UTG+1', 'UTG+2', 'MP', 'HJ', 'CO'],
    10: ['BTN', 'SB', 'BB', 'UTG', 'UTG+1', 'UTG+2', 'MP', 'MP+1', 'HJ', 'CO'],
}


def derive_positions(hand):
    """Return {seat: position_name} for a hand.

    Walks clockwise from dealerSeat through sorted occupied seats.
    """
    dealer_seat = hand.get('dealerSeat')
    seats = sorted(p['seat'] for p in hand.get('players', []))
    n = len(seats)

    if n == 0 or dealer_seat is None:
        return {}

    names = _POSITION_NAMES.get(n, _POSITION_NAMES.get(10, []))
    if n > 10:
        names = ['BTN', 'SB', 'BB'] + [f'P{i}' for i in range(n - 3)]

    # Find dealer index and walk clockwise
    if dealer_seat in seats:
        start = seats.index(dealer_seat)
    else:
        # Dealer left the table; pick closest seat
        start = 0
        for i, s in enumerate(seats):
            if s >= dealer_seat:
                start = i
                break

    result = {}
    for i, name in enumerate(names):
        idx = (start + i) % n
        result[seats[idx]] = name

    return result


# ── Per-player stat accumulator ──────────────────────────────────────────

def _new_player_stats(name, pid):
    return {
        'name': name, 'id': pid,
        'hands': 0,
        # VPIP / PFR
        'vpip_n': 0, 'vpip_d': 0,
        'pfr_n': 0, 'pfr_d': 0,
        # 3-bet / 4-bet
        '3bet_n': 0, '3bet_d': 0,
        '4bet_n': 0, '4bet_d': 0,
        # Fold to 3-bet / 4-bet
        'f3bet_n': 0, 'f3bet_d': 0,
        'f4bet_n': 0, 'f4bet_d': 0,
        # C-bet flop
        'cbet_n': 0, 'cbet_d': 0,
        # C-bet turn
        'cbet_turn_n': 0, 'cbet_turn_d': 0,
        # Fold to C-bet
        'fcbet_n': 0, 'fcbet_d': 0,
        # Triple barrel
        'triple_n': 0, 'triple_d': 0,
        # Steal
        'steal_n': 0, 'steal_d': 0,
        # Fold to steal (as SB / BB)
        'fsteal_sb_n': 0, 'fsteal_sb_d': 0,
        'fsteal_bb_n': 0, 'fsteal_bb_d': 0,
        # WTSD / W$SD
        'flops_seen': 0,
        'wtsd_n': 0,   # went to showdown
        'wsd_n': 0,    # won $ at showdown
        # Donk bet
        'donk_n': 0, 'donk_d': 0,
        # Aggression
        'postflop_br': 0,   # bets + raises
        'postflop_calls': 0,
        'postflop_total': 0,
        # Bomb pot
        'bp_hands': 0, 'bp_vpip_n': 0,
        # Double board bomb pot outcomes
        'bp_db_showdowns': 0, 'bp_scoop_n': 0, 'bp_chop_n': 0, 'bp_quartered_n': 0,
    }


def _pct(n, d):
    return round(n / d * 100, 1) if d > 0 else 0.0


def _sample(n, d):
    return f'{n}/{d}'


# ── Main stats computation ───────────────────────────────────────────────

def compute_all_stats(hands):
    """Process all hands and return stats for all players.

    Returns {
      "totalHands": int, "totalBombPots": int,
      "stats": [{name, id, handsPlayed, vpip, pfr, threeBet, ..., samples: {...}}]
    }
    """
    players = {}  # id -> stats dict

    def get(name, pid):
        if pid not in players:
            players[pid] = _new_player_stats(name, pid)
        players[pid]['name'] = name  # update to latest name
        return players[pid]

    total_hands = 0
    total_bp = 0

    for hand in hands:
        seat_to_name = {}
        seat_to_id = {}
        for p in hand.get('players', []):
            seat_to_name[p['seat']] = p['name']
            seat_to_id[p['seat']] = p['id']
            get(p['name'], p['id'])

        events = hand.get('events', [])
        positions = derive_positions(hand)
        is_bomb_pot = hand.get('bombPot', False)

        # Check events for bomb pot marker too
        if not is_bomb_pot:
            for ev in events:
                if ev.get('payload', {}).get('type') == ANTE:
                    # Could be a regular ante or bomb pot; bomb pot marker in CSV sets hand['bombPot']
                    pass

        if is_bomb_pot:
            total_bp += 1
            _process_bomb_pot_hand(hand, events, seat_to_name, seat_to_id, players, positions)
        else:
            total_hands += 1
            _process_standard_hand(hand, events, seat_to_name, seat_to_id, players, positions)

    # Build results
    result_stats = []
    for pid, s in players.items():
        total_played = s['hands'] + s['bp_hands']
        if total_played == 0:
            continue
        result_stats.append({
            'name': s['name'],
            'id': s['id'],
            'handsPlayed': s['hands'],
            'vpip': _pct(s['vpip_n'], s['vpip_d']),
            'pfr': _pct(s['pfr_n'], s['pfr_d']),
            'threeBet': _pct(s['3bet_n'], s['3bet_d']),
            'fourBet': _pct(s['4bet_n'], s['4bet_d']),
            'foldTo3Bet': _pct(s['f3bet_n'], s['f3bet_d']),
            'foldTo4Bet': _pct(s['f4bet_n'], s['f4bet_d']),
            'cbet': _pct(s['cbet_n'], s['cbet_d']),
            'cbetTurn': _pct(s['cbet_turn_n'], s['cbet_turn_d']),
            'foldToCbet': _pct(s['fcbet_n'], s['fcbet_d']),
            'tripleBarrel': _pct(s['triple_n'], s['triple_d']),
            'stealAttempt': _pct(s['steal_n'], s['steal_d']),
            'foldToStealSB': _pct(s['fsteal_sb_n'], s['fsteal_sb_d']),
            'foldToStealBB': _pct(s['fsteal_bb_n'], s['fsteal_bb_d']),
            'wtsd': _pct(s['wtsd_n'], s['flops_seen']),
            'wsd': _pct(s['wsd_n'], s['wtsd_n']),
            'donkBet': _pct(s['donk_n'], s['donk_d']),
            'af': round(s['postflop_br'] / s['postflop_calls'], 2) if s['postflop_calls'] > 0 else (
                round(s['postflop_br'], 2) if s['postflop_br'] > 0 else 0.0
            ),
            'afq': _pct(s['postflop_br'], s['postflop_total']),
            'bpHandsPlayed': s['bp_hands'],
            'bpVpip': _pct(s['bp_vpip_n'], s['bp_hands']),
            'bpDbShowdowns': s['bp_db_showdowns'],
            'bpScoop': _pct(s['bp_scoop_n'], s['bp_db_showdowns']),
            'bpChop': _pct(s['bp_chop_n'], s['bp_db_showdowns']),
            'bpQuartered': _pct(s['bp_quartered_n'], s['bp_db_showdowns']),
            'samples': {
                'vpip': _sample(s['vpip_n'], s['vpip_d']),
                'pfr': _sample(s['pfr_n'], s['pfr_d']),
                'threeBet': _sample(s['3bet_n'], s['3bet_d']),
                'fourBet': _sample(s['4bet_n'], s['4bet_d']),
                'foldTo3Bet': _sample(s['f3bet_n'], s['f3bet_d']),
                'foldTo4Bet': _sample(s['f4bet_n'], s['f4bet_d']),
                'cbet': _sample(s['cbet_n'], s['cbet_d']),
                'cbetTurn': _sample(s['cbet_turn_n'], s['cbet_turn_d']),
                'foldToCbet': _sample(s['fcbet_n'], s['fcbet_d']),
                'tripleBarrel': _sample(s['triple_n'], s['triple_d']),
                'stealAttempt': _sample(s['steal_n'], s['steal_d']),
                'foldToStealSB': _sample(s['fsteal_sb_n'], s['fsteal_sb_d']),
                'foldToStealBB': _sample(s['fsteal_bb_n'], s['fsteal_bb_d']),
                'wtsd': _sample(s['wtsd_n'], s['flops_seen']),
                'wsd': _sample(s['wsd_n'], s['wtsd_n']),
                'donkBet': _sample(s['donk_n'], s['donk_d']),
                'af': f"{s['postflop_br']}/{s['postflop_calls']}",
                'afq': _sample(s['postflop_br'], s['postflop_total']),
                'bpVpip': _sample(s['bp_vpip_n'], s['bp_hands']),
                'bpScoop': _sample(s['bp_scoop_n'], s['bp_db_showdowns']),
                'bpChop': _sample(s['bp_chop_n'], s['bp_db_showdowns']),
                'bpQuartered': _sample(s['bp_quartered_n'], s['bp_db_showdowns']),
            },
        })

    result_stats.sort(key=lambda x: x['handsPlayed'] + x['bpHandsPlayed'], reverse=True)
    return {'totalHands': total_hands, 'totalBombPots': total_bp, 'stats': result_stats}


def _process_standard_hand(hand, events, seat_to_name, seat_to_id, players, positions):
    """Process a non-bomb-pot hand, accumulating all stats."""

    all_seats = set(seat_to_name.keys())

    # Increment hand count for everyone
    for seat in all_seats:
        pid = seat_to_id[seat]
        players[pid]['hands'] += 1
        players[pid]['vpip_d'] += 1
        players[pid]['pfr_d'] += 1

    # ── Track state through events ──

    street = 'preflop'  # preflop, flop, turn, river
    seats_in_hand = set(all_seats)
    preflop_raise_count = 0
    preflop_aggressor = None  # last raiser preflop
    first_raiser = None
    second_raiser = None
    third_raiser = None
    preflop_vpip_seats = set()
    preflop_pfr_seats = set()
    preflop_acted = set()  # seats that have acted preflop
    seats_folded_before_first_raise = set()  # folded before open raise
    seats_active_at_3bet = set()  # seats still in hand when 3-bet happened

    # Flop tracking
    seats_saw_flop = set()
    flop_first_bettor = None
    flop_aggressor_bet = False  # did preflop aggressor bet on flop?
    flop_aggressor_acted = False
    flop_cbet_made = False
    flop_donk_made = False  # someone donk-bet before aggressor acted
    flop_acted_before_aggressor = set()  # seats that acted on flop before aggressor

    # Turn tracking
    turn_aggressor_bet = False

    # River tracking
    river_aggressor_bet = False

    # For fold-to-3bet / 4bet: track what happened to the raiser
    first_raiser_folded = False
    second_raiser_folded = False

    # Steal tracking
    is_steal_attempt = False
    steal_seat = None
    steal_opportunity_seats = set()  # steal-position seats with action folded to them
    preflop_open_action_taken = False  # True once first voluntary call/raise preflop

    for ev in events:
        pl = ev.get('payload', {})
        t = pl.get('type')
        seat = pl.get('seat')

        # ── Street advancement ──
        if t == COMMUNITY:
            turn = pl.get('turn', 0)
            if turn == 1:
                # Entering flop — finalize preflop stats
                street = 'flop'
                seats_saw_flop = set(seats_in_hand)
                for s in seats_saw_flop:
                    players[seat_to_id[s]]['flops_seen'] += 1
            elif turn == 2:
                street = 'turn'
            elif turn == 3:
                street = 'river'
            continue

        # ── Fold ──
        if t == FOLD and seat in seats_in_hand:
            seats_in_hand.discard(seat)

            if street == 'preflop':
                preflop_acted.add(seat)
                # Track who folded before the open raise (no 3-bet opportunity)
                if preflop_raise_count == 0:
                    seats_folded_before_first_raise.add(seat)
                # Steal: player in steal position folded when action was folded to them
                if not preflop_open_action_taken:
                    pos = positions.get(seat, '')
                    if pos in ('CO', 'BTN', 'SB'):
                        steal_opportunity_seats.add(seat)
                # Track fold-to-3bet: first raiser folds after facing 3bet
                if seat == first_raiser and preflop_raise_count >= 2:
                    first_raiser_folded = True
                # Track fold-to-4bet: second raiser folds after facing 4bet
                if seat == second_raiser and preflop_raise_count >= 3:
                    second_raiser_folded = True

            elif street == 'flop':
                # Track who acted before the aggressor on the flop
                if seat != preflop_aggressor and not flop_aggressor_acted:
                    flop_acted_before_aggressor.add(seat)
                # Fold to c-bet
                if flop_cbet_made and seat != preflop_aggressor:
                    pid = seat_to_id[seat]
                    players[pid]['fcbet_n'] += 1

            continue

        # ── Check ──
        if t == CHECK:
            if street == 'flop':
                if seat == preflop_aggressor and not flop_aggressor_acted:
                    flop_aggressor_acted = True
                    # Aggressor checked — no c-bet
                elif seat != preflop_aggressor and not flop_aggressor_acted:
                    flop_acted_before_aggressor.add(seat)

            # Aggression tracking (postflop)
            if street in ('flop', 'turn', 'river') and seat in seats_in_hand:
                pid = seat_to_id.get(seat)
                if pid:
                    players[pid]['postflop_total'] += 1
            continue

        # ── Call ──
        if t == CALL and seat is not None:
            if street == 'preflop':
                preflop_vpip_seats.add(seat)
                preflop_acted.add(seat)
                # Steal: limp from steal position still counts as having opportunity
                if not preflop_open_action_taken:
                    pos = positions.get(seat, '')
                    if pos in ('CO', 'BTN', 'SB'):
                        steal_opportunity_seats.add(seat)
                preflop_open_action_taken = True
            else:
                # Postflop aggression
                pid = seat_to_id.get(seat)
                if pid:
                    players[pid]['postflop_calls'] += 1
                    players[pid]['postflop_total'] += 1
            continue

        # ── Bet / Raise ──
        if t == BET_RAISE and seat is not None:
            if street == 'preflop':
                # Steal: raise from steal position when action folded to them
                if not preflop_open_action_taken:
                    pos = positions.get(seat, '')
                    if pos in ('CO', 'BTN', 'SB'):
                        steal_opportunity_seats.add(seat)
                preflop_open_action_taken = True

                preflop_raise_count += 1
                preflop_vpip_seats.add(seat)
                preflop_pfr_seats.add(seat)
                preflop_aggressor = seat
                preflop_acted.add(seat)

                if preflop_raise_count == 1:
                    first_raiser = seat
                elif preflop_raise_count == 2:
                    second_raiser = seat
                    seats_active_at_3bet = set(seats_in_hand)
                elif preflop_raise_count == 3:
                    third_raiser = seat

            elif street == 'flop':
                pid = seat_to_id.get(seat)
                if pid:
                    players[pid]['postflop_br'] += 1
                    players[pid]['postflop_total'] += 1

                if not flop_aggressor_acted and seat == preflop_aggressor:
                    # Preflop aggressor bets flop = c-bet
                    flop_aggressor_acted = True
                    if not flop_donk_made:
                        # Only a c-bet if not already donk-bet into
                        flop_aggressor_bet = True
                        flop_cbet_made = True
                elif seat != preflop_aggressor:
                    if not flop_aggressor_acted:
                        # Non-aggressor acts before aggressor
                        flop_acted_before_aggressor.add(seat)
                    if flop_first_bettor is None:
                        # Someone else bets first = potential donk bet
                        flop_first_bettor = seat
                        if not flop_aggressor_acted:
                            # Aggressor hasn't acted yet, so this is a donk bet
                            flop_donk_made = True
                            pid_donk = seat_to_id.get(seat)
                            if pid_donk:
                                players[pid_donk]['donk_n'] += 1

            elif street == 'turn':
                pid = seat_to_id.get(seat)
                if pid:
                    players[pid]['postflop_br'] += 1
                    players[pid]['postflop_total'] += 1
                if seat == preflop_aggressor:
                    turn_aggressor_bet = True

            elif street == 'river':
                pid = seat_to_id.get(seat)
                if pid:
                    players[pid]['postflop_br'] += 1
                    players[pid]['postflop_total'] += 1
                if seat == preflop_aggressor:
                    river_aggressor_bet = True

            continue

        # ── Payout (showdown / win) ──
        if t == PAYOUT and seat is not None:
            # W$SD: count as showdown win if 2+ players reached showdown
            if len(seats_in_hand) >= 2 and len(seats_saw_flop) >= 2:
                pid = seat_to_id.get(seat)
                if pid and seat in seats_saw_flop:
                    players[pid]['wsd_n'] += 1

    # ── Post-hand stat accumulation ──

    # VPIP
    for seat in preflop_vpip_seats:
        players[seat_to_id[seat]]['vpip_n'] += 1

    # PFR
    for seat in preflop_pfr_seats:
        players[seat_to_id[seat]]['pfr_n'] += 1

    # 3-bet: opportunity = player faced the open raise (was still in hand, didn't fold before it)
    if first_raiser is not None:
        for seat in all_seats:
            if seat == first_raiser:
                continue
            if seat in seats_folded_before_first_raise:
                continue
            players[seat_to_id[seat]]['3bet_d'] += 1
        if preflop_raise_count >= 2 and second_raiser:
            pid = seat_to_id[second_raiser]
            players[pid]['3bet_n'] += 1

    # 4-bet: opportunity = player faced the 3-bet (was still in hand when it happened)
    if preflop_raise_count >= 2 and second_raiser:
        for seat in seats_active_at_3bet:
            if seat == second_raiser:
                continue
            players[seat_to_id[seat]]['4bet_d'] += 1
        if preflop_raise_count >= 3 and third_raiser:
            pid = seat_to_id[third_raiser]
            players[pid]['4bet_n'] += 1

    # Fold to 3-bet
    if preflop_raise_count >= 2 and first_raiser is not None:
        pid = seat_to_id[first_raiser]
        players[pid]['f3bet_d'] += 1
        if first_raiser_folded:
            players[pid]['f3bet_n'] += 1

    # Fold to 4-bet
    if preflop_raise_count >= 3 and second_raiser is not None:
        pid = seat_to_id[second_raiser]
        players[pid]['f4bet_d'] += 1
        if second_raiser_folded:
            players[pid]['f4bet_n'] += 1

    # Steal attempt: first raise from CO, BTN, or SB when action folded to them
    if first_raiser is not None and first_raiser in steal_opportunity_seats:
        raiser_pos = positions.get(first_raiser, '')
        if raiser_pos in ('CO', 'BTN', 'SB'):
            is_steal_attempt = True
            steal_seat = first_raiser

    # Steal opportunity: tracked per-seat during event processing
    for seat in steal_opportunity_seats:
        players[seat_to_id[seat]]['steal_d'] += 1
    if is_steal_attempt:
        players[seat_to_id[steal_seat]]['steal_n'] += 1

    # Fold to steal (SB/BB): when CO/BTN/SB open-raised
    if is_steal_attempt:
        steal_pos = positions.get(steal_seat, '')
        for seat in all_seats:
            pos = positions.get(seat, '')
            # SB faces steal from CO/BTN (not from SB itself)
            if pos == 'SB' and steal_pos != 'SB':
                players[seat_to_id[seat]]['fsteal_sb_d'] += 1
                if seat not in seats_in_hand:
                    players[seat_to_id[seat]]['fsteal_sb_n'] += 1
            # BB faces steal from CO/BTN/SB
            elif pos == 'BB':
                players[seat_to_id[seat]]['fsteal_bb_d'] += 1
                if seat not in seats_in_hand:
                    players[seat_to_id[seat]]['fsteal_bb_n'] += 1

    # C-bet opportunity: preflop aggressor saw the flop and wasn't donk-bet into
    if preflop_aggressor is not None and preflop_aggressor in seats_saw_flop and not flop_donk_made:
        pid = seat_to_id[preflop_aggressor]
        players[pid]['cbet_d'] += 1
        if flop_aggressor_bet:
            players[pid]['cbet_n'] += 1

    # Fold to c-bet opportunity: everyone else who saw the flop
    if flop_cbet_made:
        for seat in seats_saw_flop:
            if seat != preflop_aggressor:
                players[seat_to_id[seat]]['fcbet_d'] += 1

    # C-bet turn opportunity: if aggressor c-bet the flop and turn was dealt
    if flop_aggressor_bet and street in ('turn', 'river'):
        pid = seat_to_id.get(preflop_aggressor)
        if pid:
            players[pid]['cbet_turn_d'] += 1
            if turn_aggressor_bet:
                players[pid]['cbet_turn_n'] += 1

    # Triple barrel opportunity: aggressor c-bet flop, bet turn, and reached river
    if flop_aggressor_bet and turn_aggressor_bet and street == 'river':
        pid = seat_to_id.get(preflop_aggressor)
        if pid:
            players[pid]['triple_d'] += 1
            if river_aggressor_bet:
                players[pid]['triple_n'] += 1

    # Donk bet opportunity: players who acted on the flop before the preflop aggressor
    if preflop_aggressor is not None and len(seats_saw_flop) >= 2:
        for seat in flop_acted_before_aggressor:
            players[seat_to_id[seat]]['donk_d'] += 1

    # WTSD: 2+ players still in at showdown (after seeing flop)
    if len(seats_in_hand) >= 2 and len(seats_saw_flop) >= 2:
        for seat in seats_in_hand:
            if seat in seats_saw_flop:
                players[seat_to_id[seat]]['wtsd_n'] += 1


def _process_bomb_pot_hand(hand, events, seat_to_name, seat_to_id, players, positions):
    """Process a bomb pot hand — tracks BP hands, BP VPIP, and double board outcomes."""
    all_seats = set(seat_to_name.keys())
    for seat in all_seats:
        players[seat_to_id[seat]]['bp_hands'] += 1

    postflop = False
    bp_vpip_counted = set()
    seats_in_hand = set(all_seats)

    for ev in events:
        pl = ev.get('payload', {})
        t = pl.get('type')
        seat = pl.get('seat')

        if t == COMMUNITY:
            postflop = True
            continue

        if t == FOLD and seat in seats_in_hand:
            seats_in_hand.discard(seat)

        if postflop and t in (CALL, BET_RAISE) and seat is not None:
            if seat not in bp_vpip_counted:
                pid = seat_to_id.get(seat)
                if pid:
                    players[pid]['bp_vpip_n'] += 1
                    bp_vpip_counted.add(seat)

    # Track double board outcomes (scoop/chop/quartered)
    _track_double_board_outcome(hand, seats_in_hand, seat_to_name, seat_to_id, players)


def _evaluate_hand_for_board(hole_cards, board, is_omaha):
    """Evaluate a player's best hand on a specific board. Returns comparable score."""
    if _HAVE_EVAL7:
        e7_hole = [eval7.Card(c) for c in hole_cards]
        e7_board = [eval7.Card(c) for c in board]
        if is_omaha:
            return _best_omaha_hand(e7_hole, e7_board)
        return eval7.evaluate(e7_hole + e7_board)
    if is_omaha:
        best = None
        for h2 in itertools.combinations(hole_cards, 2):
            for b3 in itertools.combinations(board, 3):
                s = _eval5(list(h2) + list(b3))
                if best is None or s > best:
                    best = s
        return best
    return _best5of7(hole_cards + board)


def _track_double_board_outcome(hand, survivors, seat_to_name, seat_to_id, players):
    """Track scoop/chop/quartered for double board hands at showdown."""
    board1, board2 = _board_up_to(hand, len(hand.get('events', [])) - 1)
    if not board2 or len(board1) < 5 or len(board2) < 5:
        return
    if len(survivors) < 2:
        return

    known_hole = _collect_hole_cards(hand)
    survivor_seats = [s for s in survivors if s in known_hole and len(known_hole[s]) >= 2]
    if len(survivor_seats) < 2:
        return

    is_omaha = _is_omaha(hand.get('gameType', ''))
    scores1 = {s: _evaluate_hand_for_board(known_hole[s], board1, is_omaha) for s in survivor_seats}
    scores2 = {s: _evaluate_hand_for_board(known_hole[s], board2, is_omaha) for s in survivor_seats}

    best1 = max(scores1.values())
    best2 = max(scores2.values())
    winners1 = {s for s, sc in scores1.items() if sc == best1}
    winners2 = {s for s, sc in scores2.items() if sc == best2}

    for seat in survivor_seats:
        pid = seat_to_id.get(seat)
        if not pid:
            continue
        players[pid]['bp_db_showdowns'] += 1
        won_b1 = seat in winners1
        won_b2 = seat in winners2
        if won_b1 and won_b2:
            players[pid]['bp_scoop_n'] += 1
        elif won_b1 or won_b2:
            shared = (won_b1 and len(winners1) > 1) or (won_b2 and len(winners2) > 1)
            if shared:
                players[pid]['bp_quartered_n'] += 1
            else:
                players[pid]['bp_chop_n'] += 1


# ── Winnings computation ─────────────────────────────────────────────────

def compute_winnings(hands):
    """Compute cumulative winnings per player over time.

    Returns {"players": {name: [cumulative_values]}, "handLabels": [1,2,...]}
    """
    all_players = sorted({p['name'] for h in hands for p in h.get('players', [])})
    cumulative = {name: [0.0] for name in all_players}
    labels = []

    amounts_in_cents = bool(hands and hands[0].get('cents', False))

    for i, hand in enumerate(hands):
        deltas = _compute_deltas(hand)
        labels.append(hand.get('number', str(i + 1)))
        for name in all_players:
            prev = cumulative[name][-1]
            cumulative[name].append(prev + deltas.get(name, 0.0))

    if amounts_in_cents:
        for name in all_players:
            cumulative[name] = [round(v / 100.0, 2) for v in cumulative[name]]

    return {'players': cumulative, 'handLabels': labels}


def _compute_deltas(hand):
    """Event-driven accounting for a single hand.

    Tracks contributions, refunds, payouts. Uses per-street 'to-amount' logic.
    Ported from winnings_graph.py (correct implementation).
    """
    seat_to_name = {p['seat']: p['name'] for p in hand.get('players', [])}
    deltas = defaultdict(float)
    street_contrib = defaultdict(float)

    for ev in hand.get('events', []):
        pl = ev.get('payload', {})
        t = pl.get('type')
        seat = pl.get('seat')
        name = seat_to_name.get(seat)
        val = float(pl.get('value', 0) or 0)

        # Posts / antes
        if t in (ANTE, BIG_BLIND, SMALL_BLIND, POSTED_BB, POSTED_SB_DEAD):
            if name:
                deltas[name] -= val
                if t in (ANTE, BIG_BLIND, SMALL_BLIND, POSTED_BB):
                    street_contrib[name] += val

        # Call to amount
        elif t == CALL and name:
            addl = max(0.0, val - street_contrib[name])
            if addl > 0:
                deltas[name] -= addl
                street_contrib[name] += addl

        # Bet/raise to amount
        elif t == BET_RAISE and name:
            addl = max(0.0, val - street_contrib[name])
            if addl > 0:
                deltas[name] -= addl
                street_contrib[name] += addl

        # Refund
        elif t == REFUND and name and val:
            deltas[name] += val
            street_contrib[name] = max(0.0, street_contrib[name] - val)

        # Payout
        elif t == PAYOUT and name and val:
            deltas[name] += val

        # Bounties
        elif t == BOUNTIES:
            for s, v in pl.get('paidBounties', []) or []:
                n = seat_to_name.get(s)
                if n:
                    deltas[n] -= float(v or 0)
            for s, v in pl.get('receivedBounties', []) or []:
                n = seat_to_name.get(s)
                if n:
                    deltas[n] += float(v or 0)

        # Street advance — reset per-street contributions
        elif t == COMMUNITY:
            street_contrib.clear()

    return deltas


# ── All-in EV (optional, requires eval7) ─────────────────────────────────

try:
    import eval7
    _HAVE_EVAL7 = True
except ImportError:
    eval7 = None
    _HAVE_EVAL7 = False


def has_eval7():
    return _HAVE_EVAL7


def compute_allin_ev(hands, mc_trials=100000, progress_callback=None):
    """Compute all-in EV for each hand where all-in occurs.

    For each all-in hand, computes:
      - committed: how much each player put into the pot
      - ev_payout: expected payout based on equity (MC simulation)
      - actual_payout: what PokerNow actually paid out
      - ev_net = ev_payout - committed
      - actual_net = actual_payout - committed
      - diff = actual_net - ev_net = actual_payout - ev_payout

    Since both ev_payout and actual_payout distribute the same total pot,
    sum(diff) across all players in a hand is always 0.

    Returns {
      "perPlayer": {name: {"count","actual","ev","diff"}},
      "evRows": [{hand_number, board, players:[{name,hand,equity,ev,actual,diff}]}],
      "available": bool
    } or {"available": False} if eval7 not installed.
    """
    if not _HAVE_EVAL7:
        return {'available': False}

    amounts_in_cents = bool(hands and hands[0].get('cents', False))
    scale = 0.01 if amounts_in_cents else 1.0

    rows = []
    per_player = defaultdict(lambda: {'count': 0, 'actual': 0.0, 'ev': 0.0, 'diff': 0.0})
    skip_reasons = defaultdict(int)

    # Pre-scan for all-in hands
    allin_hands = []
    for hand in hands:
        lock_idx = _find_allin_lock(hand)
        if lock_idx is not None:
            allin_hands.append((hand, lock_idx))

    total_allin = len(allin_hands)
    if progress_callback:
        progress_callback(0, total_allin)

    for idx, (hand, lock_idx) in enumerate(allin_hands):
        # Skip river all-ins — no cards left to simulate, EV = actual
        board1_at_lock, _ = _board_up_to(hand, lock_idx)
        if len(board1_at_lock) >= 5:
            skip_reasons['river all-in (EV = actual)'] += 1
            if progress_callback:
                progress_callback(idx + 1, total_allin)
            continue

        if _is_omaha(hand.get('gameType', '')):
            trials = int(mc_trials * 0.04) if '5' in str(hand.get('gameType', '')) else int(mc_trials * 0.1)
        else:
            trials = mc_trials

        exp, info = _expected_payout(hand, lock_idx, trials)
        if exp is None:
            reason = str(info) if info else 'unknown'
            if 'few survivor' in reason.lower():
                skip_reasons['no contest (everyone folded)'] += 1
            elif 'missing' in reason.lower():
                skip_reasons['missing hole cards'] += 1
            else:
                skip_reasons[reason] += 1
            if progress_callback:
                progress_callback(idx + 1, total_allin)
            continue

        seat_to_name = {p['seat']: p['name'] for p in hand.get('players', [])}
        names = info['names']

        # Identify which seats actually went all-in.
        # If ALLIN_APPROVAL exists, all survivors are all-in.
        has_approval = any(
            e.get('payload', {}).get('type') == ALLIN_APPROVAL
            for e in hand.get('events', [])
        )
        if has_approval:
            allin_names = set(names)
        else:
            allin_seats = set()
            for e in hand.get('events', []):
                pl = e.get('payload', {})
                if pl.get('type') in (CALL, BET_RAISE) and pl.get('allIn'):
                    allin_seats.add(pl.get('seat'))
            allin_names = {seat_to_name.get(s) for s in allin_seats}

        # Decide who to report EV for:
        # - If ≤1 non-all-in survivor → equity locked for everyone → report all
        # - If 2+ non-all-in survivors and 2+ all-in → report only all-in players
        # - If 2+ non-all-in survivors and 1 all-in → skip (not a real lock)
        non_allin = [n for n in names if n not in allin_names]
        if len(non_allin) >= 2:
            if len(allin_names) < 2:
                skip_reasons['single all-in with multiple callers'] += 1
                if progress_callback:
                    progress_callback(idx + 1, total_allin)
                continue
            report_names = allin_names
        else:
            # ≤1 non-all-in survivor: everyone's equity is locked
            report_names = set(names)

        actual_by_seat = defaultdict(float)
        for e in hand.get('events', []):
            pl = e.get('payload', {})
            if pl.get('type') == PAYOUT and pl.get('value'):
                actual_by_seat[pl.get('seat')] += float(pl['value'])
        actual = defaultdict(float)
        for s, v in actual_by_seat.items():
            actual[seat_to_name.get(s, str(s))] += v

        committed_by_name = info['locked']
        ev_total = sum(exp.get(n, 0.0) for n in names)

        players_detail = []
        for n in names:
            a_payout = actual.get(n, 0.0) * scale
            e_payout = exp.get(n, 0.0) * scale
            invested = committed_by_name.get(n, 0.0) * scale
            eq = (exp.get(n, 0.0) / ev_total) if ev_total > 1e-12 else 0.0

            a_net = a_payout - invested
            e_net = e_payout - invested
            diff = a_net - e_net

            # Only report EV for players whose equity is locked
            if n not in report_names:
                continue

            players_detail.append({
                'name': n,
                'hand': ' '.join(info['hands_by_name'].get(n, [])),
                'equity': round(eq * 100, 1),
                'ev': round(e_net, 2),
                'actual': round(a_net, 2),
                'diff': round(diff, 2),
            })
            per_player[n]['count'] += 1
            per_player[n]['actual'] += a_net
            per_player[n]['ev'] += e_net
            per_player[n]['diff'] += diff

        # Determine street of all-in
        b1_len = len(board1_at_lock)
        allin_street = {0: 'preflop', 3: 'flop', 4: 'turn'}.get(b1_len, 'flop')

        rows.append({
            'handNumber': hand.get('number'),
            'board': info['board'],
            'players': players_detail,
            'street': allin_street,
        })

        if progress_callback:
            progress_callback(idx + 1, total_allin)

    # Round final per-player totals
    pp = {}
    for name, agg in per_player.items():
        pp[name] = {
            'count': agg['count'],
            'actual': round(agg['actual'], 2),
            'ev': round(agg['ev'], 2),
            'diff': round(agg['diff'], 2),
        }

    return {
        'available': True, 'perPlayer': pp, 'evRows': rows,
        'totalAllin': total_allin, 'totalAnalyzed': len(rows),
        'skipReasons': dict(skip_reasons),
    }


def _find_allin_lock(hand):
    """Find the all-in lock point (type 14 approval or last allIn bet/call).

    Returns lock index if at least one player is all-in, or None.
    Caller decides whether to analyze based on survivor structure.
    """
    events = hand.get('events', [])
    last_approve = None
    for i, ev in enumerate(events):
        if ev.get('payload', {}).get('type') == ALLIN_APPROVAL:
            last_approve = i
    if last_approve is not None:
        return last_approve

    last_allin = None
    for i, ev in enumerate(events):
        pl = ev.get('payload', {})
        if pl.get('type') in (CALL, BET_RAISE) and pl.get('allIn'):
            last_allin = i
    return last_allin


def _settlement_idx(hand, lock_idx):
    events = hand.get('events', [])
    j = lock_idx
    for k in range(lock_idx + 1, len(events)):
        t = events[k].get('payload', {}).get('type')
        if t in (REFUND, SHOW_MUCK, ALLIN_APPROVAL, CHECK, FOLD, END_OF_HAND):
            j = k
            continue
        if t in (COMMUNITY, PAYOUT, ANTE, BIG_BLIND, SMALL_BLIND, POSTED_BB, POSTED_SB_DEAD, CALL, BET_RAISE):
            break
    return j


def _contribs_until(hand, until_idx):
    committed = defaultdict(float)
    street_contrib = defaultdict(float)
    folded = set()
    for i, ev in enumerate(hand.get('events', [])):
        if i > until_idx:
            break
        pl = ev.get('payload', {})
        t = pl.get('type')
        seat = pl.get('seat')
        val = float(pl.get('value', 0) or 0)
        if t in (ANTE, BIG_BLIND, SMALL_BLIND, POSTED_BB, POSTED_SB_DEAD):
            committed[seat] += val
            if t in (ANTE, BIG_BLIND, SMALL_BLIND, POSTED_BB):
                street_contrib[seat] += val
        elif t == CALL:
            addl = max(0.0, val - street_contrib[seat])
            if addl > 0:
                committed[seat] += addl
                street_contrib[seat] += addl
        elif t == BET_RAISE:
            addl = max(0.0, val - street_contrib[seat])
            if addl > 0:
                committed[seat] += addl
                street_contrib[seat] += addl
        elif t == REFUND:
            committed[seat] = max(0.0, committed[seat] - val)
            street_contrib[seat] = max(0.0, street_contrib[seat] - val)
        elif t == FOLD:
            folded.add(seat)
        elif t == COMMUNITY:
            street_contrib.clear()
    return committed, folded


def _board_up_to(hand, idx):
    """Return board cards up to idx. Returns (board1, board2) if double board, else (board1, None)."""
    board1 = []
    board2 = []
    for i, ev in enumerate(hand.get('events', [])):
        if i > idx:
            break
        pl = ev.get('payload', {})
        if pl.get('type') == COMMUNITY and pl.get('cards'):
            board_num = pl.get('board') or pl.get('run', 1)
            if board_num == 2:
                board2.extend(pl['cards'])
            else:
                board1.extend(pl['cards'])
    return board1, board2 if board2 else None


def _collect_hole_cards(hand):
    """Collect all hole cards per seat (2 for Hold'em, 4 for PLO, 5 for PLO5)."""
    seat_to_cards = {}
    for p in hand.get('players', []):
        hc = p.get('hand')
        if hc:
            valid = [c for c in hc if c]
            if len(valid) >= 2:
                seat_to_cards[p['seat']] = valid
    for ev in hand.get('events', []):
        pl = ev.get('payload', {})
        if pl.get('type') == SHOW_MUCK and pl.get('cards'):
            c = [x for x in pl['cards'] if x]
            if len(c) >= 2:
                seat_to_cards[pl.get('seat')] = c
    return seat_to_cards


def _build_pots(committed, survivors):
    committed = {s: v for s, v in committed.items() if v > 0}
    if not committed:
        return []
    caps = sorted(set(committed.values()))
    pots = []
    prev = 0.0
    for cap in caps:
        contrib_seats = [s for s, v in committed.items() if v >= cap]
        layer = cap - prev
        if layer > 0 and contrib_seats:
            pot_size = layer * len(contrib_seats)
            eligible = set(contrib_seats) & set(survivors)
            pots.append((pot_size, eligible))
        prev = cap
    return pots


def _actual_payouts_after(hand, from_idx):
    out = defaultdict(float)
    for i, ev in enumerate(hand.get('events', [])):
        if i < from_idx:
            continue
        pl = ev.get('payload', {})
        if pl.get('type') == PAYOUT and pl.get('value'):
            out[pl.get('seat')] += float(pl['value'])
    return out


def _is_omaha(game_type):
    """Check if the game type is any Omaha variant."""
    gt = str(game_type).lower()
    return 'omaha' in gt or gt in ('oh', 'plo', 'plo5')


def _best_omaha_hand(hole_e7, board_e7):
    """Evaluate the best Omaha hand: must use exactly 2 from hole + 3 from board."""
    best = 0
    for h2 in itertools.combinations(hole_e7, 2):
        for b3 in itertools.combinations(board_e7, 3):
            score = eval7.evaluate(list(h2) + list(b3))
            if score > best:
                best = score
    return best


def _evaluate_hands(e7_hole, e7_board_full, is_omaha):
    """Score each player's hand. Uses Omaha rules if is_omaha, else Hold'em."""
    if is_omaha:
        return [_best_omaha_hand(h, e7_board_full) for h in e7_hole]
    else:
        return [eval7.evaluate(h + e7_board_full) for h in e7_hole]


def _expected_payout(hand, lock_idx, mc_trials):
    gt = str(hand.get('gameType', '')).lower()
    is_holdem = gt in ('th', 'he', 'holdem')
    omaha = _is_omaha(hand.get('gameType', ''))
    if not is_holdem and not omaha:
        return None, f'Unsupported game type: {gt}'

    min_hole = 4 if omaha else 2

    board1, board2 = _board_up_to(hand, lock_idx)
    is_double = board2 is not None and len(board2) > 0

    # Use ALL events to get final committed amounts and folds
    all_events_idx = len(hand.get('events', [])) - 1
    committed, folded = _contribs_until(hand, all_events_idx)
    survivors = [s for s, v in committed.items() if v > 0 and s not in folded]
    if len(survivors) < 2:
        return None, 'Too few survivors'

    pots = _build_pots(committed, survivors)
    if not pots:
        return None, 'No pots'

    seat_to_name = {p['seat']: p['name'] for p in hand.get('players', [])}
    known_hole = _collect_hole_cards(hand)
    for s in survivors:
        if s not in known_hole or len(known_hole[s]) < min_hole:
            return None, f'Missing cards for {seat_to_name.get(s)}'

    seats = list(survivors)
    names = [seat_to_name[s] for s in seats]
    hole = [known_hole[s] for s in seats]

    # Build eval7 card objects
    try:
        e7_hole = [[eval7.Card(c) for c in h] for h in hole]
        e7_board1 = [eval7.Card(c) for c in board1]
        e7_board2 = [eval7.Card(c) for c in board2] if is_double else []
    except Exception:
        return None, 'Card parse error'

    # Build deck minus ALL known cards (hole cards + both boards)
    known_cards = set()
    for h in hole:
        known_cards.update(h)
    known_cards.update(board1)
    if is_double:
        known_cards.update(board2)

    deck = eval7.Deck()
    for cs in known_cards:
        deck.cards.remove(eval7.Card(cs))

    seat_idx = {s: i for i, s in enumerate(seats)}
    n = len(seats)
    payouts = [0.0] * n

    missing1 = 5 - len(e7_board1)
    missing2 = (5 - len(e7_board2)) if is_double else 0
    total_missing = missing1 + missing2

    def _board_winners(full_board):
        """Return list of winner indices per pot for one board."""
        scores = _evaluate_hands(e7_hole, full_board, omaha)
        result = []
        for pot_size, elig in pots:
            idxs = [seat_idx[s] for s in elig]
            best = max(scores[i] for i in idxs)
            winners = [i for i in idxs if scores[i] == best]
            result.append(winners)
        return result

    def _pay_trial(fb1, fb2=None):
        """Award payouts for one trial. Double board splits each pot 50/50."""
        w1 = _board_winners(fb1)
        if fb2 is not None:
            w2 = _board_winners(fb2)
            for pi, (pot_size, elig) in enumerate(pots):
                half = pot_size / 2.0
                for i in w1[pi]:
                    payouts[i] += half / len(w1[pi])
                for i in w2[pi]:
                    payouts[i] += half / len(w2[pi])
        else:
            for pi, (pot_size, elig) in enumerate(pots):
                for i in w1[pi]:
                    payouts[i] += pot_size / len(w1[pi])

    if total_missing == 0:
        # All cards known — evaluate directly
        _pay_trial(e7_board1, e7_board2 if is_double else None)
    elif total_missing <= 2 and not omaha and not is_double:
        # Exact enumeration (single board Hold'em with 1-2 missing)
        total = 0
        for extra in itertools.combinations(deck.cards, total_missing):
            _pay_trial(e7_board1 + list(extra))
            total += 1
        payouts = [p / total if total else 0 for p in payouts]
    else:
        # Monte Carlo — draw for BOTH boards from the same deck simultaneously
        for _ in range(mc_trials):
            deck.shuffle()
            draw = deck.peek(total_missing)
            fb1 = e7_board1 + list(draw[:missing1])
            fb2 = (e7_board2 + list(draw[missing1:total_missing])) if is_double else None
            _pay_trial(fb1, fb2)
        payouts = [p / mc_trials for p in payouts]

    exp_by_name = {names[i]: payouts[i] for i in range(len(names))}
    locked = {seat_to_name[s]: committed.get(s, 0.0) for s in survivors}
    hands_by_name = {names[i]: hole[i] for i in range(len(names))}

    board_display = board1
    if is_double:
        board_display = board1 + ['|'] + board2

    info = {
        'board': board_display,
        'names': names,
        'hands_by_name': hands_by_name,
        'locked': locked,
    }
    return exp_by_name, info


# ── Hand history (per-player) ────────────────────────────────────────────

def compute_hand_history(hands):
    """Return per-player hand-by-hand data: hole cards, delta, boards, game type."""
    amounts_in_cents = bool(hands and hands[0].get('cents', False))
    scale = 0.01 if amounts_in_cents else 1.0

    history = defaultdict(list)

    for hand in hands:
        deltas = _compute_deltas(hand)
        hole_cards = _collect_hole_cards(hand)
        board1, board2 = _board_up_to(hand, len(hand.get('events', [])) - 1)

        for p in hand.get('players', []):
            name = p['name']
            cards = hole_cards.get(p['seat']) or []
            cards = [c for c in cards if c]
            delta = round(deltas.get(name, 0.0) * scale, 2)

            entry = {'hand': hand.get('number', ''), 'delta': delta}
            if cards:
                entry['cards'] = cards
            if board1:
                entry['board1'] = board1
            if board2:
                entry['board2'] = board2
            if hand.get('bombPot'):
                entry['bp'] = True
            gt = hand.get('gameType', '')
            if gt:
                entry['gt'] = gt

            history[name].append(entry)

    return dict(history)


# ── Equity calculator ────────────────────────────────────────────────────

RANKS = ['2', '3', '4', '5', '6', '7', '8', '9', 'T', 'J', 'Q', 'K', 'A']
SUITS = ['s', 'h', 'd', 'c']


def _rank_int(card):
    return RANKS.index(card[0]) + 2


def _suit_char(card):
    return card[1]


def _eval5(cards):
    """Evaluate a 5-card hand. Returns a comparable tuple (category, tiebreakers)."""
    ranks = sorted((_rank_int(c) for c in cards), reverse=True)
    suits = [_suit_char(c) for c in cards]

    is_flush = len(set(suits)) == 1

    # Straight check
    unique = sorted(set(ranks), reverse=True)
    is_straight = False
    top_straight = 0
    if len(unique) >= 5:
        for i in range(len(unique) - 4):
            if unique[i] - unique[i + 4] == 4:
                is_straight = True
                top_straight = unique[i]
                break
        if not is_straight and {14, 5, 4, 3, 2}.issubset(set(unique)):
            is_straight = True
            top_straight = 5

    freq = {}
    for r in ranks:
        freq[r] = freq.get(r, 0) + 1
    freq_sorted = sorted(freq.items(), key=lambda x: (x[1], x[0]), reverse=True)

    if is_flush and is_straight:
        return (9, top_straight)
    if freq_sorted[0][1] == 4:
        return (8, freq_sorted[0][0], freq_sorted[1][0])
    if freq_sorted[0][1] == 3 and freq_sorted[1][1] == 2:
        return (7, freq_sorted[0][0], freq_sorted[1][0])
    if is_flush:
        return (6,) + tuple(ranks)
    if is_straight:
        return (5, top_straight)
    if freq_sorted[0][1] == 3:
        kickers = sorted([x[0] for x in freq_sorted[1:]], reverse=True)
        return (4, freq_sorted[0][0]) + tuple(kickers)
    if freq_sorted[0][1] == 2 and len(freq_sorted) > 1 and freq_sorted[1][1] == 2:
        high_pair = max(freq_sorted[0][0], freq_sorted[1][0])
        low_pair = min(freq_sorted[0][0], freq_sorted[1][0])
        kicker = freq_sorted[2][0] if len(freq_sorted) > 2 else 0
        return (3, high_pair, low_pair, kicker)
    if freq_sorted[0][1] == 2:
        kickers = sorted([x[0] for x in freq_sorted[1:]], reverse=True)
        return (2, freq_sorted[0][0]) + tuple(kickers)
    return (1,) + tuple(ranks)


def _best5of7(seven_cards):
    best = None
    for combo in itertools.combinations(seven_cards, 5):
        val = _eval5(combo)
        if best is None or val > best:
            best = val
    return best


def compute_equity(player_hands, board=None, trials=100000):
    """Monte Carlo equity calculator. Supports Hold'em, PLO4, PLO5.

    Uses eval7 (C extension) when available for ~30-100x speedup.
    Auto-scales trial count for Omaha to keep runtime reasonable.

    Args:
        player_hands: list of cards per player, e.g. [["As","Kd"], ["Th","Td"]]
                      Supports 2 (Hold'em), 4 (PLO4), or 5 (PLO5) hole cards.
        board: optional list of community cards (0-5)
        trials: number of MC simulations (auto-scaled down for Omaha)

    Returns {"equities": [{"hand","equity","wins","ties"}], "trials": int}
    """
    if board is None:
        board = []

    n_players = len(player_hands)
    n_hole = max((len(h) for h in player_hands), default=0)
    is_omaha = n_hole >= 4
    missing = 5 - len(board)

    # Auto-scale trials for Omaha (many more evaluations per trial)
    if is_omaha:
        combos_per_player = (n_hole * (n_hole - 1) // 2) * 10  # C(n,2) * C(5,3)
        evals_per_trial = n_players * combos_per_player
        trials = min(trials, max(1000, 4_000_000 // max(evals_per_trial, 1)))

    if _HAVE_EVAL7:
        return _equity_eval7(player_hands, board, trials, is_omaha, missing, n_players)
    return _equity_fallback(player_hands, board, trials, is_omaha, missing, n_players)


def _equity_eval7(player_hands, board, trials, is_omaha, missing, n_players):
    """Equity calculation using eval7 C extension."""
    used = set()
    for h in player_hands:
        used.update(h)
    used.update(board)

    e7_hands = [[eval7.Card(c) for c in h] for h in player_hands]
    e7_board = [eval7.Card(c) for c in board]
    deck = [eval7.Card(r + s) for r in RANKS for s in SUITS if (r + s) not in used]

    wins = [0] * n_players
    ties = [0] * n_players
    total = 0

    if missing == 0:
        # Board complete — single evaluation
        if is_omaha:
            scores = [_best_omaha_hand(h, e7_board) for h in e7_hands]
        else:
            scores = [eval7.evaluate(h + e7_board) for h in e7_hands]
        best = max(scores)
        winners = [i for i in range(n_players) if scores[i] == best]
        total = 1
        if len(winners) == 1:
            wins[winners[0]] = 1
        else:
            for w in winners:
                ties[w] = 1

    elif not is_omaha and missing <= 2:
        # Hold'em: exact enumeration when only 1-2 cards to come
        for draw in itertools.combinations(deck, missing):
            fb = e7_board + list(draw)
            scores = [eval7.evaluate(h + fb) for h in e7_hands]
            best = max(scores)
            winners = [i for i in range(n_players) if scores[i] == best]
            total += 1
            if len(winners) == 1:
                wins[winners[0]] += 1
            else:
                for w in winners:
                    ties[w] += 1

    elif is_omaha:
        # Omaha MC with pre-computed combos and reusable eval buffer
        hole_pairs = [list(itertools.combinations(h, 2)) for h in e7_hands]
        board_triple_idx = list(itertools.combinations(range(5), 3))
        buf = [None] * 5

        for _ in range(trials):
            random.shuffle(deck)
            fb = e7_board + deck[:missing]
            b3s = [tuple(fb[i] for i in idx) for idx in board_triple_idx]

            scores = []
            for pairs in hole_pairs:
                best = 0
                for h2 in pairs:
                    buf[0] = h2[0]; buf[1] = h2[1]
                    for b3 in b3s:
                        buf[2] = b3[0]; buf[3] = b3[1]; buf[4] = b3[2]
                        s = eval7.evaluate(buf)
                        if s > best:
                            best = s
                scores.append(best)

            best_score = max(scores)
            winners = [i for i in range(n_players) if scores[i] == best_score]
            total += 1
            if len(winners) == 1:
                wins[winners[0]] += 1
            else:
                for w in winners:
                    ties[w] += 1

    else:
        # Hold'em Monte Carlo
        for _ in range(trials):
            random.shuffle(deck)
            fb = e7_board + deck[:missing]
            scores = [eval7.evaluate(h + fb) for h in e7_hands]
            best = max(scores)
            winners = [i for i in range(n_players) if scores[i] == best]
            total += 1
            if len(winners) == 1:
                wins[winners[0]] += 1
            else:
                for w in winners:
                    ties[w] += 1

    results = []
    for i in range(n_players):
        eq = (wins[i] + ties[i] / 2.0) / total * 100 if total > 0 else 0
        results.append({
            'hand': ' '.join(player_hands[i]),
            'equity': round(eq, 1),
            'wins': wins[i],
            'ties': ties[i],
        })
    return {'equities': results, 'trials': total}


def _equity_fallback(player_hands, board, trials, is_omaha, missing, n_players):
    """Pure Python equity calculation (fallback when eval7 unavailable)."""
    used = set()
    for h in player_hands:
        used.update(h)
    used.update(board)
    deck = [r + s for r in RANKS for s in SUITS if (r + s) not in used]

    if is_omaha:
        trials = min(trials, 3000)

    wins = [0] * n_players
    ties = [0] * n_players
    total = 0

    for _ in range(trials):
        drawn = random.sample(deck, missing)
        fb = board + drawn

        if is_omaha:
            scores = []
            for h in player_hands:
                best = None
                for h2 in itertools.combinations(h, 2):
                    for b3 in itertools.combinations(fb, 3):
                        s = _eval5(list(h2) + list(b3))
                        if best is None or s > best:
                            best = s
                scores.append(best)
        else:
            scores = [_best5of7(h + fb) for h in player_hands]

        best = max(scores)
        winners = [i for i in range(n_players) if scores[i] == best]
        total += 1
        if len(winners) == 1:
            wins[winners[0]] += 1
        else:
            for w in winners:
                ties[w] += 1

    results = []
    for i in range(n_players):
        eq = (wins[i] + ties[i] / 2.0) / total * 100 if total > 0 else 0
        results.append({
            'hand': ' '.join(player_hands[i]),
            'equity': round(eq, 1),
            'wins': wins[i],
            'ties': ties[i],
        })
    return {'equities': results, 'trials': total}
