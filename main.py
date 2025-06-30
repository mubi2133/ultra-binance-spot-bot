# main.py — Part 1: Imports, Env Setup, Configs
import os, time, threading, requests
import numpy as np, pandas as pd
from flask import Flask
from dotenv import load_dotenv
from binance.client import Client
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator, StochRSIIndicator
from ta.volatility import BollingerBands
from ta.volume import OnBalanceVolumeIndicator

# 🔐 Load Environment Variables
load_dotenv()
api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")
telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")

# 🤖 Binance Client & Flask App
client = Client(api_key, api_secret)
app = Flask(__name__)

# ⚙️ Config Settings
SCAN_INTERVAL = 10                      # Time between coin scan attempts
RESCAN_INTERVAL = 45                   # Wait time if no 90+ signal found
TRADE_UPDATE_INTERVAL = 30             # Time between trade checks
SIGNAL_WINRATE_THRESHOLD = 90          # Minimum confidence for auto-trade
ALERT_THRESHOLD = 80                   # Alert level for signal notification
SELL_SIGNAL_DROP = 15                  # Drop in winrate to trigger sell
MIN_VOLUME = 500000                    # Min 24h volume to consider a pair
TRADE_COOLDOWN = 120                   # Cooldown after any trade (seconds)
BALANCE_USAGE_RATIO = 0.95             # Use 95% of available USDT balance

# ⛔ Smart CPU Saver (for Railway)
SLEEP_BETWEEN_SCAN_SETS = 15           # Pause after each batch of 25 symbols
PAUSE_IF_NO_ACTIVITY = 120             # Long pause if no valid signal found

# 📊 Global State
latest_signals = []
active_trade_symbol = None
last_trade_time = 0
# scanner.py — Part 2: USDT Pairs Scanner with 25-Batch Logic
from utils import send_telegram_message, analyze_symbol
import time

def get_all_usdt_pairs():
    try:
        exchange_info = client.get_exchange_info()
        usdt_pairs = [
            symbol['symbol'] for symbol in exchange_info['symbols']
            if symbol['quoteAsset'] == 'USDT' and symbol['status'] == 'TRADING'
            and not symbol['symbol'].endswith('UPUSDT') and not symbol['symbol'].endswith('DOWNUSDT')
        ]
        return usdt_pairs
    except Exception as e:
        send_telegram_message(f"❌ Error fetching USDT pairs: {e}")
        return []

def scan_market():
    global latest_signals
    usdt_pairs = get_all_usdt_pairs()
    found_90plus = False
    latest_signals = []

    for i in range(0, len(usdt_pairs), 25):
        batch = usdt_pairs[i:i+25]
        send_telegram_message(f"🔍 Scanning batch {i//25 + 1} of {len(usdt_pairs)//25 + 1}...")
        
        for symbol in batch:
            try:
                signal_data = analyze_symbol(symbol)
                if not signal_data:
                    continue
                
                confidence = signal_data['confidence']
                pattern_match = signal_data['pattern']
                if confidence >= ALERT_THRESHOLD:
                    msg = f"📈 Signal Found: {symbol} | Confidence: {confidence}% | Pattern: {pattern_match}"
                    send_telegram_message(msg)
                
                if confidence >= SIGNAL_WINRATE_THRESHOLD and pattern_match:
                    latest_signals.append(signal_data)
                    found_90plus = True
                    return signal_data  # Immediately return first strong trade
            except Exception as e:
                print(f"Error scanning {symbol}: {e}")
                continue

        time.sleep(SLEEP_BETWEEN_SCAN_SETS)

    if not found_90plus:
        send_telegram_message("⚠️ No strong 90+ signals found. Waiting before next scan.")
        time.sleep(RESCAN_INTERVAL)
    
    return None
  # analyzer.py — Part 3: Analyzer Engine
import pandas as pd
import numpy as np
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator, StochRSIIndicator
from ta.volatility import BollingerBands
from ta.volume import OnBalanceVolumeIndicator

def get_klines(symbol, interval='15m', limit=100):
    try:
        data = client.get_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(data, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_asset_volume', 'number_of_trades',
            'taker_buy_base_vol', 'taker_buy_quote_vol', 'ignore'
        ])
        df['close'] = df['close'].astype(float)
        df['volume'] = df['volume'].astype(float)
        return df
    except Exception as e:
        print(f"Error fetching Klines for {symbol}: {e}")
        return None

