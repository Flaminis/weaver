# CLOB V2 migration — LOL-Trade

**Status:** migrated. No smaug-core dependency — this project has its own
direct `py_clob_client_v2` integration.
**Cutover:** April 22, 2026 ~11:00 UTC.

---

## What changed in this repo

| File | Change |
|---|---|
| `scripts/polymarket/client.py` | V2 imports (with the existing `try/except ImportError` fallback preserved); `create_or_derive_api_key`; `cancel_order(OrderPayload(...))`; `get_open_orders()` |
| `README.md` | Updated `pip install` command to `py-clob-client-v2` |

The WS price stream (`scripts/polymarket/ws_prices.py`), paper-trade
simulators, and web dashboard don't touch signed CLOB calls — only public
market-data endpoints, which are V1/V2-compatible.

---

## Before you deploy

```bash
cd /path/to/LOL-Trade
source .venv/bin/activate     # or .venv311/bin/activate
pip install -r requirements.txt   # (if one exists — otherwise:)
pip install py-clob-client-v2>=1.0.0
```

Restart whatever service runs the LOL trader loop.

---

## `.env` changes

LOL-Trade's `polymarket.config` uses its own settings schema. Check
`scripts/polymarket/config.py` for the exact env var names and add:

```
POLY_BUILDER_CODE=0x...
POLY_BUILDER_ADDRESS=0x...
```

Until cutover:

```
CLOB_URL=https://clob-v2.polymarket.com   # or whatever key config.py reads
```

---

## Smoke test after deploy

LOL-Trade has its own dry-run mode — use it:

```bash
python scripts/polymarket/client.py    # if exposed as runnable
# or
python -c "
from scripts.polymarket.client import poly_client
import asyncio
asyncio.run(poly_client.connect())
print('ready:', poly_client.is_ready)
print('balance:', poly_client.get_balance())
"
```

If `HAS_CLOB` ends up `False` in the import log, the V2 package didn't install.

---

## Cutover day

LOL-Trade places GTC sell orders as its maker exit. Those **will be wiped** at
cutover. The sell monitor (`check_sell_order`) will see the order disappear
and re-place on the next tick — but watch the logs to confirm.

---

## pUSD

Same rule as everyone else: the wallet LOL-Trade signs with needs pUSD to
place orders after April 22. Wrap USDC.e via the Collateral Onramp before
then.
