# PokerNow Assistant - Project Guide

## Overview
A web app that generates ledgers and statistics from PokerNow.com poker games. Users paste a game link to get an instant ledger, and upload CSV log files for detailed statistics.

**Live site:** Deployed on Render (Python 3.11 required for eval7 compatibility)
**Repo:** https://github.com/Belugaluga2/Pokernow-Assistant

## Architecture

### Backend: `server.py`
- Python HTTP server (stdlib `http.server`)
- Serves static frontend (`index.html`)
- API endpoints for ledger, stats, and EV computation
- Uses Socket.IO to get live game state (player stacks) from PokerNow
- Uses `curl` subprocess (with `requests` library fallback) to fetch PokerNow log API
- Runs on `PORT` env variable (default 8000)

### Frontend: `index.html`
- Single-page app, no build tools
- Dark poker-themed UI
- Two main tabs: Ledger and Statistics
- Statistics loaded via CSV file upload (not from API — too slow)

### Stats Engine: `stats_engine.py`
- Computes VPIP, PFR, 3-Bet, C-Bet, WTSD, AF, and many more stats
- Bomb pot hands are tracked separately from standard hands
- All-in EV analysis using Monte Carlo simulation with eval7
- Supports Hold'em, PLO (4 cards), and PLO5 (5 cards)
- Supports double board games

### CSV Parser: `csv_parser.py`
- Parses PokerNow CSV log files into structured hand data
- Also parses PokerNow JSON replay format
- Converts text log entries into event objects with typed payloads
- Tags community cards with `board: 1` or `board: 2` for double board games

## Key API Endpoints

### `GET /api/ledger/{gameId}`
Fast ledger generation (~6 seconds):
1. Connects via Socket.IO to get current player stacks instantly
2. Fetches money-only messages (`mm=true` filter, ~3 pages) for buy-ins/cash-outs
3. Computes net profit and optimal settlements

### `POST /api/stats/upload` (or `/api/stats/csv`)
Upload CSV/JSON file for statistics. Returns all stats except EV (which is on-demand).

### `POST /api/stats/ev`
On-demand all-in EV computation. Separate from main stats because it's slow (Monte Carlo simulation).

### `POST /api/equity`
Equity calculator for arbitrary hands vs board.

## PokerNow Data Access

### Log API
- `GET https://www.pokernow.com/games/{gameId}/log` — paginated log (50 entries/page)
- `?mm=true` — money messages only (joins, quits, admin stack changes) — typically ~3 pages
- `?before_at={created_at}` — pagination cursor
- Cloudflare blocks Python `urllib` — must use `curl` subprocess or `requests` with browser User-Agent

### Socket.IO (for live game state)
1. `GET https://www.pokernow.com/games/{gameId}` to get session cookies
2. Connect Socket.IO to `https://www.pokernow.com?gameID={gameId}` with cookies
3. Emit `{"type": "RUP"}` on connect
4. Listen for `registered` event — contains full game state including all player stacks (in cents, divide by 100)
5. Player stacks are in `gameState.players.{id}.stack`

### log_v3 API
- `GET https://www.pokernow.com/api/games/{gameId}/log_v3?hand_number={N}` — returns all entries for a specific hand

### Rate Limiting
- PokerNow rate-limits aggressively (429 errors)
- Use 0.5-1.2 second delays between requests
- Retry with exponential backoff (2s, 4s) on 429

## Ledger Calculation

### Buy-in Sources (money entering the game)
- `"The admin approved the player ... participation with a stack of X"` — primary buy-in source
- Positive diffs from `"The admin updated the player ... stack from A to B"` (B > A = top-up)
- **DO NOT** count `"joined the game with a stack of X"` as buy-in — this includes re-joins where player brings back their own chips

### Cash-out Sources (money leaving the game)
- `"The player ... quits the game with a stack of X"`
- Negative diffs from admin stack updates (B < A = partial cashout)

### Active Player Stacks
- For players still at the table: get current stack from Socket.IO `rup` response
- Fallback: last known stack from "joined" or "stand up" messages
- Zero-sum correction: if exactly 1 active player's stack is uncertain, derive it from the constraint that all nets must sum to 0

