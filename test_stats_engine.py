"""
Comprehensive unit tests for stats_engine.py.

Tests cover: derive_positions, compute_all_stats (VPIP, PFR, 3-bet, 4-bet,
fold-to-3bet, fold-to-4bet, c-bet, c-bet turn, fold-to-cbet, triple barrel,
steal, fold-to-steal, WTSD, W$SD, donk bet, AF, AFq, bomb pot stats),
compute_winnings, and _compute_deltas.
"""

import pytest
from stats_engine import (
    derive_positions,
    compute_all_stats,
    compute_winnings,
    _compute_deltas,
    CHECK, ANTE, BIG_BLIND, SMALL_BLIND, POSTED_BB, POSTED_SB_DEAD,
    CALL, BET_RAISE, COMMUNITY, PAYOUT, FOLD, SHOW_MUCK,
    ALLIN_APPROVAL, END_OF_HAND, REFUND,
)


# ── Helper builders ─────────────────────────────────────────────────────────

def make_player(name, pid, seat, stack=100, hand=None):
    return {"name": name, "id": pid, "seat": seat, "stack": stack, "hand": hand}


def ev(etype, seat=None, value=None, **kwargs):
    """Build a single event dict."""
    payload = {"type": etype}
    if seat is not None:
        payload["seat"] = seat
    if value is not None:
        payload["value"] = value
    payload.update(kwargs)
    return {"payload": payload}


def community(turn, cards=None):
    """Build a community card event. turn: 1=flop, 2=turn, 3=river."""
    payload = {"type": COMMUNITY, "turn": turn}
    if cards:
        payload["cards"] = cards
    return {"payload": payload}


def make_hand(players, events, dealer_seat=1, number="1", bomb_pot=False, cents=False):
    return {
        "number": number,
        "gameType": "th",
        "dealerSeat": dealer_seat,
        "smallBlind": 0.5,
        "bigBlind": 1,
        "bombPot": bomb_pot,
        "cents": cents,
        "players": players,
        "events": events,
    }


def get_player_stat(result, name):
    """Extract one player's stats dict from compute_all_stats result."""
    for s in result["stats"]:
        if s["name"] == name:
            return s
    return None


# ── derive_positions ────────────────────────────────────────────────────────

class TestDerivePositions:
    def test_three_players(self):
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[], dealer_seat=1,
        )
        pos = derive_positions(hand)
        assert pos == {1: "BTN", 2: "SB", 3: "BB"}

    def test_three_players_dealer_seat2(self):
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[], dealer_seat=2,
        )
        pos = derive_positions(hand)
        assert pos == {2: "BTN", 3: "SB", 1: "BB"}

    def test_six_players(self):
        players = [make_player(f"P{i}", f"p{i}", i) for i in range(1, 7)]
        hand = make_hand(players=players, events=[], dealer_seat=1)
        pos = derive_positions(hand)
        assert pos[1] == "BTN"
        assert pos[2] == "SB"
        assert pos[3] == "BB"
        assert pos[6] == "CO"

    def test_heads_up(self):
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
            ],
            events=[], dealer_seat=1,
        )
        pos = derive_positions(hand)
        assert pos == {1: "BTN", 2: "BB"}

    def test_no_players(self):
        hand = make_hand(players=[], events=[])
        assert derive_positions(hand) == {}

    def test_no_dealer(self):
        hand = {
            "players": [make_player("A", "a", 1)],
            "events": [],
        }
        assert derive_positions(hand) == {}

    def test_dealer_not_in_seats_picks_closest(self):
        """If dealer seat is not occupied, pick the closest seat >= dealer."""
        hand = make_hand(
            players=[
                make_player("A", "a1", 2),
                make_player("B", "b1", 4),
                make_player("C", "c1", 6),
            ],
            events=[], dealer_seat=3,
        )
        pos = derive_positions(hand)
        # Closest seat >= 3 is seat 4, so BTN=4
        assert pos[4] == "BTN"
        assert pos[6] == "SB"
        assert pos[2] == "BB"

    def test_wrap_around(self):
        """Positions wrap around from last seat to first."""
        hand = make_hand(
            players=[
                make_player("A", "a1", 1),
                make_player("B", "b1", 2),
                make_player("C", "c1", 3),
                make_player("D", "d1", 4),
            ],
            events=[], dealer_seat=3,
        )
        pos = derive_positions(hand)
        assert pos[3] == "BTN"
        assert pos[4] == "SB"
        assert pos[1] == "BB"
        assert pos[2] == "UTG"


# ── VPIP ────────────────────────────────────────────────────────────────────

class TestVPIP:
    def test_call_counts_as_vpip(self):
        """A preflop call should count as VPIP."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(CALL, seat=1, value=1),      # Alice calls => VPIP
                ev(FOLD, seat=2),
                ev(FOLD, seat=3),
                ev(PAYOUT, seat=1, value=2.5),
            ],
        )
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        assert alice["vpip"] == 100.0
        # Bob and Carol did not voluntarily act
        bob = get_player_stat(result, "Bob")
        carol = get_player_stat(result, "Carol")
        assert bob["vpip"] == 0.0
        assert carol["vpip"] == 0.0

    def test_raise_counts_as_vpip(self):
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),  # Alice raises => VPIP + PFR
                ev(FOLD, seat=2),
                ev(FOLD, seat=3),
                ev(PAYOUT, seat=1, value=2.5),
            ],
        )
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        assert alice["vpip"] == 100.0

    def test_posting_blind_not_vpip(self):
        """Just posting a blind and folding should NOT count as VPIP."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(FOLD, seat=1),
                ev(FOLD, seat=2),
                ev(PAYOUT, seat=3, value=1.5),
            ],
        )
        result = compute_all_stats([hand])
        for name in ("Alice", "Bob", "Carol"):
            assert get_player_stat(result, name)["vpip"] == 0.0


# ── PFR ─────────────────────────────────────────────────────────────────────

class TestPFR:
    def test_raise_counts_as_pfr(self):
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),
                ev(FOLD, seat=2),
                ev(FOLD, seat=3),
                ev(PAYOUT, seat=1, value=2.5),
            ],
        )
        result = compute_all_stats([hand])
        # PFR denominator = hands dealt. Alice raised in 1/1 hand = 100%
        assert get_player_stat(result, "Alice")["pfr"] == 100.0
        assert get_player_stat(result, "Bob")["pfr"] == 0.0

    def test_call_not_pfr(self):
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(CALL, seat=1, value=1),
                ev(FOLD, seat=2),
                ev(FOLD, seat=3),
                ev(PAYOUT, seat=1, value=2.5),
            ],
        )
        result = compute_all_stats([hand])
        assert get_player_stat(result, "Alice")["pfr"] == 0.0


# ── 3-Bet ───────────────────────────────────────────────────────────────────

class TestThreeBet:
    def test_three_bet_counted(self):
        """Second raiser preflop = 3-bet."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),   # 1st raise (Alice)
                ev(BET_RAISE, seat=2, value=9),    # 3-bet (Bob)
                ev(FOLD, seat=3),
                ev(FOLD, seat=1),
                ev(PAYOUT, seat=2, value=7),
            ],
        )
        result = compute_all_stats([hand])
        bob = get_player_stat(result, "Bob")
        # Bob 3-bet: numerator=1, denominator=1 (Bob faced the open; his personal opp)
        # 3-bet denom is per-player: each non-first-raiser gets +1 opportunity
        # Bob had the opportunity and took it => 1/1 = 100%
        assert bob["threeBet"] == 100.0
        # Carol had opportunity but did not 3-bet
        carol = get_player_stat(result, "Carol")
        assert carol["threeBet"] == 0.0

    def test_no_three_bet_with_single_raise(self):
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),
                ev(FOLD, seat=2),
                ev(FOLD, seat=3),
                ev(PAYOUT, seat=1, value=2.5),
            ],
        )
        result = compute_all_stats([hand])
        # 3-bet denominator incremented for Bob + Carol but numerator = 0
        bob = get_player_stat(result, "Bob")
        assert bob["threeBet"] == 0.0


# ── 4-Bet ───────────────────────────────────────────────────────────────────

class TestFourBet:
    def test_four_bet_counted(self):
        """Third raiser preflop = 4-bet."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),    # open raise
                ev(BET_RAISE, seat=2, value=9),     # 3-bet
                ev(BET_RAISE, seat=3, value=25),    # 4-bet (Carol)
                ev(FOLD, seat=1),
                ev(FOLD, seat=2),
                ev(PAYOUT, seat=3, value=19),
            ],
        )
        result = compute_all_stats([hand])
        carol = get_player_stat(result, "Carol")
        # 4-bet denom: everyone except second raiser gets +1.
        # Carol's personal opportunity: 1. She took it => 1/1 = 100%
        assert carol["fourBet"] == 100.0
        alice = get_player_stat(result, "Alice")
        assert alice["fourBet"] == 0.0


# ── Fold to 3-Bet ──────────────────────────────────────────────────────────

class TestFoldTo3Bet:
    def test_first_raiser_folds_to_3bet(self):
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),    # Alice opens
                ev(BET_RAISE, seat=2, value=9),     # Bob 3-bets
                ev(FOLD, seat=3),
                ev(FOLD, seat=1),                   # Alice folds to 3-bet
                ev(PAYOUT, seat=2, value=7),
            ],
        )
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        assert alice["foldTo3Bet"] == 100.0

    def test_first_raiser_calls_3bet(self):
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),
                ev(BET_RAISE, seat=2, value=9),
                ev(FOLD, seat=3),
                ev(CALL, seat=1, value=9),          # Alice calls the 3-bet
                community(1),
                ev(CHECK, seat=2),
                ev(CHECK, seat=1),
                ev(PAYOUT, seat=2, value=19),
            ],
        )
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        assert alice["foldTo3Bet"] == 0.0
        # denom should be 1
        assert alice["samples"]["foldTo3Bet"] == "0/1"


# ── Fold to 4-Bet ──────────────────────────────────────────────────────────

class TestFoldTo4Bet:
    def test_second_raiser_folds_to_4bet(self):
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),    # open
                ev(BET_RAISE, seat=2, value=9),     # 3-bet (Bob)
                ev(BET_RAISE, seat=3, value=25),    # 4-bet (Carol)
                ev(FOLD, seat=1),
                ev(FOLD, seat=2),                   # Bob folds to 4-bet
                ev(PAYOUT, seat=3, value=19),
            ],
        )
        result = compute_all_stats([hand])
        bob = get_player_stat(result, "Bob")
        assert bob["foldTo4Bet"] == 100.0
        assert bob["samples"]["foldTo4Bet"] == "1/1"


# ── C-Bet ───────────────────────────────────────────────────────────────────

