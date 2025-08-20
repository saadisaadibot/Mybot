# -*- coding: utf-8 -*-
import json, time, math, threading, traceback
import requests
from collections import deque, defaultdict
from websocket import WebSocketApp
from flask import Flask

# =========================
# إعدادات ثابتة (لا .env)
# =========================
SAQAR_WEBHOOK   = "https://saadisaadibot-saqarxbo-production.up.railway.app/"  # <-- جاهز
TOP_N           = 10             # كم عملة EUR من Bitvavo نفحص
GAP_SPREAD_BP   = 30.0           # حد الفجوة بالـ basis points (30 = 0.30%)
COOLDOWN_SEC    = 45             # كولداون لكل عملة قبل إرسال إشارة جديدة
SCAN_INTERVAL   = 180            # كل كم ثانية نعيد جلب قائمة Bitvavo EUR

# فلاتر إضافية (تقدر تعدّلها براحتك)
SUSTAIN_SEC         = 1.20       # لازم السبريد يبقى ≥ الحد لمدة X ثوانٍ
MIN_TOP_QTY_USDT    = 1200.0     # حد أدنى للسيولة عند أفضل Bid/Ask (بالدولار التقريبي)
USE_IMBALANCE       = True       # فلتر انحياز دفتر أوامر؟
IMB_RATIO_MIN       = 1.6        # (max(bid_usdt, ask_usdt) / min(...)) ≥ 1.6

# إزالة تكرار + سقف الرسايل
DEDUP_WINDOW_SEC    = 4.0        # نفس التنبيه لنفس الزوج خلال 4s
MAX_ALERTS_PER_MIN  = 12         # سقف التنبيهات بالدقيقة
DEBUG_REJECTIONS    = True       # اطبع سبب الرفض في اللوج

# =========================
# متغيرات داخلية
# =========================
app = Flask(__name__)
_binance_symbols_ok = set()          # من exchangeInfo
_targets_lock = threading.Lock()
_targets      = set()                # أمثلة: {"ADAUSDT","OGNUSDT", ...}
_last_fire    = defaultdict(float)   # تبريد per symbol
_ws           = None

# للحفاظ على الاستمرارية/الفلترة
_seen_spread  = {}                   # {symbol: deque[(ts, spread_pct), ...]}
_dedup_seen   = defaultdict(float)   # {key: ts}
_alert_bucket = deque()              # timestamps لأخر التنبيهات (rate limit)

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
        log(f"📨 SAQAR → {symbol_base} | status={r.status_code} | resp={r.text[:200]}")
    except Exception as e:
        log("❌ SAQAR error:", repr(e))
        traceback.print_exc()

def midprice(bid, ask):
    return (bid + ask) / 2.0 if (bid is not None and ask is not None) else None

def _push_spread(symbol, spread_pct):
    q = _seen_spread.get(symbol)
    if q is None:
        q = deque(maxlen=64)
        _seen_spread[symbol] = q
    q.append((time.time(), spread_pct))

def _sustained(symbol, threshold_pct, window_sec):
    q = _seen_spread.get(symbol)
    if not q:
        return False
    now = time.time()
    had = False
    for ts, sp in q:
        if ts >= now - window_sec:
            had = True
            if sp < threshold_pct:
                return False
    return had

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
        if symbol:
            log(f"⏭️  skip {symbol} | {reason}")
        else:
            log(f"⏭️  skip | {reason}")

# =========================
# Bitvavo (قائمة EUR)
# =========================
import requests as _req
def fetch_bitvavo_eur_top():
    """يرجع مجموعة رموز BASE الموجودة على Bitvavo مقابل EUR (Top N بالأبجدية)."""
    try:
        resp = _req.get("https://api.bitvavo.com/v2/markets", timeout=12)
        data = resp.json()
        bases = []
        for m in data:
            market = m.get("market", "")
            if market.endswith("-EUR"):
                base = market.split("-")[0].upper()
                bases.append(base)
        # ترتيب ثابت + top N
        bases = sorted(set(bases))[:TOP_N]
        log("📊 Top Bitvavo (EUR):", ", ".join(bases))
        return set(bases)
    except Exception as e:
        log("❌ Bitvavo markets error:", repr(e))
        traceback.print_exc()
        return set()

# =========================
# Binance
# =========================
def fetch_binance_exchange_info():
    """نجيب exchangeInfo مرة ونبني مجموعة بالرموز المتاحة."""
    global _binance_symbols_ok
    try:
        url = "https://api.binance.com/api/v3/exchangeInfo"
        data = requests.get(url, timeout=15).json()
        ok = set()
        for s in data.get("symbols", []):
            if s.get("status") == "TRADING":
                ok.add(s.get("symbol", ""))
        _binance_symbols_ok = ok
        log(f"✅ exchangeInfo loaded: {len(ok)} symbols")
    except Exception as e:
        log("❌ exchangeInfo error:", repr(e))
        traceback.print_exc()

def refresh_targets_loop():
    """كل SCAN_INTERVAL ثواني: نحدث لائحة العملات المستهدفة من Bitvavo EUR ∩ Binance USDT."""
    while True:
        try:
            bases = fetch_bitvavo_eur_top()
            with _targets_lock:
                new_targets = set()
                for base in bases:
                    cand = f"{base}USDT"
                    if cand in _binance_symbols_ok:
                        new_targets.add(cand)
                # إن لم يوجد تطابق، لا نُفرغ القائمة القديمة
                if new_targets:
                    _targets.clear()
                    _targets.update(new_targets)
                    log("🎯 Targets (Binance):", ", ".join(sorted(_targets)))
                else:
                    log("⚠️ لا توجد تقاطعات حالياً بين Bitvavo EUR و Binance USDT.")
        except Exception as e:
            log("❌ refresh_targets_loop error:", repr(e))
            traceback.print_exc()
        time.sleep(SCAN_INTERVAL)

