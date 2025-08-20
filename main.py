# main.py — Gap Sniper (Binance → Saqar)
# يعمل على Railway مباشرة ويطبع أخطاء اللوج

import os, time, json, math, threading, traceback
import requests
from collections import defaultdict, deque
from flask import Flask

# ===== إعدادات عامة =====
PORT               = int(os.getenv("PORT", "8080"))
SAQAR_URL          = os.getenv("SAQAR_URL", "https://saqar.example.com/hook")  # عدّلها لعنوان صقر
SAQAR_TOKEN        = os.getenv("SAQAR_TOKEN", "changeme")                      # توكن صقر إن وجد، أو اتركه فاضي
BITVAVO_MARKETS_URL= "https://api.bitvavo.com/v2/markets"
BINANCE_REST_EXINF = "https://api.binance.com/api/v3/exchangeInfo"
BIN_WS_URL         = "wss://stream.binance.com:9443/stream?streams="

# ===== تحكم بالإشعارات (لتخفيف السبام) =====
MIN_SPREAD_BP              = 45.0     # أدنى فجوة ⩾ 0.45% (ارفعها/خفضها حسب الرغبة)
CONFIRM_TICKS              = 3        # لازم تتكرر الفجوة نفسها عبر 3 تيكات متتالية
PER_SYMBOL_COOLDOWN_SEC    = 120      # تبريد لكل رمز بعد إرسال إشارة
GLOBAL_MIN_INTERVAL_SEC    = 1.5      # ريت-ليمِت عام بين كل إشارتين

# فلتر عمق دفتر الأوامر (حماية من فجوات كاذبة)
MIN_BID_QTY_USDT           = 300.0    # أقل سيولة على جانب الشراء
MIN_ASK_QTY_USDT           = 300.0    # أقل سيولة على جانب البيع
MIN_NOTIONAL_USDT          = 10.0     # أقل قيمة صفقة منطقية

# طباعة لوج أكثر
VERBOSE = True

app = Flask(__name__)

def log(*a):
    print(*a, flush=True)

# ======= 1) جلب عملات Bitvavo/EUR ومطابقتها مع Binance/USDT =======
def fetch_bitvavo_eur_symbols():
    try:
        data = requests.get(BITVAVO_MARKETS_URL, timeout=10).json()
        syms = set()
        for m in data:
            mk = m.get("market","")
            if mk.endswith("-EUR"):
                syms.add(mk.split("-")[0].upper())
        return syms
    except Exception as e:
        log("Bitvavo fetch error:", e)
        return set()

def fetch_binance_exchange_info():
    data = requests.get(BINANCE_REST_EXINF, timeout=10).json()
    return {s["symbol"]: s for s in data["symbols"]}

def build_targets():
    bv = fetch_bitvavo_eur_symbols()
    ex = fetch_binance_exchange_info()
    targets = []
    for base in sorted(bv):
        sym = base + "USDT"
        if sym in ex and ex[sym]["status"] == "TRADING":
            targets.append(sym)
    return targets

# ======= 2) WebSocket للـ order book (أفضلية depth 5) =======
import websocket  # from "websocket-client"

def stream_name(sym):   # e.g. actusdt@bookTicker
    return f"{sym.lower()}@bookTicker"

# حالة لكل رمز
last_sent_at       = defaultdict(lambda: 0.0)
global_last_signal = 0.0
tick_buffers       = defaultdict(lambda: deque(maxlen=CONFIRM_TICKS))

def usdt_value(price, qty):
    try:
        return float(price) * float(qty)
    except:
        return 0.0

def eligible_gap(msg):
    """
    يعيد (is_ok, details_dict)
    msg يحتوي bid/ask/qty والرمز
    """
    try:
        b = float(msg["b"])  # best bid
        a = float(msg["a"])  # best ask
        Bq= float(msg.get("B", 0))  # bid qty
        Aq= float(msg.get("A", 0))  # ask qty
        sym = msg["s"]
    except Exception:
        return False, {}

    if a <= 0 or b <= 0 or a <= b:
        return False, {}

    spread_pct = (a/b - 1.0) * 100.0
    spread_bp  = spread_pct * 100.0

    # فلتر السيولة
    if usdt_value(b, Bq) < MIN_BID_QTY_USDT:   # جانب المشتري ضعيف
        return False, {}
    if usdt_value(a, Aq) < MIN_ASK_QTY_USDT:   # جانب البائع ضعيف
        return False, {}
    if min(usdt_value(a, 1), usdt_value(b, 1)) < MIN_NOTIONAL_USDT:
        return False, {}

    ok = spread_bp >= MIN_SPREAD_BP
    return ok, {"sym": sym, "bid": b, "ask": a, "spread_bp": spread_bp}

