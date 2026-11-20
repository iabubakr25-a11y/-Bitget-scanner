#!/usr/bin/env python3
"""
Bitget Stealth Volume Scanner
-----------------------------
بيدور على عملات حجم تداولها قفز فجأة بالنسبة لمتوسطها الطبيعي
لكن السعر لسه ما اتحركش (تجميع محتمل).

التشغيل:
    pip install requests
    python bitget_scanner.py                 # مسح مرة واحدة
    python bitget_scanner.py --loop 300      # مسح كل 5 دقايق
    python bitget_scanner.py --ratio 5 --max-move 1.5 --min-usd 1500

تنبيهات تيليجرام (اختياري):
    export TG_TOKEN="..."  TG_CHAT="..."
"""
import argparse
import json
import os
import smtplib
import ssl
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from email.message import EmailMessage

import requests

BASE = "https://api.bitget.com"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "stealth-scanner/1.0"})


def api_get(path, params=None, retries=3):
    for i in range(retries):
        try:
            r = SESSION.get(BASE + path, params=params, timeout=10)
            if r.status_code == 429:
                time.sleep(1 + i)
                continue
            r.raise_for_status()
            j = r.json()
            if j.get("code") != "00000":
                raise RuntimeError(j.get("msg"))
            return j["data"]
        except Exception:
            if i == retries - 1:
                return None
            time.sleep(0.5 * (i + 1))
    return None


def get_candidates(a, data=None):
    """فلترة أولية من التيكرز: عملات USDT صغيرة الحجم وسعرها هادي في 24 ساعة."""
    if data is None:
        data = api_get("/api/v2/spot/market/tickers") or []
    out = []
    for t in data:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        try:
            vol24 = float(t.get("usdtVolume") or t.get("quoteVolume") or 0)
            chg24 = float(t.get("change24h") or 0) * 100
        except ValueError:
            continue
        if a.min_vol24 <= vol24 <= a.max_vol24 and abs(chg24) <= a.max_24h:
            out.append((sym, vol24, chg24))
    return out


def analyze(item, a):
    sym, vol24, chg24 = item
    raw = api_get(
        "/api/v2/spot/market/candles",
        {"symbol": sym, "granularity": a.tf, "limit": "200"},
    )
    if not raw or len(raw) < a.window + 40:
        return None

    # [ts, open, high, low, close, baseVol, usdtVol, quoteVol]
    rows = sorted(raw, key=lambda c: int(c[0]))
    k = a.window
    recent, base = rows[-k:], rows[:-k]

    base_vols = [float(r[6]) for r in base]
    baseline = max(
        statistics.median(base_vols),
        statistics.mean(base_vols) * 0.5,
        a.floor,
    )
    recent_total = sum(float(r[6]) for r in recent)
    ratio = (recent_total / k) / baseline

    o = float(recent[0][1])
    c = float(recent[-1][4])
    hi = max(float(r[2]) for r in recent)
    lo = min(float(r[3]) for r in recent)
    if o <= 0 or lo <= 0:
        return None
    move = (c / o - 1) * 100
    rng = (hi / lo - 1) * 100

    # عدد الشموع النشطة (حجم أكبر من ضعف الطبيعي) — تجميع مستمر مش شمعة واحدة
    active = sum(1 for r in recent if float(r[6]) > 2 * baseline)

    if (
        ratio >= a.ratio
        and recent_total >= a.min_usd
        and abs(move) <= a.max_move
        and rng <= a.max_range
        and active >= a.min_active
    ):
        return {
            "symbol": sym,
            "ratio": ratio,
            "recent_usd": recent_total,
            "baseline_per_candle": baseline,
            "avg_recent": recent_total / k,
            "price": c,
            "move": move,
            "range": rng,
            "active": active,
            "vol24": vol24,
            "chg24": chg24,
            "hidden": abs(move) <= a.h_move and rng <= a.h_range and abs(chg24) <= a.h_24,
        }
    return None