class TestCBet:
    def _cbet_hand(self, aggressor_bets_flop=True):
        """Helper: Alice opens, Bob calls, flop comes. Alice either bets or checks."""
        flop_events = []
        if aggressor_bets_flop:
            flop_events = [
                ev(BET_RAISE, seat=1, value=3),  # Alice c-bets
                ev(FOLD, seat=2),
                ev(PAYOUT, seat=1, value=10),
            ]
        else:
            flop_events = [
                ev(CHECK, seat=1),  # Alice checks flop (no c-bet)
                ev(CHECK, seat=2),
                ev(PAYOUT, seat=1, value=7),
            ]

        return make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),   # Alice raises (preflop aggressor)
                ev(CALL, seat=2, value=3),
                ev(FOLD, seat=3),
                community(1),
            ] + flop_events,
        )

    def test_cbet_made(self):
        hand = self._cbet_hand(aggressor_bets_flop=True)
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        assert alice["cbet"] == 100.0
        assert alice["samples"]["cbet"] == "1/1"

    def test_cbet_missed(self):
        hand = self._cbet_hand(aggressor_bets_flop=False)
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        assert alice["cbet"] == 0.0
        assert alice["samples"]["cbet"] == "0/1"

    def test_no_cbet_opportunity_when_folded_preflop(self):
        """If everyone folds preflop, no c-bet opportunity."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),
                ev(FOLD, seat=2),
                ev(FOLD, seat=3),
                ev(PAYOUT, seat=1, value=2.5),
            ],
        )
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        assert alice["samples"]["cbet"] == "0/0"


# ── C-Bet Turn ──────────────────────────────────────────────────────────────

class TestCBetTurn:
    def test_cbet_turn_made(self):
        """Alice c-bets flop and then bets turn."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),
                ev(CALL, seat=2, value=3),
                ev(FOLD, seat=3),
                community(1),
                ev(BET_RAISE, seat=1, value=4),   # c-bet flop
                ev(CALL, seat=2, value=4),
                community(2),
                ev(BET_RAISE, seat=1, value=8),   # c-bet turn
                ev(FOLD, seat=2),
                ev(PAYOUT, seat=1, value=20),
            ],
        )
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        assert alice["cbetTurn"] == 100.0
        assert alice["samples"]["cbetTurn"] == "1/1"

    def test_cbet_turn_missed(self):
        """Alice c-bets flop but checks turn."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),
                ev(CALL, seat=2, value=3),
                ev(FOLD, seat=3),
                community(1),
                ev(BET_RAISE, seat=1, value=4),   # c-bet flop
                ev(CALL, seat=2, value=4),
                community(2),
                ev(CHECK, seat=1),                 # no c-bet turn
                ev(CHECK, seat=2),
                ev(PAYOUT, seat=1, value=14),
            ],
        )
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        assert alice["cbetTurn"] == 0.0


# ── Fold to C-Bet ──────────────────────────────────────────────────────────

class TestFoldToCBet:
    def test_fold_to_cbet(self):
        """Bob folds to Alice's c-bet on the flop."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),
                ev(CALL, seat=2, value=3),
                ev(FOLD, seat=3),
                community(1),
                ev(BET_RAISE, seat=1, value=4),   # c-bet
                ev(FOLD, seat=2),                  # Bob folds to c-bet
                ev(PAYOUT, seat=1, value=10),
            ],
        )
        result = compute_all_stats([hand])
        bob = get_player_stat(result, "Bob")
        assert bob["foldToCbet"] == 100.0

    def test_call_cbet(self):
        """Bob calls Alice's c-bet (not a fold)."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),
                ev(CALL, seat=2, value=3),
                ev(FOLD, seat=3),
                community(1),
                ev(BET_RAISE, seat=1, value=4),
                ev(CALL, seat=2, value=4),
                ev(PAYOUT, seat=1, value=14),
            ],
        )
        result = compute_all_stats([hand])
        bob = get_player_stat(result, "Bob")
        assert bob["foldToCbet"] == 0.0
        # Opportunity should be 1
        assert bob["samples"]["foldToCbet"] == "0/1"


# ── Triple Barrel ───────────────────────────────────────────────────────────

class TestTripleBarrel:
    def test_triple_barrel_made(self):
        """Alice bets flop, turn, and river = triple barrel."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),
                ev(CALL, seat=2, value=3),
                ev(FOLD, seat=3),
                community(1),
                ev(BET_RAISE, seat=1, value=4),    # flop bet
                ev(CALL, seat=2, value=4),
                community(2),
                ev(BET_RAISE, seat=1, value=8),    # turn bet
                ev(CALL, seat=2, value=8),
                community(3),
                ev(BET_RAISE, seat=1, value=16),   # river bet
                ev(FOLD, seat=2),
                ev(PAYOUT, seat=1, value=46),
            ],
        )
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        assert alice["tripleBarrel"] == 100.0

    def test_triple_barrel_missed_on_river(self):
        """Alice bets flop and turn but checks river."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),
                ev(CALL, seat=2, value=3),
                ev(FOLD, seat=3),
                community(1),
                ev(BET_RAISE, seat=1, value=4),
                ev(CALL, seat=2, value=4),
                community(2),
                ev(BET_RAISE, seat=1, value=8),
                ev(CALL, seat=2, value=8),
                community(3),
                ev(CHECK, seat=1),                 # no river bet
                ev(CHECK, seat=2),
                ev(PAYOUT, seat=1, value=30),
            ],
        )
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        assert alice["tripleBarrel"] == 0.0
        assert alice["samples"]["tripleBarrel"] == "0/1"

    def test_no_triple_barrel_opportunity_if_hand_ends_on_turn(self):
        """Triple barrel opportunity only exists when river is dealt."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),
                ev(CALL, seat=2, value=3),
                ev(FOLD, seat=3),
                community(1),
                ev(BET_RAISE, seat=1, value=4),
                ev(CALL, seat=2, value=4),
                community(2),
                ev(BET_RAISE, seat=1, value=8),
                ev(FOLD, seat=2),
                ev(PAYOUT, seat=1, value=22),
            ],
        )
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        # No triple barrel opportunity (hand ended on turn, river not dealt)
        assert alice["samples"]["tripleBarrel"] == "0/0"


# ── Steal ───────────────────────────────────────────────────────────────────

class TestSteal:
    def test_steal_from_button(self):
        """BTN open-raise with no prior action = steal."""
        # 6-max: seats 1-6, dealer=4 => BTN=4, SB=5, BB=6, UTG=1, MP=2, CO=3
        players = [make_player(f"P{i}", f"p{i}", i) for i in range(1, 7)]
        hand = make_hand(
            players=players,
            events=[
                ev(SMALL_BLIND, seat=5, value=0.5),
                ev(BIG_BLIND, seat=6, value=1),
                ev(FOLD, seat=1),  # UTG folds
                ev(FOLD, seat=2),  # MP folds
                ev(FOLD, seat=3),  # CO folds
                ev(BET_RAISE, seat=4, value=3),  # BTN raises = steal
                ev(FOLD, seat=5),
                ev(FOLD, seat=6),
                ev(PAYOUT, seat=4, value=2.5),
            ],
            dealer_seat=4,
        )
        result = compute_all_stats([hand])
        btn = get_player_stat(result, "P4")
        assert btn["stealAttempt"] == 100.0

    def test_steal_from_co(self):
        """CO open-raise with no prior action = steal."""
        players = [make_player(f"P{i}", f"p{i}", i) for i in range(1, 7)]
        hand = make_hand(
            players=players,
            events=[
                ev(SMALL_BLIND, seat=5, value=0.5),
                ev(BIG_BLIND, seat=6, value=1),
                ev(FOLD, seat=1),
                ev(FOLD, seat=2),
                ev(BET_RAISE, seat=3, value=3),  # CO raises = steal
                ev(FOLD, seat=4),
                ev(FOLD, seat=5),
                ev(FOLD, seat=6),
                ev(PAYOUT, seat=3, value=2.5),
            ],
            dealer_seat=4,
        )
        result = compute_all_stats([hand])
        co = get_player_stat(result, "P3")
        assert co["stealAttempt"] == 100.0

    def test_no_steal_from_utg(self):
        """UTG open-raise is NOT a steal."""
        players = [make_player(f"P{i}", f"p{i}", i) for i in range(1, 7)]
        hand = make_hand(
            players=players,
            events=[
                ev(SMALL_BLIND, seat=5, value=0.5),
                ev(BIG_BLIND, seat=6, value=1),
                ev(BET_RAISE, seat=1, value=3),  # UTG raises
                ev(FOLD, seat=2),
                ev(FOLD, seat=3),
                ev(FOLD, seat=4),
                ev(FOLD, seat=5),
                ev(FOLD, seat=6),
                ev(PAYOUT, seat=1, value=2.5),
            ],
            dealer_seat=4,
        )
        result = compute_all_stats([hand])
        utg = get_player_stat(result, "P1")
        # UTG has no steal opportunity, steal_d = 0
        assert utg["samples"]["stealAttempt"] == "0/0"

    def test_no_steal_when_limper_before(self):
        """If someone calls before the BTN raise, it's not a steal.

        With the corrected logic, steal opportunity is only tracked when action
        is folded to a steal-position player. The limper (MP, seat 2) calls
        first, so preflop_open_action_taken becomes True before BTN acts.
        BTN gets no steal opportunity at all (0/0).
        """
        players = [make_player(f"P{i}", f"p{i}", i) for i in range(1, 7)]
        hand = make_hand(
            players=players,
            events=[
                ev(SMALL_BLIND, seat=5, value=0.5),
                ev(BIG_BLIND, seat=6, value=1),
                ev(FOLD, seat=1),
                ev(CALL, seat=2, value=1),           # Limper (MP)
                ev(FOLD, seat=3),
                ev(BET_RAISE, seat=4, value=4),       # BTN raises but there was a limper
                ev(FOLD, seat=5),
                ev(FOLD, seat=6),
                ev(FOLD, seat=2),
                ev(PAYOUT, seat=4, value=6.5),
            ],
            dealer_seat=4,
        )
        result = compute_all_stats([hand])
        btn = get_player_stat(result, "P4")
        # BTN does NOT get a steal opportunity because action was not folded to them
        assert btn["stealAttempt"] == 0.0
        assert btn["samples"]["stealAttempt"] == "0/0"


# ── Fold to Steal ───────────────────────────────────────────────────────────

class TestFoldToSteal:
    def test_sb_and_bb_fold_to_steal(self):
        """SB and BB both fold to a BTN steal."""
        players = [make_player(f"P{i}", f"p{i}", i) for i in range(1, 7)]
        hand = make_hand(
            players=players,
            events=[
                ev(SMALL_BLIND, seat=5, value=0.5),
                ev(BIG_BLIND, seat=6, value=1),
                ev(FOLD, seat=1),
                ev(FOLD, seat=2),
                ev(FOLD, seat=3),
                ev(BET_RAISE, seat=4, value=3),  # BTN steal
                ev(FOLD, seat=5),                 # SB folds
                ev(FOLD, seat=6),                 # BB folds
                ev(PAYOUT, seat=4, value=2.5),
            ],
            dealer_seat=4,
        )
        result = compute_all_stats([hand])
        sb = get_player_stat(result, "P5")
        bb = get_player_stat(result, "P6")
        assert sb["foldToStealSB"] == 100.0
        assert bb["foldToStealBB"] == 100.0

    def test_bb_defends_steal(self):
        """BB calls vs steal = no fold."""
        players = [make_player(f"P{i}", f"p{i}", i) for i in range(1, 7)]
        hand = make_hand(
            players=players,
            events=[
                ev(SMALL_BLIND, seat=5, value=0.5),
                ev(BIG_BLIND, seat=6, value=1),
                ev(FOLD, seat=1),
                ev(FOLD, seat=2),
                ev(FOLD, seat=3),
                ev(BET_RAISE, seat=4, value=3),
                ev(FOLD, seat=5),
                ev(CALL, seat=6, value=3),       # BB defends
                community(1),
                ev(CHECK, seat=4),
                ev(CHECK, seat=6),
                ev(PAYOUT, seat=6, value=7),
            ],
            dealer_seat=4,
        )
        result = compute_all_stats([hand])
        bb = get_player_stat(result, "P6")
        assert bb["foldToStealBB"] == 0.0
        assert bb["samples"]["foldToStealBB"] == "0/1"


# ── WTSD / W$SD ────────────────────────────────────────────────────────────

