import os

class GridConfig:
    # Modalità esecuzione (PAPER o LIVE)
    MODE = os.getenv("GRID_MODE", "PAPER")

    # Coppie da tradare di default
    PAIRS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

    # Parametri di default per ogni coppia
    DEFAULT_PAIR_SETTINGS = {
        "range_pct": 0.05,       # ±5%
        "num_levels": 20,        # 20 livelli totali
        "order_size_usd": 15.0,  # $15 per ordine
    }

    # Parametri specifici per coppia (sovrascrivono i default)
    PAIR_SETTINGS = {
        "BTC/USDT": {
            "range_pct": 0.03,
            "num_levels": 14,
            "order_size_usd": 20.0,
        },
        "ETH/USDT": {
            "range_pct": 0.04,
            "num_levels": 16,
            "order_size_usd": 15.0,
        },
        "SOL/USDT": {
            "range_pct": 0.08,
            "num_levels": 20,
            "order_size_usd": 10.0,
        }
    }

    # Limiti di rischio
    MAX_DAILY_LOSS_USD = 50.0
    MAX_INVENTORY_USD_PER_PAIR = 500.0
    MAX_OPEN_ORDERS_PER_PAIR = 40

    # Percorsi per log e persistenza
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    REPORTS_DIR = os.path.join(BASE_DIR, "reports")
    TRADE_LOG_CSV = os.path.join(REPORTS_DIR, "grid_trades.csv")
    GRID_STATE_JSON = os.path.join(REPORTS_DIR, "grid_state.json")
    DASHBOARD_HTML = os.path.join(REPORTS_DIR, "grid_dashboard.html")

    # Autenticazione Binance
    # Nomi delle variabili standard per Binance API
    BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
    BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
