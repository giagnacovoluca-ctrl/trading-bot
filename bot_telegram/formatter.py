"""
formatter.py — costruisce i messaggi Telegram (HTML) dai record segnale.
PREMIUM: dettaglio completo (prezzo al segnale, liq, BSR, link).
FREE: teaser ritardato, SENZA prezzo (incentivo all'upgrade).
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


def _format_midcap(row: dict, teaser: bool) -> str:
    """midcap_scanner: coppie spot su CEX (binance/mexc/gateio), schema diverso
    dai segnali on-chain — niente address/liquidity/BSR, ma score/RSI/ADX/mcap."""
    sym       = _e(row.get("token_symbol") or row.get("symbol", "?"))
    pair      = _e(row.get("pair_address") or row.get("symbol", ""))
    direction = _e(row.get("direction", "?"))
    score     = _f(row.get("score"), None)
    score_s   = f" · score {score:.0f}" if score is not None else ""
    lines = [
        f"📊 <b>Mid-Cap Squeeze</b> · CEX SPOT",
        f"<b>${sym}</b> ({pair}) · {direction}{score_s}",
    ]
    if teaser:
        lines += [
            f"7d {_fmt_pct(row.get('change_7d'))} · Mcap {_fmt_usd((row.get('mcap_m') or 0) and float(row['mcap_m'])*1e6)}",
            "",
            "🔒 <i>Price, RSI/ADX and full details reserved for Premium members.</i>",
            "➡️ /plans for real-time access.",
        ]
    else:
        lines += [
            (f"Price <code>{_e(_fmt_usd(row.get('price_usd') or row.get('price')))}</code> · "
             f"7d {_fmt_pct(row.get('change_7d'))} · "
             f"Mcap {_fmt_usd((row.get('mcap_m') or 0) and float(row['mcap_m'])*1e6)}"),
            f"RSI {_e(row.get('rsi', '—'))} · ADX {_e(row.get('adx', '—'))} · "
            f"Vol24h {_fmt_usd((row.get('volume_24h_m') or 0) and float(row['volume_24h_m'])*1e6)}",
        ]
    lines.append("\n<i>⚠️ Not financial advice. DYOR.</i>")
    return "\n".join(lines)


def format_closure_free(info: dict) -> str:
    """Positive closure notification for FREE channel."""
    sym = _e(info.get("symbol", "?"))
    system = info.get("system", "")
    label = _SYS_LABEL.get(system, system)
    chain = (info.get("chain", "") or "").lower()
    chain_s = f" · {_e(chain.upper())}" if chain else ""
    pnl = info.get("pnl_eur", 0.0)
    invested = info.get("invested_eur")
    pct = info.get("pct")
    lines = [
        f"✅ <b>Trade closed</b> · {_e(label)}{chain_s}",
        f"<b>${sym}</b>",
    ]
    if invested and invested > 0:
        lines.append(f"💰 Invested: <code>€{invested:.2f}</code>")
    if pct is not None:
        lines.append(f"📈 Result: <b>{pct:+.1f}%</b> · <b>+€{pnl:.2f}</b>")
    else:
        lines.append(f"📈 Profit: <b>+€{pnl:.2f}</b>")
    lines += ["", "💎 Get real-time entry signals → /plans"]
    return "\n".join(lines)


def format_full(row: dict, system: str) -> str:
    """Messaggio completo per canale PREMIUM."""
    if system == "midcap":
        return _format_midcap(row, teaser=False)
    chain = (row.get("chain", "") or "").lower()
    emoji = _CHAIN_EMOJI.get(chain, "🔹")
    label = _SYS_LABEL.get(system, system)
    sym = _e(row.get("token_symbol", "?"))
    prob = _f(row.get("pump_probability"), None)
    prob_s = f" · prob {prob:.2f}" if (prob is not None and prob > 0.001) else ""

    lines = [
        f"{emoji} <b>{label}</b> · {_e(chain.upper())}",
        f"<b>${sym}</b>{prob_s}",
        (f"Signal price <code>{_e(_fmt_usd(row.get('price_entry_usd')))}</code> · "
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
    lines.append("\n<i>⚠️ Not financial advice. DYOR.</i>")
    return "\n".join(lines)


def format_teaser(row: dict, system: str) -> str:
    """Teaser FREE: prezzo storico (al momento del segnale, 15m fa)."""
    if system == "midcap":
        return _format_midcap(row, teaser=True)
    chain = (row.get("chain", "") or "").lower()
    emoji = _CHAIN_EMOJI.get(chain, "🔹")
    label = _SYS_LABEL.get(system, system)
    sym = _e(row.get("token_symbol", "?"))
    prob = _f(row.get("pump_probability"), None)
    prob_s = f" · prob {prob:.2f}" if (prob is not None and prob > 0.001) else ""
    entry = _fmt_usd(row.get("price_entry_usd"))
    lines = [
        f"{emoji} <b>{label}</b> · {_e(chain.upper())}",
        f"<b>${sym}</b>{prob_s}",
        f"⏱ Signal price (15m ago): <code>{entry}</code>",
        f"1h {_fmt_pct(row.get('change_1h_pct'))} · Liq {_fmt_usd(row.get('liquidity_usd'))}",
        "",
        "🔒 <i>BSR, charts and real-time signals are reserved for Premium members.</i>",
        "➡️ /plans for real-time access.",
    ]
    return "\n".join(lines)
