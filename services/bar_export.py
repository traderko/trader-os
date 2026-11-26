"""봉 데이터 내보내기 - 텔레그램 /bars (또는 /봉) 로 MT5 과거 봉을 zip 파일로 받음

쓰임: 돌파캔들(BOB) 뒤 주요 저점·고점까지 몇 KTR 인지 같은 연구를 밖(Claude 등)에서 계산할 때
  /bars          → 골드 · 나스닥, 최근 120일
  /bars 60       → 최근 60일
  /bars 90 gold  → 골드만 90일
내용: 종목마다 2분 · 5분 · 10분 · 1시간 · 2시간 봉 (진짜 시가 · 고가 · 저가 · 종가)
  CSV 열: time(UTC epoch 초), time_kst, open, high, low, close, tick_volume
  MT5 한 번에 5000봉 제한 → 날짜를 잘라 여러 번 받음. 브로커가 그만큼 과거 봉을 안 갖고 있으면 있는 만큼만
뒤에서 받아서 끝나면 파일로 보냄 (텔레그램 버튼 처리 · 다른 명령은 막지 않음)
"""
from __future__ import annotations

import asyncio
import csv
import io
import zipfile
from datetime import datetime, timedelta, timezone

from services import symbols

TFS = [("M2", 2), ("M5", 5), ("M10", 10), ("H1", 60), ("H2", 120)]
DEFAULT_DAYS = 120
MAX_DAYS = 400
_running = False


async def _aid() -> int | None:
    from services.breakout import _accounts
    from services.worker_client import worker_get
    for a in await _accounts():
        try:
            await asyncio.wait_for(worker_get(a.id, "/positions-all"), 8)
            return a.id
        except Exception:
            continue
    return None


async def fetch(aid: int, group: str, code: str, minutes: int, days: int, now: datetime) -> list[dict]:
    from services.worker_client import worker_get
    step = timedelta(days=max(1, int(3500 * minutes / 1440)))     # 한 번에 5000봉 안쪽
    t, end = now - timedelta(days=days), now + timedelta(minutes=minutes)
    seen: dict[int, dict] = {}
    while t < end:
        t2 = min(t + step, end)
        rows = await worker_get(aid, "/rates", {"symbol": group, "timeframe": code,
                                                "from_ts": t.isoformat(), "to_ts": t2.isoformat()}, timeout=60)
        for r in rows or []:
            seen[int(r["time"])] = r
        t = t2
    return [seen[k] for k in sorted(seen)]


def _csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "time_kst", "open", "high", "low", "close", "tick_volume"])
    for r in rows:
        w.writerow([r["time"], r.get("time_kst", ""), r["open"], r["high"], r["low"], r["close"], r.get("tick_volume", 0)])
    return buf.getvalue().encode("utf-8")


async def build(days: int, groups: list[str]) -> tuple[bytes, list[str]]:
    aid = await _aid()
    if aid is None:
        raise RuntimeError("MT5 에 붙은 계좌가 없어요 (MT5 가 꺼져 있거나 다시 켜지는 중)")
    now = datetime.now(timezone.utc)
    zbuf, lines = io.BytesIO(), []
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        for g in groups:
            parts = []
            for code, mins in TFS:
                try:
                    rows = await fetch(aid, g, code, mins, days, now)
                except Exception as e:
                    parts.append(f"{code} 실패({str(e)[:40]})")
                    continue
                z.writestr(f"{g}_{code}.csv", _csv(rows))
                first = datetime.fromtimestamp(rows[0]["time"], timezone.utc).strftime("%m/%d") if rows else "-"
                parts.append(f"{code} {len(rows):,}개(~{first})")
            lines.append(f"{symbols.display_name(g)}: " + " · ".join(parts))
    return zbuf.getvalue(), lines


async def _job(bot, token, cid: str, days: int, groups: list[str]) -> None:
    global _running
    import httpx
    async with httpx.AsyncClient() as client:
        await _job2(bot, client, token, cid, days, groups)


async def _job2(bot, client, token, cid: str, days: int, groups: list[str]) -> None:
    global _running
    try:
        data, lines = await build(days, groups)
        name = f"bars_{'_'.join(groups)}_{days}d_{datetime.now(timezone.utc):%Y%m%d}.zip"
        from services.telegram_live import API
        r = await client.post(API.format(token=token, method="sendDocument"),
                              data={"chat_id": cid, "caption": "📦 봉 데이터 (최근 %d일)\n%s" % (days, "\n".join(lines))[:1000]},
                              files={"document": (name, data, "application/zip")}, timeout=120)
        if not r.json().get("ok"):
            await bot._send(client, token, cid, f"⚠️ 파일을 보내지 못했어요: {r.json().get('description')}")
    except Exception as e:
        from services.telegram_live import command_error_text
        await bot._send(client, token, cid, command_error_text(e))
    finally:
        _running = False


async def cmd(bot, client, token, cid: str, args: str) -> None:
    global _running
    days, groups = DEFAULT_DAYS, []
    for a in args.split():
        if a.isdigit():
            days = max(1, min(MAX_DAYS, int(a)))
        elif symbols.group_by_key(a.lower()):
            groups.append(a.lower())
    groups = groups or [g for g in ("gold", "nasdaq") if symbols.group_by_key(g)]
    if _running:
        await bot._send(client, token, cid, "⏳ 이미 봉 데이터를 모으는 중이에요. 끝나면 파일로 보내 드릴게요.")
        return
    _running = True
    await bot._send(client, token, cid, f"📦 {', '.join(symbols.display_name(g) for g in groups)} 최근 {days}일 봉"
                                        f" (2분·5분·10분·1시간·2시간) 모으는 중… 1~3분 뒤 파일로 보내 드릴게요.")
    asyncio.create_task(_job(bot, token, cid, days, groups))