def enrich(r, a):
    """مرحلة التأكيد: اتجاه الشراء العدواني + عمق الأوردر بوك + فحص البوتات."""
    sym = r["symbol"]
    fills = api_get("/api/v2/spot/market/fills", {"symbol": sym, "limit": "100"}) or []
    buy = sell = 0.0
    sizes = []
    for f in fills:
        try:
            usd = float(f["price"]) * float(f["size"])
        except (KeyError, ValueError):
            continue
        sizes.append(usd)
        if str(f.get("side", "")).lower() == "buy":
            buy += usd
        else:
            sell += usd
    tot = buy + sell
    r["buy_pct"] = buy / tot * 100 if tot else 0.0
    r["buy_usd"], r["sell_usd"] = buy, sell
    r["trades"] = len(sizes)
    r["bid_usd"] = r["ask_usd"] = 0.0
    r["botlike"] = False
    if len(sizes) >= 30:
        m = statistics.mean(sizes)
        r["botlike"] = m > 0 and statistics.pstdev(sizes) / m < 0.15

    ob = api_get("/api/v2/spot/market/orderbook", {"symbol": sym, "type": "step0", "limit": "50"}) or {}
    try:
        bids = [(float(x[0]), float(x[1])) for x in ob.get("bids", [])]
        asks = [(float(x[0]), float(x[1])) for x in ob.get("asks", [])]
    except (ValueError, IndexError):
        bids, asks = [], []
    r["book_pct"], r["spread"] = 0.0, 99.0
    if bids and asks:
        mid = (bids[0][0] + asks[0][0]) / 2
        lo, hi = mid * (1 - a.depth / 100), mid * (1 + a.depth / 100)
        b = sum(p * q for p, q in bids if p >= lo)
        k = sum(p * q for p, q in asks if p <= hi)
        r["book_pct"] = b / (b + k) * 100 if (b + k) else 0.0
        r["bid_usd"], r["ask_usd"] = b, k
        r["spread"] = (asks[0][0] / bids[0][0] - 1) * 100

    r["strong"] = (
        r["buy_pct"] >= a.min_buy
        and r["book_pct"] >= a.min_book
        and r["spread"] <= a.max_spread
        and not r["botlike"]
    )
    return r


def scan(a, tickers=None):
    cands = get_candidates(a, tickers)
    print(f"[{time.strftime('%H:%M:%S')}] عملات بعد الفلترة الأولية: {len(cands)}")
    results = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for res in ex.map(lambda it: analyze(it, a), cands):
            if res:
                results.append(res)
    if results:
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            results = list(ex.map(lambda r: enrich(r, a), results))
    if not a.weak:
        results = [r for r in results if r["strong"]]
    results.sort(key=lambda r: (r["strong"], r["ratio"]), reverse=True)
    return results


def print_table(results, a):
    if not results:
        print("مفيش حاجة مطابقة للشروط دلوقتي.\n")
        return
    print(
        f"{'SYMBOL':<14}{'VOLx':>7}{'RECENT$':>10}{'MOVE%':>8}"
        f"{'BUY%':>7}{'BOOK%':>7}{'SPRD%':>7}  FLAG"
    )
    for r in results[: a.top]:
        flag = "STRONG" if r["strong"] else ("BOT?" if r["botlike"] else "weak")
        print(
            f"{r['symbol']:<14}{r['ratio']:>7.1f}{r['recent_usd']:>10,.0f}"
            f"{r['move']:>8.2f}{r['buy_pct']:>7.0f}{r['book_pct']:>7.0f}"
            f"{r['spread']:>7.2f}  {flag}"
        )
    print()


def fmt_price(p):
    if p >= 1:
        s = f"{p:.4f}"
    elif p >= 0.01:
        s = f"{p:.5f}"
    else:
        s = f"{p:.8f}"
    return s.rstrip("0").rstrip(".")


def format_alert(r, a):
    """شكل التنبيه: الحكم + الأرقام + الأسباب + لينك Bitget."""
    if r["strong"]:
        tag = "✅ محتمل حقيقي"
    elif r["botlike"]:
        tag = "🤖 شبهة بوت"
    else:
        tag = "⚠️ ضعيف"
    reasons = f"إيجابي: الشراء غالب {r['buy_pct']:.0f}%"
    if not r["botlike"]:
        reasons += f"، أحجام الصفقات متنوعة ({r['trades']} صفقة)"
    reasons += f"، طلبات شراء تحت السعر (${r['bid_usd']:,.0f})"
    reasons += f"، الفوليوم عالي في {r['active']} من آخر {a.window} شموع"
    kind = "🕶 مخفي" if r["hidden"] else f"📢 ظاهر (السعر اتحرك {r['move']:+.1f}%)"
    if r.get("repeat"):
        kind += " 🔁 فوليوم مكمل"
    return "\n".join(
        [
            f"{tag}  {r['symbol']}  |  {kind}",
            f"الفوليوم: ${r['baseline_per_candle']:,.0f} ← ${r['avg_recent']:,.0f} (x{r['ratio']:.1f})",
            f"حركة السعر: {r['move']:+.1f}%  |  السعر: {fmt_price(r['price'])}  |  فوليوم 24س: ${r['vol24']:,.0f}",
            f"آخر {r['trades']} صفقة: شراء ${r['buy_usd']:,.0f} مقابل بيع ${r['sell_usd']:,.0f}",
            reasons,
            f"https://www.bitget.com/spot/{r['symbol']}",
        ]
    )