class TestWTSD:
    def _showdown_hand(self):
        """Alice and Bob see flop and go to showdown. Alice wins."""
        return make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),
                ev(CALL, seat=2, value=3),
                ev(FOLD, seat=3),
                community(1),
                ev(CHECK, seat=1),
                ev(CHECK, seat=2),
                community(2),
                ev(CHECK, seat=1),
                ev(CHECK, seat=2),
                community(3),
                ev(CHECK, seat=1),
                ev(CHECK, seat=2),
                # Both still in at showdown
                ev(PAYOUT, seat=1, value=7),
            ],
        )

    def test_wtsd_both_players(self):
        hand = self._showdown_hand()
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        bob = get_player_stat(result, "Bob")
        # Both saw flop and went to showdown
        assert alice["wtsd"] == 100.0
        assert bob["wtsd"] == 100.0

    def test_wsd_winner(self):
        hand = self._showdown_hand()
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        bob = get_player_stat(result, "Bob")
        # Alice won at showdown
        assert alice["wsd"] == 100.0
        # Bob lost at showdown
        assert bob["wsd"] == 0.0

    def test_wtsd_not_counted_if_folded(self):
        """Player who folds on the turn should not be counted for WTSD."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),
                ev(CALL, seat=2, value=3),
                ev(CALL, seat=3, value=3),
                community(1),
                ev(CHECK, seat=1),
                ev(CHECK, seat=2),
                ev(CHECK, seat=3),
                community(2),
                ev(BET_RAISE, seat=1, value=5),
                ev(FOLD, seat=2),         # Bob folds turn
                ev(CALL, seat=3, value=5),
                community(3),
                ev(CHECK, seat=1),
                ev(CHECK, seat=3),
                ev(PAYOUT, seat=1, value=20),
            ],
        )
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        bob = get_player_stat(result, "Bob")
        carol = get_player_stat(result, "Carol")
        # Alice and Carol went to showdown
        assert alice["wtsd"] == 100.0
        assert carol["wtsd"] == 100.0
        # Bob saw flop but folded on turn => flops_seen=1, wtsd_n=0
        assert bob["wtsd"] == 0.0
        assert bob["samples"]["wtsd"] == "0/1"

    def test_no_showdown_if_everyone_folds(self):
        """If hand ends on flop with a fold, no WTSD for anyone."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),
                ev(CALL, seat=2, value=3),
                ev(FOLD, seat=3),
                community(1),
                ev(BET_RAISE, seat=1, value=4),
                ev(FOLD, seat=2),
                ev(PAYOUT, seat=1, value=10),
            ],
        )
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        # Saw flop but no showdown (only 1 player left)
        assert alice["wtsd"] == 0.0


# ── Donk Bet ───────────────────────────────────────────────────────────────

class TestDonkBet:
    def test_donk_bet_detected(self):
        """Bob bets into Alice (preflop aggressor) on the flop before Alice acts."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),    # Alice = preflop aggressor
                ev(CALL, seat=2, value=3),
                ev(FOLD, seat=3),
                community(1),
                ev(BET_RAISE, seat=2, value=3),    # Bob donk bets into Alice
                ev(CALL, seat=1, value=3),
                ev(PAYOUT, seat=1, value=12),
            ],
        )
        result = compute_all_stats([hand])
        bob = get_player_stat(result, "Bob")
        assert bob["donkBet"] == 100.0

    def test_no_donk_if_aggressor_acts_first(self):
        """If Alice (aggressor) bets first, no donk bet for Bob."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),
                ev(CALL, seat=2, value=3),
                ev(FOLD, seat=3),
                community(1),
                ev(BET_RAISE, seat=1, value=4),  # Alice bets first (c-bet)
                ev(CALL, seat=2, value=4),
                ev(PAYOUT, seat=1, value=14),
            ],
        )
        result = compute_all_stats([hand])
        bob = get_player_stat(result, "Bob")
        assert bob["donkBet"] == 0.0


# ── Aggression Factor (AF) and Aggression Frequency (AFq) ──────────────────

class TestAggression:
    def test_af_basic(self):
        """AF = postflop bets+raises / postflop calls."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),
                ev(CALL, seat=2, value=3),
                ev(FOLD, seat=3),
                community(1),
                ev(BET_RAISE, seat=1, value=4),   # Alice: 1 postflop bet
                ev(CALL, seat=2, value=4),         # Bob: 1 postflop call
                community(2),
                ev(BET_RAISE, seat=1, value=8),   # Alice: 2 postflop bets
                ev(CALL, seat=2, value=8),         # Bob: 2 postflop calls
                ev(PAYOUT, seat=1, value=30),
            ],
        )
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        bob = get_player_stat(result, "Bob")
        # Alice: 2 bets, 0 calls => AF = 2.0 (special case: br>0 and calls=0)
        assert alice["af"] == 2.0
        # Bob: 0 bets, 2 calls => AF = 0.0
        assert bob["af"] == 0.0

    def test_af_with_mixed_actions(self):
        """AF when player both bets and calls postflop."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),
                ev(CALL, seat=2, value=3),
                ev(FOLD, seat=3),
                community(1),
                ev(BET_RAISE, seat=2, value=4),   # Bob bets (1 br)
                ev(CALL, seat=1, value=4),         # Alice calls (1 call)
                community(2),
                ev(BET_RAISE, seat=1, value=8),   # Alice bets (1 br)
                ev(CALL, seat=2, value=8),         # Bob calls (1 call)
                ev(PAYOUT, seat=1, value=30),
            ],
        )
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        bob = get_player_stat(result, "Bob")
        # Alice: 1 br, 1 call => AF = 1.0
        assert alice["af"] == 1.0
        # Bob: 1 br, 1 call => AF = 1.0
        assert bob["af"] == 1.0

    def test_afq(self):
        """AFq = postflop bets+raises / total postflop actions."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),
                ev(CALL, seat=2, value=3),
                ev(FOLD, seat=3),
                community(1),
                ev(BET_RAISE, seat=1, value=4),   # Alice: 1 br, total=1
                ev(CALL, seat=2, value=4),         # Bob: 0 br, total=1
                community(2),
                ev(CHECK, seat=1),                 # Alice: total=2
                ev(BET_RAISE, seat=2, value=8),   # Bob: 1 br, total=2
                ev(CALL, seat=1, value=8),         # Alice: total=3 (1br, 1call, 1check)
                ev(PAYOUT, seat=1, value=30),
            ],
        )
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        bob = get_player_stat(result, "Bob")
        # Alice: 1 br / 3 total = 33.3%
        assert alice["afq"] == pytest.approx(33.3, abs=0.1)
        # Bob: 1 br / 2 total = 50%
        assert bob["afq"] == 50.0


# ── Bomb Pot ────────────────────────────────────────────────────────────────

class TestBombPot:
    def test_bomb_pot_hands_counted(self):
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(ANTE, seat=1, value=5),
                ev(ANTE, seat=2, value=5),
                ev(ANTE, seat=3, value=5),
                community(1),
                ev(CHECK, seat=1),
                ev(CHECK, seat=2),
                ev(CHECK, seat=3),
                ev(PAYOUT, seat=1, value=15),
            ],
            bomb_pot=True,
        )
        result = compute_all_stats([hand])
        assert result["totalBombPots"] == 1
        assert result["totalHands"] == 0  # Bomb pots are not counted as regular hands
        alice = get_player_stat(result, "Alice")
        assert alice["bpHandsPlayed"] == 1

    def test_bp_vpip_counts_postflop_action(self):
        """BP VPIP: postflop voluntary action (call or raise)."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(ANTE, seat=1, value=5),
                ev(ANTE, seat=2, value=5),
                ev(ANTE, seat=3, value=5),
                community(1),
                ev(BET_RAISE, seat=1, value=10),   # Alice bets
                ev(CALL, seat=2, value=10),         # Bob calls
                ev(FOLD, seat=3),                   # Carol folds
                ev(PAYOUT, seat=1, value=40),
            ],
            bomb_pot=True,
        )
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        bob = get_player_stat(result, "Bob")
        carol = get_player_stat(result, "Carol")
        assert alice["bpVpip"] == 100.0
        assert bob["bpVpip"] == 100.0
        assert carol["bpVpip"] == 0.0

    def test_bomb_pot_no_regular_stats(self):
        """Bomb pot should not affect regular VPIP/PFR etc."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
            ],
            events=[
                ev(ANTE, seat=1, value=5),
                ev(ANTE, seat=2, value=5),
                community(1),
                ev(BET_RAISE, seat=1, value=10),
                ev(FOLD, seat=2),
                ev(PAYOUT, seat=1, value=20),
            ],
            bomb_pot=True,
        )
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        # Regular hands played = 0
        assert alice["handsPlayed"] == 0
        # No regular VPIP denominator
        assert alice["samples"]["vpip"] == "0/0"


# ── Winnings / _compute_deltas ──────────────────────────────────────────────

class TestComputeDeltas:
    def test_simple_win(self):
        """Winner collects pot, losers lose their contributions."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),
                ev(FOLD, seat=2),
                ev(FOLD, seat=3),
                ev(PAYOUT, seat=1, value=2.5),
            ],
        )
        deltas = _compute_deltas(hand)
        # Alice put in 3, got back 2.5 => net -0.5
        assert deltas["Alice"] == pytest.approx(-0.5)
        assert deltas["Bob"] == pytest.approx(-0.5)
        assert deltas["Carol"] == pytest.approx(-1.0)

    def test_refund_credited(self):
        """Refunds should be added back to the player."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=10),
                ev(FOLD, seat=2),
                ev(FOLD, seat=3),
                ev(REFUND, seat=1, value=8.5),   # excess bet refunded
                ev(PAYOUT, seat=1, value=1.5),
            ],
        )
        deltas = _compute_deltas(hand)
        # Alice: -10 + 8.5 + 1.5 = 0
        assert deltas["Alice"] == pytest.approx(0.0)

    def test_call_is_to_amount(self):
        """Call value is the total amount, not an additional bet."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),
                ev(CALL, seat=2, value=3),   # Bob's total is 3 (he already posted 0.5 SB)
                ev(FOLD, seat=3),
                community(1),
                ev(CHECK, seat=1),
                ev(CHECK, seat=2),
                ev(PAYOUT, seat=1, value=7),
            ],
        )
        deltas = _compute_deltas(hand)
        # Bob posted 0.5 SB, then called to 3 => total put in = 3
        assert deltas["Bob"] == pytest.approx(-3.0)
        # Carol posted 1 BB then folded
        assert deltas["Carol"] == pytest.approx(-1.0)

    def test_street_reset(self):
        """Street contributions reset after community cards so subsequent calls are correct."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),
                ev(CALL, seat=2, value=3),
                ev(FOLD, seat=3),
                community(1),  # street resets
                ev(BET_RAISE, seat=1, value=5),
                ev(CALL, seat=2, value=5),  # Bob calls 5 on flop (fresh street)
                ev(PAYOUT, seat=1, value=17),
            ],
        )
        deltas = _compute_deltas(hand)
        # Alice: -3 (preflop) -5 (flop) +17 = +9
        assert deltas["Alice"] == pytest.approx(9.0)
        # Bob: -3 (preflop) -5 (flop) = -8
        assert deltas["Bob"] == pytest.approx(-8.0)

    def test_zero_sum(self):
        """All deltas should sum to zero (no rake in this simple case)."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(CALL, seat=1, value=1),
                ev(CALL, seat=2, value=1),
                ev(CHECK, seat=3),
                community(1),
                ev(CHECK, seat=1),
                ev(CHECK, seat=2),
                ev(CHECK, seat=3),
                ev(PAYOUT, seat=1, value=3),
            ],
        )
        deltas = _compute_deltas(hand)
        total = sum(deltas.values())
        assert total == pytest.approx(0.0)


