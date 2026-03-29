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

### Known Issue
The EV calculations may not be net-zero across all players. This needs investigation — likely related to pot building, refund handling, or the settlement index calculation.

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

## File Structure
```
index.html          — Frontend (single HTML file with embedded CSS/JS)
server.py           — HTTP server + API endpoints + Socket.IO + PokerNow proxy
stats_engine.py     — All statistics computation + EV analysis + equity calculator
csv_parser.py       — CSV and JSON log parser
requirements.txt    — Python dependencies
.python-version     — Pins Python 3.11 for Render
```

## Contributors
- Evan Cantwell (Belugaluga2) — original implementation
- Riley Bonner — additional statistics
- tatty2004 — stats engine, csv parser, EV analysis
