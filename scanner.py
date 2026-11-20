#!/usr/bin/env python3
"""
Bitget Stealth Volume Scanner
-----------------------------
بيدور على عملات حجم تداولها قفز فجأة بالنسبة لمتوسطها الطبيعي
لكن السعر لسه ما اتحركش (تجميع محتمل).
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


def get_candidates(a):
    """فلترة أولية من التيكرز: عملات USDT صغيرة الحجم وسعرها هادي في 24 ساعة."""
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
            "move": move,
            "range": rng,
            "active": active,
            "vol24": vol24,
            "chg24": chg24,
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
        r["spread"] = (asks[0][0] / bids[0][0] - 1) * 100

    r["strong"] = (
        r["buy_pct"] >= a.min_buy
        and r["book_pct"] >= a.min_book
        and r["spread"] <= a.max_spread
        and not r["botlike"]
    )
    return r


def scan(a):
    cands = get_candidates(a)
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


def telegram(msg):
    tok, chat = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT")
    if not tok or not chat:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            data={"chat_id": chat, "text": msg},
            timeout=10,
        )
    except Exception:
        pass


def send_email(body):
    user, pwd = os.getenv("EMAIL_USER"), os.getenv("EMAIL_PASS")
    if not user or not pwd:
        return
    to = os.getenv("EMAIL_TO") or user
    host = os.getenv("EMAIL_SMTP", "smtp.gmail.com")
    try:
        msg = EmailMessage()
        msg["Subject"] = "Bitget scanner: عملات حجمها قفز"
        msg["From"] = user
        msg["To"] = to
        msg.set_content(body)
        with smtplib.SMTP_SSL(host, 465, context=ssl.create_default_context(), timeout=20) as srv:
            srv.login(user, pwd)
            srv.send_message(msg)
    except Exception as e:
        print("email failed:", e)


def send_ntfy(body):
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        return
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers={"Title": "Bitget scanner", "Priority": "high", "Tags": "mag"},
            timeout=10,
        )
    except Exception as e:
        print("ntfy failed:", e)


def notify(body):
    telegram(body)
    send_email(body)
    send_ntfy(body)


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
    p.add_argument("--cooldown", type=int, default=3600, help="ثواني قبل تكرار تنبيه نفس العملة")
    p.add_argument("--min-buy", type=float, default=58, help="أقل نسبة شراء عدواني %%")
    p.add_argument("--min-book", type=float, default=52, help="أقل نسبة سيولة bid في الأوردر بوك %%")
    p.add_argument("--max-spread", type=float, default=1.5, help="أقصى سبريد bid/ask %%")
    p.add_argument("--depth", type=float, default=2.0, help="عمق الأوردر بوك المحسوب حوالين السعر %%")
    p.add_argument("--weak", action="store_true", help="اعرض كمان العملات الضعيفة (للتجربة)")
    p.add_argument("--state", default=None, help="ملف لحفظ التنبيهات اللي اتبعتت (لمنع التكرار)")
    a = p.parse_args()

    seen = load_state(a.state)
    while True:
        results = scan(a)
        print_table(results, a)
        now = time.time()
        fresh = [r for r in results if now - seen.get(r["symbol"], 0) >= a.cooldown]
        if fresh:
            lines = ["🔎 عملات حجمها قفز والسعر لسه هادي:"]
            for r in fresh[:10]:
                lines.append(
                    f"{r['symbol']} | حجم x{r['ratio']:.1f} | ${r['recent_usd']:,.0f} | "
                    f"حركة {r['move']:.2f}% | شراء {r['buy_pct']:.0f}% | دفتر {r['book_pct']:.0f}%"
                )
                seen[r["symbol"]] = now
            notify("\n".join(lines))
        seen = {k: v for k, v in seen.items() if now - v < 86400}
        save_state(a.state, seen)
        if not a.loop:
            break
        time.sleep(a.loop)


if __name__ == "__main__":
    main()