def send_to_saqar(symbol_base):
    global global_last_signal
    now = time.time()

    # ريت-ليمِت عام
    if now - global_last_signal < GLOBAL_MIN_INTERVAL_SEC:
        return False, "global-rate-limit"

    # تبريد خاص بالرمز
    if now - last_sent_at[symbol_base] < PER_SYMBOL_COOLDOWN_SEC:
        return False, "symbol-cooldown"

    payload = {"text": f"اشتري {symbol_base}"}
    headers = {}
    if SAQAR_TOKEN:
        headers["Authorization"] = f"Bearer {SAQAR_TOKEN}"

    try:
        r = requests.post(SAQAR_URL, json=payload, headers=headers, timeout=6)
        ok = (r.status_code // 100 == 2)
        last_sent_at[symbol_base] = now
        global_last_signal = now
        log(f"📩 SAQAR → {symbol_base} | status={r.status_code} | resp={getattr(r,'text','')[:80]}")
        return ok, r.text
    except Exception as e:
        log("SAQAR send error:", e)
        return False, "send-exception"

def on_message(ws, raw):
    try:
        data = json.loads(raw)
        msg  = data.get("data") or data  # يدعم صيغة stream المجمّعة أو المفردة
        if not isinstance(msg, dict) or "s" not in msg:
            return

        ok, det = eligible_gap(msg)
        if not ok:
            # مسح البوفر لأن الفجوة ما عادت موجودة
            tick_buffers[msg["s"]].clear()
            return

        sym  = det["sym"]
        base = sym[:-4] if sym.endswith("USDT") else sym
        tick_buffers[sym].append(det["spread_bp"])

        # تأكيد عبر عدة تيكات متتالية
        if len(tick_buffers[sym]) == tick_buffers[sym].maxlen and all(
            bp >= MIN_SPREAD_BP for bp in tick_buffers[sym]
        ):
            # جاهز نرسل إشارة
            log(f"⚡ GAP DETECTED {sym}: spread={det['spread_bp']:.3f}bp bid={det['bid']:.6g} ask={det['ask']:.6g}")
            ok, why = send_to_saqar(base)
            if not ok and VERBOSE:
                log(f"↪︎ skipped {base} ({why})")
            tick_buffers[sym].clear()  # امنع التكرار فورًا
    except Exception as e:
        log("on_message error:", e)
        if VERBOSE:
            traceback.print_exc()

def on_error(ws, err):
    log("WS error:", err)

def on_close(ws, *_):
    log("WS closed — reconnecting soon…")

def open_ws(symbols):
    streams = "/".join(stream_name(s) for s in symbols)
    url = BIN_WS_URL + streams
    ws = websocket.WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close)
    t = threading.Thread(target=ws.run_forever, kwargs={"ping_interval": 20, "ping_timeout": 10}, daemon=True)
    t.start()
    return ws

# ======= 3) HTTP للـ Railway (صحة) =======
@app.route("/", methods=["GET"])
def health():
    return "Gap Sniper is alive ✅", 200

def main():
    log("🚀 Gap Sniper is starting…")
    targets = build_targets()
    log(f"✅ exchangeInfo loaded; {len(targets)} matched symbols.")
    log("🎯 Targets:", ", ".join(targets[:30]) + (" …" if len(targets) > 30 else ""))

    ws = open_ws(targets)
    log("🟢 WS opened")

    # شغّل Flask للصحة
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()

    # لوب بسيط ليبقي الحاوية حيّة ويُظهر أي خطأ
    while True:
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log("main loop error:", e)

if __name__ == "__main__":
    main()