"""
PLO5 hand category engine.

Generates representative 5-card PLO5 hands matching named categories
(top set, nut flush draw, wraps, etc.) given a specific board.
"""

import itertools
import random

RANKS = ['2', '3', '4', '5', '6', '7', '8', '9', 'T', 'J', 'Q', 'K', 'A']
SUITS = ['s', 'h', 'd', 'c']
RANK_INT = {r: i + 2 for i, r in enumerate(RANKS)}
ALL_CARDS = [r + s for r in RANKS for s in SUITS]


def _rank(card):
    return RANK_INT[card[0]]


def _suit(card):
    return card[1]


def _board_ranks(board):
    """Return sorted board ranks (high to low)."""
    return sorted([_rank(c) for c in board], reverse=True)


def _board_suits(board):
    """Return suit counts on board."""
    counts = {}
    for c in board:
        s = _suit(c)
        counts[s] = counts.get(s, 0) + 1
    return counts


def _flush_draw_suits(board):
    """Return suits that appear exactly 2x on board (flush draw possible)."""
    return [s for s, cnt in _board_suits(board).items() if cnt == 2]


def _nut_flush_card(board, suit):
    """Return the highest card of `suit` not on the board (the nut flush draw card)."""
    board_set = set(board)
    for r in reversed(RANKS):  # A, K, Q, ... 2
        card = r + suit
        if card not in board_set:
            return card
    return None


def _flush_made_suits(board):
    """Return suits that appear 3+ times on board (flush already possible)."""
    return [s for s, cnt in _board_suits(board).items() if cnt >= 3]


def _remaining_deck(used):
    """Cards not in used set."""
    used_set = set(used)
    return [c for c in ALL_CARDS if c not in used_set]


def _has_straight_from_ranks(hole_ranks, board_ranks):
    """Check if any 2 hole ranks + 3 board ranks form a straight. Rank-based (faster)."""
    for h2 in itertools.combinations(range(len(hole_ranks)), 2):
        for b3 in itertools.combinations(range(len(board_ranks)), 3):
            ranks = sorted(set([hole_ranks[h2[0]], hole_ranks[h2[1]],
                                board_ranks[b3[0]], board_ranks[b3[1]], board_ranks[b3[2]]]))
            if len(ranks) >= 5:
                for i in range(len(ranks) - 4):
                    if ranks[i + 4] - ranks[i] == 4:
                        return True
            # Ace-low
            rank_set = set(ranks)
            if {14, 2, 3, 4, 5}.issubset(rank_set):
                return True
    return False


def _count_straight_outs(hole, board):
    """Count how many cards in the remaining deck complete a straight.
    Returns the number of individual cards (not ranks) that complete a straight.
    Optimized: checks by rank first, then multiplies by available suits."""
    used = set(hole + board)
    hole_ranks = [_rank(c) for c in hole]
    board_ranks = [_rank(c) for c in board]

    out_count = 0
    # Check each possible rank (2-14) as a potential out
    for rank_val in range(2, 15):
        # How many cards of this rank are available?
        rank_char = RANKS[rank_val - 2]
        avail_suits = sum(1 for s in SUITS if (rank_char + s) not in used)
        if avail_suits == 0:
            continue
        # Test if adding this rank to the board creates a straight
        test_board_ranks = board_ranks + [rank_val]
        if _has_straight_from_ranks(hole_ranks, test_board_ranks):
            out_count += avail_suits
    return out_count


def _fast_wrap_score(hole, board):
    """Fast heuristic for wrap potential. Returns estimated straight outs.
    Checks how many 5-rank windows can be filled using exactly 2 hole + 3 board ranks."""
    hole_ranks = set(_rank(c) for c in hole)
    board_ranks = set(_rank(c) for c in board)
    all_ranks = hole_ranks | board_ranks

    outs = set()
    # Check each possible 5-consecutive window
    windows = [list(range(s, s + 5)) for s in range(2, 11)]  # 2-6 through 10-14
    windows.append([14, 2, 3, 4, 5])  # A-low

    for window in windows:
        w_set = set(window)
        # How many window ranks are on board?
        board_in = w_set & board_ranks
        # How many in hole?
        hole_in = w_set & hole_ranks
        # Need exactly 2 from hole + 3 from board(+turn) to make this straight
        # On flop (3 board cards): need all 3 board cards in window + 2 hole in window
        if len(board_in) >= 2 and len(hole_in) >= 2:
            # Missing ranks from the window
            missing = w_set - all_ranks
            for r in missing:
                outs.add(r)

    return len(outs)


def _has_straight(hole, board):
    """Check if any combination of exactly 2 hole + 3 board makes a straight."""
    hole_ranks = [_rank(c) for c in hole]
    board_ranks = [_rank(c) for c in board]
    return _has_straight_from_ranks(hole_ranks, board_ranks)


def _has_flush(hole, board):
    """Check if any combination of exactly 2 hole + 3 board makes a flush."""
    for h2 in itertools.combinations(hole, 2):
        for b3 in itertools.combinations(board, 3):
            suits = [_suit(c) for c in list(h2) + list(b3)]
            if len(set(suits)) == 1:
                return True
    return False


def _has_set_or_better(hole, board):
    """Check if hand has set, full house, or quads using exactly 2 hole + 3 board."""
    for h2 in itertools.combinations(hole, 2):
        for b3 in itertools.combinations(board, 3):
            ranks = [_rank(c) for c in list(h2) + list(b3)]
            from collections import Counter
            counts = Counter(ranks)
            max_count = max(counts.values())
            if max_count >= 3:
                return True
            # Full house: 3+2
            vals = sorted(counts.values(), reverse=True)
            if vals[0] == 3 and len(vals) > 1 and vals[1] >= 2:
                return True
    return False


def _hand_makes_set(hole, board, target_rank):
    """Check if hand specifically makes a set of the target rank."""
    hole_ranks = [_rank(c) for c in hole]
    if hole_ranks.count(target_rank) < 1:
        return False
    # Must use one from hole + one on board to make trips
    board_has = sum(1 for c in board if _rank(c) == target_rank)
    hole_has = sum(1 for c in hole if _rank(c) == target_rank)
    return board_has >= 1 and hole_has >= 2


# ── Category generators ────────────────────────────────────────────────────