def candle_pump(sym, a, now):
    """شمعة 5 دقايق أقفلت خضرا فوق أعلى الشمعة اللي قبلها،
    والسعر زاد pump_pct% عن أقل جسم شمعة في آخر pump_window دقيقة."""
    n = a.pump_window // 5 + 1
    raw = api_get("/api/v2/spot/market/candles", {"symbol": sym, "granularity": "5min", "limit": str(n + 3)})
    if not raw or len(raw) < 4:
        return None
    rows = sorted(raw, key=lambda c: int(c[0]))
    if int(rows[-1][0]) / 1000 + 300 > now:  # آخر شمعة لسه بتتكون، نشيلها
        rows = rows[:-1]
    if len(rows) < 3:
        return None
    last, prev = rows[-1], rows[-2]
    o, c, prev_high = float(last[1]), float(last[4]), float(prev[2])
    win = rows[-n:]
    low, low_ts = min((min(float(r[1]), float(r[4])), int(r[0])) for r in win)
    if low <= 0:
        return None
    rise = (c / low - 1) * 100
    if c > o and c > prev_high and rise >= a.pump_pct:
        return {"price": c, "rise": rise, "mins": (int(last[0]) - low_ts) / 60000 + 5}
    return None


def track_pumps(tickers, a, state, now):
    """تتبع الصعود: عملة صعدت pump_pct% وشمعة الـ5 دقايق أقفلت فوق الشمعة اللي قبلها.
    وتتكرر نفس العملة كل ما تكمل صعود pump_step% فوق سعر آخر تنبيه."""
    if not tickers:
        return []
    hist = state.setdefault("hist", {})
    last = state.setdefault("pump", {})
    window = a.pump_window * 60
    cands, live = [], set()
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        try:
            price = float(t.get("lastPr") or t.get("close") or 0)
            vol24 = float(t.get("usdtVolume") or t.get("quoteVolume") or 0)
            chg24 = float(t.get("change24h") or 0) * 100
        except ValueError:
            continue
        if price <= 0:
            continue
        live.add(sym)
        h = [p for p in hist.get(sym, []) if now - p[0] <= window]
        h.append([now, price])
        hist[sym] = h
        if vol24 < a.pump_min_vol24:
            continue
        low = min(p[1] for p in h)
        hist_rise = (price / low - 1) * 100
        # فلتر أولي رخيص عشان منطلبش شموع لكل العملات
        if hist_rise >= a.pump_pct * 0.5 or sym in last:
            cands.append((sym, vol24, chg24, price))
    for s in list(hist):
        if s not in live:
            del hist[s]
    out = []
    if cands:
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            res = list(ex.map(lambda it: candle_pump(it[0], a, now), cands))
        for (sym, vol24, chg24, price), r in zip(cands, res):
            ref = last.get(sym)
            if ref and (now - ref["ts"] > 6 * 3600 or price <= ref["price"] * (1 - a.pump_pct / 100)):
                last.pop(sym, None)
                ref = None
            if r and (ref is None or r["price"] >= ref["price"] * (1 + a.pump_step / 100)):
                out.append(
                    {
                        "symbol": sym,
                        "price": r["price"],
                        "rise": r["rise"],
                        "mins": r["mins"],
                        "vol24": vol24,
                        "chg24": chg24,
                        "repeat": ref is not None,
                        "from_alert": ref["price"] if ref else None,
                    }
                )
                last[sym] = {"price": r["price"], "ts": now}
    out.sort(key=lambda r: r["rise"], reverse=True)
    return out


