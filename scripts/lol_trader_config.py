"""
All constants and thresholds for the LoL Polymarket trading bot.

Entry: FAK buy (taker)
Exit: HOLD TO RESOLUTION — no active selling. Winning shares pay $1.00.
"""

# ── Entry gates ─────────────────────────────────────────────────────────

TRADE_MIN_PRICE = 0.02          # Don't buy below 2c
TRADE_MAX_PRICE = 0.85          # Don't buy above 85c
MAX_SPREAD = 0.02               # Max 2c spread
MIN_EDGE = 0.02                 # v2: impact_prior - spread must exceed this
MIN_BOOK_DEPTH = 30             # $ depth within 3c of best (buy side)
PRICED_IN_WINDOW_SEC = 2.0      # Look-back window for priced-in check
PRICED_IN_THRESHOLD = 0.045     # v2: stricter on noisy legs (was 5c flat)
NEAR_RESOLVED_FLOOR = 0.03      # Skip markets priced below 3c
NEAR_RESOLVED_CEIL = 0.97       # Skip markets priced above 97c

# ── Exit ────────────────────────────────────────────────────────────────
# Strategy: HOLD TO RESOLUTION. No active selling.
# Polymarket redeems winning shares at $1.00, losing shares at $0.00.
# Resolution detected via _check_finished_matches (price near 0/1 + PandaScore finished).

# ── Sizing ──────────────────────────────────────────────────────────────

BET_SIZE_BASE = 10.0            # $10 base per trade (objectives)
KILL_SIZE_1 = 10.0              # $10 on a single kill
KILL_SIZE_2 = 40.0              # $40 on 2 stacked kills
KILL_SIZE_3PLUS = 100.0         # $100 on 3+ stacked kills
MAX_SINGLE_BET = 100.0          # $100 max single trade
MAX_TOTAL_EXPOSURE = 300.0      # $300 max across all open positions

# ── Cooldowns ───────────────────────────────────────────────────────────

TOKEN_COOLDOWN_SEC = 30         # Don't re-enter same token for 30s
MATCH_COOLDOWN_SEC = 5          # Min 5s between trades on same match

# ── PandaScore ──────────────────────────────────────────────────────────

PS_BASE = "https://api.pandascore.co"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
ESPORTS_TAG_ID = 64

# ── LLF ─────────────────────────────────────────────────────────────────

LLF_RECONNECT_DELAY = 5
LLF_NOT_OPEN_DELAY = 30
LLF_RECV_TIMEOUT = 300

# ── Combo detection (signal v2) ─────────────────────────────────────────

COMBO_WINDOW_SEC = 30           # Events within 30s are part of same combo
TEAMFIGHT_KILL_THRESHOLD = 3    # Label "teamfight" when >= this in window
TEAMFIGHT_WINDOW_SEC = 15       # Kill burst window (stacked kills for same team)
POST_OBJECTIVE_KILL_WINDOW_SEC = 45.0  # KILL right after baron/inhib/drake counts as "follow-up"

# ── Logging ─────────────────────────────────────────────────────────────

LOG_TRADES_DIR = "logs"
