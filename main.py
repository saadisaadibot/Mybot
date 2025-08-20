# -*- coding: utf-8 -*-
import json, time, threading, traceback
import requests
from collections import deque, defaultdict
from websocket import WebSocketApp
from flask import Flask

# =========================
# إعدادات ثابتة (لا .env)
# =========================
SAQAR_WEBHOOK   = "https://saadisaadibot-saqarxbo-production.up.railway.app/"
TOP_N           = 10             # كم عملة EUR من Bitvavo نفحص
GAP_SPREAD_BP   = 30.0           # حد الفجوة الأساسي (bp) 30 = 0.30%
STRONG_GAP_BP   = 45.0           # عتبة "فرصة قوية" (bp) 45 = 0.45%
COOLDOWN_SEC    = 3600             # كولداون لكل عملة
SCAN_INTERVAL   = 180            # كل كم ثانية نعيد جلب قائمة Bitvavo EUR

# فلاتر إضافية
SUSTAIN_SEC         = 1.20       # لازم السبريد يبقى ≥ الحد لمدة X ثوانٍ
MIN_TOP_QTY_USDT    = 1200.0     # حد أدنى للسيولة عند أفضل Bid/Ask
USE_IMBALANCE       = True
IMB_RATIO_MIN       = 1.6

# إزالة تكرار + سقف الرسايل
DEDUP_WINDOW_SEC    = 6.0
MAX_ALERTS_PER_MIN  = 12
DEBUG_REJECTIONS    = False      # إذا بدك تشوف سبب الرفض = True

# =========================
# متغيرات داخلية
# =========================
app = Flask(__name__)
_binance_symbols_ok = set()
_targets_lock = threading.Lock()
_targets      = set()
_last_fire    = defaultdict(float)
_ws           = None

_seen_spread  = {}
_dedup_seen   = defaultdict(float)
_alert_bucket = deque()
_fire_lock    = threading.Lock()

# =========================
# أدوات مساعدة
# =========================
def log(*args):
    print(*args, flush=True)

def post_to_saqr(symbol_base):
    """إرسال أمر شراء لصقر مباشرة."""
    try:
        payload = {"text": f"اشتري {symbol_base}"}
        r = requests.post(SAQAR_WEBHOOK, json=payload, timeout=10)
        log(f"📨 SAQAR → {symbol_base} | status={r.status_code}")
    except Exception as e:
        log("❌ SAQAR error:", repr(e))
        traceback.print_exc()

def midprice(bid, ask):
    return (bid + ask) / 2.0 if (bid and ask) else None

def _push_spread(symbol, spread_pct):
    q = _seen_spread.get(symbol)
    if q is None:
        q = deque(maxlen=64)
        _seen_spread[symbol] = q
    q.append((time.time(), spread_pct))

def _sustained(symbol, threshold_pct, window_sec):
    q = _seen_spread.get(symbol)
    if not q: return False
    now = time.time()
    for ts, sp in q:
        if ts >= now - window_sec and sp < threshold_pct:
            return False
    return True

def _rate_ok():
    now = time.time()
    while _alert_bucket and _alert_bucket[0] < now - 60:
        _alert_bucket.popleft()
    return len(_alert_bucket) < MAX_ALERTS_PER_MIN

def _mark_alert():
    _alert_bucket.append(time.time())

def _dedup(key):
    now = time.time()
    last = _dedup_seen.get(key, 0.0)
    if now - last < DEDUP_WINDOW_SEC:
        return False
    _dedup_seen[key] = now
    return True

def _reject(reason, symbol=None):
    if DEBUG_REJECTIONS:
        log(f"⏭️  skip {symbol or ''} | {reason}")

# =========================
# Bitvavo (قائمة EUR)
# =========================
def fetch_bitvavo_eur_top():
    try:
        resp = requests.get("https://api.bitvavo.com/v2/markets", timeout=12)
        data = resp.json()
        bases = [m["market"].split("-")[0] for m in data if m.get("market","").endswith("-EUR")]
        bases = sorted(set(bases))[:TOP_N]
        return set(bases)
    except:
        return set()

