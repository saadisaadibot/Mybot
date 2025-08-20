# main.py — Gap Sniper (Binance → Saqar) — gunicorn-friendly
import os, time, json, threading, traceback, requests
from collections import defaultdict, deque
from flask import Flask

# ===== إعدادات عامة =====
PORT               = int(os.getenv("PORT", "8080"))
SAQAR_URL          = os.getenv("SAQAR_URL", "https://saqar.example.com/hook")  # عدّل لعنوان «صقر»
SAQAR_TOKEN        = os.getenv("SAQAR_TOKEN", "")                              # اتركه فارغ إذا مو لازم

BITVAVO_MARKETS_URL= "https://api.bitvavo.com/v2/markets"
BINANCE_REST_EXINF = "https://api.binance.com/api/v3/exchangeInfo"
BIN_WS_URL         = "wss://stream.binance.com:9443/stream?streams="

# ===== تحكم بالإشعارات =====
MIN_SPREAD_BP              = 45.0       # أدنى فجوة (basis points) = 0.45%
CONFIRM_TICKS              = 3          # تأكيد عبر 3 تيكات
PER_SYMBOL_COOLDOWN_SEC    = 120        # تبريد لكل رمز
GLOBAL_MIN_INTERVAL_SEC    = 1.5        # ريت ليمت عام
MIN_BID_QTY_USDT           = 300.0      # سيولة أدنى على الـ bid
MIN_ASK_QTY_USDT           = 300.0      # سيولة أدنى على الـ ask
MIN_NOTIONAL_USDT          = 10.0
HEARTBEAT_SEC              = 30         # طباعة “أنا شغّال” كل 30s
VERBOSE                    = True

app = Flask(__name__)
def log(*a): print(*a, flush=True)

# ===== Utils =====
def fetch_bitvavo_eur_symbols():
    try:
        data = requests.get(BITVAVO_MARKETS_URL, timeout=10).json()
        return {m["market"].split("-")[0].upper()
                for m in data if m.get("market","").endswith("-EUR")}
    except Exception as e:
        log("Bitvavo fetch error:", e); return set()

def fetch_binance_exchange_info():
    data = requests.get(BINANCE_REST_EXINF, timeout=10).json()
    return {s["symbol"]: s for s in data["symbols"]}

def build_targets():
    bv = fetch_bitvavo_eur_symbols()
    ex = fetch_binance_exchange_info()
    return [b+"USDT" for b in sorted(bv) if (b+"USDT") in ex and ex[b+"USDT"]["status"]=="TRADING"]

import websocket  # pip install websocket-client
def stream_name(sym): return f"{sym.lower()}@bookTicker"

last_sent_at       = defaultdict(lambda: 0.0)
global_last_signal = 0.0
tick_buffers       = defaultdict(lambda: deque(maxlen=CONFIRM_TICKS))
targets_cache      = []          # للعرض في heartbeat
ws_connected       = threading.Event()

def usdt_value(p, q):
    try: return float(p)*float(q)
    except: return 0.0

def eligible_gap(msg):
    try:
        b = float(msg["b"]); a = float(msg["a"])
        Bq= float(msg.get("B",0)); Aq= float(msg.get("A",0))
        sym = msg["s"]
    except Exception:
        return False, {}
    if a <= b or a<=0 or b<=0: return False, {}
    spread_bp = (a/b - 1.0) * 100.0 * 100.0
    if usdt_value(b,Bq) < MIN_BID_QTY_USDT: return False, {}
    if usdt_value(a,Aq) < MIN_ASK_QTY_USDT: return False, {}
    if min(usdt_value(a,1), usdt_value(b,1)) < MIN_NOTIONAL_USDT: return False, {}
    return spread_bp >= MIN_SPREAD_BP, {"sym": sym, "bid": b, "ask": a, "spread_bp": spread_bp}

