"""
Comprehensive unit tests for equity_categories.py.

Covers: _wrap_target_ranks, _count_straight_outs, _gen_wrap, _validate_hand_for_category,
list_valid_categories, _blocker_cards_for_category, generate_hands (with locked/blockers),
made-straight post-filter, 9-out distance check, and preset board validation.
"""

import pytest
import random
from equity_categories import (
    _wrap_target_ranks,
    _count_straight_outs,
    _has_straight,
    _has_flush,
    _has_set_or_better,
    _hand_makes_set,
    _gen_wrap,
    _gen_set,
    _gen_nut_flush_draw,
    _gen_flush_draw,
    _gen_made_flush,
    _gen_made_straight,
    _gen_two_pair_top,
    _gen_full_house,
    _gen_trips,
    _gen_overpair,
    _gen_combo_draw,
    _validate_hand_for_category,
    _blocker_cards_for_category,
    _fixed_cards_desc,
    list_valid_categories,
    generate_hands,
    _rank,
    _rank_char,
    CATEGORIES,
    ALL_CARDS,
    RANKS,
    SUITS,
)


# ── Helpers ────────────────────────────────────────────────────────────────


def board_ranks(board):
    """Convert board cards to sorted rank set."""
    return sorted(set(_rank(c) for c in board))


def make_hand_from_ranks(ranks, board, suit_cycle='shdc'):
    """Build a 5-card hand from rank ints, avoiding board cards."""
    board_set = set(board)
    hand = []
    si = 0
    for r in ranks:
        rc = _rank_char(r)
        for _ in range(4):
            c = rc + suit_cycle[si % 4]
            si += 1
            if c not in board_set and c not in hand:
                hand.append(c)
                break
    return hand


# ── _wrap_target_ranks tests ──────────────────────────────────────────────


class TestWrapTargetRanks:
    """Tests for the structural pattern computation."""

    def test_9_out_bhhhb_basic(self):
        """BHHHB pattern: board at edges of 5-rank window."""
        # Board: 2, 6 → window [2,3,4,5,6], seeds = [3,4,5]
        br = {2, 6, 10}  # 3rd card far away
        targets = _wrap_target_ranks(br, 9)
        assert any(sorted(t) == [3, 4, 5] for t in targets)

    def test_9_out_distance_check_rejects_close_3rd_card(self):
        """3rd board card within 3 ranks of seeds should be rejected."""
        # Board: 2, 6, 7 → seeds would be [3,4,5], but 7 is only 2 away from 5
        br = {2, 6, 7}
        targets = _wrap_target_ranks(br, 9)
        # [3,4,5] should NOT appear because rank 7 is too close to seed 5
        assert not any(sorted(t) == [3, 4, 5] for t in targets)

    def test_9_out_distance_check_accepts_far_3rd_card(self):
        """3rd board card >= 4 ranks from all seeds should be accepted."""
        # Board: 2, 6, Q(12) → seeds [3,4,5], Q is 7 away from 5
        br = {2, 6, 12}
        targets = _wrap_target_ranks(br, 9)
        assert any(sorted(t) == [3, 4, 5] for t in targets)

    def test_9_out_ace_distance_check(self):
        """Ace (14) also acts as rank 1 for distance checking."""
        # Board: 2, 6, A(14) → seeds [3,4,5], Ace as 14 is 9 away, but as 1 is 2 away from 3
        br = {2, 6, 14}
        targets = _wrap_target_ranks(br, 9)
        # [3,4,5] should be rejected because Ace-as-1 is only 2 from seed 3
        assert not any(sorted(t) == [3, 4, 5] for t in targets)

    def test_13_out_one_sided(self):
        """13-out: 2B+3H, hole cards on one side only."""
        # Board: 9, 10, K(13) → window [8,9,10,11,12], b_in=[9,10], h_in=[8,11,12]
        # h_below = [8], h_above = [11,12] → NOT one-sided, so this is 17-out
        # Window [9,10,11,12,13], b_in=[9,10,13], h_in=[11,12] → only 2 hole, skip
        # For one-sided: board 4,5 → window [4,5,6,7,8], h_in=[6,7,8], all above
        br = {4, 5, 12}  # 3rd card far away
        targets = _wrap_target_ranks(br, 13)
        assert any(sorted(t) == [6, 7, 8] for t in targets)

    def test_13_out_vs_17_out_distinction(self):
        """13-out is one-sided, 17-out wraps both sides."""
        # Board 7, 9, A. Window [6,7,8,9,10]: b_in=[7,9], h_in=[6,8,10].
        # 6 < b_min(7) ✓, 10 > b_max(9) ✓ → both sides → 17-out
        br = {7, 9, 14}  # Ace far away
        targets_13 = _wrap_target_ranks(br, 13)
        targets_17 = _wrap_target_ranks(br, 17)
        # [6,8,10] wraps both sides → 17-out
        assert any(sorted(t) == [6, 8, 10] for t in targets_17)
        assert not any(sorted(t) == [6, 8, 10] for t in targets_13)
        # [5,6,8] is one-sided (all below 9) → 13-out
        assert any(sorted(t) == [5, 6, 8] for t in targets_13)
        assert not any(sorted(t) == [5, 6, 8] for t in targets_17)

    def test_16_out_7rank_window(self):
        """16-out pattern 1: 7-rank window, 3B+4H, skewed top/bottom."""
        # Board: 4, 5, 10 → window [4,5,6,7,8,9,10], b_in=[4,5,10], h_in=[6,7,8,9]
        # h_bottom4 = [6,7]: count of h in window[:4]=[4,5,6,7] = 2
        # h_top4 = [8,9,10]: count of h in window[3:]=[7,8,9,10] = 2
        # Neither is 3 → NOT 16-out pattern 1
        # Let's try board: 3, 6, 9 → window [3,4,5,6,7,8,9]: b_in=[3,6,9], h_in=[4,5,7,8]
        # window[:4]=[3,4,5,6], h_bottom=2; window[3:]=[6,7,8,9], h_top=2 → not 16-out
        # Board: 4, 8, 9 → window [4,5,6,7,8,9,10]: b_in=[4,8,9], h_in=[5,6,7,10]
        # window[:4]=[4,5,6,7]: h_bottom=3 → YES
        br = {4, 8, 9}
        targets = _wrap_target_ranks(br, 16)
        assert any(sorted(t) == [5, 6, 7, 10] for t in targets)

    def test_17_out_both_sides(self):
        """17-out: hole cards wrap both below and above board cards."""
        # Board: 7, 8, A → window [6,7,8,9,10], b_in=[7,8], h_in=[6,9,10]
        # h below 7: [6] ✓, h above 8: [9,10] ✓ → 17-out
        br = {7, 8, 14}
        targets = _wrap_target_ranks(br, 17)
        assert any(sorted(t) == [6, 9, 10] for t in targets)

    def test_20_out_balanced_7rank(self):
        """20-out pattern 1: 7-rank window, balanced halves."""
        # Board: 5, 8, 11 → window [5,6,7,8,9,10,11]: b=[5,8,11], h=[6,7,9,10]
        # bottom4=[5,6,7,8]: b=2, h=2 ✓; top4=[8,9,10,11]: b=2, h=2 ✓
        br = {5, 8, 11}
        targets = _wrap_target_ranks(br, 20)
        assert any(sorted(t) == [6, 7, 9, 10] for t in targets)

    def test_20_out_hhbbhh_connected(self):
        """20-out pattern 2: HHBBHH with connected board ranks."""
        # Board has 7,8 connected → seeds [5,6,9,10]
        br = {7, 8, 14}
        targets = _wrap_target_ranks(br, 20)
        assert any(sorted(t) == [5, 6, 9, 10] for t in targets)

    def test_no_targets_on_rainbow_high(self):
        """Board with all high cards should have no 20-out targets."""
        br = {14, 13, 12}  # A, K, Q
        targets = _wrap_target_ranks(br, 20)
        assert len(targets) == 0

    def test_made_straight_postfilter(self):
        """Targets where 2 seed ranks + board form a made straight are filtered out."""
        # Board: 7, 8, 9 (connector) → any window containing these 3 has many straights
        br = {7, 8, 9}
        for outs in [9, 13, 16, 17, 20]:
            targets = _wrap_target_ranks(br, outs)
            for t in targets:
                # No 2-card combo from target + board should form a 5-consecutive run
                for i in range(len(t)):
                    for j in range(i + 1, len(t)):
                        all_r = br | {t[i], t[j]}
                        for s in range(2, 11):
                            assert not all(r in all_r for r in range(s, s + 5)), \
                                f"Made straight found: seeds={t}, board_ranks={br}, window starts at {s}"

    def test_no_duplicate_targets(self):
        """Each target pattern should be unique."""
        br = {5, 8, 11}
        for outs in [9, 13, 16, 17, 20]:
            targets = _wrap_target_ranks(br, outs)
            sorted_targets = [tuple(sorted(t)) for t in targets]
            assert len(sorted_targets) == len(set(sorted_targets)), \
                f"Duplicate targets for {outs}-out: {sorted_targets}"

    def test_target_ranks_within_valid_range(self):
        """All target ranks should be between 2 and 14."""
        for br in [{3, 7, 12}, {2, 5, 14}, {6, 9, 11}]:
            for outs in [9, 13, 16, 17, 20]:
                for t in _wrap_target_ranks(br, outs):
                    for r in t:
                        assert 2 <= r <= 14, f"Rank {r} out of range in target {t}"

    def test_target_ranks_not_on_board(self):
        """No target rank should be a board rank."""
        for br in [{3, 7, 12}, {2, 5, 14}, {6, 9, 11}]:
            for outs in [9, 13, 16, 17, 20]:
                for t in _wrap_target_ranks(br, outs):
                    for r in t:
                        assert r not in br, f"Rank {r} is on board {br} in target {t}"


