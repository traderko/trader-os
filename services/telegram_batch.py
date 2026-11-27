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


def report() -> str:
    from api.trade import batch_log
    from services.settings_service import telegram_cfg
    wait = telegram_cfg().get("trade_batch_sec", 0)
    lines = [f"📦 <b>나눠 체결 묶기: {wait}초</b>" + (" (꺼짐 - 하나씩)" if not wait else ""),
             f"같은 종목 · 같은 방향 체결이 앞 체결 도착 뒤 {wait}초 안에 또 오면 알림 하나로 묶어요 (최대 {wait * 3}초까지 기다림)."]
    log = batch_log()[-15:]
    if not log:
        lines.append("\n<i>서버를 켠 뒤 아직 체결이 없어요.</i>")
    else:
        lines.append("\n<b>최근 체결</b> (도착 · 앞 체결과 도착 간격 / 체결 시각 간격 · 결과)")
        prev = None
        for r in log:
            gap_a = gap_d = ""
            if prev and prev["symbol"] == r["symbol"] and prev["kind"] == r["kind"] and prev["side"] == r["side"]:
                gap_a = f"+{r['arrive'] - prev['arrive']:.1f}초"
                if r["deal"] and prev["deal"]:
                    gap_d = f" / 체결 +{r['deal'] - prev['deal']:.1f}초"
            res = (f"알림 {r['sent']}건 묶음" if r["sent"] and r["sent"] > 1 else "알림 1건") if r["sent"] else "앞 알림에 묶임"
            lines.append(f"{_hms(r['arrive'])} {html.escape(r['symbol'])} {'진입' if r['kind'] == 'OPEN' else '청산'} "
                         f"{r['side']} {r['vol']:g} · {gap_a or '-'}{gap_d} · {res}")
            prev = r
        lines.append("\n<i>도착 간격이 묶기 시간보다 길면 따로 와요. 체결 간격은 짧은데 도착 간격만 길면 서버가 늦게 받은 것이에요.</i>")
    lines.append("<i>바꾸기: /묶기 10 (0~30초)</i>")
    return "\n".join(lines)


async def cmd(live, client, token, cid: str, arg: str = "") -> None:
    a = (arg or "").strip().replace("초", "")
    if a.isdigit():
        from services.settings_service import update_settings
        v = max(0, min(30, int(a)))
        update_settings({"telegram": {"trade_batch_sec": v}})
    await live._send(client, token, cid, report())