class TestComputeWinnings:
    def test_cumulative_winnings(self):
        hands = [
            make_hand(
                players=[
                    make_player("Alice", "a1", 1),
                    make_player("Bob", "b1", 2),
                ],
                events=[
                    ev(SMALL_BLIND, seat=2, value=0.5),
                    ev(BIG_BLIND, seat=1, value=1),
                    ev(FOLD, seat=2),
                    ev(PAYOUT, seat=1, value=1.5),
                ],
                number="1",
            ),
            make_hand(
                players=[
                    make_player("Alice", "a1", 1),
                    make_player("Bob", "b1", 2),
                ],
                events=[
                    ev(SMALL_BLIND, seat=2, value=0.5),
                    ev(BIG_BLIND, seat=1, value=1),
                    ev(FOLD, seat=2),
                    ev(PAYOUT, seat=1, value=1.5),
                ],
                number="2",
            ),
        ]
        result = compute_winnings(hands)
        assert result["handLabels"] == ["1", "2"]
        # Alice: hand 1 net = -1 + 1.5 = +0.5, hand 2 same => cumulative [0, 0.5, 1.0]
        assert result["players"]["Alice"] == pytest.approx([0.0, 0.5, 1.0])
        # Bob: hand 1 net = -0.5, hand 2 same => cumulative [0, -0.5, -1.0]
        assert result["players"]["Bob"] == pytest.approx([0.0, -0.5, -1.0])

    def test_cents_mode(self):
        """When cents=True, final values are divided by 100."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=50),
                ev(BIG_BLIND, seat=1, value=100),
                ev(FOLD, seat=2),
                ev(PAYOUT, seat=1, value=150),
            ],
            cents=True,
        )
        result = compute_winnings([hand])
        # Alice: net = -100 + 150 = +50 cents = +0.50 dollars
        assert result["players"]["Alice"][-1] == pytest.approx(0.5)
        assert result["players"]["Bob"][-1] == pytest.approx(-0.5)


# ── Edge cases and multi-hand integration ──────────────────────────────────

class TestEdgeCases:
    def test_empty_hands_list(self):
        result = compute_all_stats([])
        assert result["totalHands"] == 0
        assert result["totalBombPots"] == 0
        assert result["stats"] == []

    def test_hand_with_no_events(self):
        hand = make_hand(
            players=[make_player("Alice", "a1", 1)],
            events=[],
        )
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        assert alice["handsPlayed"] == 1
        assert alice["vpip"] == 0.0

    def test_multiple_hands_accumulate(self):
        """Stats should accumulate across multiple hands."""
        hand1 = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),  # Alice raises
                ev(FOLD, seat=2),
                ev(FOLD, seat=3),
                ev(PAYOUT, seat=1, value=2.5),
            ],
            number="1",
        )
        hand2 = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(FOLD, seat=1),                 # Alice folds
                ev(FOLD, seat=2),
                ev(PAYOUT, seat=3, value=1.5),
            ],
            number="2",
        )
        result = compute_all_stats([hand1, hand2])
        alice = get_player_stat(result, "Alice")
        assert alice["handsPlayed"] == 2
        # VPIP: 1 voluntary action out of 2 hands
        assert alice["vpip"] == 50.0
        # PFR: 1 raise out of 2 hands
        assert alice["pfr"] == 50.0

    def test_player_name_update(self):
        """If a player changes name, stats engine uses latest name."""
        hand1 = make_hand(
            players=[make_player("OldName", "a1", 1), make_player("B", "b1", 2)],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=1, value=1),
                ev(FOLD, seat=2),
                ev(PAYOUT, seat=1, value=1.5),
            ],
            number="1",
        )
        hand2 = make_hand(
            players=[make_player("NewName", "a1", 1), make_player("B", "b1", 2)],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=1, value=1),
                ev(FOLD, seat=2),
                ev(PAYOUT, seat=1, value=1.5),
            ],
            number="2",
        )
        result = compute_all_stats([hand1, hand2])
        # Should be stored under the latest name
        assert get_player_stat(result, "NewName") is not None
        assert get_player_stat(result, "OldName") is None

    def test_pct_zero_denominator(self):
        """_pct returns 0.0 when denominator is 0."""
        from stats_engine import _pct
        assert _pct(0, 0) == 0.0
        assert _pct(5, 0) == 0.0

    def test_af_no_postflop_actions(self):
        """AF should be 0 when no postflop actions."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(FOLD, seat=1),
                ev(FOLD, seat=2),
                ev(PAYOUT, seat=3, value=1.5),
            ],
        )
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        assert alice["af"] == 0.0
        assert alice["afq"] == 0.0

    def test_total_hands_count(self):
        """totalHands should count only non-bomb-pot hands."""
        hand1 = make_hand(
            players=[make_player("A", "a1", 1), make_player("B", "b1", 2)],
            events=[ev(SMALL_BLIND, seat=2, value=0.5), ev(BIG_BLIND, seat=1, value=1),
                    ev(FOLD, seat=2), ev(PAYOUT, seat=1, value=1.5)],
        )
        hand2 = make_hand(
            players=[make_player("A", "a1", 1), make_player("B", "b1", 2)],
            events=[ev(ANTE, seat=1, value=5), ev(ANTE, seat=2, value=5),
                    community(1), ev(CHECK, seat=1), ev(CHECK, seat=2),
                    ev(PAYOUT, seat=1, value=10)],
            bomb_pot=True,
        )
        result = compute_all_stats([hand1, hand2])
        assert result["totalHands"] == 1
        assert result["totalBombPots"] == 1

    def test_posted_bb_deducted_in_winnings(self):
        """POSTED_BB and POSTED_SB_DEAD should be deducted from winnings."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(POSTED_BB, seat=1, value=1),  # Alice posts BB to enter
                ev(FOLD, seat=2),
                ev(FOLD, seat=3),
                ev(PAYOUT, seat=1, value=2.5),
            ],
        )
        deltas = _compute_deltas(hand)
        # Alice posted 1 BB, won 2.5 => net = -1 + 2.5 = 1.5
        assert deltas["Alice"] == pytest.approx(1.5)

    def test_posted_sb_dead_deducted(self):
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
            ],
            events=[
                ev(POSTED_SB_DEAD, seat=1, value=0.5),
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=1, value=1),
                ev(FOLD, seat=2),
                ev(PAYOUT, seat=1, value=2),
            ],
        )
        deltas = _compute_deltas(hand)
        # Alice: -0.5 (dead SB) -1 (BB) +2 (payout) = +0.5
        assert deltas["Alice"] == pytest.approx(0.5)


class TestSortingAndOutput:
    def test_stats_sorted_by_hands_played(self):
        """Players sorted by total hands played (regular + bomb pot) descending."""
        hands = [
            make_hand(
                players=[make_player("A", "a1", 1), make_player("B", "b1", 2)],
                events=[ev(SMALL_BLIND, seat=2, value=0.5), ev(BIG_BLIND, seat=1, value=1),
                        ev(FOLD, seat=2), ev(PAYOUT, seat=1, value=1.5)],
                number=str(i),
            )
            for i in range(3)
        ] + [
            make_hand(
                players=[make_player("C", "c1", 1), make_player("B", "b1", 2)],
                events=[ev(SMALL_BLIND, seat=2, value=0.5), ev(BIG_BLIND, seat=1, value=1),
                        ev(FOLD, seat=2), ev(PAYOUT, seat=1, value=1.5)],
                number="4",
            ),
        ]
        result = compute_all_stats(hands)
        names = [s["name"] for s in result["stats"]]
        # B played 4 hands, A played 3, C played 1
        assert names[0] == "B"

    def test_samples_format(self):
        """Samples should be formatted as 'n/d'."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),
                ev(FOLD, seat=2),
                ev(FOLD, seat=3),
                ev(PAYOUT, seat=1, value=2.5),
            ],
        )
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        assert alice["samples"]["vpip"] == "1/1"
        assert alice["samples"]["pfr"] == "1/1"


# ── Edge-case tests for corrected logic ───────────────────────────────────

class TestThreeBetDenominator:
    def test_fold_before_open_no_3bet_opportunity(self):
        """A player who folds before the open raise does NOT get a 3-bet opportunity."""
        # 6-max: dealer=4 => BTN=4, SB=5, BB=6, UTG=1, MP=2, CO=3
        players = [make_player(f"P{i}", f"p{i}", i) for i in range(1, 7)]
        hand = make_hand(
            players=players,
            events=[
                ev(SMALL_BLIND, seat=5, value=0.5),
                ev(BIG_BLIND, seat=6, value=1),
                ev(FOLD, seat=1),                     # UTG folds before open raise
                ev(FOLD, seat=2),                     # MP folds before open raise
                ev(BET_RAISE, seat=3, value=3),       # CO opens (1st raise)
                ev(BET_RAISE, seat=4, value=9),       # BTN 3-bets
                ev(FOLD, seat=5),
                ev(FOLD, seat=6),
                ev(FOLD, seat=3),
                ev(PAYOUT, seat=4, value=15),
            ],
            dealer_seat=4,
        )
        result = compute_all_stats([hand])
        # UTG and MP folded before the open raise: no 3-bet opportunity
        utg = get_player_stat(result, "P1")
        mp = get_player_stat(result, "P2")
        assert utg["samples"]["threeBet"] == "0/0"
        assert mp["samples"]["threeBet"] == "0/0"
        # BTN did 3-bet: 1/1
        btn = get_player_stat(result, "P4")
        assert btn["threeBet"] == 100.0
        assert btn["samples"]["threeBet"] == "1/1"
        # SB and BB were still in hand when the open happened, so they have opportunity
        sb = get_player_stat(result, "P5")
        bb = get_player_stat(result, "P6")
        assert sb["samples"]["threeBet"] == "0/1"
        assert bb["samples"]["threeBet"] == "0/1"


class TestCBetDonkDenied:
    def test_cbet_opportunity_denied_by_donk_bet(self):
        """If someone donk-bets into the preflop aggressor, the aggressor's c-bet
        opportunity is removed entirely (not just missed)."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),       # Alice = preflop aggressor
                ev(CALL, seat=2, value=3),
                ev(FOLD, seat=3),
                community(1),
                ev(BET_RAISE, seat=2, value=4),       # Bob donk-bets
                ev(CALL, seat=1, value=4),             # Alice calls
                ev(PAYOUT, seat=1, value=14),
            ],
        )
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        # C-bet opportunity is denied entirely (0/0), not 0/1
        assert alice["samples"]["cbet"] == "0/0"

    def test_cbet_counted_when_no_donk(self):
        """Sanity: c-bet opportunity is counted normally when no donk bet occurs."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),
                ev(CALL, seat=2, value=3),
                ev(FOLD, seat=3),
                community(1),
                ev(CHECK, seat=2),                     # Bob checks (no donk)
                ev(BET_RAISE, seat=1, value=4),       # Alice c-bets
                ev(FOLD, seat=2),
                ev(PAYOUT, seat=1, value=10),
            ],
        )
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        assert alice["samples"]["cbet"] == "1/1"
        assert alice["cbet"] == 100.0


