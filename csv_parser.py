"""
CSV & JSON parser for PokerNow hand histories.

Converts both formats into a common structured hand format:
  [{"number", "dealerSeat", "smallBlind", "bigBlind", "bombPot", "gameType",
    "players": [{"name","id","seat","stack","hand"}],
    "events": [{"payload": {"type","seat","value",...}}]
  }, ...]

Event types (verified from PokerNow JSON data):
  0=Check, 1=Ante/BombPot, 2=BigBlind, 3=SmallBlind, 4=PostedBB, 5=PostedSBDead,
  7=Call, 8=Bet/Raise, 9=Community, 10=Payout, 11=Fold, 12=Show/Muck,
  14=AllInApproval, 15=EndOfHand, 16=Refund, 18=Bounties
"""

import csv
import io
import json
import re

# ── Card notation conversion ──────────────────────────────────────────────

_SUIT_MAP = {'♠': 's', '♥': 'h', '♦': 'd', '♣': 'c',
             's': 's', 'h': 'h', 'd': 'd', 'c': 'c'}

def _normalize_card(raw):
    """Convert a card like 'A♠' or '10♦' to 'As' or 'Td'."""
    raw = raw.strip().rstrip('.')
    if not raw:
        return None
    # Last char is suit (unicode or ascii)
    suit_char = raw[-1]
    suit = _SUIT_MAP.get(suit_char)
    if not suit:
        return None
    rank = raw[:-1]
    if rank == '10':
        rank = 'T'
    if rank not in ('2','3','4','5','6','7','8','9','T','J','Q','K','A'):
        return None
    return rank + suit


def _parse_cards_from_text(text):
    """Extract cards from a string like '[8♦, J♠, A♠]' or 'A♠, K♦'."""
    # Remove brackets
    text = text.replace('[', '').replace(']', '')
    parts = re.split(r'[,\s]+', text)
    cards = []
    for p in parts:
        c = _normalize_card(p)
        if c:
            cards.append(c)
    return cards


# ── CSV parsing ───────────────────────────────────────────────────────────

# Player reference: "Name @ ID"
_PLAYER_RE = r'"(.+?)\s+@\s+(.+?)"'

# Hand boundary
_START_RE = re.compile(
    r'-- starting hand #(\d+)'
    r'(?:\s*\(id:\s*(\w+)\))?'
    r'\s+(.+?)\s+'                 # game type (not in parens)
    r'(?:\(dealer:\s*' + _PLAYER_RE + r'\)'
    r'|'
    r'\(dead button\))'            # or dead button
    r'\s*--'
)
_END_RE = re.compile(r'-- ending hand #(\d+)')

# Player stacks line
_STACKS_RE = re.compile(r'#(\d+)\s+"(.+?)\s+@\s+(.+?)"\s+\(([\d.]+)\)')

# Action patterns (applied to the 'entry' column which has quotes unescaped)
_SB_RE    = re.compile(_PLAYER_RE + r' posts a small blind of ([\d.]+)')
_BB_RE    = re.compile(_PLAYER_RE + r' posts a big blind of ([\d.]+)')
_ANTE_RE  = re.compile(_PLAYER_RE + r' posts an ante of ([\d.]+)')
_FOLD_RE  = re.compile(_PLAYER_RE + r' folds')
_CHECK_RE = re.compile(_PLAYER_RE + r' checks')
_CALL_RE  = re.compile(_PLAYER_RE + r' calls ([\d.]+)(?:\s+and go all in)?')
_RAISE_RE = re.compile(_PLAYER_RE + r' raises to ([\d.]+)(?:\s+and go all in)?')
_BET_RE   = re.compile(_PLAYER_RE + r' bets ([\d.]+)(?:\s+and go all in)?')
_ALLIN_RE = re.compile(r'and go all in')

# Board
_FLOP_RE  = re.compile(r'Flop:\s*(.+)')
_TURN_RE  = re.compile(r'Turn:\s*(.+)')
_RIVER_RE = re.compile(r'River:\s*(.+)')

# Outcomes
_COLLECT_RE  = re.compile(_PLAYER_RE + r' collected ([\d.]+) from pot')
_RETURN_RE   = re.compile(r'Uncalled bet of ([\d.]+) returned to ' + _PLAYER_RE)
_SHOWS_RE    = re.compile(_PLAYER_RE + r' shows a (.+?)\.?$')

# Bomb pot
_BOMB_RE = re.compile(r'bomb pot bet')

