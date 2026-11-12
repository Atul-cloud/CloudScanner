#!/usr/bin/env python3
"""
Gold (XAU/USD) EMA 20 / EMA 50 cross notifier  ->  Discord + Telegram

* Asks which timeframe to watch: 1, 3, 5 or 15 minutes.
* Data: Twelve Data. EMAs are calculated locally, so after one start-up download each new
  bar costs a single API request (daily counter keeps you under the 800 free requests).
    3m bars are built locally from 1m bars (Twelve Data has no 3min).
* Sends a message when the script starts, then one whenever EMA20 crosses EMA50.
  A cross is checked on CLOSED bars only (same as TradingView's ta.crossover / ta.crossunder).

Run:  python gold_ema_alert.py            (asks for the timeframe)
      python gold_ema_alert.py --tf 5     (skips the question)
Needs: pip install pandas requests
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
import time
from collections import deque

import pandas as pd
import requests

# --- CONFIGURATION PACKETS ---
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1516498223944962058/UD6AFnaCF1RJFAn92AUVA9tIrc1j7jf3ZW3BrNK1vTcsA7VKmO64BLQ_IT-iebxQvaiy"
TELEGRAM_TOKEN = "8794952355:AAEoZfhAomXe_Y18KmiLeKIb6RESOnfQ_uA"
TELEGRAM_CHAT_ID = "786053316"
TWELVEDATA_API_KEY = "3410684668ac4b138d3c2485ac3a9c3b"

SYMBOL = "XAU/USD"
FAST, SLOW = 20, 50
TIMEFRAMES = [1, 3, 5, 15]
DAILY_BUDGET = 780        # stay under the 800 free requests / day
PER_MIN_LIMIT = 8         # free plan: 8 requests / minute
WARMUP_BARS = 1500        # history used to settle the EMAs (downloaded once)

log = logging.getLogger("gold")
IST = pd.Timedelta(hours=5, minutes=30)


def _now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def fmt(ts: pd.Timestamp) -> str:
    return f"{ts:%Y-%m-%d %H:%M} UTC  ({(ts + IST):%H:%M} IST)"


# ───────────────────────── EMA (same as Pine ta.ema: seeded with the SMA) ─────────────────────────
class EMA:
    def __init__(self, n: int):
        self.n, self.alpha, self.cnt, self.sum, self.val = n, 2.0 / (n + 1), 0, 0.0, float("nan")

    def update(self, x: float) -> float:
        self.cnt += 1
        if self.cnt < self.n:
            self.sum += x
            self.val = float("nan")
        elif self.cnt == self.n:
            self.sum += x
            self.val = self.sum / self.n
        else:
            self.val = self.alpha * x + (1 - self.alpha) * self.val
        return self.val


# ───────────────────────── Notifications ─────────────────────────
def notify(text: str):
    """Send to Discord and Telegram. Failures are logged (without secrets) and never stop the bot."""
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": text[:1990]}, timeout=10).raise_for_status()
        log.info("Discord sent")
    except Exception as e:
        log.error("Discord failed: %s", type(e).__name__)
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]}, timeout=10).raise_for_status()
        log.info("Telegram sent")
    except Exception as e:
        log.error("Telegram failed: %s", type(e).__name__)


# ───────────────────────── Twelve Data (budget-aware) ─────────────────────────
URL = "https://api.twelvedata.com/time_series"
BASE = {1: ("1min", 1), 3: ("1min", 1), 5: ("5min", 5), 15: ("15min", 15)}   # tf -> (interval, base minutes)


class TwelveData:
    def __init__(self):
        self.day, self.used, self.recent = None, 0, deque()

    def _throttle(self):
        while True:
            now = _now()
            if self.day != now.date():                      # credits reset at 00:00 UTC
                self.day, self.used = now.date(), 0
            if self.used >= DAILY_BUDGET:
                wait = (pd.Timestamp(now.date(), tz="UTC") + pd.Timedelta(days=1) - now).total_seconds() + 5
                log.warning("Daily request budget used - sleeping until 00:00 UTC (%.0f min)", wait / 60)
                time.sleep(min(wait, 600))
                continue
            while self.recent and (now - self.recent[0]).total_seconds() >= 60:
                self.recent.popleft()
            if len(self.recent) >= PER_MIN_LIMIT:
                time.sleep(max(1.0, 60 - (now - self.recent[0]).total_seconds() + 0.5))
                continue
            self.recent.append(now)
            self.used += 1
            return

    def bars(self, tf: int, n: int = 0, since: pd.Timestamp = None) -> pd.DataFrame:
        """Last ~n CLOSED bars of timeframe tf (indexed by bar-open time, UTC).
        With `since`, n is computed AFTER any throttle sleep so no bar can be skipped."""
        interval, bmin = BASE[tf]
        k = tf // bmin
        self._throttle()
        if since is not None:
            n = int((_now() - since) / pd.Timedelta(minutes=tf)) + 3
        r = requests.get(URL, timeout=20, params=dict(
            symbol=SYMBOL, interval=interval, outputsize=int(min(max(n * k + k, 2), 5000)),
            timezone="UTC", order="asc", apikey=TWELVEDATA_API_KEY))
        j = r.json()
        if j.get("status") == "error" or "values" not in j:
            raise RuntimeError(f"TwelveData: {j.get('message', j)}")
        df = pd.DataFrame(j["values"])
        df.index = pd.to_datetime(df["datetime"], utc=True)
        df = df[["open", "high", "low", "close"]].astype(float).dropna()
        df = df[~df.index.duplicated(keep="last")].sort_index()
        df = df[df.index + pd.Timedelta(minutes=bmin) <= _now()]          # drop the still-forming bar
        if df.empty or k == 1:
            return df
        last_close = df.index[-1] + pd.Timedelta(minutes=bmin)             # build 3m from 1m
        out = df.resample(f"{tf}min", origin="epoch").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        return out[out.index + pd.Timedelta(minutes=tf) <= last_close]


# ───────────────────────── Cross detector ─────────────────────────
class CrossWatcher:
    def __init__(self):
        self.ef, self.es = EMA(FAST), EMA(SLOW)
        self.pf = self.ps = float("nan")
        self.fast = self.slow = float("nan")

    def on_close(self, close: float):
        """Returns 'BULL', 'BEAR' or None  (== ta.crossover / ta.crossunder of EMA20 vs EMA50)."""
        self.fast, self.slow = self.ef.update(close), self.es.update(close)
        ok = not any(math.isnan(v) for v in (self.pf, self.ps, self.fast, self.slow))
        sig = None
        if ok and self.fast > self.slow and self.pf <= self.ps:
            sig = "BULL"
        elif ok and self.fast < self.slow and self.pf >= self.ps:
            sig = "BEAR"
        self.pf, self.ps = self.fast, self.slow
        return sig


def cross_message(sig: str, tf: int, ts: pd.Timestamp, close: float, w: CrossWatcher) -> str:
    if sig == "BULL":
        head, line = "🟢 GOLD BULLISH CROSS", f"EMA{FAST} crossed ABOVE EMA{SLOW}"
    else:
        head, line = "🔴 GOLD BEARISH CROSS", f"EMA{FAST} crossed BELOW EMA{SLOW}"
    return (f"{head}  ({tf}m)\n{line}\nBar closed: {fmt(ts + pd.Timedelta(minutes=tf))}\n"
            f"Close: {close:.2f}\nEMA{FAST}: {w.fast:.2f}   EMA{SLOW}: {w.slow:.2f}")


# ───────────────────────── Main loop ─────────────────────────
def run(tf: int, delay: int):
    tfd = pd.Timedelta(minutes=tf)
    td, w = TwelveData(), CrossWatcher()
    est = int(23 * 60 / tf)
    log.info("Timeframe %dm -> about %d API requests/day while the market is open%s", tf, est,
             "  (above the 800 free limit: the daily cap will pause the bot for part of the day)" if est > DAILY_BUDGET else "")

    n = min(WARMUP_BARS, 5000 // (tf // BASE[tf][1]) - 1)
    df = td.bars(tf, n)                                  # one download; EMAs are computed locally
    if len(df) < SLOW + 1:
        raise SystemExit("Not enough history returned by Twelve Data")
    for c in df["close"]:
        w.on_close(c)                                    # warm-up: no alerts for old crosses
    last = df.index[-1]
    state = f"EMA{FAST} is ABOVE EMA{SLOW} (bullish)" if w.fast > w.slow else f"EMA{FAST} is BELOW EMA{SLOW} (bearish)"
    notify(f"✅ Gold EMA {FAST}/{SLOW} cross alert STARTED  ({tf}m)\nLast closed bar: {fmt(last + tfd)}\n"
           f"Close: {df.close.iloc[-1]:.2f}\nEMA{FAST}: {w.fast:.2f}   EMA{SLOW}: {w.slow:.2f}\nNow: {state}\n"
           f"You will be alerted on every new cross.")
    log.info("Running. Last closed bar %s UTC, waiting for the next one...", last)

    retry = 5
    while True:
        try:
            now = _now()
            due = last + 2 * tfd + pd.Timedelta(seconds=delay)        # close of next bar + delay
            if now < due:
                time.sleep(min((due - now).total_seconds(), 60))
                continue
            df = td.bars(tf, since=last)
            new = df[df.index > last]
            if new.empty:                                             # data lag / market closed
                time.sleep(retry)
                retry = min(retry * 2, 600)
                continue
            retry = 5
            for ts, c in new["close"].items():
                sig = w.on_close(c)
                last = ts
                if sig:
                    log.info("%s cross at %s", sig, ts)
                    notify(cross_message(sig, tf, ts, c, w))
            log.info("bar %s | close %.2f | EMA%d %.2f EMA%d %.2f | API used today: %d",
                     last, new.close.iloc[-1], FAST, w.fast, SLOW, w.slow, td.used)
        except KeyboardInterrupt:
            log.info("Stopped.")
            return
        except Exception as e:
            log.error("loop error: %s: %s", type(e).__name__, str(e)[:200])
            time.sleep(30)


def ask_timeframe() -> int:
    print("\nSelect timeframe for XAU/USD:")
    print("   1 -> 1 minute     3 -> 3 minute     5 -> 5 minute     15 -> 15 minute")
    while True:
        s = input("Timeframe (minutes): ").strip().lower().rstrip("m").strip()
        if s.isdigit() and int(s) in TIMEFRAMES:
            return int(s)
        print(f"  please enter one of {TIMEFRAMES}")


def main():
    ap = argparse.ArgumentParser(description="Gold EMA 20/50 cross notifier")
    ap.add_argument("--tf", type=int, choices=TIMEFRAMES, help="timeframe in minutes (asked if omitted)")
    ap.add_argument("--delay", type=int, default=3, help="seconds to wait after a bar closes before fetching")
    ap.add_argument("--test-alert", action="store_true", help="send a test message and exit")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    if a.test_alert:
        notify("✅ Test alert - Discord and Telegram are connected.")
        return
    tf = a.tf
    if tf is None:
        if not sys.stdin.isatty():
            raise SystemExit("--tf is required when not running interactively")
        tf = ask_timeframe()
    run(tf, a.delay)


if __name__ == "__main__":
    main()
