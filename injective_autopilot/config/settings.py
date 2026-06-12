from __future__ import annotations

from pathlib import Path
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict

_HERE = Path(__file__).resolve().parent.parent  # → injective_autopilot/

# Top 30 perpetual markets on Injective mainnet, ranked by open interest (2026-06-09)
TOP_30_MARKET_IDS: list[str] = [
    "0xae6a59cf878786e337905f34c6bf0142a3f382d9f6153a91dbb0911e0081e76d",  # PEPE
    "0xc5a03089cff2aa0364864b428a11654b8e8e34a0476f9cd81f52fe518fde66e1",  # PUMP
    "0xb846b1623674f02ab48faec679d4bd31ed40b6a125f9605be6f99131286e8492",  # OP
    "0xb562b7a440a435e0065d1a22ab74f1f40f25e35476854cd62ee2897c444a287d",  # DOGE
    "0xeee0f9cf374a8e20a2d0e20b827984f8a398615cf633fcfdec0323d8c1a4877a",  # TIA
    "0x771f3b734d5f6b5a5f0d0584a027655744aba77130ca0ea5090d9332a6586c8b",  # ARB
    "0x142846ceefba05b25d0da3faa67ce6f29a73bf1b2da30d87129b3a16a20d76ec",  # WIF
    "0xaea49da65a3676005195f1a255b16b246f6c7dd9b01597354568be1fd11cd21e",  # S
    "0xacaafac6a484ac5a41a7f5914b1c01bff55ea40b67283746447ba29a949ed781",  # SEI
    "0x790aee464fbbd02cf4476444554c71d1225f7edfe15e6dc7f874c455fd883d31",  # INJ
    "0xcf40fa9f6f7ee66e71d3750d8fb8a69b85cd084c628d804db477274652e6c693",  # TON
    "0x33353622e471d42f4e42334f0dc1f3b49738f18abf4c75b240d4f6ba5f2a52d8",  # ATOM
    "0xff2ff43d84ce9786d8cc342ed6f6f699aef0a5015b7ffd60891448192f8679b7",  # TRUMP
    "0x2dd4d0b4f3e8f59fde6bd10dde3c234d45e1fa3e3b122c9f08d313c52aa515e0",  # DRIFT
    "0x34b37a03ba1f002f25aa804558972aaff44844088acc92b8dce61e8b2103a6f5",  # HOOD
    "0x9e95b6e5f29291dbe1d64eff9ac1fbd63a92124dc9fc7ca5941cbd635a18a410",  # ADA
    "0xcf98ddf04e0d858561f0ecdfbf2ea4bcf8fd60f78481f78a7454b35ec6faf2ea",  # SUI
    "0xaa8380d7fc430481bfe398ab82a9fa0d61ea0bfad45398fc3f02a10fe204e2b8",  # MANTA
    "0x5d379fa10f0a74cd5983391b7391fa72af9cb4c0740a72adb1aed966e78103f3",  # HYPE
    "0xc27b4c131562c9b0d3ddcec8cd627dd1199422051adaa2e5986bb605734b5ccc",  # AVAX
    "0x1ecae98c2b02aa2871d7ce58380d130b10af87b6254c62635f3d0045835140be",  # ONDO
    "0x190fe9419c623610ecab416e582b16bbca794b64f04f216348d6f5159e6363be",  # SCROLL
    "0x291f404810c663e5fdd68b50454c1014760227842d420a2315f88311efe6ec41",  # SOL
    "0xed6d8aad45b25a6ba782ea90b3a82d7ed1ac62d7e88f52b52d9ba0b2ff046d5c",  # LAYER
    "0xf5328304b9f25c429f60682f5b62c261c78cdcbf62a6f5cc07493837a7cf1c48",  # TRX
    "0x5dec5f8bbd072dd1a361c45856516b0cefcbd74b3ea93a2075b5e208395b4136",  # ZRO
    "0xae60d32c901e0fcd36951ad9c80947e6a3206d0fabe089064fba5b4dbe87c088",  # TAO
    "0x324dd1a31695cb8cd7243f9e04286492653c9c146d7576379fb2848ab7cab76a",  # AAVE
    "0xcf98ddf04e0d858561f0ecdfbf2ea4bcf8fd60f78481f78a7454b35ec6faf2ea",  # SUI (dup-safe)
    "0xe9c90a90ec75194ba9693f12b58a88a06937599e00c2adbc565a7b1a6ffbe4ed",  # ETH
]