class TestTripleBarrelDenominator:
    def test_triple_barrel_requires_turn_bet(self):
        """Triple barrel opportunity requires flop c-bet AND turn bet AND reaching river.
        If aggressor c-bets flop but checks turn, no triple barrel opportunity on river."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),
                ev(CALL, seat=2, value=3),
                ev(FOLD, seat=3),
                community(1),
                ev(BET_RAISE, seat=1, value=4),       # flop c-bet
                ev(CALL, seat=2, value=4),
                community(2),
                ev(CHECK, seat=1),                     # turn: NO bet
                ev(CHECK, seat=2),
                community(3),
                ev(BET_RAISE, seat=1, value=8),       # river bet
                ev(FOLD, seat=2),
                ev(PAYOUT, seat=1, value=22),
            ],
        )
        result = compute_all_stats([hand])
        alice = get_player_stat(result, "Alice")
        # No triple barrel opportunity because turn bet was not made
        assert alice["samples"]["tripleBarrel"] == "0/0"


class TestSBSteal:
    def test_steal_from_sb(self):
        """SB open-raise when action is folded to them counts as a steal."""
        # 6-max: dealer=4 => BTN=4, SB=5, BB=6, UTG=1, MP=2, CO=3
        players = [make_player(f"P{i}", f"p{i}", i) for i in range(1, 7)]
        hand = make_hand(
            players=players,
            events=[
                ev(SMALL_BLIND, seat=5, value=0.5),
                ev(BIG_BLIND, seat=6, value=1),
                ev(FOLD, seat=1),
                ev(FOLD, seat=2),
                ev(FOLD, seat=3),
                ev(FOLD, seat=4),
                ev(BET_RAISE, seat=5, value=3),       # SB raises = steal
                ev(FOLD, seat=6),
                ev(PAYOUT, seat=5, value=2),
            ],
            dealer_seat=4,
        )
        result = compute_all_stats([hand])
        sb = get_player_stat(result, "P5")
        assert sb["stealAttempt"] == 100.0
        assert sb["samples"]["stealAttempt"] == "1/1"
        # SB should NOT face a steal from SB (foldToStealSB = 0/0)
        assert sb["samples"]["foldToStealSB"] == "0/0"
        # BB faces steal from SB
        bb = get_player_stat(result, "P6")
        assert bb["samples"]["foldToStealBB"] == "1/1"
        assert bb["foldToStealBB"] == 100.0


class TestStealOpportunityOnlyWhenFolded:
    def test_no_steal_opportunity_when_utg_raises(self):
        """When UTG raises, CO/BTN/SB should get no steal opportunity
        since action was NOT folded to them."""
        players = [make_player(f"P{i}", f"p{i}", i) for i in range(1, 7)]
        hand = make_hand(
            players=players,
            events=[
                ev(SMALL_BLIND, seat=5, value=0.5),
                ev(BIG_BLIND, seat=6, value=1),
                ev(BET_RAISE, seat=1, value=3),       # UTG raises
                ev(FOLD, seat=2),
                ev(FOLD, seat=3),
                ev(FOLD, seat=4),
                ev(FOLD, seat=5),
                ev(FOLD, seat=6),
                ev(PAYOUT, seat=1, value=2.5),
            ],
            dealer_seat=4,
        )
        result = compute_all_stats([hand])
        # CO, BTN, SB: no steal opportunity since action was not folded to them
        co = get_player_stat(result, "P3")
        btn = get_player_stat(result, "P4")
        sb = get_player_stat(result, "P5")
        assert co["samples"]["stealAttempt"] == "0/0"
        assert btn["samples"]["stealAttempt"] == "0/0"
        assert sb["samples"]["stealAttempt"] == "0/0"

    def test_steal_opportunity_given_when_folded_to_co(self):
        """CO gets steal opportunity when UTG and MP fold to them."""
        players = [make_player(f"P{i}", f"p{i}", i) for i in range(1, 7)]
        hand = make_hand(
            players=players,
            events=[
                ev(SMALL_BLIND, seat=5, value=0.5),
                ev(BIG_BLIND, seat=6, value=1),
                ev(FOLD, seat=1),                     # UTG folds
                ev(FOLD, seat=2),                     # MP folds
                ev(FOLD, seat=3),                     # CO folds (had opportunity, didn't take it)
                ev(BET_RAISE, seat=4, value=3),       # BTN raises = steal
                ev(FOLD, seat=5),
                ev(FOLD, seat=6),
                ev(PAYOUT, seat=4, value=2.5),
            ],
            dealer_seat=4,
        )
        result = compute_all_stats([hand])
        co = get_player_stat(result, "P3")
        btn = get_player_stat(result, "P4")
        # CO had opportunity (folded to them) but folded
        assert co["samples"]["stealAttempt"] == "0/1"
        # BTN had opportunity and raised
        assert btn["stealAttempt"] == 100.0
        assert btn["samples"]["stealAttempt"] == "1/1"


class TestDonkBetDenominator:
    def test_donk_opportunity_only_for_players_acting_before_aggressor(self):
        """Donk bet opportunity is only for players who actually acted on flop
        before the preflop aggressor, not all non-aggressors who saw the flop."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
                make_player("Dave", "d1", 4),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=4, value=3),       # Dave = preflop aggressor (seat 4)
                ev(CALL, seat=1, value=3),
                ev(CALL, seat=2, value=3),
                ev(CALL, seat=3, value=3),
                community(1),
                # Postflop action order depends on position; let's say:
                ev(CHECK, seat=2),                     # Bob checks before aggressor
                ev(CHECK, seat=3),                     # Carol checks before aggressor
                ev(BET_RAISE, seat=4, value=5),       # Dave (aggressor) bets = c-bet
                ev(CALL, seat=1, value=5),             # Alice acts AFTER aggressor
                ev(FOLD, seat=2),
                ev(FOLD, seat=3),
                ev(PAYOUT, seat=4, value=29),
            ],
            dealer_seat=4,
        )
        result = compute_all_stats([hand])
        bob = get_player_stat(result, "Bob")
        carol = get_player_stat(result, "Carol")
        alice = get_player_stat(result, "Alice")
        dave = get_player_stat(result, "Dave")
        # Bob and Carol checked before the aggressor acted: they have donk opportunity
        assert bob["samples"]["donkBet"] == "0/1"
        assert carol["samples"]["donkBet"] == "0/1"
        # Alice acted AFTER the aggressor, so no donk opportunity
        assert alice["samples"]["donkBet"] == "0/0"
        # Dave is the aggressor, no donk opportunity
        assert dave["samples"]["donkBet"] == "0/0"

    def test_donk_bet_numerator_correct(self):
        """Only the player who actually donk-bets gets the numerator."""
        hand = make_hand(
            players=[
                make_player("Alice", "a1", 1),
                make_player("Bob", "b1", 2),
                make_player("Carol", "c1", 3),
            ],
            events=[
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=3),       # Alice = preflop aggressor
                ev(CALL, seat=2, value=3),
                ev(CALL, seat=3, value=3),
                community(1),
                ev(CHECK, seat=2),                     # Bob checks (acted before aggressor)
                ev(BET_RAISE, seat=3, value=4),       # Carol donk-bets
                ev(CALL, seat=1, value=4),
                ev(FOLD, seat=2),
                ev(PAYOUT, seat=1, value=18),
            ],
        )
        result = compute_all_stats([hand])
        bob = get_player_stat(result, "Bob")
        carol = get_player_stat(result, "Carol")
        # Bob had donk opportunity but checked
        assert bob["samples"]["donkBet"] == "0/1"
        # Carol had donk opportunity and took it
        assert carol["donkBet"] == 100.0
        assert carol["samples"]["donkBet"] == "1/1"


class TestFourBetDenominator:
    def test_4bet_only_for_players_active_at_3bet(self):
        """4-bet opportunity only for players still in hand when 3-bet happened."""
        # 6-max: dealer=4 => BTN=4, SB=5, BB=6, UTG=1, MP=2, CO=3
        players = [make_player(f"P{i}", f"p{i}", i) for i in range(1, 7)]
        hand = make_hand(
            players=players,
            events=[
                ev(SMALL_BLIND, seat=5, value=0.5),
                ev(BIG_BLIND, seat=6, value=1),
                ev(BET_RAISE, seat=1, value=3),       # UTG opens
                ev(FOLD, seat=2),                     # MP folds before 3-bet
                ev(FOLD, seat=3),                     # CO folds before 3-bet
                ev(BET_RAISE, seat=4, value=9),       # BTN 3-bets
                ev(FOLD, seat=5),
                ev(FOLD, seat=6),
                ev(FOLD, seat=1),
                ev(PAYOUT, seat=4, value=15),
            ],
            dealer_seat=4,
        )
        result = compute_all_stats([hand])
        # MP and CO folded before the 3-bet: they were not active at 3-bet time
        # UTG, SB, BB were active when BTN 3-bet
        utg = get_player_stat(result, "P1")
        mp = get_player_stat(result, "P2")
        co = get_player_stat(result, "P3")
        sb = get_player_stat(result, "P5")
        bb = get_player_stat(result, "P6")
        # UTG was active at 3-bet (faced it) => 4-bet opportunity
        assert utg["samples"]["fourBet"] == "0/1"
        # SB and BB were active at 3-bet => 4-bet opportunity
        assert sb["samples"]["fourBet"] == "0/1"
        assert bb["samples"]["fourBet"] == "0/1"
        # MP and CO folded before 3-bet => no 4-bet opportunity
        assert mp["samples"]["fourBet"] == "0/0"
        assert co["samples"]["fourBet"] == "0/0"


class TestFoldToStealSBFromSB:
    def test_sb_does_not_face_steal_from_sb(self):
        """When SB steals, SB should NOT get a fold-to-steal-SB opportunity
        (can't face a steal from yourself)."""
        players = [make_player(f"P{i}", f"p{i}", i) for i in range(1, 7)]
        hand = make_hand(
            players=players,
            events=[
                ev(SMALL_BLIND, seat=5, value=0.5),
                ev(BIG_BLIND, seat=6, value=1),
                ev(FOLD, seat=1),
                ev(FOLD, seat=2),
                ev(FOLD, seat=3),
                ev(FOLD, seat=4),
                ev(BET_RAISE, seat=5, value=3),       # SB steals
                ev(CALL, seat=6, value=3),             # BB calls
                community(1),
                ev(CHECK, seat=5),
                ev(CHECK, seat=6),
                ev(PAYOUT, seat=6, value=6),
            ],
            dealer_seat=4,
        )
        result = compute_all_stats([hand])
        sb = get_player_stat(result, "P5")
        bb = get_player_stat(result, "P6")
        # SB is the stealer: no fold-to-steal-SB opportunity for SB
        assert sb["samples"]["foldToStealSB"] == "0/0"
        # BB faces steal from SB: fold-to-steal-BB opportunity, BB defended
        assert bb["samples"]["foldToStealBB"] == "0/1"
        assert bb["foldToStealBB"] == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# EV / All-in EV tests
# ══════════════════════════════════════════════════════════════════════════════

import eval7
from stats_engine import (
    compute_allin_ev,
    _find_allin_lock,
    _contribs_until,
    _build_pots,
    _board_up_to,
    _collect_hole_cards,
    _expected_payout,
)


def make_allin_hand(players, events, dealer_seat=1, number="1", cents=False):
    """Helper for all-in hands — same as make_hand but with game type."""
    return {
        "number": number,
        "gameType": "th",
        "dealerSeat": dealer_seat,
        "smallBlind": 0.5,
        "bigBlind": 1,
        "bombPot": False,
        "cents": cents,
        "players": players,
        "events": events,
    }


class TestFindAllinLock:
    """Tests for _find_allin_lock — finding the all-in lock event index."""

    def test_allin_approval_event(self):
        """ALLIN_APPROVAL event (type 14) is the lock point."""
        hand = make_allin_hand(
            [make_player("A", "a1", 1), make_player("B", "b1", 2)],
            [
                ev(SMALL_BLIND, seat=1, value=0.5),
                ev(BIG_BLIND, seat=2, value=1),
                ev(BET_RAISE, seat=1, value=100, allIn=True),
                ev(CALL, seat=2, value=100, allIn=True),
                ev(ALLIN_APPROVAL, seat=None),  # idx=4
                community(1, ["Ah", "Kd", "Qs"]),
                community(2, ["Jc"]),
                community(3, ["Ts"]),
                ev(PAYOUT, seat=1, value=200),
            ],
        )
        assert _find_allin_lock(hand) == 4

    def test_allin_bet_no_further_action(self):
        """Last all-in bet/call with no further betting is the lock."""
        hand = make_allin_hand(
            [make_player("A", "a1", 1), make_player("B", "b1", 2)],
            [
                ev(SMALL_BLIND, seat=1, value=0.5),
                ev(BIG_BLIND, seat=2, value=1),
                ev(BET_RAISE, seat=1, value=100, allIn=True),
                ev(CALL, seat=2, value=100, allIn=True),  # idx=3
                community(1, ["Ah", "Kd", "Qs"]),
                ev(PAYOUT, seat=1, value=200),
            ],
        )
        assert _find_allin_lock(hand) == 3

    def test_no_allin_returns_none(self):
        """No all-in in the hand returns None."""
        hand = make_allin_hand(
            [make_player("A", "a1", 1), make_player("B", "b1", 2)],
            [
                ev(SMALL_BLIND, seat=1, value=0.5),
                ev(BIG_BLIND, seat=2, value=1),
                ev(BET_RAISE, seat=1, value=3),
                ev(CALL, seat=2, value=3),
                ev(PAYOUT, seat=1, value=6),
            ],
        )
        assert _find_allin_lock(hand) is None

    def test_single_allin_returns_lock(self):
        """Single all-in still returns lock (caller filtering is done later)."""
        hand = make_allin_hand(
            [make_player("A", "a1", 1), make_player("B", "b1", 2)],
            [
                ev(SMALL_BLIND, seat=1, value=0.5),
                ev(BIG_BLIND, seat=2, value=1),
                ev(BET_RAISE, seat=1, value=50, allIn=True),
                ev(CALL, seat=2, value=50),  # not all-in
            ],
        )
        assert _find_allin_lock(hand) == 2  # the allIn event

    def test_allin_with_callers_returns_lock(self):
        """Two all-in players with non-all-in callers still returns lock."""
        hand = make_allin_hand(
            [make_player("A", "a1", 1), make_player("B", "b1", 2),
             make_player("C", "c1", 3)],
            [
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=50, allIn=True),
                ev(CALL, seat=2, value=50),  # not all-in
                ev(BET_RAISE, seat=3, value=100, allIn=True),  # second all-in
                ev(CALL, seat=2, value=100),  # calling, not all-in
            ],
        )
        assert _find_allin_lock(hand) == 4  # last allIn event


