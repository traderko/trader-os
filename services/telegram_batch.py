"""텔레그램 /묶기 (/batch) - 나눠 체결된 진입 · 청산 알림을 하나로 묶는 시간 · 최근 체결이 왜 따로 왔는지

  /묶기        : 지금 묶기 시간 + 최근 체결 알림 기록 (서버 도착 간격 · 체결 시각 간격 · 몇 건으로 묶였나)
  /묶기 10     : 10초 안에 이어서 들어온 같은 종목 · 방향 체결은 알림 하나로 (0 = 하나씩, 최대 30)

묶는 기준은 '서버에 도착한 간격'. MT5 에서 3초 안에 체결돼도 EA 가 서버에 알리는 게 늦으면(서버가 바쁠 때) 따로 올 수 있음
→ 기록의 '도착 간격'과 '체결 간격'이 다르면 그 경우 (api/trade.py _arrivals)
"""
from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def _hms(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, KST).strftime("%H:%M:%S") if ts else "-"


def _took(arrive: float) -> float | None:
    """그 체결 보고를 서버가 처리하는 데 걸린 시간 (POST /trades 기록에서 찾음)"""
    from api.trade import _req_log
    best, gap = None, 1.0                 # 알림 기록은 처리 끝 무렵에 남으므로 끝 시각이 가장 가까운 요청
    for s, e, _ in _req_log:
        if s - 0.2 <= arrive <= e + 1.0 and abs(e - arrive) < gap:
            best, gap = e - s, abs(e - arrive)
    return best


def report() -> str:
    from api.trade import batch_log, _lag_log
    from services.settings_service import telegram_cfg
    wait = telegram_cfg().get("trade_batch_sec", 0)
    lines = [f"📦 <b>나눠 체결 묶기: {wait}초</b>" + (" (꺼짐 - 하나씩)" if not wait else ""),
             f"같은 종목 · 같은 방향 체결이 앞 체결 도착 뒤 {wait}초 안에 또 오면 알림 하나로 묶어요 (최대 {wait * 3}초까지 기다림)."]
    log = batch_log()[-15:]
    if not log:
        lines.append("\n<i>서버를 켠 뒤 아직 체결이 없어요.</i>")
    else:
        lines.append("\n<b>최근 체결</b> (도착 · 앞 체결과 도착 간격 / 체결 시각 간격 · 체결→도착 · 서버 처리 · 결과)")
        prev = None
        for r in log:
            gap_a = gap_d = ""
            if prev and prev["symbol"] == r["symbol"] and prev["kind"] == r["kind"] and prev["side"] == r["side"]:
                gap_a = f"+{r['arrive'] - prev['arrive']:.1f}초"
                if r["deal"] and prev["deal"]:
                    gap_d = f" / 체결 +{r['deal'] - prev['deal']:.1f}초"
            res = (f"알림 {r['sent']}건 묶음" if r["sent"] and r["sent"] > 1 else "알림 1건") if r["sent"] else "앞 알림에 묶임"
            late = f" · 체결→도착 {r['arrive'] - r['deal']:.1f}초" if r.get("deal") else ""
            took = _took(r["arrive"])
            took_s = f" · 처리 {took:.1f}초{' ⚠️' if took >= 4 else ''}" if took is not None else ""
            lines.append(f"{_hms(r['arrive'])} {html.escape(r['symbol'])} {'진입' if r['kind'] == 'OPEN' else '청산'} "
                         f"{r['side']} {r['vol']:g} · {gap_a or '-'}{gap_d}{late}{took_s} · {res}")
            prev = r
        lines.append("\n<i>도착 간격이 묶기 시간보다 길면 따로 와요. 체결 간격은 짧은데 도착 간격만 길면 서버가 늦게 받은 것이에요.\n"
                     "EA 는 체결을 하나씩 보내고 서버 답을 5초까지 기다려요 - '처리' 가 길면(⚠️) 다음 체결이 그만큼 밀려요.</i>")
    import time as _time
    lags = [(t, s) for t, s in _lag_log if _time.time() - t < 3600]
    if lags:
        worst = sorted(lags, key=lambda x: -x[1])[:5]
        lines.append("\n⏸ <b>최근 1시간 서버가 멈췄던 때</b> (다른 작업이 붙잡음): "
                     + " · ".join(f"{_hms(t)} {s:.1f}초" for t, s in sorted(worst)))
    else:
        lines.append("\n✅ 최근 1시간 서버가 멈춘 적 없음 (0.5초 넘게)")
    lines.append("<i>바꾸기: /묶기 10 (0~30초)</i>")
    return "\n".join(lines)


async def cmd(live, client, token, cid: str, arg: str = "") -> None:
    a = (arg or "").strip().replace("초", "")
    if a.isdigit():
        from services.settings_service import update_settings
        v = max(0, min(30, int(a)))
        update_settings({"telegram": {"trade_batch_sec": v}})
    await live._send(client, token, cid, report())