def format_pump(r):
    head = "🚀 كمّلت صعود" if r["repeat"] else "🚀 صعود جديد"
    lines = [
        f"{head}  {r['symbol']}",
        f"صعدت {r['rise']:.1f}% في آخر {r['mins']:.0f} دقيقة  |  السعر: {fmt_price(r['price'])}",
        "شمعة 5 دقايق أقفلت فوق أعلى الشمعة اللي قبلها ✅",
        f"فوليوم 24س: ${r['vol24']:,.0f}  |  تغير 24س: {r['chg24']:+.1f}%",
    ]
    if r["repeat"]:
        gain = (r["price"] / r["from_alert"] - 1) * 100
        lines.append(f"زادت {gain:.1f}% عن سعر التنبيه السابق ({fmt_price(r['from_alert'])})")
    if "buy_pct" in r:
        flow = f"آخر {r['trades']} صفقة: الشراء {r['buy_pct']:.0f}%  |  سيولة الشراء في الأوردر بوك {r['book_pct']:.0f}%"
        if r["botlike"]:
            flow += "  |  🤖 شبهة بوت"
        lines.append(flow)
        if r["book_pct"] < 40:
            lines.append("⚠️ طلبات البيع أكتر من الشراء، احتمال تصريف")
    lines.append("⚠️ الصعود حصل بالفعل، اتأكد من الشارت قبل أي قرار")
    lines.append(f"https://www.bitget.com/spot/{r['symbol']}")
    return "\n".join(lines)


def telegram(msg):
    tok, chat = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT")
    if not tok or not chat:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            data={"chat_id": chat, "text": msg[:4000]},
            timeout=10,
        )
    except Exception:
        pass


def send_email(body, subject="Bitget scanner: عملات حجمها قفز"):
    user, pwd = os.getenv("EMAIL_USER"), os.getenv("EMAIL_PASS")
    if not user or not pwd:
        return
    to = os.getenv("EMAIL_TO") or user
    host = os.getenv("EMAIL_SMTP", "smtp.gmail.com")
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = to
        msg.set_content(body)
        with smtplib.SMTP_SSL(host, 465, context=ssl.create_default_context(), timeout=20) as srv:
            srv.login(user, pwd)
            srv.send_message(msg)
    except Exception as e:
        print("email failed:", e)


def send_ntfy(body, title="Bitget scanner", tags="mag"):
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        print("ntfy: NTFY_TOPIC مش متضبط")
        return
    data = body.encode("utf-8")[:3800].decode("utf-8", "ignore").encode("utf-8")
    for attempt in range(3):
        try:
            r = requests.post(
                f"https://ntfy.sh/{topic}",
                data=data,
                headers={"Title": title, "Priority": "high", "Tags": tags},
                timeout=15,
            )
            if r.status_code == 200:
                print(f"ntfy: اتبعت ({len(data)} بايت)")
                return
            print(f"ntfy: فشل HTTP {r.status_code} {r.text[:150]}")
            if r.status_code != 429:
                return
            time.sleep(6)
        except Exception as e:
            print("ntfy failed:", e)
            time.sleep(2)


def notify(body, subject="Bitget scanner: عملات حجمها قفز", title="Bitget scanner", tags="mag"):
    telegram(body)
    send_email(body, subject)
    send_ntfy(body, title, tags)