def _gen_set(board, dead, target_rank, count, max_attempts=5000):
    """Generate hands that make a set of the target rank."""
    deck = _remaining_deck(board + dead)
    # Must have exactly 2 cards of target_rank in hole
    target_cards = [c for c in deck if _rank(c) == target_rank]
    if len(target_cards) < 2:
        return []
    other_cards = [c for c in deck if _rank(c) != target_rank]

    hands = []
    seen = set()
    attempts = 0
    while len(hands) < count and attempts < max_attempts:
        attempts += 1
        # Pick 2 cards of the set rank
        pair = random.sample(target_cards, 2)
        # Fill 3 more from other cards
        fill = random.sample(other_cards, 3)
        hand = pair + fill
        key = tuple(sorted(hand))
        if key in seen:
            continue
        seen.add(key)
        # Validate: should make a set, not a full house or better
        if _hand_makes_set(hand, board, target_rank):
            # Reject if it accidentally makes a full house
            from collections import Counter
            all_ranks = [_rank(c) for c in hand + board]
            # Check if any 2h+3b combo makes better than trips
            dominated = False
            for h2 in itertools.combinations(hand, 2):
                for b3 in itertools.combinations(board, 3):
                    ranks = [_rank(c) for c in list(h2) + list(b3)]
                    rc = Counter(ranks)
                    vals = sorted(rc.values(), reverse=True)
                    if vals[0] >= 4 or (vals[0] == 3 and len(vals) > 1 and vals[1] >= 2):
                        dominated = True
                        break
                if dominated:
                    break
            if not dominated:
                hands.append(hand)
    return hands


def _gen_nut_flush_draw(board, dead, count, max_attempts=5000):
    """Generate hands with nut flush draw (highest available card of flush suit)."""
    fd_suits = _flush_draw_suits(board)
    if not fd_suits:
        return []

    deck = _remaining_deck(board + dead)
    suit = random.choice(fd_suits)

    # Need the highest card of this suit not on the board
    nut_card = _nut_flush_card(board, suit)
    if nut_card is None or nut_card not in deck:
        return []

    # Need at least 1 more card of this suit in hole
    suited_cards = [c for c in deck if _suit(c) == suit and c != nut_card]
    other_cards = [c for c in deck if c != nut_card]

    hands = []
    seen = set()
    attempts = 0
    while len(hands) < count and attempts < max_attempts:
        attempts += 1
        if not suited_cards:
            break
        extra_suited = random.choice(suited_cards)
        remaining = [c for c in other_cards if c != extra_suited]
        fill = random.sample(remaining, 3)
        hand = [nut_card, extra_suited] + fill
        key = tuple(sorted(hand))
        if key in seen:
            continue
        seen.add(key)
        # Validate: has flush draw but doesn't already have a made flush
        if not _has_flush(hand, board):
            hands.append(hand)
    return hands


def _gen_flush_draw(board, dead, count, max_attempts=5000):
    """Generate hands with non-nut flush draw (second-highest or lower)."""
    fd_suits = _flush_draw_suits(board)
    if not fd_suits:
        return []

    deck = _remaining_deck(board + dead)
    suit = random.choice(fd_suits)

    # Need 2 cards of this suit, NOT the nut card (dynamic based on board)
    nut = _nut_flush_card(board, suit)
    suited_cards = [c for c in deck if _suit(c) == suit and c != nut]
    if len(suited_cards) < 2:
        return []
    other_cards = [c for c in deck if _suit(c) != suit]

    hands = []
    seen = set()
    attempts = 0
    while len(hands) < count and attempts < max_attempts:
        attempts += 1
        pair = random.sample(suited_cards, 2)
        fill = random.sample(other_cards, 3)
        hand = pair + fill
        key = tuple(sorted(hand))
        if key in seen:
            continue
        seen.add(key)
        if not _has_flush(hand, board):
            hands.append(hand)
    return hands


def _wrap_target_ranks(board_ranks, target_outs):
    """Compute target hole card ranks for each wrap type based on H/B structural patterns.

    Wrap taxonomy (H=hole, B=board, reading the rank window):
      20-out: 7-rank window 3B+4H (balanced halves), HHBBHH (connected board),
              span-6 multi-window bridges (gaps [2,4] or [4,2]), wheel (A+5+7)
      17-out: 5-rank window 2B+3H wrapping BOTH sides of board cards
      16-out: 7-rank window 3B+4H (skewed: top4 or bottom4 has 3H+1B) or 6-rank 2B+4H
      13-out: 5-rank window 2B+3H wrapping ONE side only
       9-out: 5-rank window BHHHB (board at edges, hole between)
    """
    br_set = set(board_ranks)
    targets = []

    if target_outs == 20:
        # Pattern 1: 7-rank window, 3B+4H, both top-4 and bottom-4 have 2H+2B
        for start in range(2, 9):
            window = list(range(start, start + 7))
            b_in = [r for r in window if r in br_set]
            h_in = [r for r in window if r not in br_set]
            if len(b_in) == 3 and len(h_in) == 4:
                b4 = sum(1 for r in window[:4] if r in br_set)
                t4 = sum(1 for r in window[3:] if r in br_set)
                if b4 == 2 and t4 == 2:
                    targets.append(h_in)
        # Pattern 2: HHBBHH — 2 connected board ranks, 2 below + 2 above
        for r in board_ranks:
            if r + 1 in br_set:
                seed = [r - 2, r - 1, r + 2, r + 3]
                seed = [s for s in seed if 2 <= s <= 14 and s not in br_set]
                if len(seed) == 4:
                    targets.append(seed)
        # Pattern 3: span=6 with gaps [2,4] — multi-window bridge
        # Board at (r, r+2, r+6). Works when r >= 3.
        # Targets: [r-1, r+1, r+3, r+4]
        for r in board_ranks:
            if r + 2 in br_set and r + 6 in br_set and r >= 3:
                seed = [r - 1, r + 1, r + 3, r + 4]
                seed = [s for s in seed if 2 <= s <= 14 and s not in br_set]
                if len(seed) == 4:
                    targets.append(seed)
        # Pattern 4: span=6 with gaps [4,2] — multi-window bridge
        # Board at (r, r+4, r+6). Works when r+6 <= 12.
        # Targets: [r+2, r+3, r+5, r+7]
        for r in board_ranks:
            if r + 4 in br_set and r + 6 in br_set and r + 6 <= 12:
                seed = [r + 2, r + 3, r + 5, r + 7]
                seed = [s for s in seed if 2 <= s <= 14 and s not in br_set]
                if len(seed) == 4:
                    targets.append(seed)
        # Pattern 5: Wheel — Ace + 5 + 7 on board
        if 14 in br_set and 5 in br_set and 7 in br_set:
            seed = [3, 4, 6, 8]
            seed = [s for s in seed if s not in br_set]
            if len(seed) == 4:
                targets.append(seed)

    elif target_outs == 17:
        # 5-rank window, 2B+3H, hole cards wrap BOTH below and above board
        for start in range(2, 11):
            window = list(range(start, start + 5))
            b_in = [r for r in window if r in br_set]
            h_in = [r for r in window if r not in br_set]
            if len(b_in) == 2 and len(h_in) == 3:
                b_min, b_max = min(b_in), max(b_in)
                if any(r < b_min for r in h_in) and any(r > b_max for r in h_in):
                    targets.append(h_in)

    elif target_outs == 16:
        # Pattern 1: 7-rank window, 3B+4H, top-4 or bottom-4 has 3H+1B
        for start in range(2, 9):
            window = list(range(start, start + 7))
            b_in = [r for r in window if r in br_set]
            h_in = [r for r in window if r not in br_set]
            if len(b_in) == 3 and len(h_in) == 4:
                h_bottom = sum(1 for r in window[:4] if r not in br_set)
                h_top = sum(1 for r in window[3:] if r not in br_set)
                if h_bottom == 3 or h_top == 3:
                    targets.append(h_in)
        # Pattern 2: 6-rank window, 2B+4H wrapping both sides
        for start in range(2, 10):
            window = list(range(start, start + 6))
            b_in = [r for r in window if r in br_set]
            h_in = [r for r in window if r not in br_set]
            if len(b_in) == 2 and len(h_in) == 4:
                b_min, b_max = min(b_in), max(b_in)
                if any(r < b_min for r in h_in) and any(r > b_max for r in h_in):
                    targets.append(h_in)

    elif target_outs == 13:
        # 5-rank window, 2B+3H, hole cards on ONE side of board only
        for start in range(2, 11):
            window = list(range(start, start + 5))
            b_in = [r for r in window if r in br_set]
            h_in = [r for r in window if r not in br_set]
            if len(b_in) == 2 and len(h_in) == 3:
                b_min, b_max = min(b_in), max(b_in)
                h_below = [r for r in h_in if r < b_min]
                h_above = [r for r in h_in if r > b_max]
                if not h_below or not h_above:  # one-sided
                    targets.append(h_in)

    elif target_outs == 9:
        # BHHHB: board at edges of 5-rank window, 3 hole cards between.
        # In PLO5, the 3rd board card must be >= 4 ranks from all seed ranks,
        # otherwise the extra 2 fill cards create additional straight windows
        # that push the outs count above 9.
        for start in range(2, 11):
            window = list(range(start, start + 5))
            if window[0] in br_set and window[4] in br_set:
                h_in = [r for r in window[1:4] if r not in br_set]
                if len(h_in) == 3:
                    third_cards = br_set - {window[0], window[4]}
                    valid = True
                    for c in third_cards:
                        dist = min(abs(c - s) for s in h_in)
                        if c == 14:  # Ace also acts as rank 1 (wheel)
                            dist = min(dist, min(abs(1 - s) for s in h_in))
                        if dist < 4:
                            valid = False
                            break
                    if valid:
                        targets.append(h_in)

    # Post-filter: remove targets where any 2-card combo from target ranks + board
    # forms a made straight. Such seeds always produce made-straight hands that
    # get rejected by _gen_wrap, causing generation timeouts.
    from itertools import combinations as _comb
    filtered = []
    for t in targets:
        has_made = False
        for r1, r2 in _comb(t, 2):
            all_r = br_set | {r1, r2}
            for s in range(2, 11):
                if all(r in all_r for r in range(s, s + 5)):
                    has_made = True
                    break
            if not has_made and all(r in all_r for r in (14, 2, 3, 4, 5)):
                has_made = True
            if has_made:
                break
        if not has_made:
            filtered.append(t)
    return filtered


