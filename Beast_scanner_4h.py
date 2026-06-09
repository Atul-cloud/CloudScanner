import yfinance as yf
import pandas as pd
import requests
import sys
import os

# --- SECURE CREDENTIAL LOADING ---
TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SYMBOLS = ["^NDX", "^NSEI", "EURUSD=X", "EURJPY=X", "EURAUD=X", "USDCAD=X", "BTC-USD"]

def detect_star_patterns(df):
    if len(df) < 20:
        return None, None

    historical_bodies = (df['Close'] - df['Open']).abs().iloc[-17:-5]
    avg_body = historical_bodies.mean()
    candle_timestamp = df.index[-2].strftime('%Y-%m-%d %H:%M')

    # --- 3-CANDLE PATTERN LOGIC ---
    o3_1, c3_1 = df['Open'].iloc[-4], df['Close'].iloc[-4]
    o3_2, c3_2 = df['Open'].iloc[-3], df['Close'].iloc[-3]
    o3_3, c3_3 = df['Open'].iloc[-2], df['Close'].iloc[-2]
    b3_1, b3_2, b3_3 = abs(c3_1 - o3_1), abs(c3_2 - o3_2), abs(c3_3 - o3_3)

    is_c3_1_valid = b3_1 > (avg_body * 0.4)
    is_c3_3_valid = b3_3 > (avg_body * 0.4)
    is_c3_2_small = (b3_2 < (avg_body * 0.75)) or (b3_2 < b3_1 * 0.6 and b3_2 < b3_3 * 0.6)
    midpoint3_1 = (o3_1 + c3_1) / 2

    is_3c_morning = (c3_1 < o3_1) and is_c3_1_valid and is_c3_2_small and (c3_3 > o3_3) and is_c3_3_valid and (c3_3 > midpoint3_1)
    is_3c_evening = (c3_1 > o3_1) and is_c3_1_valid and is_c3_2_small and (c3_3 < o3_3) and is_c3_3_valid and (c3_3 < midpoint3_1)

    # --- 4-CANDLE PATTERN LOGIC ---
    o4_1, c4_1 = df['Open'].iloc[-5], df['Close'].iloc[-5]
    o4_2, c4_2 = df['Open'].iloc[-4], df['Close'].iloc[-4]
    o4_3, c4_3 = df['Open'].iloc[-3], df['Close'].iloc[-3]
    o4_4, c4_4 = df['Open'].iloc[-2], df['Close'].iloc[-2]
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
        print("⚠️ Missing Telegram credentials.")
        return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print(f"Telegram failed: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("❌ Error: Missing timeframe parameter.")
        sys.exit(1)
        
    target_tf = sys.argv[1]
    
    # NEW: Instant Telegram ping to confirm the bot is alive and working
    send_alert(f"🤖 Cloud Action Started: Running {target_tf} Scan...")
    print(f"🚀 Cloud Engine Initialized for Timeframe: {target_tf}")

    for sym in SYMBOLS:
        try:
            data = yf.download(sym, period="60d", interval=target_tf, progress=False)
            if data.empty:
                continue

            if isinstance(data.columns, pd.MultiIndex):
                data.columns = data.columns.droplevel(1)

            pattern, match_time = detect_star_patterns(data)
            clean_name = sym.replace("=X", "").replace("^", "")
            
            print(f"| {clean_name.ljust(10)} | Checked: {match_time} | Pattern: {pattern if pattern else 'None'}")
            
            if pattern:
                msg = f"☁️ 4H CLOUD MATCH ☁️\n\nAsset: {clean_name}\nTimeframe: {target_tf}\nPattern: {pattern}\nClosed Candle Mark: {match_time}"
                send_alert(msg)
                
        except Exception as e:
            print(f"Error checking asset {sym}: {e}")
            