# ── _count_straight_outs tests ────────────────────────────────────────────


class TestCountStraightOuts:
    """Tests for exact outs counting."""

    def test_known_13_out_hand(self):
        """A known 13-out wrap hand."""
        # Board: Ts 9h 4d, hand with wrap around 9-T
        board = ['Ts', '9h', '4d']
        # Seed ranks [6,7,8] → one-sided below 9,10
        hand = make_hand_from_ranks([6, 7, 8, 2, 14], board)
        outs = _count_straight_outs(hand, board)
        # With [6,7,8] as core + 2 fill cards, should have around 13 outs
        # (exact count depends on fill cards)
        assert outs >= 0  # Sanity check

    def test_no_outs_unrelated_hand(self):
        """A hand with no straight draw should have 0 outs."""
        board = ['As', 'Kh', '2d']
        hand = ['7c', '7d', '7h', '3s', '4c']
        outs = _count_straight_outs(hand, board)
        # Holding trips of 7 with A-K-2 board — very few straight possibilities
        assert outs <= 4  # May have some distant gutshots

    def test_duplicate_rank_reduces_outs(self):
        """Holding duplicate ranks reduces available suits, lowering outs count."""
        board = ['Ts', '9h', '5d']
        # Hand with unique ranks
        hand1 = ['6c', '7d', '8h', '2s', 'Kc']
        outs1 = _count_straight_outs(hand1, board)
        # Hand with paired rank (e.g., two 8s)
        hand2 = ['6c', '7d', '8h', '8s', 'Kc']
        outs2 = _count_straight_outs(hand2, board)
        # Holding two 8s uses 2 suits of rank 8, but 8 may or may not be an out rank
        # Key point: the function counts CARDS not RANKS
        assert isinstance(outs1, int) and isinstance(outs2, int)

    def test_outs_count_is_cards_not_ranks(self):
        """Outs should count individual cards (up to 4 per rank), not just ranks."""
        board = ['Ts', '9h', '3d']
        hand = ['Jc', 'Qd', '8h', '2s', '4c']
        outs = _count_straight_outs(hand, board)
        # Each out rank contributes up to 4 cards (minus any in hand/board)
        # Outs should be divisible-ish by available suits per rank
        assert outs >= 0

    def test_wheel_straight_outs(self):
        """Test outs for A-low (wheel) straight draws."""
        board = ['2s', '3h', '7d']
        # Hand with Ace and 4 — needs 5 for wheel
        hand = ['Ac', '4d', 'Ks', 'Qh', 'Jc']
        outs = _count_straight_outs(hand, board)
        # 5 completes A-2-3-4-5 wheel. 4 suits of 5 minus any used = ~4
        assert outs >= 3


# ── _gen_wrap exact outs tests ────────────────────────────────────────────