def analyze_symbol(symbol):
    df = get_klines(symbol)
    if df is None or df.empty:
        return None

    try:
        ema = EMAIndicator(df['close'], window=14).ema_indicator().iloc[-1]
        macd = MACD(df['close']).macd_diff().iloc[-1]
        rsi = RSIIndicator(df['close']).rsi().iloc[-1]
        stochrsi = StochRSIIndicator(df['close']).stochrsi_k().iloc[-1]
        boll = BollingerBands(df['close'])
        bb_width = boll.bollinger_hband().iloc[-1] - boll.bollinger_lband().iloc[-1]
        obv = OnBalanceVolumeIndicator(df['close'], df['volume']).on_balance_volume().iloc[-1]

        # Chart pattern simple logic
        last_candles = df['close'].values[-5:]
        is_uptrend = all(x < y for x, y in zip(last_candles, last_candles[1:]))
        is_bullish = (df['close'].iloc[-1] > ema) and (macd > 0) and (rsi > 55) and (stochrsi > 60)

        confidence = 0
        if is_bullish:
            confidence += 60
        if is_uptrend:
            confidence += 20
        if bb_width > 0.01:
            confidence += 10
        if obv > 0:
            confidence += 10

        return {
            'symbol': symbol,
            'confidence': confidence,
            'pattern': 'uptrend' if is_uptrend else 'none'
        }
    except Exception as e:
        print(f"Error analyzing {symbol}: {e}")
        return None
      # trader.py — Part 4: Trader Engine
import time
from binance.exceptions import BinanceAPIException

cooldown_time = 0  # Global cooldown tracker

def get_trade_quantity(symbol, usdt_balance):
    try:
        ticker = client.get_symbol_ticker(symbol=symbol)
        price = float(ticker['price'])
        quantity = round((usdt_balance * BALANCE_USAGE_RATIO) / price, 3)
        return quantity
    except Exception as e:
        print(f"Quantity calc error for {symbol}: {e}")
        return None

def place_order(symbol, quantity):
    try:
        order = client.order_market_buy(symbol=symbol, quantity=quantity)
        print(f"Buy Order Placed: {order}")
        return order
    except BinanceAPIException as e:
        print(f"Binance API error: {e.message}")
    except Exception as e:
        print(f"Order error: {e}")
    return None

def trade_executor(signal_data):
    global cooldown_time

    current_time = time.time()
    if current_time < cooldown_time:
        print("Cooldown active, skipping trade.")
        return

    symbol = signal_data['symbol']
    confidence = signal_data['confidence']
    if confidence < 90:
        print(f"Signal for {symbol} not strong enough: {confidence}%")
        return

    try:
        usdt_balance = float(client.get_asset_balance(asset='USDT')['free'])
        if usdt_balance < 10:
            print("Insufficient balance to trade.")
            return

        quantity = get_trade_quantity(symbol, usdt_balance)
        if quantity:
            order = place_order(symbol, quantity)
            if order:
                cooldown_time = current_time + TRADE_COOLDOWN
                send_telegram_message(f"✅ Trade Executed: {symbol}\nConfidence: {confidence}%\nQty: {quantity}")
    except Exception as e:
        print(f"Trade execution failed: {e}")
      # notifier.py — Part 5: Telegram Alerts & Commands
import requests
from flask import request as flask_request

def send_telegram_message(message):
    try:
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        payload = {"chat_id": telegram_chat_id, "text": message}
        requests.post(url, data=payload)
    except Exception as e:
        print(f"Telegram error: {e}")

@app.route('/webhook', methods=['POST'])
def telegram_webhook():
    try:
        data = flask_request.get_json()
        chat_id = str(data['message']['chat']['id'])
        text = data['message']['text']

        if chat_id != telegram_chat_id:
            return {"ok": False}

        if text.lower() == "/start":
            send_telegram_message("✅ Bot is already running in auto mode.")
        elif text.lower() == "/status":
            send_telegram_message("📊 Bot is running. Tracking signals & trades.")
        else:
            send_telegram_message("❓ Unknown command.")
        return {"ok": True}
    except Exception as e:
        print(f"Webhook error: {e}")
        return {"ok": False}
      # run_bot.py — Part 6: Run Flask App + Threads

from threading import Thread
from flask import jsonify

# ✅ Scan loop runs every few seconds, then sleeps smartly
def signal_scan_loop():
    while True:
        try:
            from scanner import scan_market
            scan_market()
            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            print(f"[Scanner Error] {e}")
            time.sleep(10)

# ✅ Trade monitor loop
def trade_monitor_loop():
    while True:
        try:
            from trader import manage_trades
            manage_trades()
            time.sleep(TRADE_UPDATE_INTERVAL)
        except Exception as e:
            print(f"[Trade Error] {e}")
            time.sleep(10)

# ✅ Start scanning and trading in background
@app.before_first_request
def start_background_tasks():
    Thread(target=signal_scan_loop, daemon=True).start()
    Thread(target=trade_monitor_loop, daemon=True).start()

# ✅ Flask test route
@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "🚀 Bot is running and scanning in auto mode"})

# ✅ Start Flask app
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
