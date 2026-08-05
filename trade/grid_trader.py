import os
import time
import json
import csv
import logging
import asyncio
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional
import ccxt.async_support as ccxt
import pandas as pd
import numpy as np

from grid_config import GridConfig

# Configurazione logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("GridTrader")

@dataclass
class GridLevel:
    price: float
    side: str  # 'buy' or 'sell'
    status: str  # 'pending', 'open', 'filled'
    order_id: Optional[str] = None
    qty: float = 0.0

@dataclass
class PairGrid:
    pair: str
    levels: List[GridLevel] = field(default_factory=list)
    inventory: float = 0.0  # net tokens held
    realized_pnl: float = 0.0
    center_price: float = 0.0
    range_pct: float = 0.0
    paused: bool = False
    
    def create_grid(self, current_price: float, range_pct: float, num_levels: int, order_size_usd: float):
        """Inizializza la griglia attorno al prezzo corrente."""
        self.center_price = current_price
        self.range_pct = range_pct
        self.levels.clear()
        
        half_levels = num_levels // 2
        upper_price = current_price * (1 + range_pct)
        lower_price = current_price * (1 - range_pct)
        
        # Spaziatura dinamica (lineare per semplicità, ma si può basare sull'ATR)
        step = (upper_price - lower_price) / num_levels
        
        for i in range(1, half_levels + 1):
            buy_price = current_price - (step * i)
            qty = order_size_usd / buy_price
            self.levels.append(GridLevel(price=buy_price, side='buy', status='pending', qty=qty))
            
            sell_price = current_price + (step * i)
            qty = order_size_usd / sell_price
            self.levels.append(GridLevel(price=sell_price, side='sell', status='pending', qty=qty))
        
        self.levels.sort(key=lambda x: x.price)
        logger.info(f"[{self.pair}] Griglia creata: {len(self.levels)} livelli tra {lower_price:.2f} e {upper_price:.2f}")

    def on_fill(self, level: GridLevel, fill_price: float):
        """Gestisce l'esecuzione di un livello."""
        level.status = 'filled'
        if level.side == 'buy':
            self.inventory += level.qty
            logger.info(f"[{self.pair}] BUY filled a {fill_price:.2f}. Inv: {self.inventory:.4f}")
            # Piazzare sell uno step sopra
            idx = self.levels.index(level)
            if idx + 1 < len(self.levels):
                next_level = self.levels[idx + 1]
                if next_level.side == 'sell':
                    next_level.status = 'pending'
        else:
            self.inventory -= level.qty
            # Profitto semplificato = (prezzo vendita - prezzo acquisto stimato) * qty
            # Per esattezza, la differenza col livello sotto:
            idx = self.levels.index(level)
            profit = 0.0
            if idx > 0:
                prev_level = self.levels[idx - 1]
                profit = (fill_price - prev_level.price) * level.qty
            self.realized_pnl += profit
            logger.info(f"[{self.pair}] SELL filled a {fill_price:.2f}. Inv: {self.inventory:.4f}, PnL: +${profit:.2f}")
            if idx > 0:
                prev_level = self.levels[idx - 1]
                if prev_level.side == 'buy':
                    prev_level.status = 'pending'

    def check_fills(self, current_price: float):
        """Simula gli eseguiti in PAPER mode."""
        if self.paused:
            return

        for level in self.levels:
            if level.status in ['pending', 'open']:
                if level.side == 'buy' and current_price <= level.price:
                    self.on_fill(level, level.price)
                elif level.side == 'sell' and current_price >= level.price:
                    self.on_fill(level, level.price)
                    
    def recenter(self, new_center: float, order_size_usd: float):
        """Ricentra la griglia se il prezzo sfora il range."""
        logger.info(f"[{self.pair}] Ricentramento griglia attorno a {new_center:.2f}")
        num_levels = len(self.levels)
        self.create_grid(new_center, self.range_pct, num_levels, order_size_usd)
        self.paused = False