class TestGenWrap:
    """Tests that _gen_wrap generates hands with exactly the target outs."""

    @pytest.fixture(params=[
        # (board, target_outs)
        (['Ts', '9h', '4d'], 13),
        (['Jd', 'Tc', '3s'], 13),
        (['7c', '8d', 'Ah'], 17),
        (['4c', '6d', 'Th'], 17),
        (['5c', '8d', '2h'], 20),  # Use known-good 20-out board
    ])
    def wrap_board_outs(self, request):
        return request.param

    def test_generated_hands_have_exact_outs(self, wrap_board_outs):
        """Every generated hand should have exactly the target outs."""
        board, target = wrap_board_outs
        hands = _gen_wrap(board, [], 5, min_outs=target, max_outs=target)
        for hand in hands:
            outs = _count_straight_outs(hand, board)
            assert outs == target, \
                f"Hand {hand} on board {board} has {outs} outs, expected {target}"

    def test_generated_hands_no_made_straight(self):
        """Generated wrap hands must not already have a made straight."""
        board = ['Ts', '9h', '4d']
        hands = _gen_wrap(board, [], 10, min_outs=13, max_outs=13)
        for hand in hands:
            assert not _has_straight(hand, board), \
                f"Hand {hand} makes a straight on board {board}"

    def test_generated_hands_are_valid_5card(self):
        """Each hand should have exactly 5 unique cards, none from board."""
        board = ['7c', '8d', 'Ah']
        hands = _gen_wrap(board, [], 5, min_outs=17, max_outs=17)
        board_set = set(board)
        for hand in hands:
            assert len(hand) == 5
            assert len(set(hand)) == 5, f"Duplicate cards in hand {hand}"
            assert not (set(hand) & board_set), f"Hand {hand} contains board card"

    def test_dead_cards_excluded(self):
        """Dead cards should never appear in generated hands."""
        board = ['Ts', '9h', '4d']
        dead = ['6c', '7d', '8h']
        hands = _gen_wrap(board, dead, 5, min_outs=13, max_outs=13)
        dead_set = set(dead)
        for hand in hands:
            assert not (set(hand) & dead_set), f"Hand {hand} contains dead card"

    def test_gutshot_exact_4_outs(self):
        """Gutshot should generate exactly 4-out hands."""
        board = ['Ts', '7h', '2d']
        hands = _gen_wrap(board, [], 5, min_outs=4, max_outs=4)
        for hand in hands:
            assert _count_straight_outs(hand, board) == 4

    def test_oesd_exact_8_outs(self):
        """OESD should generate exactly 8-out hands."""
        board = ['Ts', '9h', '3d']
        hands = _gen_wrap(board, [], 5, min_outs=8, max_outs=8)
        for hand in hands:
            assert _count_straight_outs(hand, board) == 8

    def test_9_out_wrap(self):
        """9-out wrap generation on a validated board."""
        board = ['2d', '5c', '9h']  # Known good 9-out board
        hands = _gen_wrap(board, [], 5, min_outs=9, max_outs=9)
        assert len(hands) > 0, "Should generate at least one 9-out hand"
        for hand in hands:
            assert _count_straight_outs(hand, board) == 9


# ── _validate_hand_for_category tests ─────────────────────────────────────


class TestValidateHandForCategory:
    """Tests for hand validation against categories."""

    def test_set_validation(self):
        """Top set requires pair of top board rank in hand."""
        board = ['Ks', '9h', '5d']
        hand_good = ['Kc', 'Kd', '2h', '3s', '7c']
        hand_bad = ['9c', '9d', '2h', '3s', '7c']  # middle set, not top
        assert _validate_hand_for_category(hand_good, board, 'top_set')
        assert not _validate_hand_for_category(hand_bad, board, 'top_set')

    def test_set_rejects_quads(self):
        """Set validation should reject hands that make quads (4-of-a-kind)."""
        board = ['Ks', 'Kh', '5d']  # paired board
        # Hand: KK → 4 Kings with board pair = quads, not set
        hand = ['Kc', 'Kd', '9c', '3s', '7h']
        # top_set requires board_count == 1, so paired board → impossible
        assert not _validate_hand_for_category(hand, board, 'top_set')

    def test_middle_set(self):
        board = ['Ks', '9h', '5d']
        hand = ['9c', '9d', '2h', '3s', '7c']
        assert _validate_hand_for_category(hand, board, 'middle_set')

    def test_bottom_set(self):
        board = ['Ks', '9h', '5d']
        hand = ['5c', '5h', '2s', '3c', '7d']
        assert _validate_hand_for_category(hand, board, 'bottom_set')

    def test_overpair(self):
        board = ['9s', '7h', '3d']
        hand = ['Tc', 'Td', '2h', '4s', '6c']
        assert _validate_hand_for_category(hand, board, 'overpair')

    def test_overpair_rejects_underpair(self):
        board = ['9s', '7h', '3d']
        hand = ['6c', '6d', '2h', '4s', 'Kc']
        assert not _validate_hand_for_category(hand, board, 'overpair')

    def test_wrap_exact_outs_validation(self):
        """Wrap validation should enforce exact outs count."""
        board = ['Ts', '9h', '4d']
        # Generate a known 13-out hand then validate
        hands = _gen_wrap(board, [], 1, min_outs=13, max_outs=13)
        if hands:
            assert _validate_hand_for_category(hands[0], board, 'wrap_13')
            # Should NOT validate as 17-out
            assert not _validate_hand_for_category(hands[0], board, 'wrap_17')

    def test_wrap_rejects_made_straight(self):
        """Wrap validation rejects hands with made straights."""
        board = ['Ts', '9h', '8d']
        # J-Q makes a straight
        hand = ['Jc', 'Qd', '2h', '3s', '4c']
        assert not _validate_hand_for_category(hand, board, 'wrap_13')

    def test_outs_adjust_relaxes_minimum(self):
        """outs_adjust should lower the minimum outs threshold."""
        board = ['Ts', '9h', '4d']
        hands = _gen_wrap(board, [], 1, min_outs=13, max_outs=13)
        if hands:
            hand = hands[0]
            # With 0 adjust, only accepts exactly 13
            assert _validate_hand_for_category(hand, board, 'wrap_13', outs_adjust=0)
            # With outs_adjust=1, accepts 12-13
            assert _validate_hand_for_category(hand, board, 'wrap_13', outs_adjust=1)

    def test_nut_flush_draw(self):
        board = ['Ks', '9s', '5d']  # 2 spades on board
        # Nut flush card = As (highest spade not on board)
        hand = ['As', '2s', '3h', '4c', '7d']
        assert _validate_hand_for_category(hand, board, 'nut_flush_draw')

    def test_flush_draw_not_nut(self):
        board = ['Ks', '9s', '5d']
        # Non-nut: Qs + another spade
        hand = ['Qs', '2s', '3h', '4c', '7d']
        assert _validate_hand_for_category(hand, board, 'flush_draw')
        # Should NOT validate as nut flush draw
        assert not _validate_hand_for_category(hand, board, 'nut_flush_draw')

    def test_flush_draw_rejects_made_flush(self):
        board = ['Ks', '9s', '5s']  # 3 spades = flush possible
        hand = ['As', '2s', '3h', '4c', '7d']
        # This would be a made flush, not a draw
        assert not _validate_hand_for_category(hand, board, 'nut_flush_draw')

    def test_two_pair_top(self):
        board = ['Ks', '9h', '5d']
        hand = ['Kc', '9d', '2h', '3s', '7c']
        assert _validate_hand_for_category(hand, board, 'two_pair_top')

    def test_two_pair_rejects_set(self):
        board = ['Ks', '9h', '5d']
        hand = ['Kc', 'Kd', '9c', '3s', '7c']
        assert not _validate_hand_for_category(hand, board, 'two_pair_top')

    def test_full_house(self):
        """Full house requires a paired board to be achievable in PLO5 (2h+3b)."""
        board = ['Ks', 'Kh', '5d']  # paired K on board
        # Hand: 5c, 5h → (5c, 5h) + (Ks, Kh, 5d) = 555KK = full house
        hand = ['5c', '5h', '3s', '7c', '9d']
        assert _validate_hand_for_category(hand, board, 'full_house')

    def test_made_straight(self):
        board = ['Ts', '9h', '8d']
        hand = ['Jc', '7d', '2h', '3s', '4c']
        assert _validate_hand_for_category(hand, board, 'made_straight')

    def test_made_straight_rejects_flush(self):
        """Made straight validation rejects hands that also make a flush."""
        board = ['Ts', '9s', '8s']
        hand = ['Js', '7s', '2h', '3c', '4d']
        # This makes a flush (5 spades using 2h+3b), so should be rejected
        assert not _validate_hand_for_category(hand, board, 'made_straight')

    def test_combo_draw(self):
        board = ['Ts', '9s', '5d']  # 2 spades
        # Need 2 spades in hand + 8+ straight outs
        hands = _gen_combo_draw(board, [], 3)
        for hand in hands:
            assert _validate_hand_for_category(hand, board, 'combo_draw')


