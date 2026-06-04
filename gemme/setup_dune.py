"""
setup_dune.py
=============
Esegui dalla cartella defi/ (gemme.py → gemme/gemmeV3.py):

    python setup_dune.py

Se le query esistono già (IDs in EXISTING_QUERY_IDS), le aggiorna con SQL corretto.
Altrimenti le crea nuove e aggiorna gemme/gemmeV3.py con gli ID.

v3 — aggiunge CTE per repeat_buyers (wallet con >= 2 trades in 24h)
     e avg_buy_per_wallet, max_single_trade per wallet cluster tracking.
"""

import os, sys, time, json
import requests

# ── Config ────────────────────────────────────────────────────────────────────
DUNE_API_KEY = os.environ.get("DUNE_API_KEY", "IBx3JQpjKGUg7RhVwHOZWxcKlnTE46Wk")
_HERE        = os.path.dirname(os.path.abspath(__file__))
GEMME_FILE   = os.path.join(_HERE, "gemmeV3.py")
BASE_URL     = "https://api.dune.com/api/v1"
HEADERS      = {"x-dune-api-key": DUNE_API_KEY, "Content-Type": "application/json"}

# Se hai già creato le query, metti qui gli ID per aggiornarle invece di ricrearle.
EXISTING_QUERY_IDS = {
    "solana_smart_money":   7417474,
    "bsc_smart_money":      7417475,
    "base_smart_money":     7417476,
    "ethereum_smart_money": 7417477,
}

# ── SQL v3 con CTE per repeat_buyers ─────────────────────────────────────────
# Nuovi campi aggiunti rispetto a v2:
#   repeat_buyers      — wallet con >= 2 acquisti dello stesso token in 24h
#   repeat_buyer_ratio — repeat_buyers / unique_buyers (0..1)
#   max_single_trade   — singolo trade più grande (proxy whale detection)
#
# avg_buy_per_wallet era già presente nelle query v2.

