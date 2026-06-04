"""
setup_dune.py  [DEPRECATO — NON ESEGUIRE]
==========================================
QUESTO FILE È OBSOLETO. Usa gemme/setup_dune.py invece.

Questo file puntava a gemmeV2.py (legacy) e contiene SQL v2 (senza CTE,
senza repeat_buyers, senza inflow_recency). Condivide gli stessi
EXISTING_QUERY_IDS di gemme/setup_dune.py — eseguirlo SOVRASCRIVE le
query v4 con il vecchio SQL v2.

Per aggiornare le query Dune:
    cd gemme && python setup_dune.py
"""
raise SystemExit(
    "\n❌ DEPRECATO: usa gemme/setup_dune.py\n"
    "   Questo file sovrascrive le query v4 con SQL v2 obsoleto.\n"
)

import os, sys, time, json
import requests

# ── Config ────────────────────────────────────────────────────────────────────
DUNE_API_KEY = os.environ.get("DUNE_API_KEY", "IBx3JQpjKGUg7RhVwHOZWxcKlnTE46Wk")
GEMME_FILE   = "gemme/gemmeV2.py"
BASE_URL     = "https://api.dune.com/api/v1"
HEADERS      = {"x-dune-api-key": DUNE_API_KEY, "Content-Type": "application/json"}

# Se hai già creato le query, metti qui gli ID per aggiornarle invece di ricrearle.
# setup_dune.py li compila automaticamente dopo la prima esecuzione.
EXISTING_QUERY_IDS = {
    "solana_smart_money":   7417474,
    "bsc_smart_money":      7417475,
    "base_smart_money":     7417476,
    "ethereum_smart_money": 7417477,
}