def send_to_saqar(base):
    global global_last_signal
    now = time.time()
    if now - global_last_signal < GLOBAL_MIN_INTERVAL_SEC:
        return False, "global-rate-limit"
    if now - last_sent_at[base] < PER_SYMBOL_COOLDOWN_SEC:
        return False, "symbol-cooldown"
    headers = {}
    if SAQAR_TOKEN: headers["Authorization"]=f"Bearer {SAQAR_TOKEN}"
    try:
        r = requests.post(SAQAR_URL, json={"text": f"اشتري {base}"}, headers=headers, timeout=6)
        ok = (r.status_code//100==2)
        last_sent_at[base] = now; global_last_signal = now
        log(f"📩 SAQAR → {base} | status={r.status_code} | resp={getattr(r,'text','')[:80]}")
        return ok, r.text
    except Exception as e:
        log("SAQAR send error:", e); return False, "send-exception"

def on_message(ws, raw):
    try:
        data = json.loads(raw)
        msg  = data.get("data") or data
        if not isinstance(msg, dict) or "s" not in msg: return
        ok, det = eligible_gap(msg)
        sym = msg["s"]
        if not ok:
            tick_buffers[sym].clear()
            return
        tick_buffers[sym].append(det["spread_bp"])
        if len(tick_buffers[sym]) == tick_buffers[sym].maxlen and all(bp>=MIN_SPREAD_BP for bp in tick_buffers[sym]):
            base = sym[:-4] if sym.endswith("USDT") else sym
            log(f"⚡ GAP DETECTED {sym}: spread={det['spread_bp']:.3f}bp bid={det['bid']:.6g} ask={det['ask']:.6g}")
            ok, why = send_to_saqar(base)
            if not ok and VERBOSE: log(f"↪︎ skipped {base} ({why})")
            tick_buffers[sym].clear()
    except Exception as e:
        log("on_message error:", e); 
        if VERBOSE: traceback.print_exc()

def on_error(ws, err):  log("WS error:", err)
def on_close(ws, *_):   log("WS closed — will reconnect")

def open_ws(symbols):
    streams = "/".join(stream_name(s) for s in symbols)
    url = BIN_WS_URL + streams
    ws = websocket.WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close)
    threading.Thread(target=ws.run_forever, kwargs={"ping_interval":20, "ping_timeout":10}, daemon=True).start()
    return ws

# ===== عامل الـ Sniper كخيط مستقل (ليعمل تحت gunicorn) =====
_started = False
def start_sniper_once():
    global _started
    if _started: 
        return
    _started = True
    threading.Thread(target=_sniper_worker, daemon=True).start()

def _sniper_worker():
    try:
        log("🚀 Gap Sniper worker booting…")
        global targets_cache
        targets_cache = build_targets()
        log(f"✅ matched symbols: {len(targets_cache)}")
        log("🎯 " + ", ".join(targets_cache[:30]) + (" …" if len(targets_cache)>30 else ""))
        open_ws(targets_cache)
        ws_connected.set()
        log("🟢 WS started")
        last_hb = 0
        while True:
            time.sleep(1)
            if time.time()-last_hb >= HEARTBEAT_SEC:
                last_hb = time.time()
                log(f"💓 heartbeat | targets={len(targets_cache)} | sent={sum(1 for v in last_sent_at.values() if time.time()-v<3600)} in last hour")
    except Exception as e:
        log("SNIPER worker fatal error:", e)
        traceback.print_exc()
        # حاول إعادة التشغيل بعد قليل
        time.sleep(5)
        _sniper_worker()

# ابدأ العامل فور الاستيراد (تحت gunicorn)
start_sniper_once()

# ضمان إضافي: إذا تم تعطيل الاستيراد المباشر، شغّله عند أول طلب HTTP
@app.before_first_request
def _boot_guard():
    log("🔥 boot-guard triggered from Flask")
    start_sniper_once()

# ===== HTTP للصحة =====
@app.route("/", methods=["GET"])
def health():
    return "Gap Sniper is alive ✅", 200

# لو شغّلت الملف محليًا (بدون gunicorn)
if __name__ == "__main__":
    start_sniper_once()
    app.run(host="0.0.0.0", port=PORT)