QUERIES = {
    # ── SOLANA ────────────────────────────────────────────────────────────────
    "solana_smart_money": {
        "name": "Gem Hunter – Solana Smart Money Inflow 8h v4",
        "description": (
            "Token con inflow distribuito su DEX Solana nelle ultime 8h (ridotto da 24h). "
            "Finestra più stretta = segnale fresco su Solana dove i pump durano ore. "
            "Aggiunge inflow_last_2h + inflow_recency_ratio per momentum tracking. "
            "Usato da gemmeV3.py."
        ),
        "query_sql": """
-- Smart Money Inflow su Solana – ultime 8h (v4)
-- Finestra 24h→8h: Solana si muove veloce, segnale vecchio = rumore
-- Nuovi campi: inflow_last_2h, inflow_recency_ratio, buyers_last_2h
-- Anti-bot: min_trade $150 (da $100), unique_buyers >= 6
WITH all_trades AS (
    SELECT
        token_bought_mint_address  AS token_address,
        token_bought_symbol        AS token_symbol,
        trader_id,
        amount_usd,
        block_time
    FROM dex_solana.trades
    WHERE
        block_time              >= NOW() - INTERVAL '8' hour
        AND amount_usd          BETWEEN 150 AND 500000
        AND token_bought_symbol IS NOT NULL
        AND UPPER(token_bought_symbol) NOT IN (
            'USDC','USDT','SOL','WSOL','MSOL','STSOL','JITOSOL',
            'BONK','WIF','POPCAT','PEPE','DOGE','JUP','RAY','ORCA'
        )
        AND token_bought_mint_address IS NOT NULL
),
token_buyers AS (
    SELECT token_address, trader_id, COUNT(*) AS buy_count
    FROM all_trades
    GROUP BY 1, 2
)
SELECT
    t.token_address,
    t.token_symbol,
    ''                                                              AS token_name,
    SUM(t.amount_usd)                                              AS inflow_usd,
    COUNT(DISTINCT t.trader_id)                                    AS unique_buyers,
    MAX(t.amount_usd)                                              AS max_single_trade,
    SUM(t.amount_usd) / COUNT(DISTINCT t.trader_id)               AS avg_buy_per_wallet,
    COUNT(DISTINCT CASE WHEN tb.buy_count >= 2 THEN t.trader_id END)
                                                                   AS repeat_buyers,
    CAST(COUNT(DISTINCT CASE WHEN tb.buy_count >= 2 THEN t.trader_id END) AS DOUBLE)
        / NULLIF(COUNT(DISTINCT t.trader_id), 0)                  AS repeat_buyer_ratio,
    -- Freshness: inflow nelle ultime 2h come proxy di momentum attivo
    SUM(CASE WHEN t.block_time >= NOW() - INTERVAL '2' hour
             THEN t.amount_usd ELSE 0 END)                        AS inflow_last_2h,
    CAST(SUM(CASE WHEN t.block_time >= NOW() - INTERVAL '2' hour
                  THEN t.amount_usd ELSE 0 END) AS DOUBLE)
        / NULLIF(SUM(t.amount_usd), 0)                            AS inflow_recency_ratio,
    COUNT(DISTINCT CASE WHEN t.block_time >= NOW() - INTERVAL '2' hour
                        THEN t.trader_id END)                     AS buyers_last_2h,
    MAX(t.block_time)                                              AS last_seen
FROM all_trades t
LEFT JOIN token_buyers tb
    ON t.token_address = tb.token_address AND t.trader_id = tb.trader_id
GROUP BY 1, 2, 3
HAVING
    SUM(t.amount_usd)                            >= 5000
    AND COUNT(DISTINCT t.trader_id)              >= 6
    AND MAX(t.amount_usd)                        <= SUM(t.amount_usd) * 0.40
    AND SUM(t.amount_usd) / COUNT(DISTINCT t.trader_id) >= 200
ORDER BY inflow_usd DESC
LIMIT 100
""",
        "parameters": [],
        "tags": ["gem-hunter", "solana", "smart-money", "v4"],
    },

    # ── BSC ───────────────────────────────────────────────────────────────────
    "bsc_smart_money": {
        "name": "Gem Hunter – BSC Smart Money Inflow 24h v3",
        "description": (
            "Token con inflow distribuito su DEX BSC nelle ultime 24h. "
            "Anti-whale + anti-bot. Aggiunge repeat_buyers per wallet cluster tracking. "
            "Usato da gemmeV3.py."
        ),
        "query_sql": """
-- Smart Money Inflow su BNB Smart Chain – ultime 24h (v3)
-- Nuovi campi: repeat_buyers, repeat_buyer_ratio, max_single_trade
WITH all_trades AS (
    SELECT
        token_bought_address    AS token_address,
        token_bought_symbol     AS token_symbol,
        taker                   AS trader_id,
        amount_usd,
        block_time
    FROM dex_bnb.trades
    WHERE
        block_time              >= NOW() - INTERVAL '24' hour
        AND amount_usd          BETWEEN 100 AND 500000
        AND token_bought_symbol IS NOT NULL
        AND UPPER(token_bought_symbol) NOT IN (
            'USDT','USDC','BUSD','DAI','FDUSD','TUSD',
            'BNB','WBNB','CAKE','ETH','BTCB','XRP','ADA','DOT'
        )
        AND token_bought_address IS NOT NULL
),
token_buyers AS (
    SELECT token_address, trader_id, COUNT(*) AS buy_count
    FROM all_trades
    GROUP BY 1, 2
)
SELECT
    t.token_address,
    t.token_symbol,
    ''                                                              AS token_name,
    SUM(t.amount_usd)                                              AS inflow_usd,
    COUNT(DISTINCT t.trader_id)                                    AS unique_buyers,
    MAX(t.amount_usd)                                              AS max_single_trade,
    SUM(t.amount_usd) / COUNT(DISTINCT t.trader_id)               AS avg_buy_per_wallet,
    COUNT(DISTINCT CASE WHEN tb.buy_count >= 2 THEN t.trader_id END)
                                                                   AS repeat_buyers,
    CAST(COUNT(DISTINCT CASE WHEN tb.buy_count >= 2 THEN t.trader_id END) AS DOUBLE)
        / NULLIF(COUNT(DISTINCT t.trader_id), 0)                  AS repeat_buyer_ratio,
    MAX(t.block_time)                                              AS last_seen
FROM all_trades t
LEFT JOIN token_buyers tb
    ON t.token_address = tb.token_address AND t.trader_id = tb.trader_id
GROUP BY 1, 2, 3
HAVING
    SUM(t.amount_usd)                            >= 5000
    AND COUNT(DISTINCT t.trader_id)              >= 5
    AND MAX(t.amount_usd)                        <= SUM(t.amount_usd) * 0.40
    AND SUM(t.amount_usd) / COUNT(DISTINCT t.trader_id) >= 200
ORDER BY inflow_usd DESC
LIMIT 100
""",
        "parameters": [],
        "tags": ["gem-hunter", "bsc", "smart-money", "v3"],
    },

    # ── BASE ──────────────────────────────────────────────────────────────────
    "base_smart_money": {
        "name": "Gem Hunter – Base Smart Money Inflow 12h v4",
        "description": (
            "Token con inflow distribuito su DEX Base nelle ultime 12h (ridotto da 24h). "
            "Blacklist estesa: VIRTUAL/TOSHI/NORMIE wash trading. "
            "Inflow minimo $3k (da $5k): Base ha volumi inferiori a Solana. "
            "Aggiunge inflow_last_2h + inflow_recency_ratio per momentum tracking. "
            "Usato da gemmeV3.py."
        ),
        "query_sql": """
-- Smart Money Inflow su Base – ultime 12h (v4)
-- Finestra 24h→12h: segnale più fresco, meno rumore da token già esauriti
-- Blacklist estesa: VIRTUAL (wash volume), TOSHI, NORMIE, AERODROME/AERO (DEX token)
-- Soglia inflow $3k (da $5k): Base ha volumi tipicamente 3-5x inferiori a Solana
-- Nuovi campi: inflow_last_2h, inflow_recency_ratio, buyers_last_2h
WITH all_trades AS (
    SELECT
        token_bought_address    AS token_address,
        token_bought_symbol     AS token_symbol,
        taker                   AS trader_id,
        amount_usd,
        block_time
    FROM dex_base.trades
    WHERE
        block_time              >= NOW() - INTERVAL '12' hour
        AND amount_usd          BETWEEN 100 AND 500000
        AND token_bought_symbol IS NOT NULL
        AND UPPER(token_bought_symbol) NOT IN (
            'USDT','USDC','DAI','ETH','WETH','CBETH','STETH',
            'RETH','FRAX','BRETT','DEGEN','USDBC','USDbC',
            'VIRTUAL','TOSHI','NORMIE','AERODROME','AERO','MORPHO'
        )
        AND token_bought_address IS NOT NULL
),
token_buyers AS (
    SELECT token_address, trader_id, COUNT(*) AS buy_count
    FROM all_trades
    GROUP BY 1, 2
)
SELECT
    t.token_address,
    t.token_symbol,
    ''                                                              AS token_name,
    SUM(t.amount_usd)                                              AS inflow_usd,
    COUNT(DISTINCT t.trader_id)                                    AS unique_buyers,
    MAX(t.amount_usd)                                              AS max_single_trade,
    SUM(t.amount_usd) / COUNT(DISTINCT t.trader_id)               AS avg_buy_per_wallet,
    COUNT(DISTINCT CASE WHEN tb.buy_count >= 2 THEN t.trader_id END)
                                                                   AS repeat_buyers,
    CAST(COUNT(DISTINCT CASE WHEN tb.buy_count >= 2 THEN t.trader_id END) AS DOUBLE)
        / NULLIF(COUNT(DISTINCT t.trader_id), 0)                  AS repeat_buyer_ratio,
    SUM(CASE WHEN t.block_time >= NOW() - INTERVAL '2' hour
             THEN t.amount_usd ELSE 0 END)                        AS inflow_last_2h,
    CAST(SUM(CASE WHEN t.block_time >= NOW() - INTERVAL '2' hour
                  THEN t.amount_usd ELSE 0 END) AS DOUBLE)
        / NULLIF(SUM(t.amount_usd), 0)                            AS inflow_recency_ratio,
    COUNT(DISTINCT CASE WHEN t.block_time >= NOW() - INTERVAL '2' hour
                        THEN t.trader_id END)                     AS buyers_last_2h,
    MAX(t.block_time)                                              AS last_seen
FROM all_trades t
LEFT JOIN token_buyers tb
    ON t.token_address = tb.token_address AND t.trader_id = tb.trader_id
GROUP BY 1, 2, 3
HAVING
    SUM(t.amount_usd)                            >= 3000
    AND COUNT(DISTINCT t.trader_id)              >= 5
    AND MAX(t.amount_usd)                        <= SUM(t.amount_usd) * 0.40
    AND SUM(t.amount_usd) / COUNT(DISTINCT t.trader_id) >= 150
ORDER BY inflow_usd DESC
LIMIT 100
""",
        "parameters": [],
        "tags": ["gem-hunter", "base", "smart-money", "v4"],
    },

    # ── ETHEREUM ──────────────────────────────────────────────────────────────
    "ethereum_smart_money": {
        "name": "Gem Hunter – Ethereum Smart Money Inflow 24h v3",
        "description": (
            "Token con inflow distribuito su DEX Ethereum nelle ultime 24h. "
            "Anti-whale + anti-bot. Aggiunge repeat_buyers per wallet cluster tracking. "
            "Usato da gemmeV3.py."
        ),
        "query_sql": """
-- Smart Money Inflow su Ethereum mainnet – ultime 24h (v3)
-- Nuovi campi: repeat_buyers, repeat_buyer_ratio, max_single_trade
WITH all_trades AS (
    SELECT
        token_bought_address    AS token_address,
        token_bought_symbol     AS token_symbol,
        taker                   AS trader_id,
        amount_usd,
        block_time
    FROM dex_ethereum.trades
    WHERE
        block_time              >= NOW() - INTERVAL '24' hour
        AND amount_usd          BETWEEN 100 AND 500000
        AND token_bought_symbol IS NOT NULL
        AND UPPER(token_bought_symbol) NOT IN (
            'USDT','USDC','DAI','WETH','ETH','STETH','WSTETH',
            'RETH','FRAX','LUSD','MKR','LINK','UNI','AAVE','CRV','LDO',
            'SHIB','PEPE','FLOKI'
        )
        AND token_bought_address IS NOT NULL
),
token_buyers AS (
    SELECT token_address, trader_id, COUNT(*) AS buy_count
    FROM all_trades
    GROUP BY 1, 2
)
SELECT
    t.token_address,
    t.token_symbol,
    ''                                                              AS token_name,
    SUM(t.amount_usd)                                              AS inflow_usd,
    COUNT(DISTINCT t.trader_id)                                    AS unique_buyers,
    MAX(t.amount_usd)                                              AS max_single_trade,
    SUM(t.amount_usd) / COUNT(DISTINCT t.trader_id)               AS avg_buy_per_wallet,
    COUNT(DISTINCT CASE WHEN tb.buy_count >= 2 THEN t.trader_id END)
                                                                   AS repeat_buyers,
    CAST(COUNT(DISTINCT CASE WHEN tb.buy_count >= 2 THEN t.trader_id END) AS DOUBLE)
        / NULLIF(COUNT(DISTINCT t.trader_id), 0)                  AS repeat_buyer_ratio,
    MAX(t.block_time)                                              AS last_seen
FROM all_trades t
LEFT JOIN token_buyers tb
    ON t.token_address = tb.token_address AND t.trader_id = tb.trader_id
GROUP BY 1, 2, 3
HAVING
    SUM(t.amount_usd)                            >= 5000
    AND COUNT(DISTINCT t.trader_id)              >= 5
    AND MAX(t.amount_usd)                        <= SUM(t.amount_usd) * 0.40
    AND SUM(t.amount_usd) / COUNT(DISTINCT t.trader_id) >= 200
ORDER BY inflow_usd DESC
LIMIT 100
""",
        "parameters": [],
        "tags": ["gem-hunter", "ethereum", "smart-money", "v3"],
    },
}