class GridTrader:
    def __init__(self, mode='PAPER'):
        self.mode = mode
        self.grids: Dict[str, PairGrid] = {}
        self.cumulative_pnl = 0.0
        
        # Setup cartelle e file
        os.makedirs(GridConfig.REPORTS_DIR, exist_ok=True)
        
        # Init exchange
        self.exchange = ccxt.binance({
            'apiKey': GridConfig.BINANCE_API_KEY,
            'secret': GridConfig.BINANCE_API_SECRET,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'spot'
            }
        })
        
        # Inizializza file log se non esiste
        if not os.path.exists(GridConfig.TRADE_LOG_CSV):
            with open(GridConfig.TRADE_LOG_CSV, mode='w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['ts', 'pair', 'side', 'price', 'qty', 'pnl_usd', 'cumulative_pnl', 'inventory'])

    def log_trade(self, pair: str, side: str, price: float, qty: float, pnl_usd: float, inventory: float):
        """Scrive l'eseguito nel log CSV."""
        with open(GridConfig.TRADE_LOG_CSV, mode='a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.now(timezone.utc).isoformat(),
                pair, side, price, qty, pnl_usd, self.cumulative_pnl, inventory
            ])

    def save_state(self):
        """Salva lo stato su file JSON."""
        state = {
            "cumulative_pnl": self.cumulative_pnl,
            "grids": {
                pair: {
                    "inventory": grid.inventory,
                    "realized_pnl": grid.realized_pnl,
                    "center_price": grid.center_price,
                    "range_pct": grid.range_pct,
                    "paused": grid.paused,
                    "levels": [asdict(lvl) for lvl in grid.levels]
                }
                for pair, grid in self.grids.items()
            }
        }
        with open(GridConfig.GRID_STATE_JSON, 'w') as f:
            json.dump(state, f, indent=4)
            
    def load_state(self) -> bool:
        """Carica lo stato precedente, se esiste."""
        if os.path.exists(GridConfig.GRID_STATE_JSON):
            try:
                with open(GridConfig.GRID_STATE_JSON, 'r') as f:
                    state = json.load(f)
                self.cumulative_pnl = state.get("cumulative_pnl", 0.0)
                for pair, data in state.get("grids", {}).items():
                    grid = PairGrid(pair=pair)
                    grid.inventory = data.get("inventory", 0.0)
                    grid.realized_pnl = data.get("realized_pnl", 0.0)
                    grid.center_price = data.get("center_price", 0.0)
                    grid.range_pct = data.get("range_pct", 0.0)
                    grid.paused = data.get("paused", False)
                    levels_data = data.get("levels", [])
                    grid.levels = [GridLevel(**lvl) for lvl in levels_data]
                    self.grids[pair] = grid
                logger.info("Stato caricato correttamente.")
                return True
            except Exception as e:
                logger.error(f"Errore nel caricamento stato: {e}")
        return False

    def generate_dashboard(self):
        """Genera una dashboard HTML in tempo reale."""
        pnl_class = "profit" if self.cumulative_pnl >= 0 else "loss"
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        html = (
            f"<html><head><title>Grid Trader Dashboard</title>"
            f"<meta http-equiv='refresh' content='30'>"
            f"<style>"
            f"body {{ font-family: Arial; padding: 20px; background-color: #f4f4f9; }}"
            f".card {{ background: white; padding: 15px; margin-bottom: 15px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}"
            f"h2 {{ color: #333; }}"
            f".profit {{ color: green; font-weight: bold; }}"
            f".loss {{ color: red; font-weight: bold; }}"
            f"table {{ width: 100%; border-collapse: collapse; }}"
            f"th, td {{ padding: 8px; text-align: left; border-bottom: 1px solid #ddd; }}"
            f"</style>"
            f"</head><body>"
            f"<h1>Dashboard Grid Trader ({self.mode})</h1>"
            f"<div class='card'>"
            f"<h2>Total PnL: <span class='{pnl_class}'>${self.cumulative_pnl:.2f}</span></h2>"
            f"<p>Aggiornato: {now_str}</p>"
            f"</div>"
        )

        for pair, grid in self.grids.items():
            paused_label = " (PAUSED)" if grid.paused else ""
            html += (
                f"<div class='card'>"
                f"<h3>{pair}{paused_label}</h3>"
                f"<p>Centro: {grid.center_price:.2f} | Range: ±{grid.range_pct*100:.1f}%</p>"
                f"<p>Inventario: {grid.inventory:.4f} | PnL: ${grid.realized_pnl:.2f}</p>"
                f"<table><tr><th>Prezzo</th><th>Side</th><th>Status</th></tr>"
            )
            for lvl in grid.levels:
                html += f"<tr><td>{lvl.price:.2f}</td><td>{lvl.side}</td><td>{lvl.status}</td></tr>"
            html += "</table></div>"

        html += "</body></html>"
        with open(GridConfig.DASHBOARD_HTML, 'w') as f:
            f.write(html)

    async def fetch_prices(self) -> Dict[str, float]:
        """Recupera i prezzi correnti dal mercato."""
        try:
            tickers = await self.exchange.fetch_tickers(GridConfig.PAIRS)
            prices = {}
            for pair in GridConfig.PAIRS:
                if pair in tickers and tickers[pair]['last'] is not None:
                    prices[pair] = tickers[pair]['last']
            return prices
        except Exception as e:
            logger.error(f"Errore nel fetch dei prezzi: {e}")
            return {}

    async def initialize_grids(self):
        """Inizializza le griglie per le coppie se non sono state caricate dallo stato."""
        prices = await self.fetch_prices()
        for pair in GridConfig.PAIRS:
            if pair not in self.grids and pair in prices:
                settings = GridConfig.PAIR_SETTINGS.get(pair, GridConfig.DEFAULT_PAIR_SETTINGS)
                grid = PairGrid(pair=pair)
                
                # Smart Grid Features: Auto Range Detection basato su 24h e (simulazione) ATR
                try:
                    # Ottieni candele 1d per high/low
                    ohlcv = await self.exchange.fetch_ohlcv(pair, '1d', limit=2)
                    if ohlcv and len(ohlcv) >= 1:
                        last_day = ohlcv[-1] # [timestamp, open, high, low, close, volume]
                        high_24h = last_day[2]
                        low_24h = last_day[3]
                        # Calcolo range_pct dinamico in base all'high/low delle 24h
                        # Se molto volatile, allarghiamo il range
                        dynamic_range_pct = ((high_24h - low_24h) / prices[pair]) / 2.0
                        # Teniamo il parametro entro limiti ragionevoli
                        dynamic_range_pct = max(0.01, min(dynamic_range_pct, 0.15))
                        logger.info(f"[{pair}] Auto Range Detection: {dynamic_range_pct*100:.2f}% (High: {high_24h}, Low: {low_24h})")
                    else:
                        dynamic_range_pct = settings["range_pct"]
                except Exception as e:
                    logger.error(f"[{pair}] Errore calcolo range dinamico: {e}. Uso default.")
                    dynamic_range_pct = settings["range_pct"]

                grid.create_grid(
                    current_price=prices[pair],
                    range_pct=dynamic_range_pct,
                    num_levels=settings["num_levels"],
                    order_size_usd=settings["order_size_usd"]
                )
                self.grids[pair] = grid

    async def check_circuit_breakers(self):
        """Controlla i limiti di rischio globale e per coppia."""
        if self.cumulative_pnl <= -GridConfig.MAX_DAILY_LOSS_USD:
            logger.error(f"CIRCUIT BREAKER: Max daily loss superato ({self.cumulative_pnl} <= -{GridConfig.MAX_DAILY_LOSS_USD}). Pausa bot.")
            # In un bot reale dovresti chiudere gli ordini e disattivare.
            for grid in self.grids.values():
                grid.paused = True
                
        for pair, grid in self.grids.items():
            # Controlla max inventory
            settings = GridConfig.PAIR_SETTINGS.get(pair, GridConfig.DEFAULT_PAIR_SETTINGS)
            # Stima valore inventory (rough estimate basato sul centro)
            inv_value = abs(grid.inventory * grid.center_price)
            if inv_value > GridConfig.MAX_INVENTORY_USD_PER_PAIR:
                logger.warning(f"[{pair}] Inventario eccessivo: ${inv_value:.2f}. Pausa griglia.")
                grid.paused = True

    async def run(self):
        """Ciclo principale del bot."""
        logger.info(f"Avvio GridTrader in modalità {self.mode}")
        if not self.load_state():
            await self.initialize_grids()
            
        last_summary_time = time.time()

        try:
            while True:
                prices = await self.fetch_prices()
                
                for pair, price in prices.items():
                    if pair in self.grids:
                        grid = self.grids[pair]
                        
                        # Anti-Trend Protection: Controlla se il prezzo è uscito dalla griglia
                        upper_bound = grid.center_price * (1 + grid.range_pct)
                        lower_bound = grid.center_price * (1 - grid.range_pct)
                        
                        if price > upper_bound or price < lower_bound:
                            if not grid.paused:
                                logger.warning(f"[{pair}] Prezzo {price:.2f} fuori range [{lower_bound:.2f}, {upper_bound:.2f}]. Pausa e ricentramento.")
                                grid.paused = True
                                settings = GridConfig.PAIR_SETTINGS.get(pair, GridConfig.DEFAULT_PAIR_SETTINGS)
                                grid.recenter(price, settings["order_size_usd"])
                        else:
                            # Controlla eseguiti (simulazione)
                            old_pnl = grid.realized_pnl
                            grid.check_fills(price)
                            
                            # Aggiorna pnl cumulativo e logga trade se ci sono stati
                            diff_pnl = grid.realized_pnl - old_pnl
                            if diff_pnl > 0:
                                self.cumulative_pnl += diff_pnl
                                self.log_trade(pair, "sell", price, 0.0, diff_pnl, grid.inventory)
                
                await self.check_circuit_breakers()
                
                self.save_state()
                self.generate_dashboard()
                
                # Statistiche orarie
                now = time.time()
                if now - last_summary_time > 3600:
                    logger.info(f"--- RIEPILOGO ORARIO ---")
                    logger.info(f"PnL Totale: ${self.cumulative_pnl:.2f}")
                    for pair, grid in self.grids.items():
                        logger.info(f"[{pair}] PnL: ${grid.realized_pnl:.2f}, Inv: {grid.inventory:.4f}")
                    last_summary_time = now
                    
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            logger.info("Spegnimento del bot...")
        except Exception as e:
            logger.error(f"Errore fatale: {e}", exc_info=True)
        finally:
            await self.exchange.close()
            self.save_state()

if __name__ == "__main__":
    mode = GridConfig.MODE
    trader = GridTrader(mode=mode)
    try:
        asyncio.run(trader.run())
    except KeyboardInterrupt:
        logger.info("Interruzione manuale.")