# =========================
# WebSocket: @bookTicker لكل Target
# =========================
def build_stream_url(symbols):
    # combined stream: /stream?streams=adausdt@bookTicker/btcusdt@bookTicker/...
    parts = [f"{s.lower()}@bookTicker" for s in symbols]
    return "wss://stream.binance.com:9443/stream?streams=" + "/".join(parts)

def on_message(ws, message):
    try:
        data = json.loads(message)
        # شكل combined: {"stream":"adausdt@bookTicker","data":{...}}
        d = data.get("data", {})
        s = d.get("s")  # SYMBOL
        if not s:
            return
        # فلترة بالtargets
        with _targets_lock:
            if s not in _targets:
                return

        try:
            bid = float(d.get("b", "0"))
            ask = float(d.get("a", "0"))
        except Exception:
            return

        if bid <= 0 or ask <= 0 or ask <= bid:
            _reject("bad-topbook", s)
            return

        mp = midprice(bid, ask)
        if not mp:
            return

        spread_pct = (ask - bid) / mp * 100.0     # %
        # مقارنة مع الإعداد (basis points)
        min_spread_pct = GAP_SPREAD_BP / 100.0
        _push_spread(s, spread_pct)

        if spread_pct < min_spread_pct:
            _reject(f"spread<{min_spread_pct:.2f}%", s)
            return

        # استمرار
        if not _sustained(s, min_spread_pct, SUSTAIN_SEC):
            _reject("not-sustained", s)
            return

        # سيولة/انحياز (اختياريان لكن مفعلان أعلاه)
        bqty = float(d.get("B", "0"))  # bestBidQty
        aqty = float(d.get("A", "0"))  # bestAskQty
        if bqty > 0 and aqty > 0:
            bid_usdt = bqty * mp
            ask_usdt = aqty * mp
            if bid_usdt < MIN_TOP_QTY_USDT and ask_usdt < MIN_TOP_QTY_USDT:
                _reject("thin-top", s)
                return
            if USE_IMBALANCE:
                big = max(bid_usdt, ask_usdt)
                small = max(1e-9, min(bid_usdt, ask_usdt))
                if big / small < IMB_RATIO_MIN:
                    _reject("no-imbalance", s)
                    return

        # تبريد الزوج
        now = time.time()
        if now - _last_fire[s] < COOLDOWN_SEC:
            _reject("pair-cooldown", s)
            return

        # rate limit عام
        if not _rate_ok():
            _reject("rate-limited")
            return

        # منع التكرار لنفس السبب/القيمة تقريبياً
        key = f"{s}:{int(spread_pct*1000)}"
        if not _dedup(key):
            _reject("dup", s)
            return

        _mark_alert()
        _last_fire[s] = now

        base = s.replace("USDT", "")
        log(f"⚡ GAP DETECTED {s}: spread={spread_pct:.3f}% | bid={bid} ask={ask} | qty(B/A)={bqty:.4f}/{aqty:.4f}")
        post_to_saqr(base)

    except Exception as e:
        log("❌ on_message error:", repr(e))
        traceback.print_exc()

def on_error(ws, error):
    log("❌ WS error:", error)
    traceback.print_exc()

def on_close(ws, a, b):
    log("⚠️ WS closed. code/desc:", a, b)

def on_open(ws):
    log("🟢 WS opened")

def ws_loop():
    """يشغّل WS للـ targets الحالية، ويُعيد التشغيل تلقائياً إذا تغيرت."""
    global _ws
    current_set = set()
    while True:
        try:
            with _targets_lock:
                t = sorted(_targets)
            if not t:
                time.sleep(3)
                continue

            # لو تغيرت القائمة نعيد فتح WS
            if t != sorted(current_set):
                current_set = set(t)
                if _ws:
                    try: _ws.close()
                    except: pass

                url = build_stream_url(t)
                log("👁 Starting WS for:", ", ".join(t))
                _ws = WebSocketApp(
                    url,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close
                )
                # نشغله blocking داخل ثريد منفصل
                th = threading.Thread(target=_ws.run_forever, kwargs={"ping_interval": 20, "ping_timeout": 10}, daemon=True)
                th.start()

            time.sleep(5)
        except Exception as e:
            log("❌ ws_loop error:", repr(e))
            traceback.print_exc()
            time.sleep(5)

# =========================
# Flask (صحة فقط)
# =========================
@app.get("/")
def health():
    with _targets_lock:
        ts = ",".join(sorted(_targets)) or "-"
    return {
        "ok": True,
        "targets": ts,
        "gap_bp": GAP_SPREAD_BP,
        "cooldown": COOLDOWN_SEC,
        "sustain_sec": SUSTAIN_SEC,
        "min_top_usdt": MIN_TOP_QTY_USDT,
        "imbalance": USE_IMBALANCE,
    }

# =========================
# الإقلاع
# =========================
def fetch_binance_symbols_once():
    fetch_binance_exchange_info()

def boot():
    log("🚀 Gap Sniper is alive ✅")
    fetch_binance_symbols_once()
    threading.Thread(target=refresh_targets_loop, daemon=True).start()
    threading.Thread(target=ws_loop, daemon=True).start()

boot()

if __name__ == "__main__":
    # للركض محلياً: python main.py
    app.run(host="0.0.0.0", port=8080)