# ── Funzioni API Dune ─────────────────────────────────────────────────────────

def update_query(query_id: int, key: str, q: dict) -> bool:
    """Aggiorna SQL di una query esistente via PATCH."""
    payload = {
        "name":        q["name"],
        "description": q["description"],
        "query_sql":   q["query_sql"].strip(),
        "tags":        q.get("tags", []),
    }
    r = requests.patch(
        f"{BASE_URL}/query/{query_id}",
        json=payload,
        headers=HEADERS,
        timeout=30,
    )
    if r.status_code in (200, 201):
        print(f"  ✅ [{key}] SQL aggiornato (query_id={query_id})")
        return True
    else:
        print(f"  ❌ [{key}] errore update {r.status_code}: {r.text[:200]}")
        return False


def create_query(key: str, q: dict) -> int:
    """Crea una nuova query su Dune e ritorna il query_id."""
    payload = {
        "name":        q["name"],
        "description": q["description"],
        "query_sql":   q["query_sql"].strip(),
        "parameters":  q.get("parameters", []),
        "tags":        q.get("tags", []),
        "is_private":  False,
    }
    r = requests.post(f"{BASE_URL}/query", json=payload, headers=HEADERS, timeout=30)
    if r.status_code in (200, 201):
        qid = r.json().get("query_id") or r.json().get("id")
        print(f"  ✅ [{key}] creata → query_id={qid}")
        return int(qid)
    else:
        print(f"  ❌ [{key}] errore create {r.status_code}: {r.text[:200]}")
        return None


