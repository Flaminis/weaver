"""
All constants and thresholds for the LoL Polymarket trading bot.

Entry: FAK buy (taker)
Exit: HOLD TO RESOLUTION — no active selling. Winning shares pay $1.00.
"""

# ── Entry gates ─────────────────────────────────────────────────────────

TRADE_MIN_PRICE = 0.02          # Don't buy below 2c
TRADE_MAX_PRICE = 0.85          # Don't buy above 85c
MAX_SPREAD = 0.02               # Max 2c spread
MIN_BOOK_DEPTH = 30             # $ depth within 3c of best (buy side)
PRE_EVENT_WINDOW_SEC = 2.0      # Look-back window for estimating pre-event mid
NEAR_RESOLVED_FLOOR = 0.03      # Skip markets priced below 3c
NEAR_RESOLVED_CEIL = 0.97       # Skip markets priced above 97c

# ── Exit ────────────────────────────────────────────────────────────────
# Strategy: HOLD TO RESOLUTION. No active selling.
# Polymarket redeems winning shares at $1.00, losing shares at $0.00.
# Resolution detected via _check_finished_matches (price near 0/1 + PandaScore finished).

# ── EV / Sizing (Kelly) ────────────────────────────────────────────────

TAKER_FEE = 0.02                # Polymarket taker fee on FAK buys (~2%)
MIN_EDGE = 0.02                 # 2c minimum edge after fees to fire a trade
KELLY_FRACTION = 0.25           # Quarter Kelly — conservative for model uncertainty
BANKROLL = 1500.0               # Total bankroll for Kelly sizing
MIN_BET = 5.0                   # Floor — below this not worth execution overhead
MAX_SINGLE_BET = 250.0          # Ceiling per trade
MAX_BOOK_PARTICIPATION = 0.30   # Never take more than 30% of available book depth

# ── PandaScore ──────────────────────────────────────────────────────────

PS_BASE = "https://api.pandascore.co"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
ESPORTS_TAG_ID = 64

# ── LLF ─────────────────────────────────────────────────────────────────

LLF_RECONNECT_DELAY = 5
LLF_NOT_OPEN_DELAY = 30
LLF_RECV_TIMEOUT = 300

# ── Model re-score cadence ─────────────────────────────────────────────
# Time is a signal: 5-0 at min 1 != 5-0 at min 30. Even when LLF is silent
# between kills/objectives, the model's P(win) should drift as game_minute
# advances. These control the periodic re-scoring loop.

MODEL_RESCORE_SEC = 5           # Re-score every N seconds for each live game
MODEL_RESCORE_DEDUP_SEC = 1.5   # Skip append if last log entry is within this gap

# ── Model selection ────────────────────────────────────────────────────
# "v2" = winprob_lgbm_v2.joblib (momentum + champion features, 13 inputs)
# "v1" = winprob_lgbm.joblib (baseline, 9 inputs)
# Emergency rollback: flip to "v1" → systemctl restart lol-trade. No deploy,
# no model rebuild. Both files live in data/models/ side by side.

PRIMARY_MODEL = "v2"            # Drives all trade sizing / impact decisions
MODEL_FALLBACK_TO_V1_ON_ERROR = True  # If v2 inference throws, log and retry on v1

# ── Combo detection (signal v2) ─────────────────────────────────────────

COMBO_WINDOW_SEC = 30           # Events within 30s are part of same combo
TEAMFIGHT_KILL_THRESHOLD = 3    # Label "teamfight" when >= this in window
TEAMFIGHT_WINDOW_SEC = 15       # Kill burst window (stacked kills for same team)
POST_OBJECTIVE_KILL_WINDOW_SEC = 45.0  # KILL right after baron/inhib/drake counts as "follow-up"

# ── Logging ─────────────────────────────────────────────────────────────

LOG_TRADES_DIR = "logs"