# Ticker name lookup for prompts and logs
MARKET_TICKER: dict[str, str] = {
    "0xae6a59cf878786e337905f34c6bf0142a3f382d9f6153a91dbb0911e0081e76d": "PEPE",
    "0xc5a03089cff2aa0364864b428a11654b8e8e34a0476f9cd81f52fe518fde66e1": "PUMP",
    "0xb846b1623674f02ab48faec679d4bd31ed40b6a125f9605be6f99131286e8492": "OP",
    "0xb562b7a440a435e0065d1a22ab74f1f40f25e35476854cd62ee2897c444a287d": "DOGE",
    "0xeee0f9cf374a8e20a2d0e20b827984f8a398615cf633fcfdec0323d8c1a4877a": "TIA",
    "0x771f3b734d5f6b5a5f0d0584a027655744aba77130ca0ea5090d9332a6586c8b": "ARB",
    "0x142846ceefba05b25d0da3faa67ce6f29a73bf1b2da30d87129b3a16a20d76ec": "WIF",
    "0xaea49da65a3676005195f1a255b16b246f6c7dd9b01597354568be1fd11cd21e": "S",
    "0xacaafac6a484ac5a41a7f5914b1c01bff55ea40b67283746447ba29a949ed781": "SEI",
    "0x790aee464fbbd02cf4476444554c71d1225f7edfe15e6dc7f874c455fd883d31": "INJ",
    "0xcf40fa9f6f7ee66e71d3750d8fb8a69b85cd084c628d804db477274652e6c693": "TON",
    "0x33353622e471d42f4e42334f0dc1f3b49738f18abf4c75b240d4f6ba5f2a52d8": "ATOM",
    "0xff2ff43d84ce9786d8cc342ed6f6f699aef0a5015b7ffd60891448192f8679b7": "TRUMP",
    "0x2dd4d0b4f3e8f59fde6bd10dde3c234d45e1fa3e3b122c9f08d313c52aa515e0": "DRIFT",
    "0x34b37a03ba1f002f25aa804558972aaff44844088acc92b8dce61e8b2103a6f5": "HOOD",
    "0x9e95b6e5f29291dbe1d64eff9ac1fbd63a92124dc9fc7ca5941cbd635a18a410": "ADA",
    "0xcf98ddf04e0d858561f0ecdfbf2ea4bcf8fd60f78481f78a7454b35ec6faf2ea": "SUI",
    "0xaa8380d7fc430481bfe398ab82a9fa0d61ea0bfad45398fc3f02a10fe204e2b8": "MANTA",
    "0x5d379fa10f0a74cd5983391b7391fa72af9cb4c0740a72adb1aed966e78103f3": "HYPE",
    "0xc27b4c131562c9b0d3ddcec8cd627dd1199422051adaa2e5986bb605734b5ccc": "AVAX",
    "0x1ecae98c2b02aa2871d7ce58380d130b10af87b6254c62635f3d0045835140be": "ONDO",
    "0x190fe9419c623610ecab416e582b16bbca794b64f04f216348d6f5159e6363be": "SCROLL",
    "0x291f404810c663e5fdd68b50454c1014760227842d420a2315f88311efe6ec41": "SOL",
    "0xed6d8aad45b25a6ba782ea90b3a82d7ed1ac62d7e88f52b52d9ba0b2ff046d5c": "LAYER",
    "0xf5328304b9f25c429f60682f5b62c261c78cdcbf62a6f5cc07493837a7cf1c48": "TRX",
    "0x5dec5f8bbd072dd1a361c45856516b0cefcbd74b3ea93a2075b5e208395b4136": "ZRO",
    "0xae60d32c901e0fcd36951ad9c80947e6a3206d0fabe089064fba5b4dbe87c088": "TAO",
    "0x324dd1a31695cb8cd7243f9e04286492653c9c146d7576379fb2848ab7cab76a": "AAVE",
    "0xe9c90a90ec75194ba9693f12b58a88a06937599e00c2adbc565a7b1a6ffbe4ed": "ETH",
}

# Deduplicate preserving order
_seen: set[str] = set()
_deduped: list[str] = []
for _mid in TOP_30_MARKET_IDS:
    if _mid not in _seen:
        _seen.add(_mid)
        _deduped.append(_mid)