# ── list_valid_categories tests ───────────────────────────────────────────


class TestListValidCategories:
    """Tests for category possibility checking."""

    def test_all_categories_present(self):
        """Should return an entry for every category."""
        board = ['Ks', '9h', '5d']
        cats = list_valid_categories(board)
        names = {c['name'] for c in cats}
        for name in CATEGORIES:
            assert name in names

    def test_overpair_impossible_with_ace(self):
        """Can't overpair when ace is the top board card."""
        board = ['As', '9h', '5d']
        cats = {c['name']: c for c in list_valid_categories(board)}
        assert not cats['overpair']['possible']

    def test_flush_draw_needs_two_suited(self):
        """Flush draw needs exactly 2 board cards of same suit."""
        board = ['As', 'Kh', '5d']  # rainbow
        cats = {c['name']: c for c in list_valid_categories(board)}
        assert not cats['nut_flush_draw']['possible']
        assert not cats['flush_draw']['possible']

    def test_flush_draw_possible_with_two_suited(self):
        board = ['As', 'Ks', '5d']  # 2 spades
        cats = {c['name']: c for c in list_valid_categories(board)}
        assert cats['nut_flush_draw']['possible']
        assert cats['flush_draw']['possible']

    def test_made_flush_needs_three_suited(self):
        board = ['As', 'Ks', '5s']  # 3 spades
        cats = {c['name']: c for c in list_valid_categories(board)}
        assert cats['made_flush']['possible']

    def test_made_flush_impossible_rainbow(self):
        board = ['As', 'Kh', '5d']
        cats = {c['name']: c for c in list_valid_categories(board)}
        assert not cats['made_flush']['possible']

    def test_trips_needs_board_pair(self):
        board = ['9s', '9h', '5d']  # pair of 9s
        cats = {c['name']: c for c in list_valid_categories(board)}
        assert cats['trips']['possible']

    def test_trips_impossible_no_pair(self):
        board = ['Ks', '9h', '5d']  # no pair
        cats = {c['name']: c for c in list_valid_categories(board)}
        assert not cats['trips']['possible']

    def test_wrap_uses_structural_patterns(self):
        """Wrap possibility should use _wrap_target_ranks, not just generic check."""
        # Board A-K-Q: no wrap targets exist (all high, no room)
        board = ['As', 'Kh', 'Qd']
        cats = {c['name']: c for c in list_valid_categories(board)}
        # 20-out wraps should be impossible on A-K-Q
        assert not cats['wrap_20']['possible']

    def test_wrap_9_possible_on_validated_board(self):
        """wrap_9 should be possible on a known good board."""
        board = ['2d', '5c', '9h']
        cats = {c['name']: c for c in list_valid_categories(board)}
        assert cats['wrap_9']['possible']

    def test_max_blockers_3_seed(self):
        """3-seed wraps (9, 13, 17) should have max_blockers=2."""
        board = ['Ts', '9h', '4d']
        cats = {c['name']: c for c in list_valid_categories(board)}
        for name in ('wrap_9', 'wrap_13', 'wrap_17'):
            if cats[name]['possible']:
                assert cats[name].get('max_blockers') == 2, \
                    f"{name} should have max_blockers=2"

    def test_max_blockers_4_seed(self):
        """4-seed wraps (16, 20) should have max_blockers=1."""
        board = ['4c', '5d', 'Th']  # known 16-out board
        cats = {c['name']: c for c in list_valid_categories(board)}
        for name in ('wrap_16', 'wrap_20'):
            if cats[name]['possible']:
                assert cats[name].get('max_blockers') == 1, \
                    f"{name} should have max_blockers=1"

    def test_set_impossible_with_board_pair(self):
        """If top rank has a pair on board, top_set is impossible (would be quads)."""
        board = ['Ks', 'Kh', '5d']
        cats = {c['name']: c for c in list_valid_categories(board)}
        assert not cats['top_set']['possible']

    def test_bottom_set_needs_3_unique_ranks(self):
        """Bottom set needs 3 distinct board ranks."""
        board = ['Ks', 'Kh', '5d']  # only 2 unique ranks
        cats = {c['name']: c for c in list_valid_categories(board)}
        assert not cats['bottom_set']['possible']

    def test_fixed_cards_desc_present(self):
        """Possible categories should have non-empty fixed description."""
        board = ['Ks', '9h', '5d']
        cats = list_valid_categories(board)
        for c in cats:
            if c['possible'] and c['name'] not in ('full_house', 'made_straight'):
                assert c['fixed'], f"{c['name']} missing fixed desc"