def _gen_wrap(board, dead, count, min_outs, max_outs=24, max_attempts=50000):
    """Generate hands with a wrap (straight draw with min_outs+ card outs).

    Uses structural H/B patterns to generate targeted seeds, then validates
    with exact outs counting.
    """
    import time as _time
    timeout = 8.0 if min_outs >= 16 else 5.0
    deadline = _time.time() + timeout

    deck = _remaining_deck(board + dead)
    deck_set = set(deck)
    board_ranks = sorted(set(_rank(c) for c in board))

    # Compute targeted seeds from structural patterns
    targeted = _wrap_target_ranks(board_ranks, min_outs)
    wrap_seeds = []
    for t in targeted:
        # Weight targeted seeds heavily
        wrap_seeds.extend([t] * 10)

    # Also add general consecutive-rank seeds as fallback
    board_rank_set = set(board_ranks)
    for i in range(len(board_ranks)):
        for j in range(i, len(board_ranks)):
            gap = board_ranks[j] - board_ranks[i]
            if gap > 5:
                continue
            low = max(2, board_ranks[i] - 4)
            high = min(14, board_ranks[j] + 4)
            for start in range(low, high - 2):
                seq = [r for r in range(start, min(start + 5, 15)) if r not in board_rank_set]
                if len(seq) >= 3:
                    wrap_seeds.append(seq[:4])

    if not wrap_seeds:
        wrap_seeds = [None]

    # For OESD/gutshot (small outs), use pure random — seeds bias toward big wraps
    if max_outs <= 8:
        wrap_seeds = [None]

    hands = []
    seen = set()
    attempts = 0
    while len(hands) < count and attempts < max_attempts:
        if attempts % 200 == 0 and _time.time() > deadline:
            break
        attempts += 1
        seed = random.choice(wrap_seeds)
        if seed:
            # Pick cards matching the seed ranks
            core_cards = []
            available = list(deck_set)
            used_in_hand = set()
            # Take 3-4 cards from the seed ranks
            n_core = min(4, len(seed))
            core_ranks = random.sample(seed, n_core)
            valid = True
            for r in core_ranks:
                rank_char = RANKS[r - 2]
                options = [c for c in available if c[0] == rank_char and c not in used_in_hand]
                if not options:
                    valid = False
                    break
                pick = random.choice(options)
                core_cards.append(pick)
                used_in_hand.add(pick)
            if not valid:
                continue
            # Fill remaining
            remaining = [c for c in available if c not in used_in_hand]
            fill_count = 5 - len(core_cards)
            if len(remaining) < fill_count:
                continue
            fill = random.sample(remaining, fill_count)
            hand = core_cards + fill
        else:
            hand = random.sample(deck, 5)

        key = tuple(sorted(hand))
        if key in seen:
            continue
        seen.add(key)

        # Reject made straights first (cheaper than full outs count)
        if _has_straight(hand, board):
            continue
        # Exact outs count for final validation
        outs = _count_straight_outs(hand, board)
        if min_outs <= outs <= max_outs:
            hands.append(hand)
    return hands


def _gen_made_flush(board, dead, count, max_attempts=5000):
    """Generate hands that already have a made flush on this board."""
    flush_suits = _flush_made_suits(board)
    if not flush_suits:
        return []

    deck = _remaining_deck(board + dead)
    suit = random.choice(flush_suits)
    suited_cards = [c for c in deck if _suit(c) == suit]
    other_cards = [c for c in deck if _suit(c) != suit]

    if len(suited_cards) < 2:
        return []

    hands = []
    seen = set()
    attempts = 0
    while len(hands) < count and attempts < max_attempts:
        attempts += 1
        pair = random.sample(suited_cards, 2)
        fill = random.sample(other_cards, 3)
        hand = pair + fill
        key = tuple(sorted(hand))
        if key in seen:
            continue
        seen.add(key)
        if _has_flush(hand, board):
            hands.append(hand)
    return hands