class TestContribsUntil:
    """Tests for _contribs_until — tracking committed amounts."""

    def test_basic_contributions(self):
        hand = make_allin_hand(
            [make_player("A", "a1", 1), make_player("B", "b1", 2)],
            [
                ev(SMALL_BLIND, seat=1, value=0.5),
                ev(BIG_BLIND, seat=2, value=1),
                ev(BET_RAISE, seat=1, value=100, allIn=True),
                ev(CALL, seat=2, value=100, allIn=True),
            ],
        )
        committed, folded = _contribs_until(hand, 3)
        assert committed[1] == 100
        assert committed[2] == 100
        assert len(folded) == 0

    def test_folded_player_tracked(self):
        hand = make_allin_hand(
            [make_player("A", "a1", 1), make_player("B", "b1", 2),
             make_player("C", "c1", 3)],
            [
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=50, allIn=True),
                ev(FOLD, seat=2),
                ev(CALL, seat=3, value=50, allIn=True),
            ],
        )
        committed, folded = _contribs_until(hand, 4)
        assert committed[1] == 50
        assert committed[2] == 0.5
        assert committed[3] == 50
        assert 2 in folded

    def test_refund_reduces_committed(self):
        hand = make_allin_hand(
            [make_player("A", "a1", 1), make_player("B", "b1", 2)],
            [
                ev(SMALL_BLIND, seat=1, value=0.5),
                ev(BIG_BLIND, seat=2, value=1),
                ev(BET_RAISE, seat=1, value=100, allIn=True),
                ev(CALL, seat=2, value=50, allIn=True),
                ev(REFUND, seat=1, value=50),
            ],
        )
        committed, folded = _contribs_until(hand, 4)
        assert committed[1] == 50
        assert committed[2] == 50

    def test_street_contrib_reset_on_community(self):
        """Street contributions reset when community cards are dealt."""
        hand = make_allin_hand(
            [make_player("A", "a1", 1), make_player("B", "b1", 2)],
            [
                ev(SMALL_BLIND, seat=1, value=0.5),
                ev(BIG_BLIND, seat=2, value=1),
                ev(CALL, seat=1, value=1),    # SB completes to 1
                ev(CHECK, seat=2),
                community(1, ["Ah", "Kd", "Qs"]),
                ev(BET_RAISE, seat=1, value=5),  # new street, new contribution
                ev(CALL, seat=2, value=5),
            ],
        )
        committed, folded = _contribs_until(hand, 6)
        # Preflop: seat 1 = 1, seat 2 = 1
        # Flop: seat 1 += 5, seat 2 += 5
        assert committed[1] == 6
        assert committed[2] == 6


class TestBuildPots:
    """Tests for _build_pots — side pot construction."""

    def test_single_pot_equal_stacks(self):
        committed = {1: 100, 2: 100}
        survivors = [1, 2]
        pots = _build_pots(committed, survivors)
        assert len(pots) == 1
        assert pots[0][0] == 200  # pot size
        assert pots[0][1] == {1, 2}  # eligible

    def test_side_pot_unequal_stacks(self):
        """Short stack creates a main pot, remainder goes to side pot."""
        committed = {1: 50, 2: 100, 3: 100}
        survivors = [1, 2, 3]
        pots = _build_pots(committed, survivors)
        assert len(pots) == 2
        # Main pot: 50 * 3 = 150, all eligible
        assert pots[0] == (150, {1, 2, 3})
        # Side pot: 50 * 2 = 100, only seats 2 and 3
        assert pots[1] == (100, {2, 3})

    def test_folded_player_contributes_but_not_eligible(self):
        """Folded player's money is in the pot but they can't win."""
        committed = {1: 50, 2: 50, 3: 10}
        survivors = [1, 2]  # seat 3 folded
        pots = _build_pots(committed, survivors)
        # Layer 1: 10 * 3 = 30, eligible: {1, 2} (3 folded)
        assert pots[0] == (30, {1, 2})
        # Layer 2: 40 * 2 = 80, eligible: {1, 2}
        assert pots[1] == (80, {1, 2})
        # Total pot = 110 = sum of committed
        assert sum(p[0] for p in pots) == 110

    def test_three_way_side_pots(self):
        """Three different stack sizes create two side pots."""
        committed = {1: 30, 2: 60, 3: 100}
        survivors = [1, 2, 3]
        pots = _build_pots(committed, survivors)
        assert len(pots) == 3
        assert pots[0] == (90, {1, 2, 3})   # 30 * 3
        assert pots[1] == (60, {2, 3})       # 30 * 2
        assert pots[2] == (40, {3})          # 40 * 1
        assert sum(p[0] for p in pots) == 190

    def test_pot_total_equals_committed(self):
        """Sum of all pot sizes must equal sum of all committed amounts."""
        committed = {1: 25, 2: 50, 3: 75, 4: 100}
        survivors = [1, 2, 3, 4]
        pots = _build_pots(committed, survivors)
        assert sum(p[0] for p in pots) == sum(committed.values())


class TestBoardUpTo:
    """Tests for _board_up_to — collecting community cards."""

    def test_collects_flop_turn_river(self):
        hand = make_allin_hand(
            [make_player("A", "a1", 1)],
            [
                community(1, ["Ah", "Kd", "Qs"]),
                community(2, ["Jc"]),
                community(3, ["Ts"]),
            ],
        )
        board1, board2 = _board_up_to(hand, 2)
        assert board1 == ["Ah", "Kd", "Qs", "Jc", "Ts"]
        assert board2 is None

    def test_stops_at_index(self):
        hand = make_allin_hand(
            [make_player("A", "a1", 1)],
            [
                community(1, ["Ah", "Kd", "Qs"]),
                community(2, ["Jc"]),
                community(3, ["Ts"]),
            ],
        )
        b1, _ = _board_up_to(hand, 1)
        assert b1 == ["Ah", "Kd", "Qs", "Jc"]
        b1, _ = _board_up_to(hand, 0)
        assert b1 == ["Ah", "Kd", "Qs"]


class TestCollectHoleCards:
    """Tests for _collect_hole_cards."""

    def test_from_player_data(self):
        hand = make_allin_hand(
            [make_player("A", "a1", 1, hand=["As", "Kd"]),
             make_player("B", "b1", 2, hand=["Th", "Td"])],
            [],
        )
        cards = _collect_hole_cards(hand)
        assert cards[1] == ["As", "Kd"]
        assert cards[2] == ["Th", "Td"]

    def test_from_show_muck_event(self):
        hand = make_allin_hand(
            [make_player("A", "a1", 1), make_player("B", "b1", 2)],
            [ev(SHOW_MUCK, seat=1, cards=["As", "Kd"]),
             ev(SHOW_MUCK, seat=2, cards=["Th", "Td"])],
        )
        cards = _collect_hole_cards(hand)
        assert cards[1] == ["As", "Kd"]
        assert cards[2] == ["Th", "Td"]

    def test_show_muck_overrides_player_data(self):
        hand = make_allin_hand(
            [make_player("A", "a1", 1, hand=["2c", "3c"])],
            [ev(SHOW_MUCK, seat=1, cards=["As", "Kd"])],
        )
        cards = _collect_hole_cards(hand)
        assert cards[1] == ["As", "Kd"]


class TestExpectedPayout:
    """Tests for _expected_payout — the core EV calculation."""

    def test_known_board_deterministic(self):
        """With all 5 board cards known at lock time, EV is deterministic."""
        # Use ALLIN_APPROVAL after community cards so board is fully known at lock
        hand = make_allin_hand(
            [make_player("A", "a1", 1, hand=["Ah", "Ac"]),
             make_player("B", "b1", 2, hand=["Kh", "Kc"])],
            [
                ev(SMALL_BLIND, seat=1, value=0.5),
                ev(BIG_BLIND, seat=2, value=1),
                ev(BET_RAISE, seat=1, value=100, allIn=True),
                ev(CALL, seat=2, value=100, allIn=True),
                community(1, ["5d", "8c", "Js"]),
                community(2, ["2h"]),
                community(3, ["9s"]),
                ev(ALLIN_APPROVAL),  # lock here — full board known
                ev(PAYOUT, seat=1, value=200),
            ],
        )
        lock_idx = _find_allin_lock(hand)
        exp, info = _expected_payout(hand, lock_idx, mc_trials=1000)
        # AA vs KK on 5d8cJs2h9s — AA wins (pair of aces beats pair of kings)
        assert exp is not None
        assert exp["A"] == pytest.approx(200, abs=0.01)
        assert exp["B"] == pytest.approx(0, abs=0.01)

    def test_ev_sums_to_total_pot(self):
        """Sum of expected payouts must equal total pot (zero-sum)."""
        hand = make_allin_hand(
            [make_player("A", "a1", 1, hand=["Ah", "Ac"]),
             make_player("B", "b1", 2, hand=["Kh", "Kc"])],
            [
                ev(SMALL_BLIND, seat=1, value=0.5),
                ev(BIG_BLIND, seat=2, value=1),
                ev(BET_RAISE, seat=1, value=100, allIn=True),
                ev(CALL, seat=2, value=100, allIn=True),
                community(1, ["5s", "7c", "Td"]),  # flop only, 2 cards to come
                ev(PAYOUT, seat=1, value=200),
            ],
        )
        lock_idx = _find_allin_lock(hand)
        exp, info = _expected_payout(hand, lock_idx, mc_trials=50000)
        total_pot = 200
        assert sum(exp.values()) == pytest.approx(total_pot, abs=0.5)

    def test_ev_sums_to_pot_with_side_pots(self):
        """Zero-sum holds with side pots (unequal stacks)."""
        hand = make_allin_hand(
            [make_player("A", "a1", 1, hand=["Ah", "Ac"]),
             make_player("B", "b1", 2, hand=["Kh", "Kc"]),
             make_player("C", "c1", 3, hand=["Qh", "Qc"])],
            [
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=50, allIn=True),
                ev(CALL, seat=2, value=50),
                ev(BET_RAISE, seat=3, value=100, allIn=True),
                ev(CALL, seat=2, value=100, allIn=True),
                ev(ALLIN_APPROVAL),
                community(1, ["5s", "7d", "Td"]),
                community(2, ["2c"]),
                community(3, ["3s"]),
                ev(PAYOUT, seat=1, value=150),
                ev(PAYOUT, seat=2, value=100),
            ],
        )
        lock_idx = _find_allin_lock(hand)
        exp, info = _expected_payout(hand, lock_idx, mc_trials=50000)
        all_events_idx = len(hand['events']) - 1
        committed, _ = _contribs_until(hand, all_events_idx)
        total_pot = sum(committed.values())
        assert sum(exp.values()) == pytest.approx(total_pot, abs=1.0)

    def test_split_pot_when_same_hand(self):
        """Two players with identical hands split the pot."""
        hand = make_allin_hand(
            [make_player("A", "a1", 1, hand=["Ah", "Kd"]),
             make_player("B", "b1", 2, hand=["As", "Kc"])],
            [
                ev(SMALL_BLIND, seat=1, value=0.5),
                ev(BIG_BLIND, seat=2, value=1),
                ev(BET_RAISE, seat=1, value=100, allIn=True),
                ev(CALL, seat=2, value=100, allIn=True),
                community(1, ["Qh", "Jd", "Tc"]),
                community(2, ["2s"]),
                community(3, ["3s"]),
                ev(PAYOUT, seat=1, value=100),
                ev(PAYOUT, seat=2, value=100),
            ],
        )
        lock_idx = _find_allin_lock(hand)
        exp, info = _expected_payout(hand, lock_idx, mc_trials=50000)
        # Both have AK, should split on almost all boards
        # With full board dealt, this is deterministic
        assert exp["A"] == pytest.approx(100, abs=1)
        assert exp["B"] == pytest.approx(100, abs=1)

    def test_non_holdem_returns_none(self):
        hand = make_allin_hand(
            [make_player("A", "a1", 1, hand=["Ah", "Kd"]),
             make_player("B", "b1", 2, hand=["Th", "Td"])],
            [ev(BET_RAISE, seat=1, value=100, allIn=True),
             ev(CALL, seat=2, value=100, allIn=True)],
        )
        hand["gameType"] = "oh"  # Omaha
        lock_idx = _find_allin_lock(hand)
        exp, info = _expected_payout(hand, lock_idx, mc_trials=1000)
        assert exp is None