def execute_query(query_id: int) -> str:
    """Avvia esecuzione e ritorna execution_id."""
    r = requests.post(
        f"{BASE_URL}/query/{query_id}/execute",
        json={"performance": "free"},
        headers=HEADERS,
        timeout=30,
    )
    if r.status_code in (200, 201):
        eid = r.json().get("execution_id")
        print(f"  ▶️  Esecuzione avviata: {eid}")
        return eid
    else:
        try:
            detail = r.json().get("error", r.text[:150])
        except Exception:
            detail = r.text[:150]
        print(f"  ⚠️  Esecuzione fallita {r.status_code}: {detail}")
        return None


def wait_execution(execution_id: str, timeout: int = 120) -> bool:
    """Attende completamento esecuzione."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(
            f"{BASE_URL}/execution/{execution_id}/status",
            headers=HEADERS, timeout=15,
        )
        state = r.json().get("state", "")
        if state == "QUERY_STATE_COMPLETED":
            print(f"  ✅ Completata.")
            return True
        if state in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
            err = r.json().get("error", {})
            print(f"  ❌ Fallita: {state} — {err}")
            return False
        print(f"  ⏳ {state}...")
        time.sleep(8)
    print("  ⏱  Timeout.")
    return False


def patch_gemme_py(ids: dict):
    """Aggiorna DUNE_QUERIES in gemme/gemmeV3.py con gli ID reali."""
    if not os.path.exists(GEMME_FILE):
        print(f"\n⚠️  {GEMME_FILE} non trovato. Aggiorna DUNE_QUERIES manualmente:")
        for k, v in ids.items():
            print(f"     '{k}': '{v}',")
        return

    with open(GEMME_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    replacements = {
        "solana_smart_money":   "PLACEHOLDER_QUERY_ID_SOLANA",
        "bsc_smart_money":      "PLACEHOLDER_QUERY_ID_BSC",
        "base_smart_money":     "PLACEHOLDER_QUERY_ID_BASE",
        "ethereum_smart_money": "PLACEHOLDER_QUERY_ID_ETHEREUM",
    }

    changed = False
    for key, placeholder in replacements.items():
        qid = ids.get(key)
        if not qid:
            continue
        if placeholder in content:
            content = content.replace(f'"{placeholder}"', f'"{qid}"')
            content = content.replace(f"'{placeholder}'", f"'{qid}'")
            content = content.replace(placeholder, str(qid))
            changed = True
            print(f"  📝 DUNE_QUERIES['{key}'] → {qid}")

    if changed:
        with open(GEMME_FILE, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"\n✅ {GEMME_FILE} aggiornato!")
    else:
        print(f"\n⚠️  Placeholder non trovati. Aggiorna DUNE_QUERIES manualmente:")
        for k, v in ids.items():
            if v:
                print(f"     '{k}': '{v}',")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  SETUP DUNE v4 — Gem Hunter Query Updater")
    print("  Solana: 8h, buyers>=6, $150 min | Base: 12h, $3k inflow, blacklist estesa")
    print("  Nuovi campi: inflow_last_2h, inflow_recency_ratio, buyers_last_2h")
    print("=" * 60)

    r = requests.get(f"{BASE_URL}/query/1", headers=HEADERS, timeout=10)
    if r.status_code == 401:
        print("\n❌ API key non valida.")
        sys.exit(1)
    print(f"\n✅ Connessione Dune OK\n")

    final_ids = {}

    for key, query_def in QUERIES.items():
        print(f"\n─── {key} ───")
        existing_id = EXISTING_QUERY_IDS.get(key)

        if existing_id:
            ok = update_query(existing_id, key, query_def)
            qid = existing_id if ok else None
        else:
            qid = create_query(key, query_def)

        if qid:
            final_ids[key] = qid
            time.sleep(1)
            eid = execute_query(qid)
            if eid:
                wait_execution(eid, timeout=90)

        time.sleep(2)

    print("\n" + "=" * 60)
    print("  RIEPILOGO QUERY ID")
    print("=" * 60)
    for k, v in final_ids.items():
        print(f"  {k}: {v}")

    print(f"\n─── Aggiornamento {GEMME_FILE} ───")
    patch_gemme_py(final_ids)
    print("\n🚀 Riavvia gemme.py!")


if __name__ == "__main__":
    main()
