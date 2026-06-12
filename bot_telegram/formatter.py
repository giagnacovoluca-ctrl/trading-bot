"""
formatter.py — costruisce i messaggi Telegram (HTML) dai record segnale.
PREMIUM: dettaglio completo (prezzo al segnale, liq, BSR, link).
FREE: teaser ritardato, SENZA prezzo (incentivo all'upgrade).
"""
from __future__ import annotations

import html
import re

import config

_SYS_LABEL = {
    "defi": "DeFi Gem", "pump_grad": "Pump Graduation", "pre_grad": "Pre-Graduation",
    "mirror": "Wallet Mirror", "midcap": "Mid-Cap Squeeze",
    "base_pump": "Base Pump", "v3": "Gem Hunter V3",
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


def premium_keyboard() -> dict | None:
    """Bottone deep-link al bot per i post sul canale FREE: nei canali i
    comandi /plans NON sono cliccabili (funzionano solo in chat privata)."""
    if not config.BOT_USERNAME:
        return None
    return {"inline_keyboard": [[{
        "text": f"💎 Premium — ${config.PRICE_PREMIUM_USD:.0f}/mo",
        "url": f"https://t.me/{config.BOT_USERNAME}?start=premium",
    }]]}


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
    lines += ["", "💎 Get real-time entry signals 👇"]
    return "\n".join(lines)


def format_wallet_event(row: dict) -> str:
    """Alert whale per canale PREMIUM (da wallet_events.csv del mirror bot).
    row: ts, wallet, side, mint, usd, confluence, wake_days, note"""
    side   = (row.get("side") or "").lower()
    wallet = row.get("wallet", "")
    mint   = row.get("mint", "")
    usd    = _f(row.get("usd"), 0.0) or 0.0
    confl  = int(_f(row.get("confluence"), 1) or 1)
    wake   = _f(row.get("wake_days"), 0.0) or 0.0
    note   = row.get("note", "") or ""

    if side == "sell":
        head = "🚨 <b>Smart money EXIT</b>"
        verb = "sold"
    else:
        head = "🐋 <b>Smart money BUY</b>"
        verb = "bought"

    lines = [
        f"{head} · SOL",
        f"Alpha wallet <code>{_e(wallet[:8])}…</code> {verb} ~<b>${usd:,.0f}</b>",
        f"Token: <code>{_e(mint)}</code>",
    ]
    if confl >= 2:
        lines.append(f"🔥 Confluence: <b>{confl} alpha wallets</b> on this token (6h)")
    if wake >= 1:
        lines.append(f"⏰ First buy after <b>{wake:.0f} days</b> of inactivity")
    if "sell_after_signal" in note:
        lines.append("⚠️ This token was signalled recently — distribution risk")
    lines.append(f'<a href="https://dexscreener.com/solana/{_e(mint)}">DexScreener</a>')
    lines.append("\n<i>⚠️ Not financial advice. DYOR.</i>")
    return "\n".join(lines)


def _format_v3(row: dict) -> str:
    """Gemma gemmeV3 (gems_log_v3.csv): stesso set di info dell'email DIAMOND/GOLD —
    tier, score, prezzo/mcap/liq, vol/BSR/Δ1h/età, smart money e lista segnali."""
    chain = (row.get("chain", "") or "").lower()
    emoji = _CHAIN_EMOJI.get(chain, "🔹")
    tier  = (row.get("tier") or "?").upper()
    tier_emoji = {"DIAMOND": "💎", "GOLD": "🥇", "SILVER": "🥈", "BRONZE": "🥉"}.get(tier, "🔸")
    sym   = _e(row.get("token_symbol", "?"))
    score = _f(row.get("score"), None)
    score_s = f" · score {score:.0f}/100" if score is not None else ""
    gem_class = _e(row.get("gem_class", "") or "")

    lines = [
        f"{tier_emoji} <b>GEM {tier}</b> · {emoji} {_e(chain.upper())}{score_s}",
        f"<b>${sym}</b>" + (f" · {gem_class}" if gem_class else ""),
        (f"Price <code>{_e(_fmt_usd(row.get('price_usd')))}</code> · "
         f"MCap {_fmt_usd(row.get('market_cap_usd'))} · "
         f"Liq {_fmt_usd(row.get('liquidity_usd'))}"),
        (f"Vol1h {_fmt_usd(row.get('volume_1h_usd'))} · "
         f"BSR {_e(row.get('buy_sell_ratio_1h', '—'))} · "
         f"1h {_fmt_pct(row.get('change_1h_pct'))}"),
    ]
    age = _f(row.get("pair_age_hours"), None)
    if age is not None:
        lines[-1] += f" · age {age:.1f}h"
    inflow  = _f(row.get("inflow_usd"), None)
    wallets = _f(row.get("inflow_wallet_count"), None)
    if inflow:
        sm = f"🐋 Smart money {_fmt_usd(inflow)}"
        if wallets:
            sm += f" from {wallets:.0f} wallets"
        avg_pnl = _f(row.get("avg_wallet_pnl_pct"), None)
        if avg_pnl is not None:
            sm += f" · avg PnL {avg_pnl:+.0f}%"
        lines.append(sm)
    signals = (row.get("signals") or "").strip()
    if signals:
        lines.append(f"<i>{_e(signals[:500])}</i>")
    ca = _ca_line(row)
    if ca:
        lines.append(ca)
    links = _links(row)
    if links:
        lines.append(links)
    lines.append("🤖 <i>Auto-managed: TP / trailing / stop-loss updates will follow here.</i>")
    lines.append("\n<i>⚠️ Not financial advice. DYOR.</i>")
    return "\n".join(lines)


def _ca_line(row: dict) -> str:
    """Address del token in chiaro (copiabile) — come il box 'Token Address'
    delle email: i link non bastano per incollare il CA in wallet/DEX."""
    addr = (row.get("token_address") or "").strip()
    return f"CA: <code>{_e(addr)}</code>" if addr else ""


def _feat(row: dict, key: str) -> str | None:
    """Estrae 'key=valore' dalla colonna top_features (es. vSol=86.0)."""
    feats = row.get("top_features", "") or ""
    m = re.search(re.escape(key) + r"=([^|]+)", feats)
    return m.group(1).strip() if m else None


def _format_pre_grad(row: dict) -> str:
    """Pre-graduation pump.fun: stesse info dell'email — vSol, velocity,
    prezzo bonding curve, liq stimata, mcap, piano exit."""
    sym  = _e(row.get("token_symbol", "?"))
    vsol = _feat(row, "vSol") or "?"
    vel  = _feat(row, "velocity") or "?"
    mcap = _feat(row, "mcap")
    lines = [
        "⚡ <b>Pre-Graduation</b> · 🟣 SOLANA",
        f"<b>${sym}</b>",
        f"Bonding curve: vSol <b>{_e(vsol)} SOL</b> · velocity {_e(vel)}",
        (f"Price BC <code>{_e(_fmt_usd(row.get('price_entry_usd')))}</code> · "
         f"Liq est. {_fmt_usd(row.get('liquidity_usd'))}"
         + (f" · MCap {_e(mcap)}" if mcap and mcap != "$0" else "")),
    ]
    ca = _ca_line(row)
    if ca:
        lines.append(ca)
    links = _links(row)
    if links:
        lines.append(links)
    lines.append("Plan: TP1 +40% · SL -12% · exit if no graduation in 20 min")
    lines.append("🤖 <i>Auto-managed: TP / trailing / stop-loss updates will follow here.</i>")
    lines.append("\n<i>⚠️ Not financial advice. DYOR.</i>")
    return "\n".join(lines)


def format_full(row: dict, system: str) -> str:
    """Messaggio completo per canale PREMIUM."""
    if system == "midcap":
        return _format_midcap(row, teaser=False)
    if system == "v3":
        return _format_v3(row)
    if system == "pre_grad":
        return _format_pre_grad(row)
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
    feats = (row.get("top_features") or "").strip()
    if feats:
        lines.append(f"<i>{_e(feats[:300])}</i>")
    ca = _ca_line(row)
    if ca:
        lines.append(ca)
    links = _links(row)
    if links:
        lines.append(links)
    lines.append("🤖 <i>Auto-managed: TP / trailing / stop-loss updates will follow here.</i>")
    lines.append("\n<i>⚠️ Not financial advice. DYOR.</i>")
    return "\n".join(lines)


_EXIT_LABEL = {
    "tp1": ("🎯", "TP1 hit"), "tp2": ("🎯🎯", "TP2 hit"),
    "trail_exit": ("🏁", "Trailing exit"), "trailing_sl": ("🏁", "Trailing stop"),
    "hard_sl": ("🛑", "Hard stop-loss"), "sl_adaptive": ("🛑", "Adaptive stop"),
    "liq_collapse": ("⚠️", "Liquidity collapse — emergency exit"),
    "exit_vol_crash": ("📉", "Volume crash exit"),
    "exit_bsr_collapse": ("📉", "Buy-pressure collapse exit"),
    "exit_time_limit": ("⏰", "Time-limit exit"),
    "exit_low_liq": ("⚠️", "Low liquidity exit"),
}


def format_exit_premium(row: dict) -> str:
    """Update di gestione posizione per PREMIUM: TP/SL/trailing del simulator.
    Dà agli abbonati il ciclo di vita completo del segnale senza inventare
    una strategia nuova: è il piano exit già attivo sul motore."""
    action = (row.get("action") or "").strip()
    emoji, label = _EXIT_LABEL.get(action, ("ℹ️", action))
    system = row.get("system", "")
    sys_label = _SYS_LABEL.get(system, system)
    sym = _e(row.get("token_symbol", "?"))
    chg = _fmt_pct(row.get("change_pct"))
    pnl = _f(row.get("pnl_eur"), None)
    remaining = _f(row.get("remaining"), None)

    lines = [
        f"{emoji} <b>{label}</b> · {_e(sys_label)}",
        f"<b>${sym}</b> · move {chg}",
    ]
    if remaining is not None and remaining > 0.001:
        lines.append(f"Sold {100 - remaining * 100:.0f}% · {remaining * 100:.0f}% riding with trailing stop")
    elif pnl is not None:
        lines.append(f"Position closed · P&L <b>{pnl:+.2f}€</b> <i>(simulated, €100/trade)</i>")
    lines.append("\n<i>⚠️ Not financial advice. DYOR.</i>")
    return "\n".join(lines)


def format_teaser_live(row: dict, system: str) -> str:
    """Teaser FREE in tempo reale: il segnale è APPENA partito sul Premium,
    ticker censurato. FOMO su un'opportunità ancora aperta (la closure FREE
    arriva invece a trade finito)."""
    import time as _time
    chain = (row.get("chain", "") or "").lower()
    emoji = _CHAIN_EMOJI.get(chain, "🔹")
    label = _SYS_LABEL.get(system, system)
    checks = ["Scanner checks ✅"]
    liq = _f(row.get("liquidity_usd"), None)
    if liq:
        checks.append(f"Liq {_fmt_usd(row.get('liquidity_usd'))}")
    prob = _f(row.get("pump_probability"), None)
    if prob is not None and prob > 0.001:
        checks.append(f"prob {prob:.2f}")
    lines = [
        f"🔒 <b>LIVE SIGNAL</b> · {emoji} {_e(chain.upper())} · {_time.strftime('%H:%M')}",
        f"Token: <b>████</b> · {_e(label)}",
        " · ".join(checks),
        "",
        "Entry, TP and SL just hit Premium members — in real time.",
        "You'll see this trade here only after it closes.",
        "⚡ Real-time access 👇",
    ]
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