# ── SQL corretti per Dune v3 (DuneSQL / Trino) ───────────────────────────────
# Usa dex_*.trades — tabelle ufficiali con prezzi USD già calcolati e DEX swap
QUERIES = {
    # ── SOLANA: usa token_bought_mint_address + trader_id ─────────────────────
    "solana_smart_money": {
        "name": "Gem Hunter – Solana Smart Money Inflow 24h v2",
        "description": "Token con inflow distribuito su DEX Solana nelle ultime 24h. Anti-whale + anti-bot. Usato da gemmeV2.py.",
        "query_sql": """
-- Smart Money Inflow su Solana – ultime 24h (v2)
-- Miglioramenti: anti-whale, buyer minimi alzati, avg_buy_per_wallet
SELECT
    token_bought_mint_address                          AS token_address,
    token_bought_symbol                                AS token_symbol,
    ''                                                 AS token_name,
    SUM(amount_usd)                                    AS inflow_usd,
    COUNT(DISTINCT trader_id)                          AS unique_buyers,
    MAX(amount_usd)                                    AS max_single_trade,
    SUM(amount_usd) / COUNT(DISTINCT trader_id)        AS avg_buy_per_wallet,
    MAX(block_time)                                    AS last_seen
FROM dex_solana.trades
WHERE
    block_time               >= NOW() - INTERVAL '24' hour
    AND amount_usd           >= 100
    AND amount_usd           <= 500000
    AND token_bought_symbol  IS NOT NULL
    AND UPPER(token_bought_symbol) NOT IN (
        'USDC','USDT','SOL','WSOL','MSOL','STSOL','JITOSOL',
        'BONK','WIF','POPCAT','PEPE','DOGE','JUP','RAY','ORCA'
    )
    AND token_bought_mint_address IS NOT NULL
GROUP BY 1, 2, 3
HAVING
    SUM(amount_usd)                                    >= 5000
    AND COUNT(DISTINCT trader_id)                      >= 5
    -- anti-whale: nessun singolo trade > 40% dell'inflow totale
    AND MAX(amount_usd)                                <= SUM(amount_usd) * 0.40
    -- acquisto medio minimo: esclude bot spam micro-transazioni
    AND SUM(amount_usd) / COUNT(DISTINCT trader_id)    >= 200
ORDER BY inflow_usd DESC
LIMIT 100
""",
        "parameters": [],
        "tags": ["gem-hunter", "solana", "smart-money", "v2"],
    },

    # ── EVM chains: token_bought_address + taker ───────────────────────────────
    "bsc_smart_money": {
        "name": "Gem Hunter – BSC Smart Money Inflow 24h v2",
        "description": "Token con inflow distribuito su DEX BSC nelle ultime 24h. Anti-whale + anti-bot. Usato da gemmeV2.py.",
        "query_sql": """
-- Smart Money Inflow su BNB Smart Chain – ultime 24h (v2)
-- Miglioramenti: anti-whale, buyer minimi alzati, avg_buy_per_wallet
SELECT
    token_bought_address                               AS token_address,
    token_bought_symbol                                AS token_symbol,
    ''                                                 AS token_name,
    SUM(amount_usd)                                    AS inflow_usd,
    COUNT(DISTINCT taker)                              AS unique_buyers,
    MAX(amount_usd)                                    AS max_single_trade,
    SUM(amount_usd) / COUNT(DISTINCT taker)            AS avg_buy_per_wallet,
    MAX(block_time)                                    AS last_seen
FROM dex_bnb.trades
WHERE
    block_time               >= NOW() - INTERVAL '24' hour
    AND amount_usd           >= 100
    AND amount_usd           <= 500000
    AND token_bought_symbol  IS NOT NULL
    AND UPPER(token_bought_symbol) NOT IN (
        'USDT','USDC','BUSD','DAI','FDUSD','TUSD',
        'BNB','WBNB','CAKE','ETH','BTCB','XRP','ADA','DOT'
    )
    AND token_bought_address IS NOT NULL
GROUP BY 1, 2, 3
HAVING
    SUM(amount_usd)                                    >= 5000
    AND COUNT(DISTINCT taker)                          >= 5
    -- anti-whale: nessun singolo trade > 40% dell'inflow totale
    AND MAX(amount_usd)                                <= SUM(amount_usd) * 0.40
    -- acquisto medio minimo: esclude bot spam micro-transazioni
    AND SUM(amount_usd) / COUNT(DISTINCT taker)        >= 200
ORDER BY inflow_usd DESC
LIMIT 100
""",
        "parameters": [],
        "tags": ["gem-hunter", "bsc", "smart-money", "v2"],
    },

    "base_smart_money": {
        "name": "Gem Hunter – Base Smart Money Inflow 24h v2",
        "description": "Token con inflow distribuito su DEX Base nelle ultime 24h. Anti-whale + anti-bot. Usato da gemmeV2.py.",
        "query_sql": """
-- Smart Money Inflow su Base – ultime 24h (v2)
-- Miglioramenti: anti-whale, buyer minimi alzati, avg_buy_per_wallet
SELECT
    token_bought_address                               AS token_address,
    token_bought_symbol                                AS token_symbol,
    ''                                                 AS token_name,
    SUM(amount_usd)                                    AS inflow_usd,
    COUNT(DISTINCT taker)                              AS unique_buyers,
    MAX(amount_usd)                                    AS max_single_trade,
    SUM(amount_usd) / COUNT(DISTINCT taker)            AS avg_buy_per_wallet,
    MAX(block_time)                                    AS last_seen
FROM dex_base.trades
WHERE
    block_time               >= NOW() - INTERVAL '24' hour
    AND amount_usd           >= 100
    AND amount_usd           <= 500000
    AND token_bought_symbol  IS NOT NULL
    AND UPPER(token_bought_symbol) NOT IN (
        'USDT','USDC','DAI','ETH','WETH','CBETH','STETH',
        'RETH','FRAX','BRETT','DEGEN','USDBC','USDbC'
    )
    AND token_bought_address IS NOT NULL
GROUP BY 1, 2, 3
HAVING
    SUM(amount_usd)                                    >= 5000
    AND COUNT(DISTINCT taker)                          >= 5
    -- anti-whale: nessun singolo trade > 40% dell'inflow totale
    AND MAX(amount_usd)                                <= SUM(amount_usd) * 0.40
    -- acquisto medio minimo: esclude bot spam micro-transazioni
    AND SUM(amount_usd) / COUNT(DISTINCT taker)        >= 200
ORDER BY inflow_usd DESC
LIMIT 100
""",
        "parameters": [],
        "tags": ["gem-hunter", "base", "smart-money", "v2"],
    },

    "ethereum_smart_money": {
        "name": "Gem Hunter – Ethereum Smart Money Inflow 24h v2",
        "description": "Token con inflow distribuito su DEX Ethereum nelle ultime 24h. Anti-whale + anti-bot. Usato da gemmeV2.py.",
        "query_sql": """
-- Smart Money Inflow su Ethereum mainnet – ultime 24h (v2)
-- Miglioramenti: anti-whale, buyer minimi alzati, avg_buy_per_wallet
SELECT
    token_bought_address                               AS token_address,
    token_bought_symbol                                AS token_symbol,
    ''                                                 AS token_name,
    SUM(amount_usd)                                    AS inflow_usd,
    COUNT(DISTINCT taker)                              AS unique_buyers,
    MAX(amount_usd)                                    AS max_single_trade,
    SUM(amount_usd) / COUNT(DISTINCT taker)            AS avg_buy_per_wallet,
    MAX(block_time)                                    AS last_seen
FROM dex_ethereum.trades
WHERE
    block_time               >= NOW() - INTERVAL '24' hour
    AND amount_usd           >= 100
    AND amount_usd           <= 500000
    AND token_bought_symbol  IS NOT NULL
    AND UPPER(token_bought_symbol) NOT IN (
        'USDT','USDC','DAI','WETH','ETH','STETH','WSTETH',
        'RETH','FRAX','LUSD','MKR','LINK','UNI','AAVE','CRV','LDO',
        'SHIB','PEPE','FLOKI'
    )
    AND token_bought_address IS NOT NULL
GROUP BY 1, 2, 3
HAVING
    SUM(amount_usd)                                    >= 5000
    AND COUNT(DISTINCT taker)                          >= 5
    -- anti-whale: nessun singolo trade > 40% dell'inflow totale
    AND MAX(amount_usd)                                <= SUM(amount_usd) * 0.40
    -- acquisto medio minimo: esclude bot spam micro-transazioni
    AND SUM(amount_usd) / COUNT(DISTINCT taker)        >= 200
ORDER BY inflow_usd DESC
LIMIT 100
""",
        "parameters": [],
        "tags": ["gem-hunter", "ethereum", "smart-money", "v2"],
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
    """Aggiorna DUNE_QUERIES in gemme/gemmeV2.py con gli ID reali."""
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
    print("  SETUP DUNE — Gem Hunter Query Updater")
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
