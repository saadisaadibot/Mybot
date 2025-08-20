# -*- coding: utf-8 -*-
import json, time, math, threading, traceback
import requests
from urllib.parse import urlencode
from websocket import WebSocketApp
from flask import Flask

# =========================
# إعدادات ثابتة (لا .env)
# =========================
SAQAR_WEBHOOK   = "https://saadisaadibot-saqarxbo-production.up.railway.app/"  # <-- عدّلها فقط
TOP_N           = 10            # كم عملة EUR من Bitvavo نفحص
GAP_SPREAD_BP   = 20.0          # حد الفجوة بالـ basis points (20 = 0.20%)
COOLDOWN_SEC    = 60            # كولداون لكل عملة قبل إرسال إشارة جديدة
SCAN_INTERVAL   = 180           # كل كم ثانية نعيد جلب قائمة Bitvavo EUR
USE_IMBALANCE   = False         # فلتر إضافي (اختياري) لعدم التفعيل حالياً
IMB_RATIO_MIN   = 1.8           # إذا مفعل: askVol/bidVol أو العكس

# =========================
# متغيرات داخلية
# =========================
app = Flask(__name__)
_binance_symbols_ok = set()          # من exchangeInfo
_targets_lock = threading.Lock()
_targets      = set()                # أمثلة: {"ADAUSDT","OGNUSDT", ...}
_last_fire    = {}                   # تبريد per symbol
_ws           = None

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

# =========================
# Bitvavo (قائمة EUR)
# =========================
def fetch_bitvavo_eur_top():
    """يرجع مجموعة رموز BASE الموجودة على Bitvavo مقابل EUR (Top N بالأبجدية)."""
    try:
        resp = requests.get("https://api.bitvavo.com/v2/markets", timeout=12)
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

        if bid <= 0 or ask <= 0 or ask < bid:
            return

        mp = midprice(bid, ask)
        if not mp:
            return

        spread = (ask - bid) / mp * 100.0     # %
        if spread * 100.0 < GAP_SPREAD_BP:    # نحول من % إلى basis points للمقارنة
            return

        # (اختياري) فلتر imbalance
        if USE_IMBALANCE:
            try:
                bqty = float(d.get("B", "0"))  # bestBidQty
                aqty = float(d.get("A", "0"))  # bestAskQty
                ratio = max((aqty+1e-12)/(bqty+1e-12), (bqty+1e-12)/(aqty+1e-12))
                if ratio < IMB_RATIO_MIN:
                    return
            except Exception:
                pass

        # تبريد
        now = time.time()
        last = _last_fire.get(s, 0)
        if now - last < COOLDOWN_SEC:
            return
        _last_fire[s] = now

        base = s.replace("USDT", "")
        log(f"⚡ GAP DETECTED {s}: spread={spread:.3f}%  bid={bid} ask={ask}")
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
        "cooldown": COOLDOWN_SEC
    }

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
    # للركض محلياً: python main.py
    app.run(host="0.0.0.0", port=8080)