### Settlement Algorithm
- Greedy debtor/creditor matching
- Sort debtors (negative net) and creditors (positive net) by amount
- Match largest debtor with largest creditor, transfer minimum of the two
- Minimizes number of transactions

## Statistics Calculation

### Standard Stats (non-bomb-pot hands only)
- **VPIP**: Voluntarily Put $ In Pot — player called, raised, or bet preflop (blind posts do NOT count)
- **PFR**: Pre-Flop Raise — player raised or bet preflop (calls do NOT count)
- Bomb pot hands are excluded from standard stats because the forced ante is not voluntary

### Bomb Pot Stats (bomb-pot hands only)
- **BP VPIP**: Player voluntarily put money in beyond the forced bomb pot ante (any bet/call/raise on flop/turn/river)
- Bomb pots are detected by `"(bomb pot bet)"` in the action text

### PokerNow "calls X" Semantics
- In PokerNow logs, `"calls X"` means the player's TOTAL for the current betting round is X (NOT X additional)
- Same as `"raises to X"` — both represent the total round investment
- To compute actual chips spent: `additional = X - previous_round_investment`
- This is critical for accurate hand parsing and EV computation

## All-in EV Analysis

### Overview
When players go all-in, computes expected value based on card equity vs actual result. The difference is "EV luck."

### Flow
1. `_find_allin_lock(hand)` — finds the point where all-in is locked (type 14 AllInApproval, or last all-in bet with no further betting)
2. `_expected_payout(hand, lock_idx, mc_trials)` — Monte Carlo simulation of remaining board cards
3. Compares expected payout (equity-based) vs actual payout

### Game Type Support
- **Hold'em**: `eval7.evaluate(2 hole + 5 board)` — 1 eval per player per trial
- **PLO (4 hole cards)**: Must use exactly 2 from hole + 3 from board. C(4,2) x C(5,3) = 60 evals per player per trial
- **PLO5 (5 hole cards)**: C(5,2) x C(5,3) = 100 evals per player per trial

### Double Board Support
- Both boards draw from the SAME deck in each MC trial
- Draw `missing1 + missing2` cards at once from shuffled deck
- First `missing1` cards complete board 1, next `missing2` complete board 2
- Board 1 winners split 50% of each pot, board 2 winners split 50%
- If same player wins both boards, they scoop 100%

### MC Trial Counts
- Hold'em: 33,000 trials
- PLO4: 3,300 trials
- PLO5: 1,300 trials
- EV is computed on-demand (separate button) because it's slow

## CSV Log Format
```
entry,at,order
"-- starting hand #1 (id: xxx)  Pot Limit Omaha Hi (dealer: ""Name @ ID"") --",timestamp,order
"Player stacks: #1 ""Name @ ID"" (100.00) | ...",timestamp,order
"""Name @ ID"" posts a small blind of 0.50",timestamp,order
...
```
- Entries are in reverse chronological order (newest first)
- Player names are in format `"Name @ UniqueID"`
- CSV uses double-quotes for escaping (`""` = literal `"`)
- Second board lines have `(second board)` in the text: `"Flop (second board): [...]"`

## Deployment (Render)

### Requirements
- Python 3.11 (pinned via `.python-version`) — required because eval7 has no pre-built wheels for 3.14
- `requirements.txt`: python-socketio[client], websocket-client, requests, simple-websocket, eval7
- Start command: `python server.py`
- Listens on `0.0.0.0:$PORT`

### Common Issues
- eval7 fails to build on Python 3.14 (needs Cython + C compiler) — pin to 3.11
- Cloudflare blocks Python urllib — server falls back to `requests` library if `curl` not available
- Socket.IO needs session cookies from game page before connecting

## Equity Explorer

### Overview
Interactive PLO5 equity tool. Users pick a board, assign hand categories to 2+ players (e.g., "Top Set vs 13-out Wrap"), and compute equity via Monte Carlo. Supports single deals and bulk (Multiple Trials) mode with SSE streaming.

### `equity_categories.py`
Generates representative 5-card PLO5 hands matching named categories on a given board.

**Categories** (defined in `CATEGORIES` dict):
- **Made hands**: top_set, middle_set, bottom_set, trips, overpair, two_pair_top, full_house, made_flush, made_straight
- **Draws**: nut_flush_draw, flush_draw, gutshot (4 outs), oesd (8 outs), wrap_9, wrap_13, wrap_16, wrap_17, wrap_20, combo_draw

