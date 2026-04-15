from __future__ import annotations
import os
from pathlib import Path
from dotenv import load_dotenv

_BASE = Path(__file__).resolve().parent
for _candidate in [_BASE / ".env", _BASE.parent / ".env", _BASE.parent.parent / ".env"]:
    if _candidate.exists():
        load_dotenv(_candidate)
        break


class _Poly:
    private_key: str = os.environ.get("POLY_PRIVATE_KEY", "")
    chain_id: int = int(os.environ.get("POLY_CHAIN_ID", "137"))
    funder_address: str = os.environ.get("POLY_FUNDER_ADDRESS", "")
    clob_url: str = os.environ.get("POLY_CLOB_URL", "https://clob.polymarket.com")
    gamma_url: str = os.environ.get("POLY_GAMMA_URL", "https://gamma-api.polymarket.com")
    api_key: str = os.environ.get("POLY_API_KEY", "")
    api_secret: str = os.environ.get("POLY_API_SECRET", "")
    api_passphrase: str = os.environ.get("POLY_API_PASSPHRASE", "")


class _Settings:
    poly = _Poly()


settings = _Settings()
