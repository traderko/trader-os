# services/grid.py
#
# 구간매매 (웹 화면 '구간' 탭) - KTR 값 구하기와 주문 계획(미리보기).
# 실제 주문은 계좌 워커(api/worker/worker_grid.py)가 MT5 로 넣는다.
#
# ── KTR ──────────────────────────────────────────────────────────────
#   장(아시아·유로·미장) 시작 봉의 크기. 플러터 앱 'KTR 매매'와 같은 계산:
#     유로장·미장 : 장 시작 첫 봉의 고가 - 저가
#     아시아장    : 장 시작 직전 봉 + 첫 봉 두 개를 합친 고가 - 저가 (장 시작 갭 포함)
#   장 시작 = 브로커 서버시간(뉴욕 + 7시간, 서머타임 자동) 아시아 01:00 / 유로 10:00 / 미국 16:30
#           = 한국 시간 07:00 / 16:00 / 22:30 (서머타임), 08:00 / 17:00 / 23:30 (그 외)
#   시간봉 2분·5분·10분·1시간·2시간, 시세는 이 계좌 MT5 또는 OANDA(관리 화면에 API 토큰).
#
# ── 주문 계획 ───────────────────────────────────────────────────────
#   첫 진입은 시장가, 그다음부터 KTR 간격으로 리밋 주문 (매수면 아래로, 매도면 위로). 손절은 모두 같은 가격.
#   KTR 거래   : 손실 %(잔고 대비)를 먼저 정하고 랏을 계산. 손절 = 첫 진입가에서 N KTR.
#                동일손실 - 모든 진입이 손절에서 같은 금액을 잃게 / 차등손실 - 남은 예산의 절반씩 (마지막은 전부)
#   일반 물타기 : 구간마다 같은 랏. 손절은 KTR 배수로 직접, 또는 손실 %로 자동
#                (N개가 다 체결된 채 손절에 닿으면 총손실 = 잔고 × %가 되는 거리)
#   1랏이 가격 1만큼 움직일 때 손익·증거금은 MT5 로 계산 (브로커 계약 크기·환율 반영).

import asyncio
import math
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")

SESSIONS = {   # id: (이름, 브로커 시각 시, 분)
    "asia": ("아시아장", 1, 0),
    "euro": ("유로장", 10, 0),
    "us": ("미장", 16, 30),
}
TIMEFRAMES = {"2m": ("M2", 2), "5m": ("M5", 5), "10m": ("M10", 10), "1h": ("H1", 60), "2h": ("H2", 120)}
OANDA_NAMES = {"gold": "XAU_USD", "nasdaq": "NAS100_USD", "oil": "WTICO_USD", "btc": "BTC_USD"}
OANDA_BASE = {"practice": "https://api-fxpractice.oanda.com/v3", "live": "https://api-fxtrade.oanda.com/v3"}
MAGIC_BASE = 71_700_000          # 구간매매 주문 magic = 71700000 ~ 71799999 (주문 묶음마다 하나)
#   71700000~71749999 = KTR 거래, 71750000~71799999 = 일반 물타기  (웹 화면도 같은 규칙: kindOfMagic)
MAX_LEVELS = 30


class GridError(Exception):
    pass


# ── 장 시작 시각 ──
def _broker_now(now: datetime) -> datetime:
    return now.astimezone(NY).replace(tzinfo=None) + timedelta(hours=7)


def _from_broker(b: datetime) -> datetime:
    return (b - timedelta(hours=7)).replace(tzinfo=NY).astimezone(KST)


def session_open(sid: str, now: datetime) -> datetime:
    """지금 이전 가장 최근의 그 장 시작 (KST) - 브로커 날짜 월~금"""
    _, hh, mm = SESSIONS[sid]
    b = _broker_now(now)
    for i in range(8):
        day = (b - timedelta(days=i)).date()
        if day.weekday() >= 5:
            continue
        t = datetime(day.year, day.month, day.day, hh, mm)
        if t <= b:
            return _from_broker(t)
    raise GridError("장 시작 시각을 찾지 못했습니다.")


def current_session(now: datetime) -> str:
    """가장 최근에 시작한 장"""
    return max(SESSIONS, key=lambda s: session_open(s, now))