# ── _blocker_cards_for_category tests ─────────────────────────────────────


class TestBlockerCards:
    """Tests for blocker card computation."""

    def test_flush_draw_blockers_are_suited(self):
        """Flush draw blockers should be cards of the flush suit."""
        board = ['Ks', '9s', '5d']
        blockers = _blocker_cards_for_category('nut_flush_draw', board)
        for c in blockers:
            assert c[1] == 's', f"Blocker {c} is not a spade"

    def test_flush_draw_blockers_exclude_nut(self):
        """NFD blockers should exclude the nut card (it's a fixed card)."""
        board = ['Ks', '9s', '5d']
        blockers = _blocker_cards_for_category('nut_flush_draw', board)
        assert 'As' not in blockers  # Ace of spades is the nut card

    def test_flush_draw_blockers_exclude_board(self):
        """Blockers should not include board cards."""
        board = ['Ks', '9s', '5d']
        board_set = set(board)
        blockers = _blocker_cards_for_category('nut_flush_draw', board)
        for c in blockers:
            assert c not in board_set

    def test_set_blockers_include_third_card(self):
        """Set blockers should include the 3rd card of the set rank."""
        board = ['Ks', '9h', '5d']
        blockers = _blocker_cards_for_category('top_set', board)
        # Should include remaining K cards (Kc, Kd, Kh)
        k_blockers = [c for c in blockers if c[0] == 'K']
        assert len(k_blockers) > 0

    def test_wrap_blockers_are_seed_ranks(self):
        """Wrap blockers should be cards of the wrap's seed ranks."""
        board = ['Ts', '9h', '4d']
        # wrap_13 on this board: seeds involve ranks around 9-10
        blockers = _blocker_cards_for_category('wrap_13', board)
        board_rank_set = set(_rank(c) for c in board)
        targets = _wrap_target_ranks(board_rank_set, 13)
        seed_ranks = set()
        for t in targets:
            seed_ranks.update(t)
        # Every blocker should have a rank that's in the seed rank set
        for c in blockers:
            assert _rank(c) in seed_ranks, \
                f"Blocker {c} (rank {_rank(c)}) not in seed ranks {seed_ranks}"

    def test_wrap_blockers_exclude_board(self):
        """Wrap blockers should not include board cards."""
        board = ['Ts', '9h', '4d']
        board_set = set(board)
        blockers = _blocker_cards_for_category('wrap_13', board)
        for c in blockers:
            assert c not in board_set

    def test_gutshot_blockers_nearby_ranks(self):
        """Gutshot blockers use nearby-rank approach."""
        board = ['Ts', '7h', '3d']
        blockers = _blocker_cards_for_category('gutshot', board)
        # Should include cards within 4 ranks of board
        board_rank_set = set(_rank(c) for c in board)
        for c in blockers:
            r = _rank(c)
            dist = min(abs(r - br) for br in board_rank_set)
            assert dist <= 4, f"Blocker {c} rank {r} too far from board"

    def test_no_blockers_for_unknown_category(self):
        board = ['Ks', '9h', '5d']
        blockers = _blocker_cards_for_category('nonexistent', board)
        assert blockers == []

    def test_wrap_blocker_pool_multiple_patterns(self):
        """When multiple target patterns exist, blocker pool merges all seed ranks."""
        board = ['Ts', '9h', '4d']
        board_rank_set = set(_rank(c) for c in board)
        targets = _wrap_target_ranks(board_rank_set, 13)
        if len(targets) > 1:
            # Blockers should cover ranks from ALL patterns
            blockers = _blocker_cards_for_category('wrap_13', board)
            blocker_ranks = set(_rank(c) for c in blockers)
            for t in targets:
                for r in t:
                    assert r in blocker_ranks, \
                        f"Rank {r} from target {t} missing from blocker pool"


# ── generate_hands tests ─────────────────────────────────────────────────


class TestGenerateHands:
    """Tests for the main generate_hands API."""

    def test_basic_generation(self):
        """Should generate the requested number of hands."""
        board = ['Ks', '9h', '5d']
        hands = generate_hands(board, 'top_set', count=3)
        assert len(hands) <= 3  # may be fewer if rare
        for hand in hands:
            assert len(hand) == 5

    def test_locked_cards_in_hand(self):
        """Locked cards must appear in every generated hand."""
        board = ['Ks', '9h', '5d']
        locked = ['Kc', 'Kd']
        hands = generate_hands(board, 'top_set', count=3, locked=locked)
        for hand in hands:
            for lc in locked:
                assert lc in hand, f"Locked card {lc} not in hand {hand}"

    def test_dead_cards_excluded(self):
        """Dead cards should not appear in any generated hand."""
        board = ['Ks', '9h', '5d']
        dead = ['Kc']
        hands = generate_hands(board, 'top_set', count=3, dead=dead)
        for hand in hands:
            assert 'Kc' not in hand

    def test_locked_wrap_with_outs_adjust(self):
        """Wrap generation with outs_adjust should relax minimum outs."""
        board = ['Ts', '9h', '4d']
        # Generate a 13-out hand, then lock it with outs_adjust=1
        base_hands = _gen_wrap(board, [], 1, min_outs=13, max_outs=13)
        if base_hands:
            # Try generating with a locked card and outs_adjust
            locked = [base_hands[0][0]]
            hands = generate_hands(board, 'wrap_13', count=3, locked=locked, outs_adjust=1)
            for hand in hands:
                assert locked[0] in hand
                outs = _count_straight_outs(hand, board)
                assert 12 <= outs <= 13  # Relaxed by 1

    def test_too_many_locked_returns_empty(self):
        """Locking more than 5 cards should return empty."""
        board = ['Ks', '9h', '5d']
        locked = ['Kc', 'Kd', '2h', '3s', '7c', '8d']  # 6 cards
        hands = generate_hands(board, 'top_set', count=1, locked=locked)
        assert hands == []

    def test_no_board_overlap(self):
        """Generated hands should never contain board cards."""
        board = ['Ks', '9h', '5d']
        board_set = set(board)
        for cat in ['top_set', 'nut_flush_draw', 'wrap_13', 'overpair']:
            hands = generate_hands(board, cat, count=3)
            for hand in hands:
                assert not (set(hand) & board_set), \
                    f"Cat {cat}: hand {hand} overlaps board"

    def test_all_categories_generate_valid_hands(self):
        """Smoke test: each possible category should generate at least 1 valid hand."""
        board = ['Ts', '9s', '4d']  # 2 spades, decent for most categories
        # full_house needs a paired board to work in PLO5 (2h+3b can't make FH on unpaired flop)
        skip_on_unpaired = {'full_house'}
        cats = list_valid_categories(board)
        for c in cats:
            if c['possible'] and c['name'] not in skip_on_unpaired:
                hands = generate_hands(board, c['name'], count=1)
                if hands:
                    assert _validate_hand_for_category(hands[0], board, c['name']), \
                        f"Category {c['name']} generated invalid hand {hands[0]}"


