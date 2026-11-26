"""평가금 기록 - 계좌마다 1분에 한 줄 (잔고 · 평가금 마지막/최저/최고 · 증거금 · 평가손익 %) → 평가금 기준 최대 낙폭
   평가손익 % = (평가금 - 잔고) ÷ 잔고 × 100 - pct(마지막) · pct_min(최저) · pct_max(최고)

왜: 통계의 최대 낙폭은 청산된 거래로만 셈 - 들고 있다가 크게 마이너스였다가 살아나서 청산한 건 안 잡힘.
    MT5 는 지난 평가금을 알려주지 않아서 TraderOS 가 직접 적어 둠 (기록을 시작한 뒤부터만 볼 수 있음)

어떻게
  - 계좌 정보(/account-info)를 읽을 때마다 (실시간 현황 등 - worker_client 가 observe 를 부름) 그 분의 값에 합침
    1분 동안 읽은 값 중 가장 낮은/높은 평가금도 같이 → 1분 사이 잠깐 깊게 빠진 것도 잡힘
  - 10초 동안 아무도 안 읽었으면 여기서 읽음 (텔레그램을 안 써도 기록됨). MT5 요청은 계좌당 10초에 많아야 1번
  - 잔고가 바뀌면 입출금 내역을 확인해 따로 적음 - 낙폭 계산에서 입출금은 빼야 해서
  - 저장: data/equity.db (SQLite, 따로 설치할 것 없음). 계좌 하나 1년 ≈ 50만 줄 · 수십 MB. 지우지 않고 다 둠

평가금 기준 최대 낙폭 (drawdown)
  입출금을 뺀 평가금(= 평가금 - 기록 시작 뒤 누적 입출금)의 고점 대비 가장 크게 빠진 폭.
  입출금이 있었던 분과 그 다음 분은 고점·저점 계산에서 뺌 (그 1분 안에서 입출금 전후 값이 섞여서)
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
POLL_SEC = 10
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_cur: dict[int, dict] = {}          # 계좌 → 지금 분의 값 {ts, bal, eq, lo, hi, margin}
_last_bal: dict[int, float] = {}
_last_obs: dict[int, float] = {}
_flow_check: set[int] = set()
_fail_until: dict[int, float] = {}


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        from db.session import DATA_DIR
        _conn = sqlite3.connect(os.path.join(DATA_DIR, "equity.db"), check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("""CREATE TABLE IF NOT EXISTS equity (
            account_id INTEGER, ts INTEGER, balance REAL, equity REAL, equity_min REAL, equity_max REAL, margin REAL,
            PRIMARY KEY (account_id, ts))""")
        _conn.execute("""CREATE TABLE IF NOT EXISTS flows (
            account_id INTEGER, ticket INTEGER, ts INTEGER, amount REAL, PRIMARY KEY (account_id, ticket))""")
        # 평가손익 % (잔고 대비) - 그 분의 마지막 · 최저 · 최고 평가금으로. 예전 기록은 처음 열 때 한 번 채움
        cols = {r[1] for r in _conn.execute("PRAGMA table_info(equity)")}
        if "pct" not in cols:
            for c in ("pct", "pct_min", "pct_max"):
                _conn.execute(f"ALTER TABLE equity ADD COLUMN {c} REAL")
            _conn.execute("""UPDATE equity SET
                pct = ROUND((equity - balance) / balance * 100, 4),
                pct_min = ROUND((COALESCE(equity_min, equity) - balance) / balance * 100, 4),
                pct_max = ROUND((COALESCE(equity_max, equity) - balance) / balance * 100, 4)
                WHERE balance > 0""")
        _conn.commit()
    return _conn


def _write(rows: list[tuple]) -> None:
    with _lock:
        c = _db()
        c.executemany("""INSERT OR REPLACE INTO equity
            (account_id, ts, balance, equity, equity_min, equity_max, margin, pct, pct_min, pct_max)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", rows)
        c.commit()


def _pct(eq: float, bal: float) -> float | None:
    """평가손익 % (잔고 대비) - 잔고가 0 이하면 None"""
    return round((eq - bal) / bal * 100, 4) if bal > 0 else None


def _flush(aid: int, b: dict) -> None:
    try:
        bal = b["bal"]
        _write([(aid, b["ts"], bal, b["eq"], b["lo"], b["hi"], b["margin"],
                 _pct(b["eq"], bal), _pct(b["lo"], bal), _pct(b["hi"], bal))])
    except Exception as e:
        print(f"[equity_log] 저장 실패: {e}")
    prev = _last_bal.get(aid)
    if prev is not None and abs(prev - b["bal"]) > 1e-6:
        _flow_check.add(aid)                     # 잔고가 바뀜 → 입출금인지 확인
    _last_bal[aid] = b["bal"]


