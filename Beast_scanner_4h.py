import yfinance as yf
import pandas as pd
import requests
import sys
import os
from datetime import datetime, timedelta

# --- SECURE CREDENTIAL LOADING ---
TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SYMBOLS = {
    "NASDAQ 100": "^NDX",
    "NIFTY 50": "^NSEI",
    "GOLD COMEX": "GC=F",
    "EUR/USD": "EURUSD=X",
    "USD/JPY": "USDJPY=X",
    "EUR/JPY": "EURJPY=X",
    "BITCOIN": "BTC-USD"
}

def detect_star_patterns(df):
    # Since we strictly drop the incomplete live bar, we now safely target df.index[-1]
    # as the true, verified closed candle block.
    if len(df) < 15:
        return None, None

    historical_bodies = (df['Close'] - df['Open']).abs().iloc[-16:-4]
    avg_body = historical_bodies.mean()
    candle_timestamp = df.index[-1].strftime('%Y-%m-%d %H:%M')

    # --- 3-CANDLE PATTERN LOGIC (Targeting last closed row: iloc[-1]) ---
    o3_1, c3_1 = df['Open'].iloc[-3], df['Close'].iloc[-3]
    o3_2, c3_2 = df['Open'].iloc[-2], df['Close'].iloc[-2]
    o3_3, c3_3 = df['Open'].iloc[-1], df['Close'].iloc[-1]
    b3_1, b3_2, b3_3 = abs(c3_1 - o3_1), abs(c3_2 - o3_2), abs(c3_3 - o3_3)

    is_c3_1_valid = b3_1 > (avg_body * 0.4)
    is_c3_3_valid = b3_3 > (avg_body * 0.4)
    is_c3_2_small = (b3_2 < (avg_body * 0.75)) or (b3_2 < b3_1 * 0.6 and b3_2 < b3_3 * 0.6)
    midpoint3_1 = (o3_1 + c3_1) / 2

    is_3c_morning = (c3_1 < o3_1) and is_c3_1_valid and is_c3_2_small and (c3_3 > o3_3) and is_c3_3_valid and (c3_3 > midpoint3_1)
    is_3c_evening = (c3_1 > o3_1) and is_c3_1_valid and is_c3_2_small and (c3_3 < o3_3) and is_c3_3_valid and (c3_3 < midpoint3_1)

    # --- 4-CANDLE PATTERN LOGIC ---
    o4_1, c4_1 = df['Open'].iloc[-4], df['Close'].iloc[-4]
    o4_2, c4_2 = df['Open'].iloc[-3], df['Close'].iloc[-3]
    o4_3, c4_3 = df['Open'].iloc[-2], df['Close'].iloc[-2]
    o4_4, c4_4 = df['Open'].iloc[-1], df['Close'].iloc[-1]
    b4_1, b4_2, b4_3, b4_4 = abs(c4_1 - o4_1), abs(c4_2 - o4_2), abs(c4_3 - o4_3), abs(c4_4 - o4_4)

    is_4c_1_valid = b4_1 > (avg_body * 0.4)
    is_4c_4_valid = b4_4 > (avg_body * 0.4)
    is_4c_2_small = (b4_2 < (avg_body * 0.75)) or (b4_2 < b4_1 * 0.6 and b4_2 < b4_4 * 0.6)
    is_4c_3_small = (b4_3 < (avg_body * 0.75)) or (b4_3 < b4_1 * 0.6 and b4_3 < b4_4 * 0.6)
    midpoint4_1 = (o4_1 + c4_1) / 2

    is_4c_morning = (c4_1 < o4_1) and is_4c_1_valid and is_4c_2_small and is_4c_3_small and (c4_4 > o4_4) and is_4c_4_valid and (c4_4 > midpoint4_1)
    is_4c_evening = (c4_1 > o4_1) and is_4c_1_valid and is_4c_2_small and is_4c_3_small and (c4_4 < o4_4) and is_4c_4_valid and (c4_4 < midpoint4_1)

    if is_3c_morning: return "3-CANDLE MORNING STAR 📈", candle_timestamp
    if is_3c_evening: return "3-CANDLE EVENING STAR 📉", candle_timestamp
    if is_4c_morning: return "4-CANDLE MORNING STAR 📈 (Multi-Doji)", candle_timestamp
    if is_4c_evening: return "4-CANDLE EVENING STAR 📉 (Multi-Doji)", candle_timestamp
    return None, candle_timestamp

def send_alert(msg):
    if not TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print(f"Telegram failed: {e}")

if __name__ == "__main__":
    print("🚀 Cloud Engine Active. Fetching Hourly feeds for Resampling...")

    for display_name, ticker in SYMBOLS.items():
        try:
            data = yf.download(ticker, period="60d", interval="1h", progress=False)
            if data.empty:
                continue

            if isinstance(data.columns, pd.MultiIndex):
                data.columns = data.columns.droplevel(1)

            # Ensure data index is timezone-aware UTC to prevent local machine drift
            if data.index.tz is None:
                data = data.tz_localize('UTC')
            else:
                data = data.tz_convert('UTC')

            # UPGRADE 1: Offset the 4h resampling by 1 hour to lock directly onto TradingView forex intervals
            df_4h = data.resample('4h', offset='1h').agg({
                'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'
            }).dropna()

            # UPGRADE 2: Eliminate the live incomplete candle bar rule
            # If the current UTC time hasn't passed the candle start time + 4 hours, drop it.
            now_utc = datetime.now(df_4h.index.tz)
            if len(df_4h) > 0:
                last_candle_time = df_4h.index[-1]
                if now_utc < (last_candle_time + timedelta(hours=4)):
                    df_4h = df_4h.iloc[:-1] # Discard the active, uncompleted live candle

            pattern, match_time = detect_star_patterns(df_4h)
            print(f"| {display_name.ljust(12)} | Checked: {match_time} | Pattern: {pattern if pattern else 'None'}")
            
            if pattern:
                msg = f"☁️ 4H CLOUD MATCH ☁️\n\nAsset: {display_name}\nTicker: {ticker}\nPattern: {pattern}\nClosed Candle Mark: {match_time} UTC"
                send_alert(msg)
                
        except Exception as e:
            print(f"Error processing {display_name}: {e}")
        
