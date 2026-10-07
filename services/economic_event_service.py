import asyncio
import json
import os
import time as _time
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from fastapi import Depends
from services.fcm_service import FCMService, get_fcm_service
from mt5.mt5Client import Mt5Client
from apscheduler.schedulers.background import BackgroundScheduler
import requests
from db.upsert import upsert_insert as insert

from db.models.economic_event import EconomicEvent
from db.session import AsyncSessionLocal

#

class EconomicEventService:
    KST = ZoneInfo(Mt5Client.KOREA_TIMEZONE_STR)

    def __init__(self, scheduler : BackgroundScheduler, fcm : FCMService):
        self.scheduler = scheduler
        self.fcm = fcm
        # check()는 BackgroundScheduler 스레드에서 돌기 때문에, DB 저장(비동기 엔진)은
        # 엔진이 붙어 있는 메인 이벤트 루프로 넘겨서 실행한다. (lifespan 안에서 생성된다는 전제)
        self.loop = asyncio.get_running_loop()

    def check(self):
        data = self._fetch_events()

        events = data.get("data", [])

        rows = []

        for event in events:
            # ✅ 조건: 고영향 + USD
            if not (event.get("impact") == 3 and event.get("currency") == "USD"):
                continue

            event_name = event.get("event_kor") or event.get("event")
            currency = event.get("currency")
            utc_str = event.get("event_timestamp")

            if not utc_str:
                continue

            # 🔥 UTC → KST 변환
            dt_utc = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
            event_time = dt_utc.astimezone(EconomicEventService.KST)

            # 👉 알림 등록
            self._schedule_event(event_time, event_name, currency)

            rows.append({
                "event_name": event_name,
                "currency": currency,
                "impact": 3,
                "event_time": dt_utc,
                "broker_date": self._broker_date(dt_utc),
            })

        # 👉 거래 잠금(아시아장 집중 구간)용으로 DB에 저장
        if rows:
            future = asyncio.run_coroutine_threadsafe(self._save(rows), self.loop)
            future.result(timeout=30)

    # ── 두 번째 출처: Forex Factory 공개 피드 ──────────────────────────
    # 거래 잠금(지표일 판단) 전용. einfomax가 끊기거나 중요도 기준이 달라 빠지는 지표를 보완한다.
    # 알림(_schedule_event)은 einfomax 쪽에서만 걸어서 같은 지표 푸시가 두 번 오지 않게 함.
    # 피드가 요청 빈도를 제한하므로 1시간 간격으로만 호출할 것 (main.py 스케줄 참고).
    FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

    # 서버를 자주 재시작하면 시작할 때마다 받아 와서 429(요청 너무 많음)가 난다.
    # 마지막으로 받은 시각을 파일에 남겨 두고 1시간 안에는 다시 받지 않는다 (이미 DB에 저장돼 있음).
    FF_STATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ff_fetch.json")
    FF_MIN_INTERVAL = 55 * 60

    def _ff_state(self) -> dict:
        try:
            return json.load(open(self.FF_STATE, encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _ff_save_state(self, **kw) -> None:
        st = self._ff_state() | kw
        try:
            os.makedirs(os.path.dirname(self.FF_STATE), exist_ok=True)
            json.dump(st, open(self.FF_STATE, "w", encoding="utf-8"))
        except OSError:
            pass

    def check_forexfactory(self):
        now = _time.time()
        st = self._ff_state()
        if now - st.get("ok_at", 0) < self.FF_MIN_INTERVAL or now < st.get("retry_after", 0):
            return   # 최근에 받았거나 차단 대기 중 - 저장된 일정 그대로 사용

        response = requests.get(self.FF_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        if response.status_code == 429:
            try:
                wait = int(response.headers.get("Retry-After", ""))
            except ValueError:
                wait = 3600
            self._ff_save_state(retry_after=now + max(wait, 600))
            print(f"[ff] Forex Factory 요청 제한(429) - {max(wait, 600) // 60}분 뒤에 다시 받습니다. 저장된 일정은 그대로 씁니다.")
            return
        response.raise_for_status()
        self._ff_save_state(ok_at=now, retry_after=0)

        rows = []

        for event in response.json():
            # impact: High / Medium / Low / Holiday, country: 통화 코드(USD 등)
            if not (event.get("impact") == "High" and event.get("country") == "USD"):
                continue

            date_str = event.get("date")  # 예: 2026-10-02T08:30:00-04:00
            if not date_str:
                continue

            dt_utc = datetime.fromisoformat(date_str).astimezone(ZoneInfo("UTC"))

            rows.append({
                "event_name": f"[FF] {event.get('title', '').strip()}",
                "currency": "USD",
                "impact": 3,
                "event_time": dt_utc,
                "broker_date": self._broker_date(dt_utc),
            })

        if rows:
            future = asyncio.run_coroutine_threadsafe(self._save(rows), self.loop)
            future.result(timeout=30)

    @staticmethod
    def _broker_date(dt_utc: datetime):
        """지표가 속한 거래일 = 브로커 서버시간 기준 날짜"""
        offset = Mt5Client._get_broker_offset(dt_utc)
        return (dt_utc + timedelta(hours=offset)).date()

    async def _save(self, rows: list[dict]):
        # 같은 (이름, 시각)이 한 배치에 두 번 있으면 ON CONFLICT가 에러를 내므로 먼저 중복 제거
        rows = list({(r["event_name"], r["event_time"]): r for r in rows}.values())

        async with AsyncSessionLocal() as db:
            stmt = insert(EconomicEvent).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["event_name", "event_time"],  # uq_economic_event_name_time
                set_={
                    "currency": stmt.excluded.currency,
                    "impact": stmt.excluded.impact,
                    "broker_date": stmt.excluded.broker_date,
                },
            )
            await db.execute(stmt)
            await db.commit()

    def _schedule_event(self, event_time, event_name, currency):
        for minutes_before in [120, 90, 60, 30, 10]:
            notify_time = event_time - timedelta(minutes=minutes_before)

            if notify_time <= datetime.now(EconomicEventService.KST):
                continue

            safe_name = event_name.replace(" ", "_")

            job_id = f"{safe_name}_{event_time}_{minutes_before}"

            if self.scheduler.get_job(job_id):
                continue  # 이미 등록됨 → 스킵

            self.scheduler.add_job(
                self.fcm.send,
                trigger="date",
                run_date=notify_time,
                args=[
                    f"[{currency}] 경제지표 발표 예정",
                    f"{event_name} ({minutes_before}분 전)"
                ],
                id=job_id
            )

    def _fetch_events(self):
        url = "https://eco-calendar.einfomax.co.kr/eco/master2"

        now = datetime.now(EconomicEventService.KST)

        start = (now + timedelta(days=-1)).strftime("%Y-%m-%d")
        end = (now + timedelta(days=2)).strftime("%Y-%m-%d")

        payload = {
            "startDate": f"{start}",
            "endDate": f"{end}",
            "country": ["1"],
            "impact": ["3"],
            "company": "ss"
        }

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://eco-calendar.einfomax.co.kr/samsung_mobile.html",
        }

        response = requests.post(url, json=payload, headers=headers)

        response.raise_for_status()

        return response.json()
    