**Wrap system** — wraps use exact canonical outs (not ranges):
- `_wrap_target_ranks(board_ranks, target_outs)` computes structural H/B patterns for each wrap type
- 3-seed wraps (9, 13, 17 outs): 5-rank window, 2B+3H
- 4-seed wraps (16, 20 outs): 6-7 rank window, 2-3B+4H
- 9-out uses BHHHB pattern with 3rd board card distance check (>= 4 ranks from all seeds)
- Post-filter removes targets where any 2-card seed combo + board forms a made straight
- `_gen_wrap()` uses targeted seeds weighted 10x + general consecutive-rank fallback seeds
- `_count_straight_outs()` counts individual CARDS not ranks (holding duplicate seed rank reduces available suits)

**Blocker system**:
- Flush draw blockers: extra cards of the flush suit
- Set blockers: 3rd card of set rank + other board rank cards
- Wrap blockers: extra cards of seed ranks (pairs). N blockers = N paired seed ranks in the hand
- Max blockers: 2 for 3-seed wraps, 1 for 4-seed wraps (constrained by 5-card hand size)
- `_blocker_cards_for_category()` merges seed ranks from ALL target patterns into the pool
- Server limits actual locked ranks to seed_count (3 or 4) per trial to avoid exceeding hand size

**Key functions**:
- `list_valid_categories(board)` — returns which categories are possible, with fixed cards, blocker pools, max_blockers
- `generate_hands(board, category, count, dead, locked, outs_adjust)` — main API
- `_validate_hand_for_category(hand, board, category, outs_adjust)` — validates a hand against category rules

**Known limitation**: `_wrap_target_ranks` can produce false-positive targets on boards where the structural pattern exists but actual outs differ (e.g., K-9-2 for 13-out: structural target [T,J,Q] exists but actual outs are only 9 due to wide gap between rank 2 and 9).

### `POST /api/equity/explore`
- **Single mode**: generates 1 matchup, returns hands + equity
- **Bulk mode** (`mode: 'bulk'`): streams results via SSE, generates `samples` matchups
  - Pre-validates categories with `list_valid_categories` before generation (fast-fail)
  - Random board mode: tries up to 200 boards per trial, validates all categories are possible
  - Scaled timeout: `max(30, samples * 2)` seconds
  - Wrap blocker locking: picks one coherent seed subset (3 or 4 ranks), pairs N of them
  - SSE progress events every 10 samples

### Preset Boards
Defined in `index.html` as `EQUITY_PRESETS`. Each preset has 10 empirically validated boards that support the target category. Boards were verified to produce exact canonical outs.

## Testing

### `test_stats_engine.py` (131 tests)
Covers: derive_positions, compute_all_stats (VPIP, PFR, 3-bet, 4-bet, fold-to-3bet, fold-to-4bet, c-bet, steal, WTSD, donk bet, AF, bomb pot stats), compute_winnings, _compute_deltas, equity calculator, all-in EV, side pots.

### `test_equity_categories.py` (136 tests)
Covers: _wrap_target_ranks (all wrap types, distance checks, post-filter), _count_straight_outs, _gen_wrap (exact outs), _validate_hand_for_category, list_valid_categories, _blocker_cards_for_category, generate_hands (locked cards, outs_adjust), preset board validation (all 50 boards), made-hand generators, server blocker logic simulation, broad coverage across 8 diverse boards.

## File Structure
```
index.html                  — Frontend (single HTML file with embedded CSS/JS)
server.py                   — HTTP server + API endpoints + Socket.IO + PokerNow proxy
stats_engine.py             — All statistics computation + EV analysis + equity calculator
equity_categories.py        — PLO5 hand category engine (wraps, sets, draws, blockers)
csv_parser.py               — CSV and JSON log parser
test_stats_engine.py        — Unit tests for stats engine (131 tests)
test_equity_categories.py   — Unit tests for equity categories (136 tests)
requirements.txt            — Python dependencies
.python-version             — Pins Python 3.11 for Render
```

## Contributors
- Evan Cantwell (Belugaluga2) — original implementation
- Riley Bonner (tatty2004) — additional statistics, equity explorer, stats engine, csv parser, EV analysis