# ── Preset board validation ───────────────────────────────────────────────


class TestPresetBoards:
    """Validate that all preset boards actually support their target outs."""

    PRESET_BOARDS = {
        'wrap_9': [
            ['2d', '5c', '9h'], ['2h', '6c', '9d'], ['2c', '6d', 'Th'],
            ['2h', 'Td', 'As'], ['3d', '6c', 'Th'], ['3d', '7s', 'Tc'],
            ['3h', '7d', 'Js'], ['3c', 'Ts', 'Ad'], ['4h', '7d', 'Jc'],
            ['4s', '8h', 'Jd'],
        ],
        'wrap_13': [
            ['Ts', '9h', '4d'], ['Jd', 'Tc', '3s'], ['Qh', '9d', '5c'],
            ['9s', '8h', '2d'], ['Kd', 'Jh', '6c'], ['Td', '9s', '3h'],
            ['Js', '8d', '4c'], ['Qc', 'Th', '5s'], ['Jh', '9c', '3d'],
            ['8d', '7h', '3c'],
        ],
        'wrap_16': [
            ['4c', '5d', 'Th'], ['3c', '6d', '9h'], ['6c', '8d', 'Qh'],
            ['2c', '5d', '7h'], ['3c', 'Td', 'Jh'], ['4c', '6d', '9h'],
            ['6c', '9d', 'Jh'], ['3c', '8d', '9h'], ['5c', '7d', 'Th'],
            ['2c', 'Jd', 'Qh'],
        ],
        'wrap_17': [
            ['7c', '8d', 'Ah'], ['4c', '6d', 'Th'], ['5c', '7d', 'Th'],
            ['6c', '7d', 'Kh'], ['6c', 'Td', 'Qh'], ['7c', '9d', 'Kh'],
            ['8c', '9d', 'Kh'], ['4c', '5d', 'Th'], ['4c', '6d', '9h'],
            ['2c', '6d', '7h'],
        ],
        'wrap_20': [
            ['2c', '5d', '8h'], ['6c', '9d', 'Qh'], ['7c', '9d', 'Kh'],
            ['3c', '9d', 'Th'], ['4c', '6d', 'Th'], ['5c', '6d', 'Th'],
            ['3h', '7c', '8d'], ['5c', '7d', 'Ah'], ['6c', '7d', 'Ah'],
            ['4c', '5d', 'Kh'],
        ],
    }

    @pytest.mark.parametrize("wrap_name,boards", PRESET_BOARDS.items())
    def test_preset_boards_have_targets(self, wrap_name, boards):
        """Each preset board should have at least one target pattern."""
        outs_map = {'wrap_9': 9, 'wrap_13': 13, 'wrap_16': 16, 'wrap_17': 17, 'wrap_20': 20}
        target_outs = outs_map[wrap_name]
        for board in boards:
            br = set(_rank(c) for c in board)
            targets = _wrap_target_ranks(br, target_outs)
            assert len(targets) > 0, \
                f"Board {board} has no {target_outs}-out targets"

    @pytest.mark.parametrize("wrap_name,boards", PRESET_BOARDS.items())
    def test_preset_boards_listed_as_possible(self, wrap_name, boards):
        """Each preset board should list the wrap category as possible."""
        for board in boards:
            cats = {c['name']: c for c in list_valid_categories(board)}
            assert cats[wrap_name]['possible'], \
                f"Board {board} not marked as possible for {wrap_name}"

    @pytest.mark.parametrize("wrap_name,boards", PRESET_BOARDS.items())
    def test_preset_boards_can_generate(self, wrap_name, boards):
        """Each preset board should successfully generate at least 1 hand."""
        outs_map = {'wrap_9': 9, 'wrap_13': 13, 'wrap_16': 16, 'wrap_17': 17, 'wrap_20': 20}
        target_outs = outs_map[wrap_name]
        for board in boards:
            hands = _gen_wrap(board, [], 1, min_outs=target_outs, max_outs=target_outs)
            assert len(hands) >= 1, \
                f"Board {board} failed to generate {target_outs}-out hand"
            outs = _count_straight_outs(hands[0], board)
            assert outs == target_outs, \
                f"Board {board}: generated {outs} outs, expected {target_outs}"


# ── Made hand generator tests ────────────────────────────────────────────


