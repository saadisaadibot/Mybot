# -*- coding: utf-8 -*-
import os, time, threading, requests, json, traceback
import websocket
from flask import Flask
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

# ========= إعدادات =========
BITVAVO_URL   = "https://api.bitvavo.com/v2"
BINANCE_URL   = "https://api.binance.com/api/v3"
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK")  # ex: https://saadisaadibot-saqarxbo-production.up.railway.app/webhook

TOP_N         = int(os.getenv("TOP_N", 10))        # عدد العملات من Bitvavo
GAP_SPREAD_BP = float(os.getenv("GAP_SPREAD_BP", 20.0))  # 20 bp = 0.20%
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", 180))      # كل كم ثانية نحدث قائمة العملات

watched = {}   # symbol -> websocket object
lock = threading.Lock()

@app.route("/")
def home():
    return "🚀 Gap Sniper is alive ✅"

# ===== إرسال لصقر =====
def notify_saqr(symbol, reason="gap"):
    try:
        msg = f"اشتري {symbol}"
        r = requests.post(SAQAR_WEBHOOK, json={"message": msg}, timeout=6)
        if r.status_code == 200:
            print(f"🚀 [ALERT] {symbol} → {reason} → Sent to Saqar ✅")
        else:
            print(f"⚠️ Webhook {r.status_code}: {r.text}")
    except Exception as e:
        print("❌ Webhook error:", e)
        traceback.print_exc()

# ===== جلب أفضل عملات من Bitvavo (EUR) =====
def get_top_bitvavo():
    try:
        r = requests.get(f"{BITVAVO_URL}/ticker/24h", timeout=8)
        data = r.json()
        ranked = sorted(
            [d for d in data if d["market"].endswith("-EUR")],
            key=lambda x: float(x.get("priceChange", 0)), reverse=True
        )
        top = [d["market"].replace("-EUR", "") for d in ranked[:TOP_N]]
        print("📊 Top Bitvavo:", top)
        return top
    except Exception as e:
        print("❌ Error get_top_bitvavo:", e)
        traceback.print_exc()
        return []

# ===== مطابقة مع Binance USDT =====
def check_binance_exists(symbol):
    try:
        r = requests.get(f"{BINANCE_URL}/exchangeInfo", timeout=8).json()
        pairs = [s["symbol"] for s in r["symbols"]]
        return f"{symbol}USDT" if f"{symbol}USDT" in pairs else None
    except Exception as e:
        print("❌ Error check_binance_exists:", e)
        traceback.print_exc()
        return None

# ===== تحليل orderbook =====
def on_msg(ws, msg):
    try:
        data = json.loads(msg)
        bid = float(data["b"])
        ask = float(data["a"])
        spread_bp = (ask - bid) / bid * 10000

        if spread_bp > GAP_SPREAD_BP:
            symbol = data["s"].replace("USDT", "")
            print(f"⚡ GAP DETECTED {symbol}: spread {spread_bp:.2f} bp")
            notify_saqr(symbol, reason=f"spread {spread_bp:.2f}bp")

    except Exception as e:
        print("❌ Error parsing WS message:", e)
        traceback.print_exc()

def on_err(ws, err): 
    print("❌ WS error:", err)
    traceback.print_exc()

def on_close(ws, *a): 
    print("🔴 WS closed")

def on_open(ws): 
    print("🟢 WS connected")

def add_ws(symbol):
    with lock:
        if symbol in watched: 
            return
        url = f"wss://stream.binance.com:9443/ws/{symbol.lower()}usdt@bookTicker"
        ws = websocket.WebSocketApp(
            url,
            on_message=on_msg,
            on_error=on_err,
            on_close=on_close,
            on_open=on_open
        )
        t = threading.Thread(target=ws.run_forever, daemon=True)
        t.start()
        watched[symbol] = ws
        print("👁 Started WS for", symbol)

# ===== دورة العمل =====
def scanner():
    while True:
        try:
            top = get_top_bitvavo()
            for sym in top:
                bpair = check_binance_exists(sym)
                if bpair:
                    add_ws(sym)
        except Exception as e:
            print("❌ Error in scanner loop:", e)
            traceback.print_exc()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    threading.Thread(target=scanner, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