# ── 캔들 ──
async def _candles_mt5(account_id: int, symbol: str, tf: str, start: datetime, end: datetime) -> list[dict]:
    from services.worker_client import worker_get
    rows = await worker_get(account_id, "/rates", {
        "symbol": symbol, "timeframe": TIMEFRAMES[tf][0],
        "from_ts": start.astimezone(timezone.utc).isoformat(), "to_ts": end.astimezone(timezone.utc).isoformat()})
    return [{"t": int(r["time"]), "h": float(r["high"]), "l": float(r["low"])} for r in rows]


def _candles_oanda_sync(instrument: str, tf: str, start: datetime, end: datetime) -> list[dict]:
    import requests
    from services.settings_service import get_settings
    cfg = get_settings().get("oanda") or {}
    token = (cfg.get("token") or "").strip()
    if not token:
        raise GridError("OANDA API 토큰이 없습니다. 관리 화면 → 텔레그램 탭 아래 'OANDA'에서 넣으세요.")
    base = OANDA_BASE.get(cfg.get("env") or "practice", OANDA_BASE["practice"])
    try:
        r = requests.get(f"{base}/instruments/{instrument}/candles", timeout=10,
                         headers={"Authorization": f"Bearer {token}"},
                         params={"granularity": TIMEFRAMES[tf][0], "price": "M",
                                 "from": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                 "to": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    except Exception as e:
        raise GridError(f"OANDA 연결 실패: {str(e).replace(token, '***')}")
    if r.status_code == 401:
        raise GridError("OANDA 토큰이 맞지 않습니다 (연습/실계좌 환경도 확인하세요).")
    if r.status_code != 200:
        raise GridError(f"OANDA 응답 {r.status_code}: {r.text[:200]}")
    out = []
    for c in r.json().get("candles", []):
        t = datetime.fromisoformat(c["time"].split(".")[0].replace("Z", "") + "+00:00")
        out.append({"t": int(t.timestamp()), "h": float(c["mid"]["h"]), "l": float(c["mid"]["l"])})
    return out


async def _candles(account_id: int, group: str, source: str, tf: str, start: datetime, end: datetime) -> list[dict]:
    if source == "oanda":
        inst = OANDA_NAMES.get(group)
        if not inst:
            raise GridError(f"OANDA 종목 이름을 모릅니다: {group}")
        candles = await asyncio.to_thread(_candles_oanda_sync, inst, tf, start, end)
    else:
        try:
            candles = await _candles_mt5(account_id, group, tf, start, end)
        except Exception as e:
            raise GridError(f"MT5 봉을 받지 못했습니다: {getattr(e, 'detail', e)}")
    return sorted(candles, key=lambda c: c["t"])


def _ktr_from(candles: list[dict], sid: str, opened: datetime, tf: str, source: str) -> dict:
    mins = TIMEFRAMES[tf][1]
    o = int(opened.timestamp())
    # 장 시작을 품은 봉 (없으면 그 뒤 첫 봉)
    idx = next((i for i in range(len(candles) - 1, -1, -1) if candles[i]["t"] <= o < candles[i]["t"] + mins * 60), None)
    if idx is None:
        idx = next((i for i, c in enumerate(candles) if c["t"] >= o), None)
    if idx is None:
        raise GridError(f"{SESSIONS[sid][0]} 시작({opened:%m/%d %H:%M}) 봉을 아직 찾지 못했습니다.")
    used = [candles[idx]]
    if sid == "asia":
        if idx == 0:
            raise GridError("아시아장 직전 봉을 찾지 못했습니다.")
        used.insert(0, candles[idx - 1])
    hi, lo = max(c["h"] for c in used), min(c["l"] for c in used)
    return {
        "ktr": hi - lo, "session": sid, "session_name": SESSIONS[sid][0], "open": opened.isoformat(),
        "tf": tf, "source": source, "high": hi, "low": lo,
        "candles": [{"time": datetime.fromtimestamp(c["t"], KST).isoformat(), "high": c["h"], "low": c["l"]} for c in used],
    }


def _window(opens: list[datetime], tf: str, now: datetime) -> tuple[datetime, datetime]:
    span = timedelta(minutes=TIMEFRAMES[tf][1])
    # 아시아장 직전 봉은 주말을 넘어갈 수 있어 넉넉히
    return min(opens) - timedelta(days=3) - span * 3, min(now, max(opens) + span * 3)


async def compute_ktr(account_id: int, group: str, broker_symbol: str, source: str, tf: str,
                      session: str | None = None, now: datetime | None = None) -> dict:
    if tf not in TIMEFRAMES:
        raise GridError("시간봉은 2m, 5m, 10m, 1h, 2h 중 하나입니다.")
    now = now or datetime.now(KST)
    sid = session if session in SESSIONS else current_session(now)
    opened = session_open(sid, now)
    start, end = _window([opened], tf, now)
    candles = await _candles(account_id, broker_symbol or group, source, tf, start, end)
    return _ktr_from(candles, sid, opened, tf, source)


async def ktr_table(account_id: int, group: str, source: str, now: datetime | None = None) -> dict:
    """장 3개 × 시간봉 5개 KTR 표. 시간봉마다 봉을 한 번만 받아서 세 장을 다 계산"""
    now = now or datetime.now(KST)
    opens = {sid: session_open(sid, now) for sid in SESSIONS}
    cells: dict = {sid: {} for sid in SESSIONS}
    errors: dict = {}
    for tf in TIMEFRAMES:
        try:
            start, end = _window(list(opens.values()), tf, now)
            candles = await _candles(account_id, group, source, tf, start, end)
        except GridError as e:
            errors[tf] = str(e)
            if "토큰" in str(e) or "OANDA 연결" in str(e):
                break                                   # 토큰 문제면 나머지도 같음
            continue
        for sid, opened in opens.items():
            try:
                r = _ktr_from(candles, sid, opened, tf, source)
                cells[sid][tf] = {"ktr": r["ktr"], "high": r["high"], "low": r["low"]}
            except GridError as e:
                cells[sid][tf] = {"error": str(e)}
    return {
        "symbol": group, "source": source, "current": current_session(now),
        "sessions": [{"id": sid, "name": SESSIONS[sid][0], "open": opens[sid].isoformat()} for sid in SESSIONS],
        "tfs": list(TIMEFRAMES), "cells": cells, "errors": errors,
    }


# ── 주문 계획 ──
def _floor_step(v: float, step: float) -> float:
    return round(math.floor(v / step + 1e-9) * step, 8)


def build_plan(*, mode: str, side: str, entry: float, ktr: float, n: int, point_value: float,
               margin_per_lot: float | None, equity: float, digits: int,
               vol_min: float, vol_step: float, vol_max: float,
               risk_pct: float | None = None, method: str = "equal",
               lot: float | None = None, stop_mode: str = "ktr", stop_ktr: float | None = None) -> dict:
    """구간별 가격·랏·손절 손실. point_value = 1랏이 가격 1만큼 움직일 때 손익(계좌 통화)"""
    side = "buy" if side.lower().startswith("b") else "sell"
    sgn = -1 if side == "buy" else 1                     # 다음 구간 방향 (매수는 아래로)
    if not (ktr > 0):
        raise GridError("KTR 값이 0 입니다.")
    if not (1 <= n <= MAX_LEVELS):
        raise GridError(f"구간 수는 1~{MAX_LEVELS} 입니다.")
    if point_value <= 0:
        raise GridError("이 종목의 1랏 손익을 계산하지 못했습니다.")
    warns: list[str] = []
    prices = [entry + sgn * ktr * i for i in range(n)]

    if mode == "ktr":
        if not (risk_pct and risk_pct > 0):
            raise GridError("손실 %를 넣으세요.")
        budget = equity * risk_pct / 100
        stop_dist = n * ktr
        raw, left = [], budget
        for i in range(n):
            dist = stop_dist - i * ktr
            alloc = budget / n if method == "equal" else (left if i == n - 1 else left / 2)
            left -= alloc
            raw.append(alloc / (dist * point_value))
        lots = []
        for v in raw:
            lv = min(_floor_step(v, vol_step), vol_max)
            if lv < vol_min:
                lv = vol_min
                if not any("최소 랏" in w for w in warns):
                    warns.append(f"손실 %에 비해 랏이 작아 최소 랏({vol_min:g})으로 올린 구간이 있습니다 - 실제 손실이 조금 큽니다.")
            lots.append(lv)
    elif mode == "avg":
        if not (lot and lot > 0):
            raise GridError("구간마다 넣을 랏을 넣으세요.")
        lv = min(max(_floor_step(lot, vol_step), vol_min), vol_max)
        if abs(lv - lot) > 1e-9:
            warns.append(f"랏을 브로커 단위에 맞춰 {lv:g}로 고쳤습니다.")
        lots = [lv] * n
        if stop_mode == "pct":
            if not (risk_pct and risk_pct > 0):
                raise GridError("손실 %를 넣으세요.")
            budget = equity * risk_pct / 100
            stop_dist = budget / (lv * point_value * n) + ktr * (n - 1) / 2
            if stop_dist <= ktr * (n - 1):
                need = lv * point_value * ktr * n * (n - 1) / 2 / equity * 100 if equity else 0
                raise GridError(f"손실 {risk_pct:g}%로는 손절이 마지막 구간보다 안쪽에 옵니다. "
                                f"손실 %를 {need:.2f}% 보다 크게 하거나 랏·구간 수를 줄이세요.")
        else:
            if not (stop_ktr and stop_ktr > 0):
                raise GridError("손절 거리(KTR 배수)를 넣으세요.")
            stop_dist = stop_ktr * ktr
            if stop_dist <= ktr * (n - 1):
                raise GridError(f"손절({stop_ktr:g} KTR)이 마지막 구간({n - 1} KTR)보다 안쪽입니다. {n - 1} 보다 크게 넣으세요.")
    else:
        raise GridError("거래 방식이 잘못됐습니다.")

    sl = entry + sgn * stop_dist
    levels, cum_lot, cum_px, cum_loss = [], 0.0, 0.0, 0.0
    for i, (p, lv) in enumerate(zip(prices, lots)):
        loss = lv * abs(sl - p) * point_value
        cum_lot += lv
        cum_px += lv * p
        cum_loss += loss
        levels.append({
            "no": i + 1, "price": round(p, digits), "lot": lv, "dist_ktr": round(abs(sl - p) / ktr, 3),
            "loss": loss, "cum_lot": round(cum_lot, 8), "avg": round(cum_px / cum_lot, digits), "cum_loss": cum_loss,
            "margin": margin_per_lot * cum_lot if margin_per_lot else None,
            "profit_1ktr": lv * ktr * point_value,
        })
    total_lot = cum_lot
    out = {
        "mode": mode, "side": side, "entry": round(entry, digits), "ktr": ktr, "n": n,
        "sl": round(sl, digits), "stop_dist": stop_dist, "stop_ktr": stop_dist / ktr,
        "levels": levels, "total_lot": round(total_lot, 8), "total_loss": cum_loss,
        "total_loss_pct": cum_loss / equity * 100 if equity else None,
        "margin_total": margin_per_lot * total_lot if margin_per_lot else None,
        "point_value": point_value, "equity": equity, "warnings": warns,
    }
    if mode == "ktr":
        out["budget"] = equity * risk_pct / 100
    return out


def new_magic(mode: str = "ktr") -> int:
    return MAGIC_BASE + (50_000 if mode == "avg" else 0) + random.randint(0, 49_999)


def kind_of(magic) -> str | None:
    """구간매매 주문이면 'ktr' / 'avg', 아니면 None (직접 거래)"""
    try:
        m = int(magic or 0)
    except (TypeError, ValueError):
        return None
    if not (MAGIC_BASE <= m < MAGIC_BASE + 100_000):
        return None
    return "avg" if m >= MAGIC_BASE + 50_000 else "ktr"


# ── 계좌 워커와 함께 (웹 API·텔레그램이 같이 씀) ─────────────────────────
MODE_KR = {"ktr": "KTR 거래", "avg": "일반 물타기"}


class GridLocked(GridError):
    pass


def _detail(e: Exception) -> str:
    d = getattr(e, "detail", None)
    if isinstance(d, str) and d.startswith("{"):
        try:
            import json
            d = json.loads(d).get("detail") or d
        except Exception:
            pass
    return str(d or e)


def _f(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise GridError(f"숫자가 아닙니다: {v}")


async def make_plan(account_id: int, body: dict) -> dict:
    """body: symbol, side, mode(ktr|avg), ktr, n, risk_pct, method, lot, stop_mode, stop_ktr - 지금 시세 기준 계획"""
    from services.worker_client import worker_get
    symbol = str(body.get("symbol") or "")
    side = "buy" if str(body.get("side", "buy")).lower().startswith("b") else "sell"
    ktr, n = _f(body.get("ktr")), int(_f(body.get("n")) or 0)
    if not symbol or not ktr:
        raise GridError("종목과 KTR 값을 넣으세요.")
    try:
        # 지금 시세 → 그 가격의 1랏 증거금, 1랏이 가격 1만큼 움직일 때 손익 (MT5 계산)
        probe = await worker_get(account_id, "/calc", {"symbol": symbol, "side": side, "volume": 1, "price_open": 1})
        entry = probe["ask"] if side == "buy" else probe["bid"]
        if not entry:
            raise GridError("지금 시세를 받지 못했습니다 (장이 닫혔을 수 있습니다).")
        sgn = 1 if side == "buy" else -1
        c = await worker_get(account_id, "/calc", {"symbol": symbol, "side": side, "volume": 1,
                                                   "price_open": entry, "price_close": entry + sgn})
    except GridError:
        raise
    except Exception as e:
        raise GridError(_detail(e))
    equity = float(c.get("equity") or c.get("balance") or 0)
    plan = build_plan(
        mode=str(body.get("mode") or "ktr"), side=side, entry=entry, ktr=ktr, n=n, point_value=abs(float(c.get("profit") or 0)),
        margin_per_lot=c.get("margin"), equity=equity, digits=int(c.get("digits") or 2),
        vol_min=float(c.get("volume_min") or 0.01), vol_step=float(c.get("volume_step") or 0.01),
        vol_max=float(c.get("volume_max") or 100), risk_pct=_f(body.get("risk_pct")),
        method=str(body.get("method") or "equal"), lot=_f(body.get("lot")),
        stop_mode=str(body.get("stop_mode") or "ktr"), stop_ktr=_f(body.get("stop_ktr")))
    plan.update(symbol=c["symbol"], digits=c.get("digits"), margin_free=c.get("margin_free"),
                currency=c.get("currency"), bid=c.get("bid"), ask=c.get("ask"), method=str(body.get("method") or "equal"))
    if plan.get("margin_total") and c.get("margin_free") is not None and plan["margin_total"] > c["margin_free"]:
        plan["warnings"].append("다 체결되면 필요한 증거금이 지금 여유 증거금보다 큽니다.")
    return plan


async def execute(db, acc, body: dict, via: str = "웹") -> dict:
    """거래 잠금 확인 → 지금 시세로 다시 계산 → 주문 → 텔레그램 알림"""
    from services.worker_client import worker_post
    if acc.lock_enabled:
        from services import trade_lock_service as lock_svc
        st = await lock_svc.get_lock_state(db, acc.id)
        if st.get("locked"):
            raise GridLocked("거래가 잠겨 있습니다. 잠금을 먼저 풀고 다시 하세요.")
    plan = await make_plan(acc.id, body)
    try:
        res = await worker_post(acc.id, "/grid/place", {
            "symbol": plan["symbol"], "side": plan["side"], "ktr": plan["ktr"], "stop_dist": plan["stop_dist"],
            "lots": [lv["lot"] for lv in plan["levels"]], "magic": new_magic(plan["mode"]),
            "comment": "KTR" if plan["mode"] == "ktr" else "AVG"})
    except Exception as e:
        raise GridError(_detail(e))
    try:
        from services.fcm_service import get_fcm_service
        get_fcm_service().send(f"🧱 구간매매 시작 ({MODE_KR.get(plan['mode'], plan['mode'])} · {via})",
                               "\n".join(result_lines(acc.account_number, plan, res)))
    except Exception as e:
        print(f"[grid] 알림 실패: {e}")
    return {"plan": plan, "result": res}


def result_lines(account_number, plan: dict, res: dict) -> list[str]:
    d = res.get("digits") or plan.get("digits") or 2
    lines = [f"계좌 {account_number} · {res['symbol']} {res['side'].upper()} · KTR {plan['ktr']:.{d}f} × {plan['n']}구간",
             f"진입 {res['fill']:.{d}f} · 손절 {res['sl']:.{d}f} · 총 {plan['total_lot']:g}lot",
             f"다 체결되고 손절이면 {-plan['total_loss']:,.2f}"
             + (f" ({-plan['total_loss_pct']:.1f}%)" if plan.get("total_loss_pct") is not None else "")]
    if res.get("failed"):
        lines.append(f"⚠ 리밋 {len(res['failed'])}개 실패: " + ", ".join(f"{x['no']}번 {x['error']}" for x in res["failed"][:3]))
    return lines


async def grids(account_id: int) -> list[dict]:
    from services.worker_client import worker_get
    try:
        return await worker_get(account_id, "/grid/list")
    except Exception as e:
        raise GridError(_detail(e))


async def close(account_id: int, magic: int, pending_only: bool) -> dict:
    from services.worker_client import worker_post
    try:
        return await worker_post(account_id, "/grid/close", {"magic": magic, "pending_only": pending_only})
    except Exception as e:
        raise GridError(_detail(e))
