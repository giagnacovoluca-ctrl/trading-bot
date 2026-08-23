import asyncio
import ccxt.async_support as ccxt
import pandas as pd
from datetime import datetime, timedelta

async def fetch_data(symbol, timeframe='5m', days=30):
    exchange = ccxt.binance()
    since = exchange.parse8601((datetime.utcnow() - timedelta(days=days)).isoformat() + 'Z')
    all_ohlcv = []
    
    while True:
        try:
            ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, since, limit=1000)
            if not ohlcv:
                break
            all_ohlcv.extend(ohlcv)
            since = ohlcv[-1][0] + 1
            if len(ohlcv) < 1000:
                break
        except Exception as e:
            print(f"Error fetching data: {e}")
            break
            
    await exchange.close()
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    return df

def simulate_grid(df, range_pct, num_levels, order_size_usd=15.0):
    start_price = df.iloc[0]['open']
    
    half_range = range_pct / 2.0
    upper_price = start_price * (1.0 + half_range)
    lower_price = start_price * (1.0 - half_range)
    grid_spacing = (upper_price - lower_price) / num_levels
    
    levels = []
    for i in range(num_levels + 1):
        levels.append(lower_price + i * grid_spacing)
        
    center_idx = num_levels // 2
    
    # State
    buy_orders = levels[:center_idx]
    sell_orders = levels[center_idx+1:]
    
    realized_pnl = 0.0
    inventory = 0.0
    trades = 0
    
    for i, row in df.iterrows():
        high = row['high']
        low = row['low']
        
        # Check sells
        executed_sells = []
        for p in sell_orders:
            if high >= p:
                qty = order_size_usd / p
                inventory -= qty
                trades += 1
                executed_sells.append(p)
                # create new buy order below
                new_buy_price = p - grid_spacing
                buy_orders.append(new_buy_price)
                realized_pnl += (grid_spacing / p) * order_size_usd  # Approssimazione profitti da un grid spazzato
                
        for p in executed_sells:
            sell_orders.remove(p)
            
        # Check buys
        executed_buys = []
        for p in buy_orders:
            if low <= p:
                qty = order_size_usd / p
                inventory += qty
                trades += 1
                executed_buys.append(p)
                # create new sell order above
                new_sell_price = p + grid_spacing
                sell_orders.append(new_sell_price)
                
        for p in executed_buys:
            buy_orders.remove(p)
            
    return realized_pnl, trades, inventory

async def main():
    print("Fetching data for SOL/USDT (last 14 days, 5m)...")
    df = await fetch_data('SOL/USDT', '5m', 14)
    print(f"Loaded {len(df)} candles.")
    
    print("\n--- TEST: Variazione Range % (Levels=20) ---")
    for r in [0.03, 0.05, 0.08, 0.12, 0.15]:
        pnl, tr, inv = simulate_grid(df, r, 20)
        print(f"Range {int(r*100)}%: PNL = ${pnl:.2f} | Trades = {tr} | Inventory = {inv:.3f} SOL")

    print("\n--- TEST: Variazione Numero Livelli (Range=8%) ---")
    for l in [10, 20, 30, 40]:
        pnl, tr, inv = simulate_grid(df, 0.08, l)
        print(f"Levels {l}: PNL = ${pnl:.2f} | Trades = {tr} | Inventory = {inv:.3f} SOL")
        
if __name__ == '__main__':
    asyncio.run(main())