# Join/quit (for context, not converted to events)
_JOIN_RE  = re.compile(_PLAYER_RE + r' (?:joined|re-joined) the game')
_QUIT_RE  = re.compile(_PLAYER_RE + r' quits the game')


def parse_csv_to_hands(csv_text):
    """Parse PokerNow CSV log text into a list of structured hand dicts.

    CSV is newest-first with columns: entry, at, order.
    Returns hands in chronological order.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = []
    for row in reader:
        entry = row.get('entry', '')
        rows.append(entry)

    # Reverse to chronological (oldest first)
    rows.reverse()

    # Split into hands
    hands = []
    current_lines = []
    current_header = None

    for line in rows:
        m = _START_RE.search(line)
        if m:
            current_header = m
            current_lines = []
            continue

        m_end = _END_RE.search(line)
        if m_end:
            if current_header and current_lines:
                hand = _build_hand(current_header, current_lines)
                if hand:
                    hands.append(hand)
            current_header = None
            current_lines = []
            continue

        if current_header is not None:
            current_lines.append(line)

    # Handle last hand if no ending marker
    if current_header and current_lines:
        hand = _build_hand(current_header, current_lines)
        if hand:
            hands.append(hand)

    return hands


def _build_hand(header_match, lines):
    """Build a structured hand dict from a parsed header and lines."""
    hand_number = header_match.group(1)
    hand_id = header_match.group(2) or ''
    game_type_str = header_match.group(3)
    dealer_name = header_match.group(4) if header_match.lastindex >= 4 else None
    dealer_id = header_match.group(5) if header_match.lastindex >= 5 else None

    # Determine game type
    game_type = 'th'  # default Texas Hold'em
    if 'Omaha' in game_type_str:
        game_type = 'oh'

    # Parse player stacks (first line should be stacks)
    players = []
    id_to_seat = {}
    dealer_seat = None
    small_blind = 0
    big_blind = 0
    is_bomb_pot = False

    event_lines = []
    for line in lines:
        m = _STACKS_RE.findall(line)
        if m and line.startswith('Player stacks:'):
            for seat_str, name, pid, stack_str in m:
                seat = int(seat_str)
                players.append({
                    'name': name.strip(),
                    'id': pid.strip(),
                    'seat': seat,
                    'stack': float(stack_str),
                    'hand': None,
                })
                id_to_seat[pid.strip()] = seat
                if pid.strip() == dealer_id:
                    dealer_seat = seat
        else:
            event_lines.append(line)

    if not players:
        return None

    # If dealer seat wasn't found from stacks, try matching by name
    if dealer_seat is None:
        for p in players:
            if p['name'] == dealer_name:
                dealer_seat = p['seat']
                break
    if dealer_seat is None and players:
        dealer_seat = players[0]['seat']

    # Parse events
    events = []
    community_turn = 0  # 0=preflop, 1=flop, 2=turn, 3=river

    for line in event_lines:
        ev = _parse_event_line(line, id_to_seat)
        if ev is None:
            continue

        # Track community card turn numbers
        if ev['payload']['type'] == 9:
            community_turn += 1
            ev['payload']['turn'] = community_turn

        # Track blind amounts
        if ev['payload']['type'] == 3:  # SB
            small_blind = ev['payload'].get('value', 0)
        elif ev['payload']['type'] == 2:  # BB
            big_blind = ev['payload'].get('value', 0)

        # Track bomb pot
        if ev['payload']['type'] == 1:
            is_bomb_pot = True

        # Track shown cards → update player hand
        if ev['payload']['type'] == 12 and ev['payload'].get('cards'):
            seat = ev['payload'].get('seat')
            if seat is not None:
                for p in players:
                    if p['seat'] == seat:
                        p['hand'] = ev['payload']['cards']

        events.append(ev)

    return {
        'id': hand_id,
        'number': hand_number,
        'gameType': game_type,
        'dealerSeat': dealer_seat,
        'smallBlind': small_blind,
        'bigBlind': big_blind,
        'bombPot': is_bomb_pot,
        'cents': False,
        'players': players,
        'events': events,
    }


def _parse_event_line(line, id_to_seat):
    """Parse a single CSV entry line into an event dict, or None if unrecognized."""

    def _seat(pid):
        return id_to_seat.get(pid.strip())

    def _is_allin(text):
        return bool(_ALLIN_RE.search(text))

    # Small blind
    m = _SB_RE.search(line)
    if m:
        seat = _seat(m.group(2))
        payload = {'type': 3, 'seat': seat, 'value': float(m.group(3))}
        return {'payload': payload}

    # Big blind
    m = _BB_RE.search(line)
    if m:
        seat = _seat(m.group(2))
        payload = {'type': 2, 'seat': seat, 'value': float(m.group(3))}
        return {'payload': payload}

    # Ante
    m = _ANTE_RE.search(line)
    if m:
        seat = _seat(m.group(2))
        payload = {'type': 1, 'seat': seat, 'value': float(m.group(3))}
        return {'payload': payload}

    # Bomb pot
    if _BOMB_RE.search(line):
        # Bomb pot ante — extract player and amount if present
        m2 = re.search(_PLAYER_RE + r'.*bomb pot.*?([\d.]+)', line)
        if m2:
            seat = _seat(m2.group(2))
            payload = {'type': 1, 'seat': seat, 'value': float(m2.group(3))}
        else:
            payload = {'type': 1}
        return {'payload': payload}

    # Fold
    m = _FOLD_RE.search(line)
    if m:
        seat = _seat(m.group(2))
        return {'payload': {'type': 11, 'seat': seat}}

    # Check
    m = _CHECK_RE.search(line)
    if m:
        seat = _seat(m.group(2))
        return {'payload': {'type': 0, 'seat': seat}}

    # Raise
    m = _RAISE_RE.search(line)
    if m:
        seat = _seat(m.group(2))
        payload = {'type': 8, 'seat': seat, 'value': float(m.group(3))}
        if _is_allin(line):
            payload['allIn'] = True
        return {'payload': payload}

    # Bet
    m = _BET_RE.search(line)
    if m:
        seat = _seat(m.group(2))
        payload = {'type': 8, 'seat': seat, 'value': float(m.group(3))}
        if _is_allin(line):
            payload['allIn'] = True
        return {'payload': payload}

    # Call
    m = _CALL_RE.search(line)
    if m:
        seat = _seat(m.group(2))
        payload = {'type': 7, 'seat': seat, 'value': float(m.group(3))}
        if _is_allin(line):
            payload['allIn'] = True
        return {'payload': payload}

    # Flop
    m = _FLOP_RE.search(line)
    if m:
        cards = _parse_cards_from_text(m.group(1))
        return {'payload': {'type': 9, 'cards': cards}}

    # Turn — format: "Turn: prev_cards [new_card]"
    m = _TURN_RE.search(line)
    if m:
        text = m.group(1)
        # The new card is in the last bracket pair
        bracket = re.search(r'\[([^\]]+)\]\s*$', text)
        if bracket:
            cards = _parse_cards_from_text(bracket.group(1))
        else:
            cards = _parse_cards_from_text(text)
        return {'payload': {'type': 9, 'cards': cards}}

    # River — same format as turn
    m = _RIVER_RE.search(line)
    if m:
        text = m.group(1)
        bracket = re.search(r'\[([^\]]+)\]\s*$', text)
        if bracket:
            cards = _parse_cards_from_text(bracket.group(1))
        else:
            cards = _parse_cards_from_text(text)
        return {'payload': {'type': 9, 'cards': cards}}

    # Collected from pot
    m = _COLLECT_RE.search(line)
    if m:
        seat = _seat(m.group(2))
        return {'payload': {'type': 10, 'seat': seat, 'value': float(m.group(3))}}

    # Uncalled bet returned
    m = _RETURN_RE.search(line)
    if m:
        seat = _seat(m.group(3))
        return {'payload': {'type': 16, 'seat': seat, 'value': float(m.group(1))}}

    # Shows cards
    m = _SHOWS_RE.search(line)
    if m:
        seat = _seat(m.group(2))
        cards = _parse_cards_from_text(m.group(3))
        return {'payload': {'type': 12, 'seat': seat, 'cards': cards}}

    # Unrecognized line — skip
    return None


# ── JSON parsing ───────���──────────────────────────────────────────────────

def parse_json_to_hands(json_text):
    """Parse PokerNow JSON hand history into a list of hand dicts.

    Expects {"hands": [...]} at the top level.
    """
    data = json.loads(json_text)
    return data.get('hands', [])


# ── Auto-detect format ───────���────────────────────────────────────────────

def parse_hand_data(text):
    """Auto-detect CSV vs JSON and parse accordingly.

    Returns (hands_list, format_name).
    """
    stripped = text.strip()
    if stripped.startswith('{') or stripped.startswith('['):
        try:
            hands = parse_json_to_hands(stripped)
            return hands, 'json'
        except json.JSONDecodeError:
            pass
    hands = parse_csv_to_hands(text)
    return hands, 'csv'