def _gen_two_pair_top(board, dead, count, max_attempts=5000):
    """Generate hands making top two pair with the board."""
    unique_ranks = sorted(set(_rank(c) for c in board), reverse=True)
    if len(unique_ranks) < 2:
        return []
    top_rank = unique_ranks[0]
    mid_rank = unique_ranks[1]

    deck = _remaining_deck(board + dead)
    top_cards = [c for c in deck if _rank(c) == top_rank]
    mid_cards = [c for c in deck if _rank(c) == mid_rank]
    other_cards = [c for c in deck if _rank(c) not in (top_rank, mid_rank)]

    if not top_cards or not mid_cards:
        return []

    hands = []
    seen = set()
    attempts = 0
    while len(hands) < count and attempts < max_attempts:
        attempts += 1
        t = random.choice(top_cards)
        m = random.choice(mid_cards)
        fill = random.sample(other_cards, 3)
        hand = [t, m] + fill
        key = tuple(sorted(hand))
        if key in seen:
            continue
        seen.add(key)
        # Reject if makes set or better
        if not _has_set_or_better(hand, board):
            hands.append(hand)
    return hands


def _gen_full_house(board, dead, count, max_attempts=5000):
    """Generate hands that make a full house on this board.
    Strategy: pick 2 cards of one board rank (making set) + 1 card of another board rank (making pair)."""
    from collections import Counter
    deck = _remaining_deck(board + dead)
    board_ranks_sorted = _board_ranks(board)
    unique_ranks = list(set(board_ranks_sorted))

    if len(unique_ranks) < 2:
        # Board has only one rank (trips on board) — need a pocket pair
        rank = unique_ranks[0]
        other_cards = [c for c in deck if _rank(c) != rank]
        # Group by rank for pairs
        rank_groups = {}
        for c in other_cards:
            r = _rank(c)
            rank_groups.setdefault(r, []).append(c)
        pair_ranks = [r for r, cards in rank_groups.items() if len(cards) >= 2]
        if not pair_ranks:
            return []
        hands = []
        seen = set()
        attempts = 0
        while len(hands) < count and attempts < max_attempts:
            attempts += 1
            pr = random.choice(pair_ranks)
            pair = random.sample(rank_groups[pr], 2)
            remaining = [c for c in other_cards if c not in pair]
            fill = random.sample(remaining, 3)
            hand = pair + fill
            key = tuple(sorted(hand))
            if key not in seen:
                seen.add(key)
                hands.append(hand)
        return hands

    hands = []
    seen = set()
    attempts = 0
    while len(hands) < count and attempts < max_attempts:
        attempts += 1
        # Pick a rank to make set with (need pair in hole + 1 on board)
        set_rank = random.choice(unique_ranks)
        set_cards = [c for c in deck if _rank(c) == set_rank]
        if len(set_cards) < 2:
            continue
        # Pick another rank to pair with
        pair_rank = random.choice([r for r in unique_ranks if r != set_rank])
        pair_cards = [c for c in deck if _rank(c) == pair_rank]
        if not pair_cards:
            continue

        set_pair = random.sample(set_cards, 2)
        pair_card = random.choice(pair_cards)
        remaining = [c for c in deck if c not in set_pair and c != pair_card]
        fill = random.sample(remaining, 2)
        hand = set_pair + [pair_card] + fill
        key = tuple(sorted(hand))
        if key in seen:
            continue
        seen.add(key)
        hands.append(hand)
    return hands


def _gen_trips(board, dead, count, max_attempts=5000):
    """Generate hands that make trips using a paired board card.
    Board must have a pair; hand has one card of that rank (not two = that's a set with pocket pair)."""
    from collections import Counter
    board_rank_counts = Counter(_rank(c) for c in board)
    paired_ranks = [r for r, cnt in board_rank_counts.items() if cnt >= 2]
    if not paired_ranks:
        return []

    deck = _remaining_deck(board + dead)
    target_rank = max(paired_ranks)  # Use highest paired rank
    target_cards = [c for c in deck if _rank(c) == target_rank]
    other_cards = [c for c in deck if _rank(c) != target_rank]

    if not target_cards or len(other_cards) < 4:
        return []

    hands = []
    seen = set()
    attempts = 0
    while len(hands) < count and attempts < max_attempts:
        attempts += 1
        trip_card = random.choice(target_cards)
        fill = random.sample(other_cards, 4)
        hand = [trip_card] + fill
        key = tuple(sorted(hand))
        if key in seen:
            continue
        seen.add(key)
        # Reject if makes full house or better
        dominated = False
        for h2 in itertools.combinations(hand, 2):
            for b3 in itertools.combinations(board, 3):
                ranks = [_rank(c) for c in list(h2) + list(b3)]
                rc = Counter(ranks)
                vals = sorted(rc.values(), reverse=True)
                if vals[0] >= 4 or (vals[0] == 3 and len(vals) > 1 and vals[1] >= 2):
                    dominated = True
                    break
            if dominated:
                break
        if not dominated:
            hands.append(hand)
    return hands


def _gen_overpair(board, dead, count, max_attempts=5000):
    """Generate hands with an overpair (pocket pair higher than top board card)."""
    top_board_rank = max(_rank(c) for c in board)
    deck = _remaining_deck(board + dead)

    # Find pairs higher than top board card
    valid_ranks = [r for r in range(top_board_rank + 1, 15)]
    if not valid_ranks:
        return []

    pair_cards_by_rank = {}
    for r in valid_ranks:
        cards = [c for c in deck if _rank(c) == r]
        if len(cards) >= 2:
            pair_cards_by_rank[r] = cards

    if not pair_cards_by_rank:
        return []

    hands = []
    seen = set()
    attempts = 0
    while len(hands) < count and attempts < max_attempts:
        attempts += 1
        rank = random.choice(list(pair_cards_by_rank.keys()))
        pair = random.sample(pair_cards_by_rank[rank], 2)
        remaining = [c for c in deck if c not in pair]
        fill = random.sample(remaining, 3)
        hand = pair + fill
        key = tuple(sorted(hand))
        if key in seen:
            continue
        seen.add(key)
        # Reject if makes set or better (board also has this rank)
        if not _has_set_or_better(hand, board):
            hands.append(hand)
    return hands