def observe(account_id: int, info: dict) -> None:
    """worker /account-info 결과를 받을 때마다 (worker_client 가 부름)"""
    try:
        bal, eq = float(info["balance"]), float(info["equity"])
    except (KeyError, TypeError, ValueError):
        return
    now = time.time()
    _last_obs[account_id] = now
    ts = int(now // 60 * 60)
    b = _cur.get(account_id)
    if b and b["ts"] != ts:
        _flush(account_id, b)
        b = None
    if not b:
        _cur[account_id] = {"ts": ts, "bal": bal, "eq": eq, "lo": eq, "hi": eq, "margin": float(info.get("margin") or 0)}
        return
    b.update(bal=bal, eq=eq, lo=min(b["lo"], eq), hi=max(b["hi"], eq), margin=float(info.get("margin") or 0))


async def _account_ids() -> list[int]:
    from sqlalchemy import select
    from db.models.account import Account
    from db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        return [a.id for a in (await db.execute(select(Account).order_by(Account.id))).scalars().all()]


async def _check_flows(aid: int) -> None:
    from services.worker_client import worker_get
    now = datetime.now(KST).replace(tzinfo=None)
    r = await asyncio.wait_for(worker_get(aid, "/balances", {
        "start_time": (now - timedelta(days=3)).isoformat(), "end_time": (now + timedelta(hours=1)).isoformat()}), 15)
    rows = []
    for f in (r.get("deposits") or []) + (r.get("withdrawals") or []):
        try:
            t = datetime.fromisoformat(str(f["time"]))
            t = t.replace(tzinfo=KST) if t.tzinfo is None else t      # 내역 시각은 한국 시간
            rows.append((aid, int(f["ticket"]), int(t.timestamp()), float(f["amount"])))
        except (KeyError, TypeError, ValueError):
            continue
    if rows:
        with _lock:
            c = _db()
            c.executemany("INSERT OR IGNORE INTO flows VALUES (?,?,?,?)", rows)
            c.commit()


async def loop() -> None:
    from services.worker_client import worker_get
    await asyncio.sleep(15)
    while True:
        try:
            now = time.time()
            for aid in await _account_ids():
                if now - _last_obs.get(aid, 0) >= POLL_SEC - 1 and now >= _fail_until.get(aid, 0):
                    try:
                        await asyncio.wait_for(worker_get(aid, "/account-info"), 8)   # observe 는 worker_client 가 부름
                    except Exception:
                        _fail_until[aid] = now + 60          # MT5 가 안 붙은 계좌는 1분 뒤 다시
                b = _cur.get(aid)
                if b and b["ts"] < int(time.time() // 60 * 60):
                    _flush(aid, _cur.pop(aid))
            for aid in list(_flow_check):
                try:
                    await _check_flows(aid)
                    _flow_check.discard(aid)
                except Exception:
                    pass
        except Exception as e:
            print(f"[equity_log] 확인 실패: {e}")
        await asyncio.sleep(POLL_SEC)


# ── 계산 ──
def _read(aid: int, start: int, end: int) -> tuple[list, list]:
    with _lock:
        c = _db()
        rows = c.execute("SELECT ts, balance, equity, equity_min, equity_max FROM equity WHERE account_id=? AND ts>=? AND ts<? ORDER BY ts",
                         (aid, start, end)).fetchall()
        flows = c.execute("SELECT ts, amount FROM flows WHERE account_id=? AND ts>=? AND ts<? ORDER BY ts",
                          (aid, rows[0][0] if rows else start, end)).fetchall()
    return rows, flows


def drawdown(aid: int, start: datetime, end: datetime) -> dict | None:
    """평가금 기준 최대 낙폭. 기록이 없으면 None
    {amount, pct, peak, trough, peak_at, trough_at, float_min, float_min_at, since, minutes}"""
    rows, flows = _read(aid, int(start.timestamp()), int(end.timestamp()))
    if not rows:
        return None
    fi, cum = 0, 0.0
    skip_until = -1
    peak = peak_raw = None
    peak_at = None
    worst = None
    fl_min, fl_at, fl_pct = 0.0, None, None
    for ts, bal, eq, lo, hi in rows:
        while fi < len(flows) and flows[fi][0] < ts + 60:
            cum += flows[fi][1]
            fi += 1
            skip_until = ts + 60                      # 이 분과 다음 분은 뺌
        lo = lo if lo is not None else eq
        hi = hi if hi is not None else eq
        if ts <= skip_until:
            continue
        if lo - bal < fl_min:                         # 가장 큰 평가손 (열린 포지션 합) · 그때 잔고 대비 %
            fl_min, fl_at = lo - bal, ts
            fl_pct = (lo - bal) / bal if bal and bal > 0 else None
        a_hi, a_lo = hi - cum, lo - cum
        if peak is None or a_hi > peak:
            peak, peak_raw, peak_at = a_hi, hi, ts
        d = peak - a_lo
        if d > 0 and (worst is None or d > worst["amount"]):
            base = peak_raw if peak_raw and peak_raw > 0 else None
            worst = {"amount": d, "pct": d / base if base else None, "peak": peak_raw, "trough": lo,
                     "peak_at": peak_at, "trough_at": ts}
    out = {"since": rows[0][0], "minutes": len(rows), "float_min": fl_min, "float_min_at": fl_at, "float_min_pct": fl_pct}
    out.update(worst or {"amount": 0.0, "pct": 0.0, "peak": None, "trough": None, "peak_at": None, "trough_at": None})
    return out