TOP_30_MARKET_IDS = _deduped


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_HERE / ".env"),
        env_prefix="INJ_",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Operational mode ─────────────────────────────────────────────
    mode: Literal["BACKTEST", "PAPER", "LIVE"] = "PAPER"

    # ── Injective network ─────────────────────────────────────────────
    network: Literal["mainnet", "testnet"] = "mainnet"
    # Multi-market: top 30 perpetuals by OI (override via INJ_MARKET_IDS env var)
    market_ids: list[str] = TOP_30_MARKET_IDS
    quote_asset: str = "USDC"

    @property
    def market_id(self) -> str:
        """Primary market (first in list) — used for LIVE order execution default."""
        return self.market_ids[0] if self.market_ids else ""

    # Wallet (keep empty for PAPER/BACKTEST)
    private_key: str = ""
    subaccount_index: int = 0
    fee_recipient: str = ""

    # ── Multi-market risk ─────────────────────────────────────────────
    max_open_positions: int = 5          # max concurrent open trades across all markets
    max_capital_per_market_pct: float = 0.20  # max 20% capital in any single market

    # ── Sentinel loop ─────────────────────────────────────────────────
    sentinel_interval_sec: int = 60          # main polling cadence
    sentinel_max_triggers_per_hour: int = 20  # rate limit on Gemini calls (total, all markets)
    sentinel_trigger_cooldown_min: int = 2    # per-market cooldown (PAPER: avoid exact duplicates; LIVE: raise to 30+)
    orderbook_depth: int = 20                # levels to fetch
    lookback_candles: int = 200              # candles kept in memory
    rpc_timeout_sec: float = 10.0            # max wait per Injective RPC call before treating it as failed

    # ── Re-check tesi di trade (no max-hold fisso) ────────────────────
    recheck_after_min: float = 90.0          # dopo N min, verifica se i segnali di apertura sono ancora attivi
    recheck_min_overlap: int = 1             # se < N segnali originali sono ancora attivi → tesi invalidata, chiudi a mercato

    # ── Signal thresholds ─────────────────────────────────────────────
    obi_threshold: float = 0.60              # |OBI| > threshold → signal
    obi_min_persist_ticks: int = 3           # OBI must persist N ticks
    funding_zscore_threshold: float = 2.5   # |z| > threshold → extreme
    funding_lookback: int = 72               # hours of funding history
    cvd_divergence_threshold: float = 0.65  # CVD/price divergence z-score
    vol_regime_ratio_high: float = 1.5      # short/long vol ratio → expansion
    vol_regime_ratio_low: float = 0.70      # short/long vol ratio → contraction
    oi_price_div_threshold: float = 0.015   # 1.5% divergence threshold
    zscore_entry_threshold: float = 2.0     # z-score for mean-reversion entry
    liquidity_void_pct: float = 0.003       # gap ≥ 0.3% = liquidity void

    # ── Risk ──────────────────────────────────────────────────────────
    capital_usdt: float = 1000.0             # total capital to trade with
    max_position_pct: float = 0.10           # max 10% per trade
    max_leverage: float = 5.0
    min_rr_ratio: float = 2.0               # minimum net R:R (after funding)
    atr_sl_multiplier: float = 2.0          # SL = entry ± ATR * mult
    atr_tp_multiplier: float = 4.0          # TP = entry ± ATR * mult

    # Kill switch
    max_daily_drawdown_pct: float = 0.05         # 5% daily DD → kill (LIVE)
    paper_max_daily_drawdown_pct: float = 0.15   # 15% daily DD → kill (PAPER/BACKTEST)
    max_weekly_drawdown_pct: float = 0.10        # 10% weekly DD → kill (LIVE)
    paper_max_weekly_drawdown_pct: float = 0.15  # 15% weekly DD → kill (PAPER/BACKTEST)
    max_margin_used_pct: float = 0.80        # 80% margin used → kill
    max_consecutive_errors: int = 5

    # ── Decision engine ───────────────────────────────────────────────
    decision_min_confidence: float = 0.45    # score minimo per approvare un trade
    decision_max_spread_bps: float = 80.0    # spread massimo accettabile (inj perp: 20-100 bps)

    # ── Database ──────────────────────────────────────────────────────
    db_url: str = f"sqlite+aiosqlite:///{_HERE}/injective_autopilot.db"

    # ── Dashboard ─────────────────────────────────────────────────────
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080
    dashboard_auto_refresh_sec: int = 10

    # ── Live validation gate ───────────────────────────────────────────
    live_min_simulated_trades: int = 50
    live_min_profit_factor: float = 1.2
    live_min_sharpe: float = 0.8
    live_max_drawdown_pct: float = 0.25
    live_min_stable_days: int = 3


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