def _gen_made_straight(board, dead, count, max_attempts=8000):
    """Generate hands that already have a made straight on this board.
    Strategy: find 5-rank windows containing 2+ board ranks, pick hole cards to fill gaps."""
    deck = _remaining_deck(board + dead)
    board_ranks = sorted(set(_rank(c) for c in board))

    # Find windows where board contributes 2-3 ranks (need exactly 2 hole + 3 board)
    straight_seeds = []
    for start in range(2, 11):
        window = list(range(start, start + 5))
        board_in = [r for r in window if r in board_ranks]
        if len(board_in) >= 3:
            hole_needed = [r for r in window if r not in board_ranks]
            if len(hole_needed) <= 2:
                straight_seeds.append(hole_needed)
    # A-low straight
    a_low = [14, 2, 3, 4, 5]
    board_in = [r for r in a_low if r in board_ranks]
    if len(board_in) >= 3:
        hole_needed = [r for r in a_low if r not in board_ranks]
        if len(hole_needed) <= 2:
            straight_seeds.append(hole_needed)

    if not straight_seeds:
        # Fallback to random
        hands = []
        seen = set()
        attempts = 0
        while len(hands) < count and attempts < max_attempts:
            attempts += 1
            hand = random.sample(deck, 5)
            key = tuple(sorted(hand))
            if key in seen:
                continue
            seen.add(key)
            if _has_straight(hand, board) and not _has_flush(hand, board):
                hands.append(hand)
        return hands

    hands = []
    seen = set()
    attempts = 0
    while len(hands) < count and attempts < max_attempts:
        attempts += 1
        seed = random.choice(straight_seeds)
        # Pick cards of the needed ranks
        core = []
        valid = True
        for r in seed:
            rank_char = RANKS[r - 2]
            options = [c for c in deck if c[0] == rank_char and c not in core]
            if not options:
                valid = False
                break
            core.append(random.choice(options))
        if not valid:
            continue
        # Fill remaining 5-len(core) cards randomly
        remaining = [c for c in deck if c not in core]
        fill = random.sample(remaining, 5 - len(core))
        hand = core + fill
        key = tuple(sorted(hand))
        if key in seen:
            continue
        seen.add(key)
        if _has_straight(hand, board) and not _has_flush(hand, board):
            hands.append(hand)
    return hands


def _gen_combo_draw(board, dead, count, max_attempts=10000):
    """Generate hands with both a wrap (3+ rank outs) and a flush draw.

    Strategy: pick 2 cards of the FD suit from wrap-adjacent ranks, then
    fill remaining cards from ranks that support straight draws.
    """
    fd_suits = _flush_draw_suits(board)
    if not fd_suits:
        return []

    deck = _remaining_deck(board + dead)
    suit = random.choice(fd_suits)
    suited_cards = [c for c in deck if _suit(c) == suit]
    other_cards = [c for c in deck if _suit(c) != suit]

    if len(suited_cards) < 2:
        return []

    hands = []
    seen = set()
    attempts = 0
    while len(hands) < count and attempts < max_attempts:
        attempts += 1
        # Pick 2 suited cards (for flush draw)
        fd_pair = random.sample(suited_cards, 2)
        # Pick 3 more cards from the remaining deck
        remaining = [c for c in other_cards if c not in fd_pair]
        fill = random.sample(remaining, 3)
        hand = fd_pair + fill
        key = tuple(sorted(hand))
        if key in seen:
            continue
        seen.add(key)
        # Reject made hands
        if _has_straight(hand, board) or _has_flush(hand, board):
            continue
        # Check wrap (8+ card outs = OESD or better)
        outs = _count_straight_outs(hand, board)
        if outs >= 8:
            hands.append(hand)
    return hands


# ── Public API ─────────────────────────────────────────────────────────────

CATEGORIES = {
    'top_set': {'label': 'Top Set', 'group': 'made'},
    'middle_set': {'label': 'Middle Set', 'group': 'made'},
    'bottom_set': {'label': 'Bottom Set', 'group': 'made'},
    'trips': {'label': 'Trips (board pair)', 'group': 'made'},
    'overpair': {'label': 'Overpair', 'group': 'made'},
    'two_pair_top': {'label': 'Top Two Pair', 'group': 'made'},
    'full_house': {'label': 'Full House', 'group': 'made'},
    'made_flush': {'label': 'Made Flush', 'group': 'made'},
    'made_straight': {'label': 'Made Straight', 'group': 'made'},
    'nut_flush_draw': {'label': 'Nut Flush Draw', 'group': 'draw'},
    'flush_draw': {'label': 'Flush Draw', 'group': 'draw'},
    'gutshot': {'label': 'Gutshot (4 outs)', 'group': 'draw'},
    'oesd': {'label': 'OESD (8 outs)', 'group': 'draw'},
    'wrap_9': {'label': 'Wrap (9 outs)', 'group': 'draw'},
    'wrap_13': {'label': 'Wrap (13 outs)', 'group': 'draw'},
    'wrap_16': {'label': 'Wrap (16 outs)', 'group': 'draw'},
    'wrap_17': {'label': 'Wrap (17 outs)', 'group': 'draw'},
    'wrap_20': {'label': 'Wrap (20 outs)', 'group': 'draw'},
    'combo_draw': {'label': 'Combo Draw (Wrap+FD)', 'group': 'draw'},
}


SUIT_SYMBOLS = {'s': '\u2660', 'h': '\u2665', 'd': '\u2666', 'c': '\u2663'}


def _rank_char(rank_int):
    """Convert rank int back to character."""
    return RANKS[rank_int - 2]


def _fixed_cards_desc(name, board):
    """Return a short description of the fixed/core cards for a category on this board."""
    board_ranks_sorted = _board_ranks(board)
    fd_suits = _flush_draw_suits(board)
    flush_suits = _flush_made_suits(board)

    def _r(rank_int):
        return _rank_char(rank_int)

    def _ss(suit):
        return SUIT_SYMBOLS.get(suit, suit)

    if name == 'top_set':
        return _r(board_ranks_sorted[0]) * 2
    elif name == 'middle_set':
        if len(set(board_ranks_sorted)) >= 2:
            return _r(board_ranks_sorted[1]) * 2
    elif name == 'bottom_set':
        if len(set(board_ranks_sorted)) >= 3:
            return _r(board_ranks_sorted[2]) * 2
    elif name == 'nut_flush_draw':
        if fd_suits:
            s = fd_suits[0]
            nut = _nut_flush_card(board, s)
            return f'{nut[0]}{_ss(s)} x{_ss(s)}' if nut else None
    elif name == 'flush_draw':
        if fd_suits:
            s = fd_suits[0]
            return f'x{_ss(s)} x{_ss(s)}'
    elif name == 'made_flush':
        if flush_suits:
            s = flush_suits[0]
            return f'x{_ss(s)} x{_ss(s)}'
    elif name == 'two_pair_top':
        unique = sorted(set(board_ranks_sorted), reverse=True)
        if len(unique) >= 2:
            return f'{_r(unique[0])}x {_r(unique[1])}x'
    elif name == 'full_house':
        return 'set + pair'
    elif name == 'trips':
        from collections import Counter
        board_rank_counts = Counter(_rank(c) for c in board)
        paired_ranks = [r for r, cnt in board_rank_counts.items() if cnt >= 2]
        if paired_ranks:
            return f'{_r(max(paired_ranks))}x'
    elif name == 'overpair':
        top_rank = max(_rank(c) for c in board)
        if top_rank < 14:
            # Show the range of possible overpairs
            lowest_op = _r(top_rank + 1)
            return f'{lowest_op}{lowest_op}+'
    elif name == 'made_straight':
        return 'straight'
    elif name == 'combo_draw':
        if fd_suits:
            s = fd_suits[0]
            return f'wrap + x{_ss(s)}x{_ss(s)}'
    elif name == 'gutshot':
        return '4 outs'
    elif name == 'oesd':
        return '8 outs'
    elif name == 'wrap_9':
        return '9 outs'
    elif name == 'wrap_13':
        return '13 outs'
    elif name == 'wrap_16':
        return '16 outs'
    elif name == 'wrap_17':
        return '17 outs'
    elif name == 'wrap_20':
        return '20 outs'
    return ''


