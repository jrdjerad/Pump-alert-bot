#!/usr/bin/env python3
"""
Binance Futures Pump Alert Bot
Monitors short-window price spikes and sends Telegram alerts
based on your exact short trading strategy.
"""

import os
import time
import logging
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Alert thresholds — Layer 1 trigger
SPIKE_1H_PCT  = float(os.getenv("SPIKE_1H_PCT",  "30"))   # % pump in 1h
SPIKE_4H_PCT  = float(os.getenv("SPIKE_4H_PCT",  "50"))   # % pump in 4h

# Layer 2 exhaustion — volume fade ratio (current vol vs avg vol)
VOL_FADE_RATIO = float(os.getenv("VOL_FADE_RATIO", "0.5")) # current < 50% of avg = fading

# Cooldown: don't re-alert same coin within this many minutes
ALERT_COOLDOWN_MIN = int(os.getenv("ALERT_COOLDOWN_MIN", "60"))

# Scan interval in seconds
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "120"))  # every 2 minutes

# Minimum 24h volume in USDT (filter out dead coins)
MIN_VOLUME_USDT = float(os.getenv("MIN_VOLUME_USDT", "500000"))

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("pump_bot.log")
    ]
)
log = logging.getLogger(__name__)

# ── State ────────────────────────────────────────────────────────────────────
alerted: dict[str, float] = {}   # symbol -> last alert timestamp


# ── Binance API ──────────────────────────────────────────────────────────────
BINANCE_BASE = "https://fapi.binance.com"

def get_usdt_futures() -> list[dict]:
    """All active USDT perpetual futures pairs."""
    r = requests.get(f"{BINANCE_BASE}/fapi/v1/ticker/24hr", timeout=10)
    r.raise_for_status()
    return [
        d for d in r.json()
        if d["symbol"].endswith("USDT") and float(d["quoteVolume"]) >= MIN_VOLUME_USDT
    ]

def get_klines(symbol: str, interval: str, limit: int = 10) -> list:
    """Fetch recent klines for a symbol."""
    r = requests.get(
        f"{BINANCE_BASE}/fapi/v1/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=10
    )
    r.raise_for_status()
    return r.json()

def calc_spike(klines: list) -> float:
    """
    % change from the open of the oldest candle to the close of the newest.
    Positive = pump, negative = dump.
    """
    if not klines or len(klines) < 2:
        return 0.0
    open_price  = float(klines[0][1])
    close_price = float(klines[-1][4])
    if open_price == 0:
        return 0.0
    return ((close_price - open_price) / open_price) * 100

def calc_volume_fade(klines: list) -> tuple[float, float, bool]:
    """
    Compare last candle volume vs average of previous candles.
    Returns (last_vol, avg_vol, is_fading).
    """
    if len(klines) < 3:
        return 0, 0, False
    volumes   = [float(k[5]) for k in klines]
    last_vol  = volumes[-1]
    avg_vol   = sum(volumes[:-1]) / len(volumes[:-1])
    is_fading = (avg_vol > 0) and (last_vol / avg_vol) < VOL_FADE_RATIO
    return last_vol, avg_vol, is_fading

def get_wick_rejection(klines: list) -> bool:
    """
    Detect upper wick rejection on last candle:
    wick > 40% of total candle range = price tried to go higher, got rejected.
    """
    if not klines:
        return False
    last  = klines[-1]
    high  = float(last[2])
    low   = float(last[3])
    close = float(last[4])
    candle_range = high - low
    if candle_range == 0:
        return False
    upper_wick = high - close
    return (upper_wick / candle_range) > 0.40

def hours_since_first_pump(klines_4h: list) -> float:
    """Estimate how many hours ago the pump started (oldest green candle in run)."""
    now_ms = time.time() * 1000
    for k in reversed(klines_4h):
        if float(k[4]) < float(k[1]):   # red candle = pump started after this
            break
    open_time_ms = float(klines_4h[-1][0])
    return (now_ms - open_time_ms) / (1000 * 3600)