def load_state(path):
    if not path:
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(path, state):
    if not path:
        return
    try:
        with open(path, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tf", default="15min", help="1min,5min,15min,30min,1h")
    p.add_argument("--window", type=int, default=4, help="عدد الشموع الحديثة (4 x 15min = ساعة)")
    p.add_argument("--ratio", type=float, default=4.0, help="أقل نسبة قفزة حجم عن الطبيعي")
    p.add_argument("--min-usd", type=float, default=1000, help="أقل حجم $ في الفترة الحديثة")
    p.add_argument("--max-move", type=float, default=2.0, help="أقصى حركة سعر%% في الفترة")
    p.add_argument("--max-range", type=float, default=4.0, help="أقصى مدى high-low%% في الفترة")
    p.add_argument("--min-active", type=int, default=1, help="أقل عدد شموع نشطة")
    p.add_argument("--floor", type=float, default=5.0, help="حد أدنى للحجم الطبيعي لكل شمعة $ (يمنع القسمة على صفر)")
    p.add_argument("--min-vol24", type=float, default=5_000)
    p.add_argument("--max-vol24", type=float, default=3_000_000)
    p.add_argument("--max-24h", type=float, default=15.0, help="استبعد العملات اللي اتحركت أكتر من كده في 24س")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--top", type=int, default=25)
    p.add_argument("--loop", type=int, default=0, help="ثواني بين كل مسح (0 = مرة واحدة)")
    p.add_argument("--duration", type=int, default=0, help="مع --loop: أقصى مدة تشغيل بالثواني قبل ما يقفل لوحده (0 = بلا حد)")
    p.add_argument("--cooldown", type=int, default=900, help="ثواني قبل تكرار تنبيه نفس العملة لو الفوليوم لسه عالي")
    p.add_argument("--vol-step", type=float, default=1.5, help="يكرر التنبيه فوراً لو الفوليوم الحديث زاد بالمعامل ده عن آخر تنبيه")
    p.add_argument("--hidden-only", action="store_true", help="الرجوع للوضع القديم: العملات الهادية سعرياً بس")
    p.add_argument("--min-buy", type=float, default=58, help="أقل نسبة شراء عدواني %%")
    p.add_argument("--min-book", type=float, default=52, help="أقل نسبة سيولة bid في الأوردر بوك %%")
    p.add_argument("--max-spread", type=float, default=1.5, help="أقصى سبريد bid/ask %%")
    p.add_argument("--depth", type=float, default=2.0, help="عمق الأوردر بوك المحسوب حوالين السعر %%")
    p.add_argument("--weak", action="store_true", help="اعرض كمان العملات الضعيفة (للتجربة)")
    p.add_argument("--pump-pct", type=float, default=5.0, help="نسبة الصعود اللي تطلّع تنبيه %%")
    p.add_argument("--pump-step", type=float, default=3.0, help="كل كام %% صعود إضافي يتكرر التنبيه لنفس العملة")
    p.add_argument("--pump-window", type=int, default=60, help="مدة قياس الصعود بالدقايق (من مضاعفات 5)")
    p.add_argument("--pump-min-vol24", type=float, default=20_000, help="أقل فوليوم 24س للعملة في تتبع الصعود $")
    p.add_argument("--no-pump", action="store_true", help="إيقاف تتبع الصعود")
    p.add_argument("--state", default=None, help="ملف لحفظ التنبيهات اللي اتبعتت (لمنع التكرار)")
    a = p.parse_args()
    # حدود "المخفي" بتتحفظ للتصنيف، وبعدين الفلاتر بتتفتح عشان العملات الظاهرة تتراقب كمان
    a.h_move, a.h_range, a.h_24 = a.max_move, a.max_range, a.max_24h
    if not a.hidden_only:
        a.max_move = a.max_range = a.max_24h = 10_000.0
        a.max_vol24 = max(a.max_vol24, 100_000_000.0)

    st = load_state(a.state)
    if "seen" not in st:
        st = {"seen": {k: v for k, v in st.items() if isinstance(v, (int, float))}}
    for k, v in list(st["seen"].items()):
        if isinstance(v, (int, float)):
            st["seen"][k] = {"ts": v, "vol": 0}
    seen = st["seen"]
    start = time.time()
    while True:
        tickers = api_get("/api/v2/spot/market/tickers") or []
        results = scan(a, tickers)
        print_table(results, a)
        now = time.time()
        fresh = []
        for r in results:
            prev = seen.get(r["symbol"])
            if prev is None:
                fresh.append(r)
            elif now - prev["ts"] >= a.cooldown or r["recent_usd"] >= prev["vol"] * a.vol_step:
                r["repeat"] = True
                fresh.append(r)
        if fresh:
            blocks = []
            for r in fresh[:10]:
                blocks.append(format_alert(r, a))
                seen[r["symbol"]] = {"ts": now, "vol": r["recent_usd"]}
            notify("\n\n".join(blocks))
        if not a.no_pump:
            pumps = track_pumps(tickers, a, st, now)
            print(f"تتبع الصعود: {len(pumps)} عملة صعدت {a.pump_pct:g}%+")
            if pumps:
                with ThreadPoolExecutor(max_workers=a.workers) as ex:
                    pumps[:8] = list(ex.map(lambda r: enrich(r, a), pumps[:8]))
                notify(
                    "\n\n".join(format_pump(r) for r in pumps[:8]),
                    subject="Bitget scanner: عملات صعدت",
                    title="Bitget pump",
                    tags="rocket",
                )
        seen = {k: v for k, v in seen.items() if now - v["ts"] < 86400}
        st["seen"] = seen
        save_state(a.state, st)
        if not a.loop:
            break
        if a.duration and time.time() - start >= a.duration:
            break
        time.sleep(a.loop)


if __name__ == "__main__":
    main()