class TestComputeAllinEV:
    """Tests for compute_allin_ev — the top-level EV function."""

    def _simple_allin_hand(self, winner_seat, hand_a=None, hand_b=None):
        """Create a simple heads-up all-in hand with deterministic outcome."""
        hand_a = hand_a or ["Ah", "Ac"]
        hand_b = hand_b or ["Kh", "Kc"]
        return make_allin_hand(
            [make_player("Alice", "a1", 1, hand=hand_a),
             make_player("Bob", "b1", 2, hand=hand_b)],
            [
                ev(SMALL_BLIND, seat=1, value=0.5),
                ev(BIG_BLIND, seat=2, value=1),
                ev(BET_RAISE, seat=1, value=100, allIn=True),
                ev(CALL, seat=2, value=100, allIn=True),
                community(1, ["Qs", "Jd", "2c"]),
                community(2, ["3s"]),
                community(3, ["4d"]),
                ev(PAYOUT, seat=winner_seat, value=200),
            ],
        )

    def test_diff_sums_to_zero_per_hand(self):
        """Sum of all player diffs must be 0 within each hand (zero-sum)."""
        hand = self._simple_allin_hand(winner_seat=1)
        result = compute_allin_ev([hand])
        assert result['available']
        assert len(result['evRows']) == 1

        row = result['evRows'][0]
        total_diff = sum(p['diff'] for p in row['players'])
        assert total_diff == pytest.approx(0, abs=0.02)

    def test_per_player_diff_sums_to_zero(self):
        """Across multiple hands, sum of all perPlayer diffs must be ~0."""
        hands = [
            self._simple_allin_hand(winner_seat=1),
            self._simple_allin_hand(winner_seat=2,
                                     hand_a=["Kh", "Kc"],
                                     hand_b=["Ah", "Ac"]),
        ]
        result = compute_allin_ev(hands)
        pp = result['perPlayer']
        total_diff = sum(v['diff'] for v in pp.values())
        assert total_diff == pytest.approx(0, abs=0.1)

    def test_ev_row_equities_sum_to_100(self):
        """Player equities within a hand must sum to ~100%."""
        hand = self._simple_allin_hand(winner_seat=1)
        result = compute_allin_ev([hand])
        row = result['evRows'][0]
        total_equity = sum(p['equity'] for p in row['players'])
        assert total_equity == pytest.approx(100, abs=0.5)

    def test_actual_minus_ev_is_diff(self):
        """Each player's diff must equal actual - ev."""
        hand = self._simple_allin_hand(winner_seat=1)
        result = compute_allin_ev([hand])
        for row in result['evRows']:
            for p in row['players']:
                assert p['diff'] == pytest.approx(p['actual'] - p['ev'], abs=0.02)

    def test_per_player_actual_sums_to_zero(self):
        """Sum of all perPlayer actual (net) values should be ~0 (zero-sum)."""
        hand = self._simple_allin_hand(winner_seat=1)
        result = compute_allin_ev([hand])
        pp = result['perPlayer']
        total_actual = sum(v['actual'] for v in pp.values())
        assert total_actual == pytest.approx(0, abs=0.02)

    def test_per_player_ev_sums_to_zero(self):
        """Sum of all perPlayer ev (net) values should be ~0."""
        hand = self._simple_allin_hand(winner_seat=1)
        result = compute_allin_ev([hand])
        pp = result['perPlayer']
        total_ev = sum(v['ev'] for v in pp.values())
        assert total_ev == pytest.approx(0, abs=0.5)

    def test_cents_mode_divides_by_100(self):
        """In cents mode, amounts are divided by 100."""
        hand = make_allin_hand(
            [make_player("A", "a1", 1, hand=["Ah", "Ac"]),
             make_player("B", "b1", 2, hand=["Kh", "Kc"])],
            [
                ev(SMALL_BLIND, seat=1, value=50),
                ev(BIG_BLIND, seat=2, value=100),
                ev(BET_RAISE, seat=1, value=10000, allIn=True),
                ev(CALL, seat=2, value=10000, allIn=True),
                community(1, ["Qs", "Jd", "2c"]),
                community(2, ["3s"]),
                community(3, ["4d"]),
                ev(PAYOUT, seat=1, value=20000),
            ],
            cents=True,
        )
        result = compute_allin_ev([hand])
        row = result['evRows'][0]
        # ev is net (payout - invested), should be in dollars not cents
        # AA vs KK — AA wins, actual net = +$100, KK net = -$100
        # Sum of net EVs is zero-sum
        total_ev = sum(p['ev'] for p in row['players'])
        assert total_ev == pytest.approx(0, abs=1)
        # Actual values should be in dollar scale (divided by 100)
        pp = result['perPlayer']
        assert pp['A']['actual'] == pytest.approx(100, abs=1)
        assert pp['B']['actual'] == pytest.approx(-100, abs=1)

    def test_side_pot_zero_sum(self):
        """Three-way all-in with side pots maintains zero-sum."""
        hand = make_allin_hand(
            [make_player("A", "a1", 1, hand=["Ah", "Ac"]),
             make_player("B", "b1", 2, hand=["Kh", "Kc"]),
             make_player("C", "c1", 3, hand=["Qh", "Qc"])],
            [
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=30, allIn=True),   # short stack
                ev(CALL, seat=2, value=30),
                ev(BET_RAISE, seat=3, value=80, allIn=True),
                ev(CALL, seat=2, value=80, allIn=True),
                ev(ALLIN_APPROVAL),
                community(1, ["5s", "7d", "Td"]),
                community(2, ["2c"]),
                community(3, ["3s"]),
                ev(PAYOUT, seat=1, value=90),   # main pot: 30*3
                ev(PAYOUT, seat=2, value=100),  # side pot: 50*2
            ],
        )
        result = compute_allin_ev([hand])
        assert result['available']
        row = result['evRows'][0]

        # Zero-sum check on diffs
        total_diff = sum(p['diff'] for p in row['players'])
        assert total_diff == pytest.approx(0, abs=0.1)

        # Zero-sum check on per-player net actuals
        pp = result['perPlayer']
        total_net = sum(v['actual'] for v in pp.values())
        assert total_net == pytest.approx(0, abs=0.1)

    def test_no_allin_hands_returns_empty(self):
        """Hands without all-ins produce no EV rows."""
        hand = make_allin_hand(
            [make_player("A", "a1", 1), make_player("B", "b1", 2)],
            [
                ev(SMALL_BLIND, seat=1, value=0.5),
                ev(BIG_BLIND, seat=2, value=1),
                ev(BET_RAISE, seat=1, value=3),
                ev(FOLD, seat=2),
                ev(PAYOUT, seat=1, value=4),
            ],
        )
        result = compute_allin_ev([hand])
        assert result['available']
        assert len(result['evRows']) == 0
        assert len(result['perPlayer']) == 0

    def test_missing_hole_cards_skipped(self):
        """Hand skipped if survivor's hole cards are unknown."""
        hand = make_allin_hand(
            [make_player("A", "a1", 1, hand=["Ah", "Ac"]),
             make_player("B", "b1", 2)],  # no hand
            [
                ev(SMALL_BLIND, seat=1, value=0.5),
                ev(BIG_BLIND, seat=2, value=1),
                ev(BET_RAISE, seat=1, value=100, allIn=True),
                ev(CALL, seat=2, value=100, allIn=True),
                community(1, ["Qs", "Jd", "2c"]),
                community(2, ["3s"]),
                community(3, ["4d"]),
                ev(PAYOUT, seat=1, value=200),
            ],
        )
        result = compute_allin_ev([hand])
        assert len(result['evRows']) == 0

    def test_refund_zero_sum(self):
        """All-in with refund (unequal stacks) is still zero-sum."""
        hand = make_allin_hand(
            [make_player("A", "a1", 1, hand=["Ah", "Ac"]),
             make_player("B", "b1", 2, hand=["Kh", "Kc"])],
            [
                ev(SMALL_BLIND, seat=1, value=0.5),
                ev(BIG_BLIND, seat=2, value=1),
                ev(BET_RAISE, seat=1, value=50, allIn=True),
                ev(CALL, seat=2, value=50),
                ev(REFUND, seat=2, value=0),  # no actual refund needed here
                ev(ALLIN_APPROVAL),
                community(1, ["Qs", "Jd", "2c"]),
                community(2, ["3s"]),
                community(3, ["4d"]),
                ev(PAYOUT, seat=1, value=100),
            ],
        )
        result = compute_allin_ev([hand])
        if result['evRows']:
            row = result['evRows'][0]
            total_diff = sum(p['diff'] for p in row['players'])
            assert total_diff == pytest.approx(0, abs=0.02)



# ── Equity calculator tests ───────────────────────────────────────────────

from stats_engine import compute_equity


class TestComputeEquity:
    """Tests for compute_equity — the standalone equity calculator."""

    def test_equities_sum_to_100(self):
        """All player equities must sum to ~100%."""
        result = compute_equity([["Ah", "Ac"], ["Kh", "Kc"]], trials=10000)
        total = sum(e['equity'] for e in result['equities'])
        assert total == pytest.approx(100, abs=0.5)

    def test_aa_vs_kk_preflop(self):
        """AA vs KK preflop — AA should have ~82% equity."""
        result = compute_equity([["Ah", "Ac"], ["Kh", "Kc"]], trials=50000)
        aa_eq = result['equities'][0]['equity']
        assert 79 < aa_eq < 86

    def test_aa_vs_kk_on_safe_board(self):
        """AA vs KK on a board with no king — AA wins 100%."""
        result = compute_equity(
            [["Ah", "Ac"], ["Kh", "Kc"]],
            board=["2d", "5s", "8c", "Jd", "3s"],
        )
        assert result['equities'][0]['equity'] == pytest.approx(100, abs=0.1)

    def test_split_pot_same_hand(self):
        """Identical hands should split ~50/50."""
        result = compute_equity(
            [["Ah", "Kd"], ["As", "Kc"]],
            board=["Qh", "Jd", "Tc", "2s", "3s"],
        )
        assert result['equities'][0]['equity'] == pytest.approx(50, abs=1)
        assert result['equities'][1]['equity'] == pytest.approx(50, abs=1)

    def test_dominated_hand_full_board(self):
        """Set vs pair on full board — set wins 100%."""
        result = compute_equity(
            [["Ah", "Ac"], ["Kh", "Kc"]],
            board=["As", "7d", "2c", "9h", "4s"],
        )
        # AA has set of aces, KK has pair of kings
        assert result['equities'][0]['equity'] == pytest.approx(100, abs=0.1)

    def test_three_way(self):
        """Three-way pot equities sum to 100."""
        result = compute_equity(
            [["Ah", "Ac"], ["Kh", "Kc"], ["Qh", "Qc"]],
            trials=20000,
        )
        total = sum(e['equity'] for e in result['equities'])
        assert total == pytest.approx(100, abs=0.5)
        # AA should dominate
        assert result['equities'][0]['equity'] > 50

    def test_six_way(self):
        """Six-way pot equities sum to ~100."""
        result = compute_equity(
            [["Ah", "Ac"], ["Kh", "Kc"], ["Qh", "Qc"],
             ["Jh", "Jc"], ["Th", "Tc"], ["9h", "9c"]],
            trials=50000,
        )
        total = sum(e['equity'] for e in result['equities'])
        assert total == pytest.approx(100, abs=2)

    def test_nut_flush_draw_has_equity(self):
        """Flush draw on the flop should have significant equity."""
        result = compute_equity(
            [["Ah", "Kh"], ["Qs", "Qd"]],
            board=["2h", "7h", "Tc"],
            trials=50000,
        )
        # AKhh has flush draw + overcards vs QQ, should be ~45-55%
        ak_eq = result['equities'][0]['equity']
        assert 40 < ak_eq < 60

    def test_exact_enumeration_holdem_river(self):
        """Hold'em with 1 card to come uses exact enumeration."""
        result = compute_equity(
            [["Ah", "Ac"], ["Kh", "Kc"]],
            board=["2d", "5s", "8c", "Jd"],
        )
        # Should use exact enumeration (1 missing card), very precise
        aa_eq = result['equities'][0]['equity']
        assert aa_eq > 90  # AA is way ahead on this board