# ── Telegram ─────────────────────────────────────────────────────────────────
def send_telegram(message: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Telegram credentials missing. Check .env file.")
        return False
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": "HTML"
    }
    try:
        r = requests.post(url, data=data, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False


# ── Alert builders ────────────────────────────────────────────────────────────
def build_layer1_alert(symbol: str, spike_1h: float, spike_4h: float,
                       price: float, vol_24h: float) -> str:
    coin = symbol.replace("USDT", "")
    vol_str = f"${vol_24h/1e6:.1f}M" if vol_24h >= 1e6 else f"${vol_24h/1e3:.0f}K"
    return (
        f"🚨 <b>LAYER 1 — PUMP DETECTED</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Coin:       <b>{coin}/USDT</b>\n"
        f"Price:      <b>${price:.5g}</b>\n"
        f"1H spike:   <b>+{spike_1h:.1f}%</b>\n"
        f"4H spike:   <b>+{spike_4h:.1f}%</b>\n"
        f"24H vol:    {vol_str}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>WATCH ONLY — wait for exhaustion signal</i>\n"
        f"Do NOT enter yet. Monitor for Layer 2."
    )

def build_layer2_alert(symbol: str, spike_1h: float, spike_4h: float,
                       price: float, vol_fade: bool, wick: bool,
                       hours_pumping: float) -> str:
    coin = symbol.replace("USDT", "")
    signals = []
    if vol_fade: signals.append("📉 Volume fading")
    if wick:     signals.append("🕯️ Wick rejection")
    if hours_pumping > 3: signals.append(f"⏱️ Pumping {hours_pumping:.1f}h — aging")
    signals_str = "\n".join(f"  • {s}" for s in signals) if signals else "  • Monitoring"
    return (
        f"🎯 <b>LAYER 2 — EXHAUSTION SIGNAL</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Coin:       <b>{coin}/USDT</b>\n"
        f"Price:      <b>${price:.5g}</b>\n"
        f"1H spike:   <b>+{spike_1h:.1f}%</b>\n"
        f"4H spike:   <b>+{spike_4h:.1f}%</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Exhaustion signals:\n{signals_str}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"✅ <b>POTENTIAL SHORT ENTRY</b>\n"
        f"Size: <b>$0.26 minimum</b> | Leverage: 10–20x\n"
        f"Wait for your support level confirmation."
    )

def build_addon_alert(symbol: str, new_high_pct: float, price: float) -> str:
    coin = symbol.replace("USDT", "")
    return (
        f"➕ <b>ADD-ON WATCH — NEW HIGH</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Coin:       <b>{coin}/USDT</b>\n"
        f"Price:      <b>${price:.5g}</b>\n"
        f"New high:   <b>+{new_high_pct:.1f}%</b> from your alert\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚡ Check technical structure for add-on\n"
        f"Size: <b>$0.26 only</b> — same as initial entry."
    )


# ── Main scan loop ────────────────────────────────────────────────────────────
# Track per-coin state for add-on logic
coin_state: dict[str, dict] = {}

def scan():
    log.info("Scanning Binance Futures...")
    try:
        tickers = get_usdt_futures()
    except Exception as e:
        log.error(f"Failed to fetch tickers: {e}")
        return

    now = time.time()
    triggered = 0

    for ticker in tickers:
        symbol   = ticker["symbol"]
        price    = float(ticker["lastPrice"])
        vol_24h  = float(ticker["quoteVolume"])

        # Skip stablecoins and weird pairs
        coin = symbol.replace("USDT", "")
        if any(x in coin for x in ["USDC", "BUSD", "TUSD", "DAI", "FDUSD"]):
            continue

        # ── Cooldown check ───────────────────────────────────────────────────
        last_alerted = alerted.get(symbol, 0)
        cooldown_sec = ALERT_COOLDOWN_MIN * 60
        in_cooldown  = (now - last_alerted) < cooldown_sec

        try:
            # Fetch klines
            klines_1h = get_klines(symbol, "1h",  limit=4)   # last 4x 1h candles
            klines_4h = get_klines(symbol, "4h",  limit=6)   # last 6x 4h candles

            spike_1h = calc_spike(klines_1h)
            spike_4h = calc_spike(klines_4h)

            _, _, vol_fading = calc_volume_fade(klines_1h)
            wick_rejection   = get_wick_rejection(klines_1h)
            hrs_pumping      = hours_since_first_pump(klines_4h)

            state = coin_state.get(symbol, {})

            # ── Layer 1: Fresh pump detected ────────────────────────────────
            if spike_1h >= SPIKE_1H_PCT or spike_4h >= SPIKE_4H_PCT:
                if not in_cooldown:
                    log.info(f"L1 ALERT: {symbol} | 1H: +{spike_1h:.1f}% | 4H: +{spike_4h:.1f}%")
                    msg = build_layer1_alert(symbol, spike_1h, spike_4h, price, vol_24h)
                    send_telegram(msg)
                    alerted[symbol] = now
                    coin_state[symbol] = {
                        "alert_price":  price,
                        "layer2_sent":  False,
                        "addon_sent":   False,
                        "peak_price":   price,
                    }
                    triggered += 1
                    time.sleep(0.3)  # gentle rate limit

                # ── Layer 2: Exhaustion on already-alerted coin ──────────────
                elif state and not state.get("layer2_sent"):
                    exhaustion_signals = sum([vol_fading, wick_rejection, hrs_pumping > 3])
                    if exhaustion_signals >= 2:
                        log.info(f"L2 ALERT: {symbol} | Exhaustion signals: {exhaustion_signals}")
                        msg = build_layer2_alert(
                            symbol, spike_1h, spike_4h, price,
                            vol_fading, wick_rejection, hrs_pumping
                        )
                        send_telegram(msg)
                        coin_state[symbol]["layer2_sent"] = True
                        triggered += 1
                        time.sleep(0.3)

                # ── Layer 3: Add-on watch (price made new high) ──────────────
                elif state and state.get("layer2_sent") and not state.get("addon_sent"):
                    peak = state.get("peak_price", price)
                    if price > peak * 1.20:   # 20% above previous peak
                        new_high_pct = ((price - state["alert_price"]) / state["alert_price"]) * 100
                        log.info(f"ADDON ALERT: {symbol} | New high +{new_high_pct:.1f}%")
                        msg = build_addon_alert(symbol, new_high_pct, price)
                        send_telegram(msg)
                        coin_state[symbol]["addon_sent"] = True
                        coin_state[symbol]["peak_price"] = price
                        triggered += 1
                        time.sleep(0.3)
                    else:
                        # Update peak
                        if price > state.get("peak_price", 0):
                            coin_state[symbol]["peak_price"] = price

        except Exception as e:
            log.debug(f"Error processing {symbol}: {e}")
            continue

    log.info(f"Scan complete. {len(tickers)} pairs checked. {triggered} alerts sent.")


def main():
    log.info("=" * 50)
    log.info("  Binance Futures Pump Alert Bot — Starting")
    log.info("=" * 50)
    log.info(f"  1H spike threshold : {SPIKE_1H_PCT}%")
    log.info(f"  4H spike threshold : {SPIKE_4H_PCT}%")
    log.info(f"  Scan interval      : {SCAN_INTERVAL}s")
    log.info(f"  Alert cooldown     : {ALERT_COOLDOWN_MIN}min")
    log.info("=" * 50)

    # Send startup message
    send_telegram(
        "🤖 <b>Pump Alert Bot Started</b>\n"
        f"Monitoring all Binance USDT Futures\n"
        f"1H threshold: <b>{SPIKE_1H_PCT}%</b> | 4H: <b>{SPIKE_4H_PCT}%</b>\n"
        f"Scan every: <b>{SCAN_INTERVAL}s</b>"
    )

    while True:
        try:
            scan()
        except KeyboardInterrupt:
            log.info("Bot stopped by user.")
            break
        except Exception as e:
            log.error(f"Unexpected error in main loop: {e}")

        log.info(f"Sleeping {SCAN_INTERVAL}s until next scan...")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