def _fixed_cards_for_category(name, board):
    """Return all candidate cards and how many are needed for this category.

    Returns: {'need': int, 'options': list[list[str]]}
    Each entry in options is a group of interchangeable cards (pick 1 from each group).
    Used to pre-populate lock slots in the UI without collisions between players.
    """
    board_set = set(board)
    deck = [c for c in ALL_CARDS if c not in board_set]
    board_ranks_sorted = _board_ranks(board)
    fd_suits = _flush_draw_suits(board)

    if name in ('top_set', 'middle_set', 'bottom_set'):
        unique = sorted(set(board_ranks_sorted), reverse=True)
        if name == 'top_set':
            r = _rank_char(unique[0])
        elif name == 'middle_set' and len(unique) >= 2:
            r = _rank_char(unique[1])
        elif name == 'bottom_set' and len(unique) >= 3:
            r = _rank_char(unique[2])
        else:
            return {'need': 0, 'options': []}
        pool = [c for c in deck if c[0] == r]
        return {'need': 2, 'options': [pool]}

    elif name == 'trips':
        from collections import Counter
        board_rank_counts = Counter(_rank(c) for c in board)
        paired_ranks = [r for r, cnt in board_rank_counts.items() if cnt >= 2]
        if paired_ranks:
            r = _rank_char(max(paired_ranks))
            pool = [c for c in deck if c[0] == r]
            return {'need': 1, 'options': [pool]}

    elif name == 'nut_flush_draw':
        if fd_suits:
            s = fd_suits[0]
            nut = _nut_flush_card(board, s)
            if nut:
                return {'need': 1, 'options': [[nut]]}

    elif name == 'flush_draw':
        if fd_suits:
            s = fd_suits[0]
            nut = _nut_flush_card(board, s)
            pool = [c for c in deck if _suit(c) == s and c != nut]
            return {'need': 2, 'options': [pool]}

    elif name == 'combo_draw':
        if fd_suits:
            s = fd_suits[0]
            pool = [c for c in deck if _suit(c) == s]
            return {'need': 2, 'options': [pool]}

    elif name == 'two_pair_top':
        unique = sorted(set(board_ranks_sorted), reverse=True)
        if len(unique) >= 2:
            r1 = _rank_char(unique[0])
            r2 = _rank_char(unique[1])
            pool1 = [c for c in deck if c[0] == r1]
            pool2 = [c for c in deck if c[0] == r2]
            return {'need': 2, 'options': [pool1, pool2]}

    elif name == 'overpair':
        top_rank = max(_rank(c) for c in board)
        if top_rank < 14:
            r = _rank_char(top_rank + 1)
            pool = [c for c in deck if c[0] == r]
            return {'need': 2, 'options': [pool]}

    return {'need': 0, 'options': []}


def _blocker_cards_for_category(name, board):
    """Return a list of blocker card options for a category.

    For flush draws: additional cards of the flush suit (blocks opponent flush draws).
    For sets: the third card of the set rank + cards matching other board ranks (blocks boats).
    """
    board_set = set(board)
    deck = [c for c in ALL_CARDS if c not in board_set]
    fd_suits = _flush_draw_suits(board)
    board_ranks_sorted = _board_ranks(board)

    if name in ('nut_flush_draw', 'flush_draw', 'combo_draw'):
        if not fd_suits:
            return []
        s = fd_suits[0]
        # Exclude nut card: for NFD it's already a fixed card, for flush_draw it's never in the hand
        fixed = set()
        if name in ('nut_flush_draw', 'flush_draw'):
            nut = _nut_flush_card(board, s)
            if nut:
                fixed.add(nut)
        # Return remaining cards of the flush suit, highest first
        return [c for c in reversed([r + s for r in RANKS])
                if c in deck and c not in fixed and c not in board_set]

    elif name in ('top_set', 'middle_set', 'bottom_set'):
        unique = sorted(set(board_ranks_sorted), reverse=True)
        if name == 'top_set':
            set_rank = unique[0]
        elif name == 'middle_set' and len(unique) >= 2:
            set_rank = unique[1]
        elif name == 'bottom_set' and len(unique) >= 3:
            set_rank = unique[2]
        else:
            return []
        set_rank_char = _rank_char(set_rank)
        blockers = []
        # Third card of the set rank (blocks opponent trips/quads)
        for c in deck:
            if c[0] == set_rank_char:
                blockers.append(c)
        # Cards matching other board ranks (blocks opponent sets)
        other_ranks = [r for r in set(board_ranks_sorted) if r != set_rank]
        for r in sorted(other_ranks, reverse=True):
            rc = _rank_char(r)
            for c in deck:
                if c[0] == rc and c not in blockers:
                    blockers.append(c)
        return blockers

    elif name in ('wrap_9', 'wrap_13', 'wrap_16', 'wrap_17', 'wrap_20'):
        # Wrap blockers = extra cards of the wrap's seed ranks.
        # Holding a duplicate seed rank means opponents have fewer of those ranks.
        outs_map = {'wrap_9': 9, 'wrap_13': 13, 'wrap_16': 16, 'wrap_17': 17, 'wrap_20': 20}
        board_rank_set = set(_rank(c) for c in board)
        targets = _wrap_target_ranks(board_rank_set, outs_map[name])
        # Collect all unique seed ranks across all target patterns
        seed_ranks = set()
        for t in targets:
            seed_ranks.update(t)
        blockers = []
        for r in sorted(seed_ranks):
            rc = _rank_char(r)
            for c in deck:
                if c[0] == rc and c not in blockers:
                    blockers.append(c)
        return blockers

    elif name in ('gutshot', 'oesd'):
        # For gutshot/OESD, use broad nearby-rank approach since there are many
        # possible configurations without structured seed patterns
        board_rank_set = set(_rank(c) for c in board)
        out_ranks = set()
        for br in board_rank_set:
            for delta in range(-4, 5):
                r = br + delta
                if 2 <= r <= 14 and r not in board_rank_set:
                    out_ranks.add(r)
        def proximity(r):
            return min(abs(r - br) for br in board_rank_set)
        sorted_ranks = sorted(out_ranks, key=lambda r: (proximity(r), -r))
        blockers = []
        for r in sorted_ranks:
            rc = _rank_char(r)
            for c in deck:
                if c[0] == rc and c not in blockers:
                    blockers.append(c)
        return blockers

    return []