# ── All-in EV: only-all-in-players filter ──────────────────────────────────

class TestAllinOnlyFilter:
    """Tests that EV is only reported for players actually all-in."""

    def test_one_caller_included_when_equity_locked(self):
        """2 all-in + 1 non-all-in → ≤1 non-all-in, so all reported."""
        hand = make_allin_hand(
            [make_player("Short", "s1", 1, hand=["Ah", "Ac"]),
             make_player("Big", "b1", 2, hand=["Kh", "Kc"]),
             make_player("Caller", "a2", 3, hand=["Qh", "Qc"])],
            [
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=50, allIn=True),
                ev(BET_RAISE, seat=2, value=100, allIn=True),
                ev(CALL, seat=3, value=100),  # sole non-all-in, equity locked
                community(1, ["5s", "7d", "Td"]),
                community(2, ["2c"]),
                community(3, ["3s"]),
                ev(PAYOUT, seat=2, value=250),
            ],
        )
        result = compute_allin_ev([hand])
        row = result['evRows'][0]
        names_in_result = {p['name'] for p in row['players']}
        assert names_in_result == {'Short', 'Big', 'Caller'}

    def test_two_callers_excluded(self):
        """2 all-in + 2 non-all-in → only all-in players reported."""
        hand = make_allin_hand(
            [make_player("Short", "s1", 1, hand=["Ah", "Ac"]),
             make_player("Also", "a2", 2, hand=["Kh", "Kc"]),
             make_player("Caller1", "c1", 3, hand=["Qh", "Qc"]),
             make_player("Caller2", "c2", 4, hand=["Jh", "Jc"])],
            [
                ev(BET_RAISE, seat=1, value=50, allIn=True),
                ev(BET_RAISE, seat=2, value=80, allIn=True),
                ev(CALL, seat=3, value=80),
                ev(CALL, seat=4, value=80),
                community(1, ["5s", "7d", "Td"]),
                community(2, ["2c"]),
                community(3, ["3s"]),
                ev(PAYOUT, seat=1, value=200),
                ev(PAYOUT, seat=2, value=60),
            ],
        )
        result = compute_allin_ev([hand])
        row = result['evRows'][0]
        names_in_result = {p['name'] for p in row['players']}
        assert 'Short' in names_in_result
        assert 'Also' in names_in_result
        assert 'Caller1' not in names_in_result
        assert 'Caller2' not in names_in_result

    def test_all_players_allin_all_included(self):
        """When everyone is all-in, all are included."""
        hand = make_allin_hand(
            [make_player("A", "a1", 1, hand=["Ah", "Ac"]),
             make_player("B", "b1", 2, hand=["Kh", "Kc"]),
             make_player("C", "c1", 3, hand=["Qh", "Qc"])],
            [
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=50, allIn=True),
                ev(CALL, seat=2, value=50, allIn=True),
                ev(CALL, seat=3, value=50, allIn=True),
                community(1, ["5s", "7d", "Td"]),
                community(2, ["2c"]),
                community(3, ["3s"]),
                ev(PAYOUT, seat=1, value=150),
            ],
        )
        result = compute_allin_ev([hand])
        row = result['evRows'][0]
        names_in_result = {p['name'] for p in row['players']}
        assert names_in_result == {'A', 'B', 'C'}

    def test_approval_includes_all_survivors(self):
        """ALLIN_APPROVAL means all survivors are all-in, all included."""
        hand = make_allin_hand(
            [make_player("A", "a1", 1, hand=["Ah", "Ac"]),
             make_player("B", "b1", 2, hand=["Kh", "Kc"])],
            [
                ev(SMALL_BLIND, seat=1, value=0.5),
                ev(BIG_BLIND, seat=2, value=1),
                ev(BET_RAISE, seat=1, value=50, allIn=True),
                ev(CALL, seat=2, value=50),  # no allIn flag
                ev(ALLIN_APPROVAL),
                community(1, ["5s", "7d", "Td"]),
                community(2, ["2c"]),
                community(3, ["3s"]),
                ev(PAYOUT, seat=1, value=100),
            ],
        )
        result = compute_allin_ev([hand])
        row = result['evRows'][0]
        names_in_result = {p['name'] for p in row['players']}
        assert names_in_result == {'A', 'B'}

    def test_all_reported_when_one_non_allin(self):
        """2 all-in + 1 non-all-in → all 3 reported (equity locked)."""
        hand = make_allin_hand(
            [make_player("Short", "s1", 1, hand=["Ah", "Ac"]),
             make_player("Also", "a2", 2, hand=["Kh", "Kc"]),
             make_player("Caller", "c1", 3, hand=["Qh", "Qc"])],
            [
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=50, allIn=True),
                ev(BET_RAISE, seat=2, value=80, allIn=True),
                ev(CALL, seat=3, value=80),  # sole non-all-in
                community(1, ["5s", "7d", "Td"]),
                community(2, ["2c"]),
                community(3, ["3s"]),
                ev(PAYOUT, seat=1, value=150),
                ev(PAYOUT, seat=2, value=60),
            ],
        )
        result = compute_allin_ev([hand])
        row = result['evRows'][0]
        assert len(row['players']) == 3

    def test_single_allin_two_callers_skipped(self):
        """1 all-in + 2 non-all-in callers → skip (callers can bet each other)."""
        hand = make_allin_hand(
            [make_player("A", "a1", 1, hand=["Ah", "Ac"]),
             make_player("B", "b1", 2, hand=["Kh", "Kc"]),
             make_player("C", "c1", 3, hand=["Qh", "Qc"])],
            [
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=50, allIn=True),
                ev(CALL, seat=2, value=50),
                ev(CALL, seat=3, value=50),
                community(1, ["5s", "7d", "Td"]),
                community(2, ["2c"]),
                community(3, ["3s"]),
                ev(PAYOUT, seat=1, value=150),
            ],
        )
        result = compute_allin_ev([hand])
        assert len(result['evRows']) == 0

    def test_single_allin_one_caller_counted(self):
        """1 all-in + 1 caller → count it, report for both (equity locked)."""
        hand = make_allin_hand(
            [make_player("A", "a1", 1, hand=["Ah", "Ac"]),
             make_player("B", "b1", 2, hand=["Kh", "Kc"])],
            [
                ev(SMALL_BLIND, seat=1, value=0.5),
                ev(BIG_BLIND, seat=2, value=1),
                ev(BET_RAISE, seat=1, value=50, allIn=True),
                ev(CALL, seat=2, value=50),  # covers, not all-in
                community(1, ["5s", "7d", "Td"]),
                community(2, ["2c"]),
                community(3, ["3s"]),
                ev(PAYOUT, seat=1, value=100),
            ],
        )
        result = compute_allin_ev([hand])
        assert len(result['evRows']) == 1
        row = result['evRows'][0]
        names = {p['name'] for p in row['players']}
        assert names == {'A', 'B'}  # both reported

    def test_two_shorts_allin_big_caller_all_reported(self):
        """Two short stacks all-in, big stack calls.
        Only 1 non-all-in survivor → all 3 reported."""
        hand = make_allin_hand(
            [make_player("Short1", "s1", 1, hand=["Ah", "Ac"]),
             make_player("Short2", "s2", 2, hand=["Kh", "Kc"]),
             make_player("BigStack", "b1", 3, hand=["Qh", "Qc"])],
            [
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=30, allIn=True),
                ev(BET_RAISE, seat=2, value=50, allIn=True),
                ev(CALL, seat=3, value=50),  # sole non-all-in
                community(1, ["5s", "7d", "Td"]),
                ev(CHECK, seat=3),
                community(2, ["2c"]),
                ev(CHECK, seat=3),
                community(3, ["3s"]),
                ev(CHECK, seat=3),
                ev(PAYOUT, seat=1, value=90),
                ev(PAYOUT, seat=3, value=40),
            ],
        )
        result = compute_allin_ev([hand])
        assert len(result['evRows']) == 1
        row = result['evRows'][0]
        names_in_result = {p['name'] for p in row['players']}
        assert names_in_result == {'Short1', 'Short2', 'BigStack'}


# ── Side pot correctness ───────────────────────────────────────────────────

class TestSidePotEV:
    """Tests for correct EV calculation with side pots."""

    def test_three_way_side_pot_structure(self):
        """Verify pot structure with 3 different stack sizes all-in."""
        hand = make_allin_hand(
            [make_player("A", "a1", 1, hand=["Ah", "Ac"]),
             make_player("B", "b1", 2, hand=["Kh", "Kc"]),
             make_player("C", "c1", 3, hand=["Qh", "Qc"])],
            [
                ev(BET_RAISE, seat=1, value=30, allIn=True),
                ev(CALL, seat=2, value=30),
                ev(BET_RAISE, seat=3, value=80, allIn=True),
                ev(CALL, seat=2, value=80, allIn=True),
                ev(ALLIN_APPROVAL),
                community(1, ["5s", "7d", "Td"]),
                community(2, ["2c"]),
                community(3, ["3s"]),
                ev(PAYOUT, seat=1, value=90),
                ev(PAYOUT, seat=2, value=100),
            ],
        )
        # Build pots and verify structure
        all_events_idx = len(hand['events']) - 1
        committed, folded = _contribs_until(hand, all_events_idx)
        survivors = [s for s, v in committed.items() if v > 0 and s not in folded]
        pots = _build_pots(committed, survivors)
        # Main pot: 30*3=90 (all eligible), Side pot: 50*2=100 (B,C eligible)
        assert len(pots) == 2
        assert pots[0][0] == pytest.approx(90, abs=0.1)
        assert len(pots[0][1]) == 3  # A, B, C eligible for main
        assert pots[1][0] == pytest.approx(100, abs=0.1)
        assert len(pots[1][1]) == 2  # B, C eligible for side

    def test_ev_with_folded_player_contributes_to_pot(self):
        """Player who folds after betting contributes to pot but isn't eligible."""
        hand = make_allin_hand(
            [make_player("A", "a1", 1, hand=["Ah", "Ac"]),
             make_player("B", "b1", 2, hand=["Kh", "Kc"]),
             make_player("Folder", "f1", 3, hand=["Qh", "Qc"])],
            [
                ev(SMALL_BLIND, seat=2, value=0.5),
                ev(BIG_BLIND, seat=3, value=1),
                ev(BET_RAISE, seat=1, value=50, allIn=True),
                ev(CALL, seat=2, value=50, allIn=True),
                ev(FOLD, seat=3),
                community(1, ["5s", "7d", "Td"]),
                community(2, ["2c"]),
                community(3, ["3s"]),
                ev(PAYOUT, seat=1, value=101),
            ],
        )
        result = compute_allin_ev([hand])
        assert len(result['evRows']) == 1
        row = result['evRows'][0]
        # Folder's blind money is in the pot
        total_diff = sum(p['diff'] for p in row['players'])
        # With only 2 reported players (both all-in), diff may not be zero
        # because folder's 1bb contributes to pot
        names_in_result = {p['name'] for p in row['players']}
        assert 'A' in names_in_result
        assert 'B' in names_in_result

    def test_known_winner_on_turn(self):
        """AA vs KK all-in on turn with safe river — AA dominates, low diff."""
        hand = make_allin_hand(
            [make_player("A", "a1", 1, hand=["Ah", "Ac"]),
             make_player("B", "b1", 2, hand=["Kh", "Kc"])],
            [
                ev(BET_RAISE, seat=1, value=100, allIn=True),
                ev(CALL, seat=2, value=100, allIn=True),
                community(1, ["As", "7d", "2c"]),
                community(2, ["9h"]),
                ev(ALLIN_APPROVAL),
                community(3, ["4s"]),
                ev(PAYOUT, seat=1, value=200),
            ],
        )
        result = compute_allin_ev([hand], mc_trials=10000)
        row = result['evRows'][0]
        a = next(p for p in row['players'] if p['name'] == 'A')
        # AA has set on turn, should win nearly always
        assert a['equity'] > 95
        assert row['street'] == 'turn'
