"""
formatter.py — costruisce i messaggi Telegram (HTML) dai record segnale.
PREMIUM/VIP: dettaglio completo (entry, liq, BSR, link).
FREE: teaser ritardato, SENZA prezzo di entry (incentivo all'upgrade).
"""
from __future__ import annotations

import html

import config

_SYS_LABEL = {
    "defi": "DeFi Gem", "pump_grad": "Pump Graduation", "pre_grad": "Pre-Graduation",
    "mirror": "Wallet Mirror", "midcap": "Mid-Cap Squeeze",
}
_CHAIN_EMOJI = {"solana": "🟣", "base": "🔵", "bsc": "🟡", "ethereum": "⚪"}


def _f(v, default="—"):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None if default is None else default


def _fmt_usd(v) -> str:
    n = _f(v, None)
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"${n/1e6:.1f}M"
    if n >= 1_000:
        return f"${n/1e3:.0f}k"
    if n >= 1:
        return f"${n:,.2f}"
    return f"${n:.8f}".rstrip("0").rstrip(".")


def _fmt_pct(v) -> str:
    n = _f(v, None)
    return "—" if n is None else f"{n:+.1f}%"


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _links(row: dict) -> str:
    chain = row.get("chain", "")
    addr = row.get("token_address", "")
    pair = row.get("pair_address", "")
    parts = []
    dx = config.dexscreener_link(chain, pair, addr)
    ex = config.explorer_link(chain, addr)
    if dx:
        parts.append(f'<a href="{_e(dx)}">📊 Chart</a>')
    if ex:
        parts.append(f'<a href="{_e(ex)}">⛓ Explorer</a>')
    return "  ·  ".join(parts)


def format_full(row: dict, system: str) -> str:
    """Messaggio completo per canale PREMIUM/VIP."""
    chain = (row.get("chain", "") or "").lower()
    emoji = _CHAIN_EMOJI.get(chain, "🔹")
    label = _SYS_LABEL.get(system, system)
    sym = _e(row.get("token_symbol", "?"))
    prob = _f(row.get("pump_probability"), None)
    prob_s = f" · prob {prob:.2f}" if prob is not None else ""

    lines = [
        f"{emoji} <b>{label}</b> · {_e(chain.upper())}",
        f"<b>${sym}</b>{prob_s}",
        (f"Entry <code>{_e(_fmt_usd(row.get('price_entry_usd')))}</code> · "
         f"Vol1h {_fmt_usd(row.get('volume_1h_usd'))} · "
         f"Liq {_fmt_usd(row.get('liquidity_usd'))}"),
        (f"BSR {_e(row.get('buy_sell_ratio_1h', '—'))} · "
         f"1h {_fmt_pct(row.get('change_1h_pct'))}"),
    ]
    lp = row.get("lp_locked")
    if lp not in (None, ""):
        lines.append(f"LP lock: {_e(lp)}  ·  honeypot: {_e(row.get('is_honeypot', '?'))}")
    links = _links(row)
    if links:
        lines.append(links)
    lines.append("\n<i>⚠️ Non è consulenza finanziaria. DYOR.</i>")
    return "\n".join(lines)


def format_teaser(row: dict, system: str) -> str:
    """Teaser FREE: niente prezzo di entry, niente link diretto immediato."""
    chain = (row.get("chain", "") or "").lower()
    emoji = _CHAIN_EMOJI.get(chain, "🔹")
    label = _SYS_LABEL.get(system, system)
    sym = _e(row.get("token_symbol", "?"))
    prob = _f(row.get("pump_probability"), None)
    prob_s = f" · prob {prob:.2f}" if prob is not None else ""
    lines = [
        f"{emoji} <b>{label}</b> · {_e(chain.upper())}",
        f"<b>${sym}</b>{prob_s}",
        f"1h {_fmt_pct(row.get('change_1h_pct'))} · Liq {_fmt_usd(row.get('liquidity_usd'))}",
        "",
        "🔒 <i>Entry price, BSR e link in tempo reale sono riservati ai membri "
        "Premium. Questo teaser arriva con 15 min di ritardo.</i>",
        "➡️ /plans per l'accesso real-time.",
    ]
    return "\n".join(lines)