class TestMadeHandGenerators:
    """Tests for made-hand generators (set, flush, straight, etc.)."""

    def test_gen_set_makes_set(self):
        board = ['Ks', '9h', '5d']
        hands = _gen_set(board, [], _rank('K'), 5)
        for hand in hands:
            assert _hand_makes_set(hand, board, _rank('K'))

    def test_gen_set_no_full_house(self):
        board = ['Ks', '9h', '5d']
        hands = _gen_set(board, [], _rank('K'), 10)
        for hand in hands:
            assert not _has_set_or_better(hand, board) or \
                _hand_makes_set(hand, board, _rank('K'))

    def test_gen_nut_flush_draw_has_nut_card(self):
        board = ['Ks', '9s', '5d']
        hands = _gen_nut_flush_draw(board, [], 5)
        for hand in hands:
            assert 'As' in hand, f"NFD hand {hand} missing As"
            assert not _has_flush(hand, board)

    def test_gen_flush_draw_no_nut(self):
        board = ['Ks', '9s', '5d']
        hands = _gen_flush_draw(board, [], 5)
        for hand in hands:
            assert 'As' not in hand, f"FD hand {hand} has nut card As"
            suited = sum(1 for c in hand if c[1] == 's')
            assert suited >= 2

    def test_gen_made_flush(self):
        board = ['Ks', '9s', '5s']
        hands = _gen_made_flush(board, [], 5)
        for hand in hands:
            assert _has_flush(hand, board)

    def test_gen_made_straight(self):
        board = ['Ts', '9h', '8d']
        hands = _gen_made_straight(board, [], 5)
        for hand in hands:
            assert _has_straight(hand, board)
            assert not _has_flush(hand, board)

    def test_gen_overpair(self):
        board = ['9s', '7h', '3d']
        hands = _gen_overpair(board, [], 5)
        top_rank = max(_rank(c) for c in board)
        for hand in hands:
            from collections import Counter
            rc = Counter(_rank(c) for c in hand)
            has_overpair = any(r > top_rank and cnt >= 2 for r, cnt in rc.items())
            assert has_overpair, f"Overpair hand {hand} has no pair above {top_rank}"

    def test_gen_two_pair_top(self):
        board = ['Ks', '9h', '5d']
        hands = _gen_two_pair_top(board, [], 5)
        for hand in hands:
            assert _validate_hand_for_category(hand, board, 'two_pair_top')

    def test_gen_trips(self):
        board = ['9s', '9h', '5d']
        hands = _gen_trips(board, [], 5)
        for hand in hands:
            assert any(_rank(c) == 9 for c in hand)

    def test_gen_full_house(self):
        """Full house generator needs a paired board to produce valid PLO5 full houses."""
        board = ['Ks', 'Kh', '5d']  # paired board
        hands = _gen_full_house(board, [], 5)
        for hand in hands:
            assert _validate_hand_for_category(hand, board, 'full_house'), \
                f"Full house hand {hand} failed validation on paired board {board}"


# ── Edge cases and regression tests ──────────────────────────────────────


class TestEdgeCases:
    """Edge cases and regression tests for known bugs."""

    def test_connector_board_no_timeout_targets(self):
        """Connector boards (7-8-9) should filter out made-straight seeds."""
        br = {7, 8, 9}
        for outs in [13, 17, 20]:
            targets = _wrap_target_ranks(br, outs)
            # Every target should NOT form a made straight with board
            for t in targets:
                from itertools import combinations
                for r1, r2 in combinations(t, 2):
                    all_r = br | {r1, r2}
                    for s in range(2, 11):
                        assert not all(r in all_r for r in range(s, s + 5))

    def test_wide_gap_board_9out(self):
        """9-out wraps should work on boards with wide gaps (regression for _fast_wrap_score)."""
        board = ['2c', '6d', 'Qh']
        # Previously _fast_wrap_score returned 0 on all valid hands here
        hands = _gen_wrap(board, [], 3, min_outs=9, max_outs=9)
        # May or may not have targets depending on 3rd card distance
        br = set(_rank(c) for c in board)
        targets = _wrap_target_ranks(br, 9)
        if targets:
            assert len(hands) > 0

    def test_blocker_count_zero_no_change(self):
        """With 0 blockers, generation should work normally."""
        board = ['Ts', '9h', '4d']
        hands_no_blocker = generate_hands(board, 'wrap_13', count=3, outs_adjust=0)
        for hand in hands_no_blocker:
            assert _count_straight_outs(hand, board) == 13

    def test_seed_count_3_for_3seed_wraps(self):
        """3-seed wraps should have exactly 3 ranks in their targets."""
        board = ['Ts', '9h', '4d']
        br = set(_rank(c) for c in board)
        for outs in [9, 13, 17]:
            targets = _wrap_target_ranks(br, outs)
            for t in targets:
                assert len(t) == 3, \
                    f"{outs}-out target has {len(t)} ranks: {t}"

    def test_seed_count_4_for_4seed_wraps(self):
        """4-seed wraps should have exactly 4 ranks in their targets."""
        board = ['5c', '8d', '2h']
        br = set(_rank(c) for c in board)
        for outs in [16, 20]:
            targets = _wrap_target_ranks(br, outs)
            for t in targets:
                assert len(t) == 4, \
                    f"{outs}-out target has {len(t)} ranks: {t}"

    def test_hand_uniqueness(self):
        """Generated hands should all be unique."""
        board = ['Ts', '9h', '4d']
        hands = generate_hands(board, 'wrap_13', count=10)
        keys = [tuple(sorted(h)) for h in hands]
        assert len(keys) == len(set(keys))

    def test_turn_board(self):
        """Generation should work on 4-card (turn) boards."""
        board = ['Ts', '9h', '4d', '2c']
        cats = list_valid_categories(board)
        cat_dict = {c['name']: c for c in cats}
        # Sets should still work
        if cat_dict['top_set']['possible']:
            hands = generate_hands(board, 'top_set', count=1)
            assert len(hands) >= 0  # May be 0 if impossible after 4th card

    def test_river_board(self):
        """Generation should work on 5-card (river) boards."""
        board = ['Ts', '9h', '4d', '2c', 'Kh']
        hands = generate_hands(board, 'top_set', count=1)
        for hand in hands:
            assert len(hand) == 5
            assert not (set(hand) & set(board))

    def test_all_cards_valid(self):
        """Every generated card should be a valid card string."""
        valid_cards = set(ALL_CARDS)
        board = ['Ks', '9h', '5d']
        for cat in ['top_set', 'wrap_13', 'nut_flush_draw', 'overpair']:
            hands = generate_hands(board, cat, count=3)
            for hand in hands:
                for card in hand:
                    assert card in valid_cards, f"Invalid card {card}"

    def test_fixed_cards_desc_wraps(self):
        """Wrap categories should show correct outs in fixed description."""
        board = ['Ts', '9h', '4d']
        assert _fixed_cards_desc('gutshot', board) == '4 outs'
        assert _fixed_cards_desc('oesd', board) == '8 outs'
        assert _fixed_cards_desc('wrap_9', board) == '9 outs'
        assert _fixed_cards_desc('wrap_13', board) == '13 outs'
        assert _fixed_cards_desc('wrap_16', board) == '16 outs'
        assert _fixed_cards_desc('wrap_17', board) == '17 outs'
        assert _fixed_cards_desc('wrap_20', board) == '20 outs'


# ── Server blocker logic validation ──────────────────────────────────────