def list_valid_categories(board):
    """Return which categories are possible on this board.

    Args:
        board: list of 3-5 card strings (e.g., ["Ks","9h","5d"])

    Returns:
        list of {"name", "label", "group", "possible", "fixed"}
    """
    results = []
    board_ranks_sorted = _board_ranks(board)
    fd_suits = _flush_draw_suits(board)
    flush_suits = _flush_made_suits(board)
    suit_counts = _board_suits(board)

    # Wraps are possible on almost any board with 2+ distinct ranks
    board_rank_set = sorted(set(_rank(c) for c in board))
    has_connected = len(board_rank_set) >= 2

    for name, info in CATEGORIES.items():
        possible = True
        if name == 'top_set':
            # Need at least 2 remaining cards of top rank AND rank appears exactly once on board
            top = board_ranks_sorted[0]
            board_count = sum(1 for c in board if _rank(c) == top)
            avail = sum(1 for c in ALL_CARDS if _rank(c) == top and c not in board)
            possible = avail >= 2 and board_count == 1  # If board has pair of this rank, it's quads not set
        elif name == 'middle_set':
            if len(set(board_ranks_sorted)) < 2:
                possible = False
            else:
                mid = board_ranks_sorted[1]
                board_count = sum(1 for c in board if _rank(c) == mid)
                avail = sum(1 for c in ALL_CARDS if _rank(c) == mid and c not in board)
                possible = avail >= 2 and board_count == 1
        elif name == 'bottom_set':
            if len(set(board_ranks_sorted)) < 3:
                possible = False
            else:
                bot = board_ranks_sorted[2]
                board_count = sum(1 for c in board if _rank(c) == bot)
                avail = sum(1 for c in ALL_CARDS if _rank(c) == bot and c not in board)
                possible = avail >= 2 and board_count == 1
        elif name == 'trips':
            from collections import Counter as _C
            board_rank_counts = _C(_rank(c) for c in board)
            possible = any(cnt >= 2 for cnt in board_rank_counts.values())
        elif name == 'overpair':
            top_rank = max(_rank(c) for c in board)
            possible = top_rank < 14  # Can't overpair aces
        elif name in ('nut_flush_draw', 'flush_draw', 'combo_draw'):
            possible = len(fd_suits) > 0
        elif name == 'made_flush':
            possible = len(flush_suits) > 0
        elif name == 'made_straight':
            # Need at least 3 board ranks within a 5-rank window (to use 2 hole + 3 board)
            possible = False
            for start in range(2, 11):
                window = set(range(start, start + 5))
                if len(window & set(board_rank_set)) >= 3:
                    possible = True
                    break
            # Also check A-low
            if not possible and len({14, 2, 3, 4, 5} & set(board_rank_set)) >= 3:
                possible = True
        elif name in ('gutshot', 'oesd', 'wrap_9', 'wrap_13', 'wrap_16', 'wrap_17', 'wrap_20'):
            if not has_connected:
                possible = False
            else:
                # Check if board ranks can participate in ANY straight
                can_draw = False
                for start in range(2, 11):
                    window = set(range(start, start + 5))
                    if len(window & set(board_rank_set)) >= 2:
                        can_draw = True
                        break
                if not can_draw and len({14, 2, 3, 4, 5} & set(board_rank_set)) >= 2:
                    can_draw = True
                if not can_draw:
                    possible = False
                elif name in ('wrap_9', 'wrap_13', 'wrap_16', 'wrap_17', 'wrap_20'):
                    # Use structural pattern check — does this board support the target outs?
                    outs_map = {'wrap_9': 9, 'wrap_13': 13, 'wrap_16': 16, 'wrap_17': 17, 'wrap_20': 20}
                    possible = len(_wrap_target_ranks(board_rank_set, outs_map[name])) > 0
        elif name == 'two_pair_top':
            # Need 2+ unique ranks, AND top rank must not be paired on board
            # (pairing with a board pair + another board card = full house, not two pair)
            from collections import Counter as _C2
            rank_counts = _C2(board_ranks_sorted)
            unique = sorted(set(board_ranks_sorted), reverse=True)
            possible = (len(unique) >= 2 and
                        rank_counts[unique[0]] == 1 and rank_counts[unique[1]] == 1)
        elif name == 'full_house':
            possible = True  # Almost always possible with 3 board cards

        # Max blockers: for wraps, based on fill slots (5 - seed_count)
        wrap_max = None
        if name in ('wrap_9', 'wrap_13', 'wrap_17'):
            wrap_max = 2  # 3 seed ranks → 2 fill slots
        elif name in ('wrap_16', 'wrap_20'):
            wrap_max = 1  # 4 seed ranks → 1 fill slot

        entry = {
            'name': name,
            'label': info['label'],
            'group': info['group'],
            'possible': possible,
            'fixed': _fixed_cards_desc(name, board) if possible else '',
            'fixed_cards': _fixed_cards_for_category(name, board) if possible else {'need': 0, 'options': []},
            'blocker_cards': _blocker_cards_for_category(name, board) if possible else [],
        }
        if wrap_max is not None:
            entry['max_blockers'] = wrap_max
        results.append(entry)
    return results


def _generate_hands_raw(board, category, count, dead):
    """Internal dispatch — generates hands without lock filtering."""
    board_ranks_sorted = _board_ranks(board)

    if category == 'top_set':
        return _gen_set(board, dead, board_ranks_sorted[0], count)
    elif category == 'middle_set':
        if len(set(board_ranks_sorted)) < 2:
            return []
        return _gen_set(board, dead, board_ranks_sorted[1], count)
    elif category == 'bottom_set':
        if len(set(board_ranks_sorted)) < 3:
            return []
        return _gen_set(board, dead, board_ranks_sorted[2], count)
    elif category == 'trips':
        return _gen_trips(board, dead, count)
    elif category == 'overpair':
        return _gen_overpair(board, dead, count)
    elif category == 'nut_flush_draw':
        return _gen_nut_flush_draw(board, dead, count)
    elif category == 'flush_draw':
        return _gen_flush_draw(board, dead, count)
    elif category == 'gutshot':
        return _gen_wrap(board, dead, count, min_outs=4, max_outs=4)
    elif category == 'oesd':
        return _gen_wrap(board, dead, count, min_outs=8, max_outs=8)
    elif category == 'wrap_9':
        return _gen_wrap(board, dead, count, min_outs=9, max_outs=9)
    elif category == 'wrap_13':
        return _gen_wrap(board, dead, count, min_outs=13, max_outs=13)
    elif category == 'wrap_16':
        return _gen_wrap(board, dead, count, min_outs=16, max_outs=16)
    elif category == 'wrap_17':
        return _gen_wrap(board, dead, count, min_outs=17, max_outs=17)
    elif category == 'wrap_20':
        return _gen_wrap(board, dead, count, min_outs=20, max_outs=20)
    elif category == 'made_flush':
        return _gen_made_flush(board, dead, count)
    elif category == 'made_straight':
        return _gen_made_straight(board, dead, count)
    elif category == 'two_pair_top':
        return _gen_two_pair_top(board, dead, count)
    elif category == 'full_house':
        return _gen_full_house(board, dead, count)
    elif category == 'combo_draw':
        return _gen_combo_draw(board, dead, count)
    else:
        return []