# =========================
# Binance
# =========================
def fetch_binance_exchange_info():
    global _binance_symbols_ok
    try:
        url = "https://api.binance.com/api/v3/exchangeInfo"
        data = requests.get(url, timeout=15).json()
        ok = {s["symbol"] for s in data.get("symbols", []) if s.get("status")=="TRADING"}
        _binance_symbols_ok = ok
        log(f"✅ exchangeInfo loaded: {len(ok)} symbols")
    except:
        pass

def refresh_targets_loop():
    while True:
        try:
            bases = fetch_bitvavo_eur_top()
            with _targets_lock:
                new_targets = {f"{b}USDT" for b in bases if f"{b}USDT" in _binance_symbols_ok}
                if new_targets:
                    _targets.clear()
                    _targets.update(new_targets)
        except:
            pass
        time.sleep(SCAN_INTERVAL)

# =========================
# WebSocket
# =========================
def build_stream_url(symbols):
    parts = [f"{s.lower()}@bookTicker" for s in symbols]
    return "wss://stream.binance.com:9443/stream?streams=" + "/".join(parts)

def on_message(ws, message):
    try:
        data = json.loads(message)
        d = data.get("data", {})
        s = d.get("s")
        if not s: return
        with _targets_lock:
            if s not in _targets: return

        bid, ask = float(d.get("b","0")), float(d.get("a","0"))
        if bid <= 0 or ask <= 0 or ask <= bid: return

        mp = midprice(bid, ask)
        if not mp: return

        spread_pct = (ask - bid) / mp * 100.0
        _push_spread(s, spread_pct)

        min_spread_pct    = GAP_SPREAD_BP   / 100.0
        strong_spread_pct = STRONG_GAP_BP   / 100.0

        if spread_pct < min_spread_pct: return
        if spread_pct < strong_spread_pct or not _sustained(s,strong_spread_pct,SUSTAIN_SEC): return

        bqty, aqty = float(d.get("B","0")), float(d.get("A","0"))
        if bqty>0 and aqty>0:
            bid_usdt, ask_usdt = bqty*mp, aqty*mp
            if bid_usdt<MIN_TOP_QTY_USDT and ask_usdt<MIN_TOP_QTY_USDT: return
            if USE_IMBALANCE and max(bid_usdt,ask_usdt)/max(1e-9,min(bid_usdt,ask_usdt))<IMB_RATIO_MIN: return

        now = time.time()
        with _fire_lock:
            if now - _last_fire[s] < COOLDOWN_SEC: return
            if not _rate_ok(): return
            key = f"{s}:{int(spread_pct*500)}"
            if not _dedup(key): return
            _mark_alert()
            _last_fire[s] = now

        base = s.replace("USDT","")
        log(f"⚡ GAP DETECTED {s}: spread={spread_pct:.3f}%")
        post_to_saqr(base)

    except Exception:
        pass

def ws_loop():
    global _ws
    current_set = set()
    while True:
        try:
            with _targets_lock: t = sorted(_targets)
            if not t: time.sleep(3); continue
            if t != sorted(current_set):
                current_set = set(t)
                if _ws:
                    try: _ws.close()
                    except: pass
                url = build_stream_url(t)
                _ws = WebSocketApp(url,on_message=on_message)
                threading.Thread(target=_ws.run_forever,kwargs={"ping_interval":20,"ping_timeout":10},daemon=True).start()
            time.sleep(5)
        except:
            time.sleep(5)

# =========================
# Flask (صحة فقط)
# =========================
@app.get("/")
def health():
    with _targets_lock:
        ts = ",".join(sorted(_targets)) or "-"
    return {"ok": True,"targets": ts}

# =========================
# الإقلاع
# =========================
def boot():
    log("🚀 Gap Sniper is alive ✅")
    fetch_binance_exchange_info()
    threading.Thread(target=refresh_targets_loop, daemon=True).start()
    threading.Thread(target=ws_loop, daemon=True).start()

boot()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)