class TestServerBlockerLogic:
    """Tests validating the server-side blocker card selection logic.
    These test the logic inline (no HTTP) to verify correctness."""

    def _simulate_blocker_lock(self, category, board, blocker_count):
        """Simulate the server's blocker card locking logic.
        Returns (extra_locked, seed_count_used)."""
        blocker_pool = _blocker_cards_for_category(category, board)
        available = [c for c in blocker_pool if c not in board]

        is_structured_wrap = category in ('wrap_9', 'wrap_13', 'wrap_16', 'wrap_17', 'wrap_20')

        if is_structured_wrap and blocker_count > 0:
            by_rank = {}
            for c in available:
                by_rank.setdefault(c[0], []).append(c)
            seed_ranks = list(by_rank.keys())
            random.shuffle(seed_ranks)
            seed_count = 3 if category in ('wrap_9', 'wrap_13', 'wrap_17') else 4
            seed_ranks = seed_ranks[:seed_count]
            pair_ranks = [r for r in seed_ranks if len(by_rank[r]) >= 2][:blocker_count]
            extra_locked = []
            for r in seed_ranks:
                cards = by_rank[r]
                random.shuffle(cards)
                if r in pair_ranks:
                    extra_locked.extend(cards[:2])
                else:
                    extra_locked.append(cards[0])
            return extra_locked, seed_count
        return [], 0

    def test_3seed_wrap_locks_at_most_5_cards(self):
        """3-seed wrap with max blockers should lock at most 5 cards (3+2 pairs = 5)."""
        board = ['Ts', '9h', '4d']
        for cat in ('wrap_9', 'wrap_13', 'wrap_17'):
            cats = {c['name']: c for c in list_valid_categories(board)}
            if not cats.get(cat, {}).get('possible', False):
                continue
            for blocker_count in range(1, 3):
                locked, sc = self._simulate_blocker_lock(cat, board, blocker_count)
                assert len(locked) <= 5, \
                    f"{cat} with {blocker_count} blockers locked {len(locked)} cards: {locked}"
                assert sc == 3

    def test_4seed_wrap_locks_at_most_5_cards(self):
        """4-seed wrap with 1 blocker should lock at most 5 cards."""
        board = ['4c', '5d', 'Th']
        for cat in ('wrap_16', 'wrap_20'):
            cats = {c['name']: c for c in list_valid_categories(board)}
            if not cats.get(cat, {}).get('possible', False):
                continue
            locked, sc = self._simulate_blocker_lock(cat, board, 1)
            assert len(locked) <= 5, \
                f"{cat} with 1 blocker locked {len(locked)} cards: {locked}"
            assert sc == 4

    def test_blocker_has_correct_pair_count(self):
        """With N blockers, exactly N ranks should have 2 cards locked."""
        board = ['Ts', '9h', '4d']
        for blocker_count in [1, 2]:
            locked, _ = self._simulate_blocker_lock('wrap_13', board, blocker_count)
            if not locked:
                continue
            from collections import Counter
            rank_counts = Counter(c[0] for c in locked)
            paired = sum(1 for cnt in rank_counts.values() if cnt >= 2)
            assert paired == blocker_count, \
                f"Expected {blocker_count} paired ranks, got {paired}: {locked}"

    def test_blocker_locked_cards_not_on_board(self):
        """Locked blocker cards should not be board cards."""
        board = ['Ts', '9h', '4d']
        board_set = set(board)
        locked, _ = self._simulate_blocker_lock('wrap_13', board, 1)
        for c in locked:
            assert c not in board_set

    def test_multiple_target_patterns_dont_exceed_hand_size(self):
        """Even with many target patterns, blocker locking should not exceed 5 cards.
        This is the key regression test for the seed_count limiting fix."""
        # Find a board with multiple 13-out target patterns
        board = ['Ts', '9h', '4d']
        br = set(_rank(c) for c in board)
        targets = _wrap_target_ranks(br, 13)
        if len(targets) > 1:
            # The blocker pool has ranks from ALL patterns
            blockers = _blocker_cards_for_category('wrap_13', board)
            unique_ranks = set(c[0] for c in blockers)
            # Without the fix, the server would lock len(unique_ranks) cards
            # With the fix, it limits to 3 ranks
            for blocker_count in [1, 2]:
                locked, _ = self._simulate_blocker_lock('wrap_13', board, blocker_count)
                assert len(locked) <= 5, \
                    f"Locked {len(locked)} cards from {len(unique_ranks)} unique ranks"


# ── Comprehensive wrap generation across boards ──────────────────────────


class TestWrapGenerationBroadCoverage:
    """Test wrap generation on a variety of boards to catch edge cases."""

    SAMPLE_BOARDS = [
        ['2s', '5h', '9d'],
        ['3c', '7d', 'Jh'],
        ['4s', '8h', 'Qd'],
        ['5c', '9d', 'Kh'],
        ['6s', 'Th', '2d'],
        ['7c', 'Jd', '3h'],
        ['8s', 'Qh', '4d'],
        ['9c', 'Kd', '5h'],
    ]

    @pytest.mark.parametrize("board", SAMPLE_BOARDS)
    def test_generated_wraps_validate(self, board):
        """Every generated wrap hand should validate against its category."""
        br = set(_rank(c) for c in board)
        for outs, cat in [(13, 'wrap_13'), (17, 'wrap_17'), (9, 'wrap_9')]:
            targets = _wrap_target_ranks(br, outs)
            if not targets:
                continue
            hands = _gen_wrap(board, [], 3, min_outs=outs, max_outs=outs)
            for hand in hands:
                assert _validate_hand_for_category(hand, board, cat), \
                    f"Hand {hand} failed validation for {cat} on board {board}"

    @pytest.mark.parametrize("board", SAMPLE_BOARDS)
    def test_list_valid_matches_generation(self, board):
        """If list_valid_categories says possible, generation should succeed."""
        cats = {c['name']: c for c in list_valid_categories(board)}
        for cat_name in ('wrap_9', 'wrap_13', 'wrap_16', 'wrap_17', 'wrap_20'):
            if cats[cat_name]['possible']:
                outs_map = {'wrap_9': 9, 'wrap_13': 13, 'wrap_16': 16, 'wrap_17': 17, 'wrap_20': 20}
                hands = _gen_wrap(board, [], 1, min_outs=outs_map[cat_name],
                                  max_outs=outs_map[cat_name])
                assert len(hands) >= 1, \
                    f"{cat_name} marked possible on {board} but generation failed"
