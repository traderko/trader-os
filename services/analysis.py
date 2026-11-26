"""거래 분석 (웹 화면 '분석' 탭) - 내 거래가 얼마나 흔들렸고(MAE) 얼마나 갔는지(MFE), 증거금을 얼마나 썼을 때 위험했는지

── 1분봉 ──────────────────────────────────────────────────────────
  거래를 들고 있는 동안 가격이 어떤 길로 움직였는지 보려고 1분봉을 씀 (워커 /rates).
  날짜(UTC)별로 받아 data/analysis.db 에 저장 - 지난 날은 다시 받지 않음 (오늘은 매번 새로).
  1분봉이 없는 오래된 날은 5분봉으로 (정밀도 낮음 - 화면에 표시).

── MAE · MFE (거래마다) ───────────────────────────────────────────
  MAE(최대 역행) = 진입 뒤 청산까지 진입가에서 가장 불리하게 간 폭 (매수면 진입가 - 최저가)
  MFE(최대 순행) = 가장 유리하게 간 폭 (매수면 최고가 - 진입가)
  MAE(최고점 전) = MFE 최고점을 찍기 전까지의 최대 역행 - "얼마나 버텼다가 갔는지"
  봉 단위라 진입 봉 · 청산 봉 안에서 진입 전 · 청산 뒤 가격이 섞일 수 있음 (1분봉이면 오차 1분)
  KTR 단위 = 그 거래가 진입한 장(아시아·유로·미장)의 KTR (구간 탭과 같은 계산 - services/grid.py)

── 증거금 사용률 (포지션을 들고 있던 구간마다) ──────────────────
  구간 = 포지션이 하나라도 열려 있던 연속된 시간 (거래들이 겹치면 한 구간)
  분마다 다시 계산:
    잔고      = 구간 첫 청산 거래의 청산 직전 잔고(trades.balance_before) + 그 뒤 청산된 손익
    평가금    = 잔고 + 열린 포지션들의 평가손익 (그 분의 가장 불리한 가격 - 보수적으로)
    증거금    = 1랏 증거금(MT5, 지금 레버리지) × 가격 비율 × 랏
    사용률    = 증거금 ÷ 평가금 × 100   (MT5 '증거금 레벨' = 평가금 ÷ 증거금 - 사용률 10% = 레벨 1000%)
  구간마다: 최대 사용률 · 평가금 최대 낙폭(구간 시작 잔고 대비 %) · 손익 · 강제청산 여부
  최대 사용률 구간(5% 미만, 5~10% …)으로 묶어서 "얼마 이상 들어갔을 때 위험했는지" 표
  한계: 레버리지 · 증거금 정책이 바뀌었으면 그 기간은 어긋남 / 입출금은 구간 안에서는 무시 / 스왑 빠짐
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

from db.session import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, "analysis.db")
MAX_DAYS = 120                 # 한 번에 새로 받는 날짜 수 상한 (첫 분석이 너무 오래 걸리지 않게)
USAGE_BUCKETS = [(0, 5), (5, 10), (10, 20), (20, 30), (30, 50), (50, None)]
TFS = ("2m", "5m", "10m", "1h", "2h")

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_sym_cache: dict[tuple[int, str], tuple[float, dict]] = {}     # (계좌, 종목) → (받은 시각, {pv, margin, ref})


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("""CREATE TABLE IF NOT EXISTS bars (
            symbol TEXT, t INTEGER, h REAL, l REAL, c REAL, PRIMARY KEY (symbol, t))""")
        _conn.execute("""CREATE TABLE IF NOT EXISTS days (
            symbol TEXT, day TEXT, tf TEXT, PRIMARY KEY (symbol, day))""")
        _conn.execute("""CREATE TABLE IF NOT EXISTS ktr (
            symbol TEXT, tf TEXT, open_ts INTEGER, ktr REAL, PRIMARY KEY (symbol, tf, open_ts))""")
        # 1랏 1포인트 손익 · 1랏 증거금 (하루 동안 다시 묻지 않음 - 서버를 다시 켜도)
        _conn.execute("""CREATE TABLE IF NOT EXISTS syminfo (
            account_id INTEGER, symbol TEXT, pv REAL, margin REAL, ref REAL, at REAL, PRIMARY KEY (account_id, symbol))""")
        # 청산 이유 (MT5 체결 기록 - 지난 거래는 바뀌지 않아 한 번만 물음). reason '' = 물어봤는데 없음
        _conn.execute("""CREATE TABLE IF NOT EXISTS reasons (
            account_id INTEGER, ticket INTEGER, reason TEXT, PRIMARY KEY (account_id, ticket))""")
        _conn.commit()
    return _conn


# ── 봉 ──
def _day_key(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%d")


async def _ensure_days(account_id: int, symbol: str, days: list[str], progress=None) -> dict[str, str]:
    """이 날짜들의 봉을 저장소에 채움. {날짜: 'M1' | 'M5' | ''} (''= 봉 없음)"""
    from services.worker_client import worker_get
    today = _day_key(datetime.now(timezone.utc))
    with _lock:
        have = dict(_db().execute("SELECT day, tf FROM days WHERE symbol=?", (symbol,)).fetchall())
    out, todo = {}, []
    for d in days:
        if d in have and d != today:
            out[d] = have[d]
        else:
            todo.append(d)
    for i, d in enumerate(todo[:MAX_DAYS]):
        if progress:
            progress(f"{symbol} 1분봉 받는 중 {i + 1}/{min(len(todo), MAX_DAYS)}일")
        start = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        got, tf_used = [], ""
        for tf in ("M1", "M5"):
            try:
                rows = await worker_get(account_id, "/rates", {
                    "symbol": symbol, "timeframe": tf, "from_ts": start.isoformat(),
                    "to_ts": (start + timedelta(days=1)).isoformat()}, timeout=40)
            except Exception as e:
                if getattr(e, "status_code", None) in (503, 404):
                    raise
                rows = []
            if rows:
                got, tf_used = rows, tf
                break
        with _lock:
            c = _db()
            if got:
                c.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?)",
                              [(symbol, int(r["time"]), float(r["high"]), float(r["low"]), float(r["close"])) for r in got])
            c.execute("INSERT OR REPLACE INTO days VALUES (?,?,?)", (symbol, d, tf_used))
            c.commit()
        out[d] = tf_used
    return out


def _bars(symbol: str, t0: int, t1: int) -> list[tuple[int, float, float, float]]:
    with _lock:
        return _db().execute("SELECT t, h, l, c FROM bars WHERE symbol=? AND t>=? AND t<=? ORDER BY t",
                             (symbol, t0, t1)).fetchall()


# ── 종목 정보 (1랏 1포인트 손익 · 1랏 증거금) ──
async def _sym_info(account_id: int, symbol: str, price: float) -> dict | None:
    from services.worker_client import worker_get
    k = (account_id, symbol)
    hit = _sym_cache.get(k)
    if hit and time.time() - hit[0] < 24 * 3600:
        return hit[1]
    with _lock:
        row = _db().execute("SELECT pv, margin, ref, at FROM syminfo WHERE account_id=? AND symbol=?", k).fetchone()
    if row and time.time() - row[3] < 24 * 3600:
        info = {"pv": row[0], "margin": row[1], "ref": row[2]}
        _sym_cache[k] = (row[3], info)
        return info
    try:
        r = await worker_get(account_id, "/calc", {"symbol": symbol, "side": "BUY", "volume": 1,
                                                   "price_open": price, "price_close": price + 1}, timeout=20)
        info = {"pv": float(r["profit"]), "margin": float(r["margin"]), "ref": price}
        if not (info["pv"] > 0 and info["margin"] > 0):
            return None
    except Exception:
        return None
    _sym_cache[k] = (time.time(), info)
    with _lock:
        _db().execute("INSERT OR REPLACE INTO syminfo VALUES (?,?,?,?,?,?)", (*k, info["pv"], info["margin"], info["ref"], time.time()))
        _db().commit()
    return info


# ── KTR ──
async def _ktr(account_id: int, symbol: str, tf: str, at: datetime) -> float | None:
    from services import grid
    try:
        sid = grid.current_session(at)
        opened = grid.session_open(sid, at)
    except Exception:
        return None
    key = (symbol, tf, int(opened.timestamp()))
    with _lock:
        r = _db().execute("SELECT ktr FROM ktr WHERE symbol=? AND tf=? AND open_ts=?", key).fetchone()
    if r:
        return r[0] or None
    try:
        start, end = grid._window([opened], tf, datetime.now(timezone.utc))
        candles = await grid._candles(account_id, symbol, "mt5", tf, start, end)
        v = grid._ktr_from(candles, sid, opened, tf, "mt5")["ktr"]
    except Exception:
        return None
    if v and v > 0:
        with _lock:
            _db().execute("INSERT OR REPLACE INTO ktr VALUES (?,?,?,?)", (*key, v))
            _db().commit()
    return v if v and v > 0 else None


# ── 계산 ──
def _pct(vals: list[float], p: float) -> float | None:
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    i = (len(v) - 1) * p / 100
    lo, hi = int(i), min(int(i) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (i - lo)


def excursion(side: str, po: float, bars: list[tuple]) -> dict | None:
    """MAE · MFE · 최고점 전 MAE (가격 폭). bars: (t, h, l, c) 시간순"""
    if not bars or po is None:
        return None
    buy = str(side).upper().startswith("B")
    mae = mfe = 0.0
    mae_pre = 0.0
    for _, h, l, _c in bars:
        fav = (h - po) if buy else (po - l)
        adv = (po - l) if buy else (h - po)
        if fav > mfe:
            mfe = fav
            mae_pre = mae          # 이 최고점 전까지 (이 봉 안의 순서는 모름 - 이전 봉까지)
        mae = max(mae, adv)
    return {"mae": max(mae, 0.0), "mfe": max(mfe, 0.0), "mae_pre": max(mae_pre, 0.0)}


def excursion_fast(side: str, po: float, H: list[float], L: list[float]) -> dict | None:
    """excursion 과 같은 결과 - 고가 · 저가 리스트로 (max/min/index 가 C 로 돌아서 빠름)"""
    if not H or po is None:
        return None
    if str(side).upper().startswith("B"):
        hi = max(H)
        i = H.index(hi)                                   # 순행 최고점이 처음 나온 봉
        mfe, mae = hi - po, po - min(L)
        mae_pre = (po - min(L[:i])) if i > 0 and mfe > 0 else 0.0    # 한 번도 유리하게 안 갔으면 0 (excursion 과 같게)
    else:
        lo = min(L)
        i = L.index(lo)
        mfe, mae = po - lo, max(H) - po
        mae_pre = (max(H[:i]) - po) if i > 0 and mfe > 0 else 0.0
    return {"mae": max(mae, 0.0), "mfe": max(mfe, 0.0), "mae_pre": max(mae_pre, 0.0)}


def _summary(rows: list[dict], key: str) -> dict:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return {"n": len(vals), "p50": _pct(vals, 50), "p75": _pct(vals, 75), "p90": _pct(vals, 90), "max": max(vals) if vals else None}


def episodes(trades: list[dict], bars_of: dict, info_of: dict) -> list[dict]:
    """포지션을 들고 있던 구간마다 분 단위로 사용률 · 평가금 계산.
    trades: {id, symbol, side, volume, price_open, open_ts, close_ts, net, balance_before, close_reason}
    분마다 거래를 다 훑지 않고, 종목 · 방향별 합계(랏 × 1포인트 손익, 랏 × 1포인트 손익 × 진입가, 랏)를
    진입 · 청산 때만 바꿔 둠 → 분마다는 종목 수만큼만 계산
      평가손익(가장 불리한 가격) = Σ매수 pv·랏·(저가 - 진입가) + Σ매도 pv·랏·(진입가 - 고가)"""
    ts = sorted((t for t in trades if t["open_ts"] and t["close_ts"]), key=lambda t: t["open_ts"])
    groups, cur, end = [], [], None
    for t in ts:
        if cur and t["open_ts"] > end:
            groups.append(cur)
            cur = []
        if not cur:
            end = t["close_ts"]
        cur.append(t)
        end = max(end, t["close_ts"])
    if cur:
        groups.append(cur)

    out = []
    for g in groups:
        first_close = min(g, key=lambda t: t["close_ts"])
        bal0 = first_close.get("balance_before")
        e = {"start": min(t["open_ts"] for t in g), "end": max(t["close_ts"] for t in g), "n": len(g),
             "trade_ids": [t["id"] for t in g], "symbols": sorted({t["symbol"] for t in g}),
             "net": round(sum(t["net"] for t in g), 2), "balance": bal0,
             "stopout": any(t.get("close_reason") == "stopout" for t in g),
             "max_usage": None, "max_margin": None, "min_equity": None, "dd_pct": None, "max_lots": None}
        if not bal0 or any(info_of.get(t["symbol"]) is None for t in g):
            out.append(e)
            continue
        m0, m1 = e["start"] // 60 * 60, e["end"] // 60 * 60
        if (m1 - m0) // 60 > 60 * 24 * 14:          # 2주 넘게 들고 있던 구간은 건너뜀 (계산이 너무 큼)
            out.append(e)
            continue
        # 이벤트: 분 m 에 열려 있는 거래 = open_ts < m + 60 and close_ts > m
        #   → 분 (open_ts - 59) // 60 * 60 ... 부터 포함, 분 m >= close_ts 부터 빠짐
        adds, rems = {}, {}
        for t in g:
            ma = max(m0, ((t["open_ts"] - 60) // 60 + 1) * 60)    # open_ts < m + 60 인 첫 분
            mr = ((t["close_ts"] - 1) // 60 + 1) * 60             # close_ts <= m 인 첫 분 (= close_ts > m 이 끝나는 분)
            if mr <= ma:
                continue
            adds.setdefault(ma, []).append(t)
            rems.setdefault(mr, []).append(t)
        agg: dict[str, list[float]] = {}     # 종목 → [매수 A, 매수 B, 매도 A, 매도 B, 랏, 종가용 A, 종가용 B]
        closes = sorted((t["close_ts"], t["net"]) for t in g)
        last_bar: dict[str, tuple] = {}
        bal, ci, n_open = bal0, 0, 0
        mx_u = mx_m = mx_l = 0.0
        mn_eq = None
        for m in range(m0, m1 + 60, 60):
            while ci < len(closes) and closes[ci][0] <= m:          # 이 분이 시작하기 전에 청산된 손익은 잔고로
                bal += closes[ci][1]
                ci += 1
            for t, sgn in [(x, -1) for x in rems.get(m, ())] + [(x, 1) for x in adds.get(m, ())]:
                inf = info_of[t["symbol"]]
                a = agg.setdefault(t["symbol"], [0.0] * 7)
                k = inf["pv"] * t["volume"] * sgn
                if str(t["side"]).upper().startswith("B"):
                    a[0] += k; a[1] += k * t["price_open"]; a[5] += k; a[6] += k * t["price_open"]
                else:
                    a[2] += k; a[3] += k * t["price_open"]; a[5] -= k; a[6] -= k * t["price_open"]
                a[4] += t["volume"] * sgn
                n_open += sgn
            if n_open <= 0:
                continue
            fl_worst = fl_close = margin = lots = 0.0
            ok = True
            for sym, a in agg.items():
                if abs(a[4]) < 1e-9:
                    continue
                b = bars_of.get(sym, {}).get(m) or last_bar.get(sym)
                if b is None:
                    ok = False
                    break
                last_bar[sym] = b
                h, l, c = b
                fl_worst += (a[0] * l - a[1]) + (a[3] - a[2] * h)
                fl_close += a[5] * c - a[6]
                inf = info_of[sym]
                margin += inf["margin"] * (c / inf["ref"]) * a[4]
                lots += a[4]
            if not ok:
                continue
            eq_c, eq_w = bal + fl_close, bal + fl_worst
            if eq_c > 0:
                mx_u = max(mx_u, margin / eq_c * 100)
            mx_m, mx_l = max(mx_m, margin), max(mx_l, lots)
            mn_eq = eq_w if mn_eq is None else min(mn_eq, eq_w)
        if mn_eq is not None:
            e.update({"max_usage": round(mx_u, 2), "max_margin": round(mx_m, 2), "max_lots": round(mx_l, 2),
                      "min_equity": round(mn_eq, 2), "dd_pct": round(max(0.0, (bal0 - mn_eq) / bal0 * 100), 2)})
        out.append(e)
    return out


def usage_table(eps: list[dict]) -> list[dict]:
    rows = []
    for lo, hi in USAGE_BUCKETS:
        es = [e for e in eps if e["max_usage"] is not None and e["max_usage"] >= lo and (hi is None or e["max_usage"] < hi)]
        dds = [e["dd_pct"] for e in es if e["dd_pct"] is not None]
        rows.append({
            "lo": lo, "hi": hi, "n": len(es),
            "dd_p50": _pct(dds, 50), "dd_max": max(dds) if dds else None,
            "dd20": sum(1 for x in dds if x >= 20), "dd50": sum(1 for x in dds if x >= 50),
            "stopout": sum(1 for e in es if e["stopout"]),
            "losses": sum(1 for e in es if e["net"] < 0), "net": round(sum(e["net"] for e in es), 2),
        })
    return rows


async def analyze(account_id: int, trades: list, tf: str = "5m", progress=None) -> dict:
    """trades: 청산된 Trade 행들. 결과는 웹 '분석' 탭 그대로. progress(문자열): 진행 상황 (웹에 보여 줌)"""
    progress = progress or (lambda _m: None)
    tm: dict[str, float] = {}                 # 단계별 걸린 시간 (초) - 화면 아래 · 서버 로그
    mark = [time.perf_counter()]

    def lap(name: str) -> None:
        now = time.perf_counter()
        tm[name] = tm.get(name, 0.0) + (now - mark[0])
        mark[0] = now
    tf = tf if tf in TFS else "5m"
    rows = [t for t in trades if t.close_time is not None and t.open_time is not None and t.price_open]
    notes: list[str] = []
    # 1) 봉 받기 (종목별 · 날짜별)
    days_of: dict[str, set[str]] = {}
    for t in rows:
        d = t.open_time.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        while d <= t.close_time:
            days_of.setdefault(t.symbol, set()).add(_day_key(d))
            d += timedelta(days=1)
    prec: dict[tuple[str, str], str] = {}
    mt5_ok = True
    for sym, days in days_of.items():
        try:
            got = await _ensure_days(account_id, sym, sorted(days), progress)
            for d, tfu in got.items():
                prec[(sym, d)] = tfu
            if len(days) > MAX_DAYS:
                notes.append(f"{sym}: 날짜가 많아 {MAX_DAYS}일만 먼저 받았어요. 다시 열면 이어서 받아요.")
        except Exception as e:
            mt5_ok = False
            notes.append(f"{sym}: MT5 에서 봉을 받지 못했어요 ({getattr(e, 'detail', e) or type(e).__name__}). 저장된 봉으로만 계산해요.")

    lap("bars")
    # 2) 종목 정보 (1랏 1포인트 손익 · 1랏 증거금) - 역행 금액 · 증거금 사용률용
    info_of: dict[str, dict | None] = {}
    syms = sorted({t.symbol for t in rows})
    refs = {sym: next((t.price_close or t.price_open for t in sorted(rows, key=lambda x: x.close_time, reverse=True)
                       if t.symbol == sym), None) for sym in syms}
    got = await asyncio.gather(*[_sym_info(account_id, sym, refs[sym]) if (refs[sym] and mt5_ok) else asyncio.sleep(0)
                                 for sym in syms])          # 종목들을 한꺼번에 물음
    for sym, inf in zip(syms, got):
        info_of[sym] = inf or None
        if info_of[sym] is None:
            notes.append(f"{sym}: 1랏 증거금 · 손익을 MT5 에서 받지 못해 증거금 사용률은 빼고 계산했어요.")
    lap("sym")
    # 3) 거래마다 MAE · MFE
    progress("거래마다 역행 · 순행 재는 중 (KTR 받는 중)")
    bars_of: dict[str, dict] = {}
    ktr_of: dict[int, float | None] = {}
    for t in rows:                                        # KTR (저장된 건 바로, 없는 장만 MT5 에)
        ktr_of[t.id] = await _ktr(account_id, t.symbol, tf, t.open_time) if mt5_ok else None
    lap("ktr")

    def measure() -> list[dict]:
        """역행 · 순행 (계산만 - 서버가 멈추지 않게 스레드에서)"""
        import bisect
        arr: dict[str, tuple[list, list, list]] = {}       # 종목 → (시각, 고가, 저가) 한 번에 읽어 둠
        for sym in {t.symbol for t in rows}:
            t0 = min(int(t.open_time.timestamp()) for t in rows if t.symbol == sym) // 60 * 60 - 300
            t1 = max(int(t.close_time.timestamp()) for t in rows if t.symbol == sym)
            b = _bars(sym, t0, t1)
            arr[sym] = ([x[0] for x in b], [x[1] for x in b], [x[2] for x in b])
        out = []
        for t in rows:
            o, c = int(t.open_time.timestamp()), int(t.close_time.timestamp())
            T, H, L = arr[t.symbol]
            span = 300 if prec.get((t.symbol, _day_key(t.open_time))) == "M5" else 60
            i = bisect.bisect_right(T, o - span)              # 끝(시작 + span)이 진입 뒤인 첫 봉 = 진입 봉
            j = bisect.bisect_right(T, c)                     # 청산 시각까지 시작한 봉
            ex = excursion_fast(t.position, t.price_open, H[i:j], L[i:j])
            ktr = ktr_of.get(t.id)
            p = prec.get((t.symbol, _day_key(t.open_time)))
            net = (t.profit or 0) + (t.commission or 0)
            r = {"id": t.id, "ticket": t.ticket, "symbol": t.symbol, "side": t.position, "volume": t.volume,
                 "open_time": t.open_time.isoformat(), "close_time": t.close_time.isoformat(),
                 "price_open": t.price_open, "price_close": t.price_close, "net": round(net, 2),
                 "precision": p or None, "ktr": round(ktr, 5) if ktr else None,
                 "mae": None, "mfe": None, "mae_pre": None, "mae_k": None, "mfe_k": None, "mae_pre_k": None,
                 "move": None, "move_k": None}
            if ex:
                d = 1 if str(t.position).upper().startswith("B") else -1
                mv = ((t.price_close - t.price_open) * d) if t.price_close else None
                r.update({k: round(ex[k], 5) for k in ("mae", "mfe", "mae_pre")})
                r["move"] = round(mv, 5) if mv is not None else None
                if ktr:
                    r.update({"mae_k": round(ex["mae"] / ktr, 2), "mfe_k": round(ex["mfe"] / ktr, 2),
                              "mae_pre_k": round(ex["mae_pre"] / ktr, 2),
                              "move_k": round(mv / ktr, 2) if mv is not None else None})
            inf = info_of.get(t.symbol)
            if ex and inf:                       # 최대 역행 때 이 거래의 평가손 (금액 · 청산 직전 잔고 대비 %)
                r["mae_usd"] = round(ex["mae"] * inf["pv"] * (t.volume or 0), 2)
                bb = getattr(t, "balance_before", None)
                r["mae_bal_pct"] = round(r["mae_usd"] / bb * 100, 2) if bb else None
            out.append(r)
        return out

    out_trades = await asyncio.to_thread(measure)
    lap("mae")

    wins = [r for r in out_trades if r["net"] > 0]
    loss = [r for r in out_trades if r["net"] <= 0]
    summary = {
        "n": len(out_trades), "n_measured": sum(1 for r in out_trades if r["mae"] is not None),
        "n_ktr": sum(1 for r in out_trades if r["mae_k"] is not None),
        "win": {"n": len(wins), "mae_k": _summary(wins, "mae_k"), "mfe_k": _summary(wins, "mfe_k"),
                "mae_pre_k": _summary(wins, "mae_pre_k"), "move_k": _summary(wins, "move_k")},
        "loss": {"n": len(loss), "mae_k": _summary(loss, "mae_k"), "mfe_k": _summary(loss, "mfe_k"),
                 "move_k": _summary(loss, "move_k"),
                 "gave_back_1k": sum(1 for r in loss if (r["mfe_k"] or 0) >= 1)},
        "win_mae_bal_pct": _summary(wins, "mae_bal_pct"), "loss_mae_bal_pct": _summary(loss, "mae_bal_pct"),
        "m5_days": sum(1 for v in prec.values() if v == "M5"), "no_bar_days": sum(1 for v in prec.values() if v == ""),
    }

    lap("mae")
    # 4) 증거금 사용률 - 구간마다
    progress("증거금 사용률 계산 중")
    # 강제청산: 거래 기록의 청산 이유, 없으면 MT5 체결 기록에서 (워커 /close-reasons) - 한 번 물은 거래는 저장해 둠
    reasons: dict[int, str] = {}
    with _lock:
        reasons = {tk: rs for tk, rs in _db().execute("SELECT ticket, reason FROM reasons WHERE account_id=?",
                                                       (account_id,)).fetchall()}
    need = [t for t in rows if not getattr(t, "close_reason", None) and int(t.ticket or 0) not in reasons]
    if mt5_ok and need:
        try:
            from services.worker_client import worker_get
            rr = await worker_get(account_id, "/close-reasons", {
                "from_ts": (min(t.open_time for t in need) - timedelta(hours=1)).isoformat(),
                "to_ts": (max(t.close_time for t in need) + timedelta(hours=1)).isoformat()}, timeout=40)
            got = {int(k): v for k, v in (rr or {}).items()}
            now_ts = time.time()
            with _lock:                      # 물어본 거래는 없음('')도 저장 - 하루 지난 거래만 (방금 청산은 다음에 다시)
                _db().executemany("INSERT OR REPLACE INTO reasons VALUES (?,?,?)",
                                  [(account_id, int(t.ticket or 0), got.get(int(t.ticket or 0), "")) for t in need
                                   if now_ts - t.close_time.timestamp() > 86400 or int(t.ticket or 0) in got])
                _db().commit()
            reasons.update(got)
        except Exception:
            pass
    lap("stopout")

    def usage() -> list[dict]:
        """증거금 사용률 (계산만 - 스레드에서)"""
        for sym in days_of:
            if info_of.get(sym) is None:
                continue
            t0 = min(int(t.open_time.timestamp()) for t in rows if t.symbol == sym) // 60 * 60 - 300
            t1 = max(int(t.close_time.timestamp()) for t in rows if t.symbol == sym)
            bm: dict[int, tuple] = {}
            for t, h, l, c in _bars(sym, t0, t1):
                if prec.get((sym, _day_key(datetime.fromtimestamp(t, timezone.utc)))) == "M5":    # 5분봉은 5분 모두에
                    for k in range(5):
                        bm[t + k * 60] = (h, l, c)
                else:
                    bm[t] = (h, l, c)
            bars_of[sym] = bm
        return episodes([{
            "id": t.id, "symbol": t.symbol, "side": t.position, "volume": t.volume or 0, "price_open": t.price_open,
            "open_ts": int(t.open_time.timestamp()), "close_ts": int(t.close_time.timestamp()),
            "net": (t.profit or 0) + (t.commission or 0), "balance_before": getattr(t, "balance_before", None),
            "close_reason": getattr(t, "close_reason", None) or reasons.get(int(t.ticket or 0)) or None} for t in rows],
            bars_of, info_of)

    eps = await asyncio.to_thread(usage)
    lap("usage")
    missing_bal = sum(1 for e in eps if not e["balance"])
    if missing_bal:
        notes.append(f"청산 직전 잔고가 아직 없는 구간 {missing_bal}개는 사용률을 못 구했어요 (서버가 채우는 중이면 잠시 뒤 다시).")

    timings = {k: round(v, 2) for k, v in tm.items()}
    print(f"[analysis] account={account_id} 거래 {len(rows)}건 · 구간 {len(eps)}개 · 걸린 시간 {timings}")
    return {"tf": tf, "timings": timings, "mt5_ok": mt5_ok and all(v is not None for v in info_of.values()), "summary": summary, "trades": out_trades, "episodes": eps,
            "usage_table": usage_table(eps), "notes": notes,
            "has_usage": any(e["max_usage"] is not None for e in eps)}
