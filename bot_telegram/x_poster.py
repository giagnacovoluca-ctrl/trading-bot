"""
x_poster.py — prepara una "card" + testo pronto per X (Twitter) per i trade
vincenti chiusi, e li manda all'admin via Telegram per il post manuale.

L'API X richiede un piano a pagamento (Basic, $200/mese) per scrivere tweet:
niente posting automatico. Il bot genera la card immagine + il testo con
hashtag/$TICKER già pronti, l'admin copia/incolla e posta in pochi secondi.

Rate-limited: X_PROMO_MIN_INTERVAL_MIN tra una proposta e l'altra, max
X_PROMO_MAX_PER_DAY/24h, solo trade sopra X_PROMO_MIN_PNL_EUR/PCT.
"""
from __future__ import annotations

import io
import logging
import time

import config
import store
import telegram_api as tg

log = logging.getLogger("x_poster")

_STATE_FILE = "x_promo.json"

_SYS_LABEL = {
    "defi": "DeFi Gem", "pump_grad": "Pump Graduation", "pre_grad": "Pre-Graduation",
    "mirror": "Wallet Mirror", "midcap": "Mid-Cap Squeeze",
    "base_pump": "Base Pump", "v3": "Gem Hunter V3", "v3_large": "Gem Hunter V3 Large",
}

_CHAIN_TAG = {
    "solana": "#Solana", "base": "#Base",
    "bsc": "#BNB", "binance-smart-chain": "#BNB",
    "eth": "#Ethereum", "ethereum": "#Ethereum",
}


# ── card immagine ────────────────────────────────────────────────────────────

def _build_card(closure: dict) -> bytes:
    """Card dark 1200x675 stile Telegram: simbolo, P&L, % e CTA canale FREE."""
    from PIL import Image, ImageDraw, ImageFont

    W, H = 1200, 675
    bg = (13, 13, 13)
    card = (22, 22, 22)
    border = (42, 42, 42)
    green = (0, 230, 118)
    red = (255, 82, 82)
    muted = (136, 136, 136)
    text = (224, 224, 224)

    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)

    def _font(size: int, bold: bool = False):
        names = ["DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"]
        for n in names:
            try:
                return ImageFont.truetype(n, size)
            except OSError:
                continue
        return ImageFont.load_default()

    pnl = closure.get("pnl_eur", 0.0)
    pct = closure.get("pct")
    sym = (closure.get("symbol") or "?").lstrip("$").upper()
    sys_label = _SYS_LABEL.get(closure.get("system", ""), closure.get("system", ""))
    color = green if pnl >= 0 else red

    # card container
    draw.rounded_rectangle([40, 40, W - 40, H - 40], radius=24, fill=card, outline=border, width=2)

    # badge
    draw.text((80, 80), "✅ TRADE CLOSED", font=_font(28, True), fill=green)

    # ticker
    draw.text((80, 150), f"${sym}", font=_font(96, True), fill=text)
    draw.text((80, 270), sys_label, font=_font(32), fill=muted)

    # pnl + pct
    pnl_str = f"{pnl:+.2f}€"
    draw.text((80, 360), pnl_str, font=_font(110, True), fill=color)
    if pct is not None:
        draw.text((80, 490), f"{pct:+.1f}%", font=_font(56, True), fill=color)

    # footer CTA
    draw.line([(80, 580), (W - 80, 580)], fill=border, width=2)
    handle = f"@{config.FREE_CHANNEL_USERNAME}" if config.FREE_CHANNEL_USERNAME else "Free channel"
    draw.text((80, 605), f"Spotted live on our free Telegram channel — {handle}",
              font=_font(28), fill=muted)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _tweet_text(closure: dict) -> str:
    """Testo pronto per X: $TICKER, risultato, CTA al canale FREE e hashtag
    per massimizzare reach/viralità (chain-specific + generici)."""
    sym = (closure.get("symbol") or "?").lstrip("$").upper()
    pnl = closure.get("pnl_eur", 0.0)
    pct = closure.get("pct")
    chain = (closure.get("chain") or "").lower()
    pct_str = f" ({pct:+.0f}%)" if pct is not None else ""

    tags = ["#Crypto", "#Altcoins", "#CryptoGems", "#Trading"]
    chain_tag = _CHAIN_TAG.get(chain)
    if chain_tag:
        tags.append(chain_tag)
    if pct is not None and pct >= 100:
        tags.append("#100x")
    elif pct is not None and pct >= 50:
        tags.append("#Gem")

    lines = [
        f"🚀 ${sym} {pnl:+.0f}€{pct_str}",
        "",
        "Spotted live on our free signal channel 👇",
    ]
    if config.FREE_CHANNEL_USERNAME:
        lines.append(f"https://t.me/{config.FREE_CHANNEL_USERNAME}")
    lines.append("")
    lines.append(" ".join(tags))
    return "\n".join(lines)


# ── invio bozza all'admin ───────────────────────────────────────────────────

def maybe_send_x_draft(closure: dict) -> bool:
    """Valuta se proporre all'admin un post X per questa chiusura vincente.
    Rate limit + soglie minime per non spammare l'admin con micro-win."""
    if not config.X_PROMO_ENABLED:
        return False
    if not config.ADMIN_CHAT_ID:
        return False

    pnl = closure.get("pnl_eur", 0.0)
    pct = closure.get("pct")
    if pnl < config.X_PROMO_MIN_PNL_EUR:
        return False
    if pct is not None and pct < config.X_PROMO_MIN_PNL_PCT:
        return False

    state = store.load(_STATE_FILE, {"last_ts": 0.0, "times": []})
    now = time.time()
    times = [t for t in state.get("times", []) if now - t < 86400]
    if len(times) >= config.X_PROMO_MAX_PER_DAY:
        return False
    if times and now - times[-1] < config.X_PROMO_MIN_INTERVAL_MIN * 60:
        return False

    try:
        image = _build_card(closure)
    except Exception as e:
        log.warning("[x_poster] card non generata: %s", e)
        return False

    caption = "📋 Pronto per X — copia e posta:\n\n" + _tweet_text(closure)
    if not tg.send_photo(config.ADMIN_CHAT_ID, image, caption=caption):
        return False

    times.append(now)
    store.save(_STATE_FILE, {"last_ts": now, "times": times})
    return True