def _validate_hand_for_category(hand, board, category, outs_adjust=0):
    """Check if a 5-card hand satisfies the given category on this board."""
    from collections import Counter
    board_ranks_sorted = _board_ranks(board)

    if category in ('top_set', 'middle_set', 'bottom_set'):
        unique = sorted(set(board_ranks_sorted), reverse=True)
        if category == 'top_set':
            target = unique[0]
        elif category == 'middle_set' and len(unique) >= 2:
            target = unique[1]
        elif category == 'bottom_set' and len(unique) >= 3:
            target = unique[2]
        else:
            return False
        if not _hand_makes_set(hand, board, target):
            return False
        # Reject full houses / quads
        for h2 in itertools.combinations(hand, 2):
            for b3 in itertools.combinations(board, 3):
                ranks = [_rank(c) for c in list(h2) + list(b3)]
                rc = Counter(ranks)
                vals = sorted(rc.values(), reverse=True)
                if vals[0] >= 4 or (vals[0] == 3 and len(vals) > 1 and vals[1] >= 2):
                    return False
        return True

    elif category == 'trips':
        rc = Counter(_rank(c) for c in board)
        paired = [r for r, cnt in rc.items() if cnt >= 2]
        if not paired:
            return False
        target = max(paired)
        hole_has = sum(1 for c in hand if _rank(c) == target)
        return hole_has >= 1

    elif category == 'overpair':
        top_rank = max(_rank(c) for c in board)
        hole_ranks = [_rank(c) for c in hand]
        rc = Counter(hole_ranks)
        return any(r > top_rank and cnt >= 2 for r, cnt in rc.items())

    elif category == 'nut_flush_draw':
        fd_suits = _flush_draw_suits(board)
        if not fd_suits:
            return False
        s = fd_suits[0]
        nut = _nut_flush_card(board, s)
        if not nut or nut not in hand:
            return False
        suited_in_hole = sum(1 for c in hand if _suit(c) == s)
        return suited_in_hole >= 2 and not _has_flush(hand, board)

    elif category == 'flush_draw':
        fd_suits = _flush_draw_suits(board)
        if not fd_suits:
            return False
        s = fd_suits[0]
        nut = _nut_flush_card(board, s)
        if nut and nut in hand:
            return False  # That's NFD, not regular flush draw
        suited_in_hole = sum(1 for c in hand if _suit(c) == s)
        return suited_in_hole >= 2 and not _has_flush(hand, board)

    elif category in ('gutshot', 'oesd', 'wrap_9', 'wrap_13', 'wrap_16', 'wrap_17', 'wrap_20'):
        outs = _count_straight_outs(hand, board)
        ranges = {'gutshot': (4, 4), 'oesd': (8, 8), 'wrap_9': (9, 9),
                  'wrap_13': (13, 13), 'wrap_16': (16, 16), 'wrap_17': (17, 17), 'wrap_20': (20, 20)}
        lo, hi = ranges[category]
        lo = max(0, lo - outs_adjust)
        return lo <= outs <= hi and not _has_straight(hand, board)

    elif category == 'combo_draw':
        fd_suits = _flush_draw_suits(board)
        if not fd_suits:
            return False
        s = fd_suits[0]
        suited_in_hole = sum(1 for c in hand if _suit(c) == s)
        if suited_in_hole < 2:
            return False
        outs = _count_straight_outs(hand, board)
        return outs >= 8 and not _has_flush(hand, board) and not _has_straight(hand, board)

    elif category == 'made_flush':
        return _has_flush(hand, board)

    elif category == 'made_straight':
        return _has_straight(hand, board) and not _has_flush(hand, board)

    elif category == 'two_pair_top':
        unique = sorted(set(board_ranks_sorted), reverse=True)
        if len(unique) < 2:
            return False
        top, second = unique[0], unique[1]
        hole_ranks = [_rank(c) for c in hand]
        return hole_ranks.count(top) >= 1 and hole_ranks.count(second) >= 1 and not _has_set_or_better(hand, board)

    elif category == 'full_house':
        for h2 in itertools.combinations(hand, 2):
            for b3 in itertools.combinations(board, 3):
                ranks = [_rank(c) for c in list(h2) + list(b3)]
                rc = Counter(ranks)
                vals = sorted(rc.values(), reverse=True)
                if vals[0] == 3 and len(vals) > 1 and vals[1] >= 2:
                    return True
        return False

    return False


def generate_hands(board, category, count=1, dead=None, locked=None, outs_adjust=0):
    """Generate random valid PLO5 hands matching the category on this board.

    Args:
        board: list of card strings (e.g., ["Ks","9h","5d"])
        category: category name string
        count: how many hands to generate
        dead: optional list of dead cards to exclude
        locked: optional list of card strings that MUST appear in the hand
                (e.g., ["As"] means every generated hand must contain As)
        outs_adjust: relax min outs by this amount (for wrap blockers that consume out-rank cards)

    Returns:
        list of 5-card hand lists, may be fewer than count if category is rare
    """
    dead = dead or []
    locked = [c for c in (locked or []) if c]

    if not locked:
        return _generate_hands_raw(board, category, count, dead)

    # With locked cards: place locked cards first, then fill remaining slots randomly.
    # Validate each hand against the category requirements.
    locked_set = set(locked)
    all_dead = set(board + dead) | locked_set
    fill_deck = [c for c in ALL_CARDS if c not in all_dead]
    need_fill = 5 - len(locked)

    if need_fill < 0 or len(fill_deck) < need_fill:
        return []

    results = []
    seen = set()
    max_attempts = max(5000, count * 200)
    for _ in range(max_attempts):
        if len(results) >= count:
            break
        fill = random.sample(fill_deck, need_fill)
        hand = list(locked) + fill
        key = tuple(sorted(hand))
        if key in seen:
            continue
        seen.add(key)
        # Validate: hand must satisfy the category on this board
        if _validate_hand_for_category(hand, board, category, outs_adjust=outs_adjust):
            results.append(hand)
    return results
