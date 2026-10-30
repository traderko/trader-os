# services/settings_service.py
#
# 관리 화면(/admin)에서 바꾸는 설정을 data/settings.json 한 파일에 저장한다.
# 코드에 상수로 박혀 있던 잠금 규칙, 텔레그램 토큰 등을 여기로 옮겼다.
#
# - 메인 서버와 계좌별 워커 프로세스가 같은 파일을 읽는다.
#   파일 수정 시각(mtime)이 바뀌면 다시 읽기 때문에, 화면에서 저장하면 재시작 없이 바로 반영된다.
# - 파일이 없거나 항목이 빠져 있으면 DEFAULTS 값을 쓴다. (기존 동작과 같은 값)
# - 텔레그램 토큰이 비어 있으면 .env(TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)를 대신 쓴다.

import copy
import json
import os
import threading

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")

WEEKDAY_KR = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]

DEFAULTS: dict = {
    "lock": {
        # 연속손절 잠금
        "consec_loss_threshold": 3,          # 이 횟수 이상 연속 손절이면 잠금
        "consec_loss_lookback_hours": 24,    # 최근 몇 시간 안의 거래만 셈
        "consec_loss_unlock_minutes": 60,    # 문구 입력 시 풀리는 시간
        # 정기 재확인
        # 정기 재확인 규칙 목록 (관리 화면에서 추가·삭제). 시간대끼리 겹치면 안 됨.
        #   start_min / end_min: 한국 시간 기준 분(0~1410, 30분 단위). 끝이 시작보다 작으면 자정을 넘김.
        #   mode: "minutes" → 구간 시작부터 every분마다 / "bars" → MT5 every시간봉 마감마다(브로커 시간 기준)
        "checkin_rules": [
            {"id": "night", "name": "새벽", "enabled": True,
             "start_min": 0, "end_min": 420, "mode": "minutes", "every": 30},
        ],
        # 위 규칙에 해당하지 않는 나머지 시간
        "checkin_default": {"mode": "bars", "every": 2},
        # 집중 구간 목록 - 이 시간에는 전용 문구를 입력해야 거래 가능 (관리 화면에서 추가·삭제)
        #   start_min / end_min: 브로커 서버시간 기준 분(0~1440). 서머타임은 자동 반영된다.
        #     KST = 브로커 시간 + 6시간(서머타임 중) / + 7시간(그 외)
        #   weekdays: 0=월 … 6=일 (브로커 시간 기준 거래일), event_days: 고영향 USD 지표일도 포함
        "sessions": [
            {
                "id": "asia", "name": "아시아장", "enabled": True,
                "start_min": 60, "end_min": 600,          # 브로커 01:00~10:00 = KST 07~16시(서머타임)
                "weekdays": [3, 4], "event_days": True, "unlock_minutes": 30,
                "event_key_only": True,                   # True: 고영향 지표 중 ⭐ 주요 지표(events.highlight)가 있는 날만
            },
        ],
        # 수동 잠금
        "manual_lock_max_minutes": 24 * 60,
        # 주말(브로커 시간 토·일 = 한국 토 06/07시 ~ 월 06/07시)에는 정기 재확인·집중 구간 잠금과 그 알림을 쉼
        #   수동 잠금·연속손절·일일 손실 한도 잠금은 주말에도 그대로
        "weekend_off": True,
        # 일일 손실 한도 - 거래일(한국 시간 06:00) 시작 평가금 대비 손실률 (services/loss_limit.py)
        #   warn  : 텔레그램 경고
        #   block : 새 진입 차단 (다음 거래일 06:00까지 풀 수 없는 잠금)
        #   close : 모든 포지션 청산 + 다음 거래일까지 잠금
        #   0 이면 그 단계는 쓰지 않음
        "daily_loss_enabled": False,
        "daily_loss_warn_pct": 5,
        "daily_loss_block_pct": 7,
        "daily_loss_close_pct": 10,
    },
    "telegram": {
        "bot_token": "",
        "chat_id": "",
        # 실시간 현황: 대화방 맨 위에 고정한 메시지 하나를 계속 고쳐 씀 (services/telegram_live.py)
        "live_enabled": True,
        "live_interval_sec": 10,
        "live_position": "top",      # top: 맨 위 고정 / bottom: 항상 마지막 메시지로 유지
        "live_grid_button": False,   # 실시간 현황 아래 [🧱 구간매매] 버튼 보이기 (기본 숨김 - /grid 는 그대로)
        # 진입·청산 알림 (api/trade.py)
        "trade_sim": True,           # 알림 아래에 랏 배수 시뮬레이션 ("10배였다면" 손익·증거금·오늘 누적)
        "trade_sim_mult": [10],      # 몇 배로 볼지 (여러 개 가능, 예: [5, 10])
        "trade_batch_sec": 3,        # 이 초 안에 같은 종목·방향으로 나눠 체결된 건 알림 하나로 (0 = 하나씩)
        # 근거 알림 - 근거 태그가 빠진 최근 거래를 모아서 보냄 (services/telegram_tags.py, 없으면 안 보냄)
        #   mode times   : 매일 times 의 시각에 (한국 시간 "HH:MM")
        #   mode interval: start~end 시간대 안에서 every_hours 시간마다 (start 부터)
        #   lookback_hours: 몇 시간 안에 진입한 거래를 볼지 (/근거 명령의 기본 기간도 이것)
        "reason_remind": {"enabled": False, "mode": "times", "times": ["22:00"],
                          "every_hours": 2, "start": "08:00", "end": "23:00", "lookback_hours": 24},
    },
    # 바깥(폰 등)에서 웹 화면·API 접속 (services/access_service.py)
    #   open      : 제한 없음 (예전 동작)
    #   tailscale : Tailscale 주소(100.64.0.0/10)와 이 PC에서 온 요청만
    #   public    : 도메인·고정 IP 서버 - 비밀번호 로그인 필요 (HTTPS 리버스 프록시 뒤에서)
    "access": {
        # 처음 설치했을 때의 접속 방식. 배포판 start.bat 은 TRADEROS_ACCESS_DEFAULT=tailscale 로 실행해서
        # 같은 와이파이의 다른 기기도 기본으로는 못 들어오게 한다 (관리 화면 '접속' 탭에서 바꾸면 그 값이 저장됨)
        "mode": os.getenv("TRADEROS_ACCESS_DEFAULT", "").strip() if os.getenv("TRADEROS_ACCESS_DEFAULT", "").strip() in ("open", "tailscale") else "open",
        "password_hash": "",
        "session_days": 30,
    },
    # 트레이딩 지침 (services/guidelines.py) - 상황이 생긴 순간 맞는 지침을 텔레그램으로
    "guidelines": {
        "enabled": True,
        "cooldown_min": 30,          # 같은 지침은 이 시간 안에 다시 보내지 않음
        "entry_gap_sec": 60,         # 같은 종목·방향 체결이 직전 체결에서 이 시간 안이면 한 번의 진입 (0.5랏씩 다다다 → 합쳐서 하나)
        "items": [
            "돈을 가져오는 매매가 아닌 돈을 주는 매매를 하고 있는가?",
            "손실보다 수익을 먼저 생각하고 있는가?",
            "길게 먹는 것이 내 실력이라고 생각하고 있는가?",
            "예수금을 숫자가 아닌 생활비라고 생각하고 있는가?",
            "도 닦는다는 심정으로 거래에 임하고 있는가?",
            "자기확신과 욕심으로 손절 세팅을 하지 않았는가?",
            "시간 당 수십 번의 거래를 하고 있는가?",
            "차트에서 보여준 근거보다 나의 희망회로가 더 우선시되는가?",
            "시간봉보다 분봉을 메인으로 보고 있는가?",
            "도파민 뇌동매매, 복수매매를 하고 있는가?",
            "미장 직전후, 지표발표를 앞두고 포지션 정리를 하였는가?",
            "나의 최대손실 비율을 허용하였는가?",
            "시나리오가 없는 매매를 하고 있는가?",
            "되돌리지 않고, 바로 가지 않는 곳에서 비중을 키웠는가?",
            "너무 많은 손실을 감내하고 있는가?",
            "목요일(지표발표일) 아시아장에서 역매매하고 있는가?",
        ],
        "rules": {
            "revenge":      {"enabled": True, "text": "도파민 뇌동매매, 복수매매를 하고 있는가?", "minutes": 10},
            "overtrade":    {"enabled": True, "text": "시간 당 수십 번의 거래를 하고 있는가?", "count": 5, "minutes": 60},
            "no_sl":        {"enabled": True, "text": "자기확신과 욕심으로 손절 세팅을 하지 않았는가?", "seconds": 60, "repeat": 0},
            "event_hold":   {"enabled": True, "text": "장 시작(아시아·유로·미장) 직전, 지표발표를 앞두고 포지션 정리를 하였는가?", "minutes": 30, "repeat": 10,
                             "asia_open": True, "euro_open": True, "us_open": True},
            "margin_use":   {"enabled": True, "text": "되돌리지 않고, 바로 가지 않는 곳에서 비중을 키웠는가?\n너무 많은 손실을 감내하고 있는가?",
                             "levels": [10, 20, 30], "repeat": 0},
            "loss_warn":    {"enabled": True, "text": "나의 최대손실 비율을 허용하였는가?\n너무 많은 손실을 감내하고 있는가?"},
            "same_price":   {"enabled": True, "text": "차트에서 보여준 근거보다 나의 희망회로가 더 우선시되는가?", "minutes": 30, "count": 3, "pct": 0.2},
            "size_jump":    {"enabled": True, "text": "되돌리지 않고, 바로 가지 않는 곳에서 비중을 키웠는가?", "ratio": 2.0, "lookback": 20},
            "asia_counter": {"enabled": True, "text": "목요일·금요일·지표발표일 아시아장에서 역매매하고 있는가?", "weekdays": [3, 4], "event_days": True},
            "first_entry":  {"enabled": True, "text": "", "count": 2},
            # 지침 목록에서 무작위 시각마다 count 개 (gap_min~gap_max 분 사이 무작위 간격, start~end 시간대, 정한 요일만)
            "random_items": {"enabled": False, "text": "", "count": 1, "gap_min": 60, "gap_max": 180,
                             "start": "08:00", "end": "23:00", "weekdays": [0, 1, 2, 3, 4]},
        },
    },
    # OANDA 시세 (구간매매 KTR 계산용, services/grid.py) - API 토큰은 OANDA 계정 → Manage API Access 에서 발급
    "oanda": {"token": "", "env": "practice"},   # env: practice(연습 계좌) | live(실계좌)
    # 구간매매 (KTR 배수)
    #   ktr_stop_gap: KTR 거래 손절 = 마지막 구간에서 이만큼 더 간 곳 (주문할 때 바꿀 수 있음)
    #   min_gap_ktr : 고정 랏 분할에서 구간 수 자동일 때 마지막 구간과 손절선 사이 최소 여유
    "grid": {"ktr_stop_gap": 1.0, "min_gap_ktr": 1.0,
             # 첫 진입 추가 리밋 (services/addon.py): 첫 진입 평균가에서 addon_mult KTR 불리한 곳에 같은 랏 리밋 1개
             #   addon_auto: 자동으로 걸기 / addon_ask: 텔레그램에 KTR 표 + 시간봉 버튼 (누르면 걸림)
             #   addon_lot: 리밋 하나의 랏 - fill(체결 한 번 크기) | total(첫 진입 합계) / addon_n: 몇 개 (평균가에서 거리 KTR 마다 하나씩) / addon_tf: 자동일 때 쓰는 시간봉 (2m 5m 10m 1h) / addon_sl_follow: 첫 진입의 SL 따라가기 / addon_src: 시세 mt5|oanda
             "addon_auto": False, "addon_ask": True, "addon_tf": "10m", "addon_mult": 1.0, "addon_n": 1, "addon_lot": "fill", "addon_sl_follow": True,
             "addon_src": "mt5"},
    # 경제지표 - 이름에 이 단어가 들어가면 '주요 지표'로 강조 (services/key_events.py). 대소문자 구분 없음
    "events": {
        "highlight": [
            "FOMC", "금리", "연준 의장", "파월", "워시", "실업수당", "원유", "비농업", "실업률", "CPI", "소비자물가", "PCE", "GDP",
            "Federal Funds", "Fed Chair", "Powell", "Warsh", "Non-Farm", "Unemployment", "Crude Oil",
        ],
    },
    # 관리 화면(:8100)을 다른 기기에서 열기 (services/admin_remote.py) - 기본 꺼짐, 켜기·비밀번호는 이 PC에서만
    "admin_remote": {
        "enabled": False,
        "password_hash": "",
        "session_days": 7,
    },
}

# (최소, 최대) - 관리 화면에서 잘못된 값을 넣어 잠금이 무력화되지 않게
_LOCK_RANGES = {
    "consec_loss_threshold": (1, 20),
    "consec_loss_lookback_hours": (1, 168),
    "consec_loss_unlock_minutes": (1, 24 * 60),
    "manual_lock_max_minutes": (1, 7 * 24 * 60),
    "daily_loss_warn_pct": (0, 50),
    "daily_loss_block_pct": (0, 50),
    "daily_loss_close_pct": (0, 50),
}

_LABELS = {
    "consec_loss_threshold": "연속 손절 횟수",
    "consec_loss_lookback_hours": "연속 손절 계산 기간(시간)",
    "consec_loss_unlock_minutes": "연속 손절 해제 시간(분)",
    "manual_lock_max_minutes": "수동 잠금 최대 시간(분)",
    "daily_loss_warn_pct": "손실 경고(%)",
    "daily_loss_block_pct": "새 진입 차단(%)",
    "daily_loss_close_pct": "전부 청산(%)",
}

_lock = threading.Lock()
_cache: dict | None = None
_cache_mtime: float | None = None


class SettingsError(ValueError):
    pass


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


MAX_SESSIONS = 10
_LEGACY_SESSION_KEYS = ("session_enabled", "session_weekdays", "session_event_days", "session_unlock_minutes")


_LEGACY_CHECKIN_KEYS = ("night_start_hour", "night_end_hour", "night_block_minutes", "day_block_hours")
BAR_HOURS = (1, 2, 3, 4, 6, 8, 12)      # MT5에 있는 시간봉 (H1~H12)
MAX_CHECKIN_RULES = 10


def _migrate_legacy(data: dict) -> dict:
    """예전 설정을 지금 형식으로 옮김.
    - 아시아장 하나(session_enabled/weekdays/…) → sessions 목록의 첫 항목
    - 새벽 시간대 하나(night_start_hour/…) + day_block_hours → checkin_rules / checkin_default"""
    lock = data.get("lock")
    if not isinstance(lock, dict):
        return data
    if "checkin_rules" not in lock and any(k in lock for k in _LEGACY_CHECKIN_KEYS):
        sh, eh = lock.get("night_start_hour", 0), lock.get("night_end_hour", 7)
        every = lock.get("night_block_minutes", 30)
        lock["checkin_rules"] = [] if sh == eh else [
            {"id": "night", "name": "새벽", "enabled": True,
             "start_min": sh * 60, "end_min": eh * 60, "mode": "minutes", "every": every}]
    if "checkin_default" not in lock and "day_block_hours" in lock:
        lock["checkin_default"] = {"mode": "bars", "every": lock["day_block_hours"]}
    for k in _LEGACY_CHECKIN_KEYS:
        lock.pop(k, None)
    if "sessions" not in lock and any(k in lock for k in _LEGACY_SESSION_KEYS):
        asia = copy.deepcopy(DEFAULTS["lock"]["sessions"][0])
        asia["enabled"] = lock.get("session_enabled", asia["enabled"])
        asia["weekdays"] = lock.get("session_weekdays", asia["weekdays"])
        asia["event_days"] = lock.get("session_event_days", asia["event_days"])
        asia["unlock_minutes"] = lock.get("session_unlock_minutes", asia["unlock_minutes"])
        lock["sessions"] = [asia]
    for k in _LEGACY_SESSION_KEYS:
        lock.pop(k, None)
    return data


def _read_file() -> dict:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        # 파일이 깨져도 서버(=잠금)는 계속 돌아야 하므로 기본값으로 동작
        print(f"[settings] settings.json 읽기 실패, 기본값 사용: {e}")
        return {}


def get_settings() -> dict:
    """현재 설정 전체 (기본값 + 파일). 파일이 바뀌었으면 다시 읽는다."""
    global _cache, _cache_mtime
    try:
        mtime = os.path.getmtime(SETTINGS_PATH)
    except OSError:
        mtime = None

    with _lock:
        if _cache is None or mtime != _cache_mtime:
            _cache = _deep_merge(DEFAULTS, _migrate_legacy(_read_file()))
            _cache_mtime = mtime
        return copy.deepcopy(_cache)


def lock_cfg() -> dict:
    return get_settings()["lock"]


def telegram_cfg() -> dict:
    t = get_settings()["telegram"]
    return {
        "bot_token": (t.get("bot_token") or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip(),
        "chat_id": str(t.get("chat_id") or os.getenv("TELEGRAM_CHAT_ID", "")).strip(),
        "live_enabled": bool(t.get("live_enabled", True)),
        "live_interval_sec": int(t.get("live_interval_sec") or 10),
        "live_position": "bottom" if t.get("live_position") == "bottom" else "top",
        "live_grid_button": bool(t.get("live_grid_button", False)),
        "trade_sim": bool(t.get("trade_sim", True)),
        "trade_batch_sec": int(t.get("trade_batch_sec", 3) or 0),
        "trade_sim_mult": [float(x) for x in (t.get("trade_sim_mult") or []) if isinstance(x, (int, float))],
    }


def _validate(settings: dict) -> None:
    lock = settings["lock"]
    for key, (lo, hi) in _LOCK_RANGES.items():
        v = lock.get(key)
        if not isinstance(v, int) or isinstance(v, bool) or not (lo <= v <= hi):
            raise SettingsError(f"{_LABELS.get(key, key)}은(는) {lo}~{hi} 사이 정수여야 합니다. (입력: {v})")

    lock["daily_loss_enabled"] = bool(lock.get("daily_loss_enabled", False))
    lock["weekend_off"] = bool(lock.get("weekend_off", True))
    steps = [lock[k] for k in ("daily_loss_warn_pct", "daily_loss_block_pct", "daily_loss_close_pct") if lock[k] > 0]
    if steps != sorted(steps) or len(set(steps)) != len(steps):
        raise SettingsError("일일 손실 한도는 경고 < 새 진입 차단 < 전부 청산 순서로 커져야 합니다. (안 쓰는 단계는 0)")
    if lock["daily_loss_enabled"] and not steps:
        raise SettingsError("일일 손실 한도를 켜려면 단계를 하나 이상 정하세요.")

    lock["sessions"] = _validate_sessions(lock.get("sessions"))
    lock["checkin_rules"] = _validate_checkin_rules(lock.get("checkin_rules"))
    lock["checkin_default"] = _validate_interval(lock.get("checkin_default"), "그 밖의 시간")

    tg = settings["telegram"]
    tg["bot_token"] = str(tg.get("bot_token") or "").strip()
    tg["chat_id"] = str(tg.get("chat_id") or "").strip()
    tg["live_enabled"] = bool(tg.get("live_enabled", True))
    iv = tg.get("live_interval_sec", 10)
    if not _int(iv) or not (5 <= iv <= 300):
        raise SettingsError(f"실시간 현황 갱신 간격은 5~300초 사이 정수여야 합니다. (입력: {iv})")
    if tg.get("live_position", "top") not in ("top", "bottom"):
        raise SettingsError("실시간 현황 위치는 top 또는 bottom 이어야 합니다.")
    tg["live_grid_button"] = bool(tg.get("live_grid_button", False))
    tg["trade_sim"] = bool(tg.get("trade_sim", True))
    mult = tg.get("trade_sim_mult", [10])
    if isinstance(mult, str):
        mult = [x for x in mult.replace("배", "").replace("x", "").replace("×", "").replace(" ", "").split(",") if x]
    try:
        mult = [float(x) for x in (mult or [])]
    except (TypeError, ValueError):
        raise SettingsError("시뮬레이션 배수는 숫자로 넣어 주세요. (예: 5, 10)")
    if len(mult) > 5 or any(not (0.01 <= x <= 1000) for x in mult):
        raise SettingsError("시뮬레이션 배수는 0.01~1000 사이로 5개까지 넣을 수 있습니다.")
    tg["trade_sim_mult"] = [int(x) if x == int(x) else x for x in mult]
    bs = tg.get("trade_batch_sec", 3)
    if not _int(bs) or not (0 <= bs <= 30):
        raise SettingsError(f"나눠 체결된 알림 묶기는 0~30초여야 합니다. (입력: {bs})")
    rr = tg.get("reason_remind") or {}
    rr["enabled"] = bool(rr.get("enabled", False))
    if rr.get("mode", "times") not in ("times", "interval"):
        raise SettingsError("근거 알림 방식은 times(정해진 시각) 또는 interval(주기)이어야 합니다.")
    times = rr.get("times", ["22:00"])
    if isinstance(times, str):
        times = times.replace("\n", ",").split(",")
    times = sorted(dict.fromkeys(_hhmm(t, "근거 알림 시각") for t in times if str(t).strip()))
    if rr["mode"] == "times" and rr["enabled"] and not times:
        raise SettingsError("근거 알림 시각을 하나 이상 넣으세요. (예: 22:00)")
    if len(times) > 12:
        raise SettingsError("근거 알림 시각은 12개까지 넣을 수 있습니다.")
    rr["times"] = times
    try:
        rr["every_hours"] = float(rr.get("every_hours", 2))
        rr["lookback_hours"] = float(rr.get("lookback_hours", 24))
    except (TypeError, ValueError):
        raise SettingsError("근거 알림 주기·기간은 숫자여야 합니다.")
    if not (0.5 <= rr["every_hours"] <= 24):
        raise SettingsError("근거 알림 주기는 0.5~24시간이어야 합니다.")
    if not (1 <= rr["lookback_hours"] <= 24 * 31):
        raise SettingsError("근거 알림 기간은 1시간~31일이어야 합니다.")
    rr["start"] = _hhmm(rr.get("start", "08:00"), "근거 알림 시작 시각")
    rr["end"] = _hhmm(rr.get("end", "23:00"), "근거 알림 끝 시각")
    tg["reason_remind"] = rr

    ac = settings["access"]
    if ac.get("mode") not in ("open", "tailscale", "lan", "public"):
        raise SettingsError("접속 방식은 open, tailscale, lan, public 중 하나여야 합니다.")
    if ac["mode"] == "public" and not ac.get("password_hash"):
        raise SettingsError("공개 서버 방식은 비밀번호를 먼저 정해야 합니다.")
    sd = ac.get("session_days", 30)
    if not _int(sd) or not (1 <= sd <= 365):
        raise SettingsError("로그인 유지 기간은 1~365일이어야 합니다.")

    _validate_guidelines(settings["guidelines"])

    ar = settings["admin_remote"]
    ar["enabled"] = bool(ar.get("enabled"))
    if ar["enabled"] and not ar.get("password_hash"):
        raise SettingsError("관리 화면 원격 접속을 켜려면 관리자 비밀번호를 먼저 정하세요.")
    sd = ar.get("session_days", 7)
    if not _int(sd) or not (1 <= sd <= 90):
        raise SettingsError("관리 화면 로그인 유지 기간은 1~90일이어야 합니다.")

    oa = settings["oanda"]
    oa["token"] = str(oa.get("token") or "").strip()
    if oa.get("env") not in ("practice", "live"):
        raise SettingsError("OANDA 환경은 practice(연습) 또는 live(실계좌)여야 합니다.")

    gr = settings["grid"]
    try:
        gr["min_gap_ktr"] = round(float(gr.get("min_gap_ktr")), 3)
    except (TypeError, ValueError):
        raise SettingsError("손절 여유(KTR)는 숫자여야 합니다.")
    if not (0.1 <= gr["min_gap_ktr"] <= 10):
        raise SettingsError("손절 여유(KTR)는 0.1~10 이어야 합니다.")
    try:
        gr["ktr_stop_gap"] = round(float(gr.get("ktr_stop_gap")), 3)
    except (TypeError, ValueError):
        raise SettingsError("KTR 거래 손절 여유는 숫자여야 합니다.")
    if not (0.1 <= gr["ktr_stop_gap"] <= 10):
        raise SettingsError("KTR 거래 손절 여유는 0.1~10 이어야 합니다.")
    gr["addon_auto"], gr["addon_ask"] = bool(gr.get("addon_auto")), bool(gr.get("addon_ask", True))
    gr["addon_sl_follow"] = bool(gr.get("addon_sl_follow", True))
    if gr.get("addon_tf") not in ("2m", "5m", "10m", "1h"):
        raise SettingsError("추가 리밋 시간봉은 2분·5분·10분·1시간 중 하나여야 합니다.")
    if gr.get("addon_src") not in ("mt5", "oanda"):
        raise SettingsError("추가 리밋 시세는 mt5 또는 oanda 여야 합니다.")
    try:
        gr["addon_mult"] = round(float(gr.get("addon_mult")), 3)
    except (TypeError, ValueError):
        raise SettingsError("추가 리밋 거리(KTR)는 숫자여야 합니다.")
    if not (0.1 <= gr["addon_mult"] <= 10):
        raise SettingsError("추가 리밋 거리(KTR)는 0.1~10 이어야 합니다.")
    try:
        gr["addon_n"] = int(gr.get("addon_n") or 1)
    except (TypeError, ValueError):
        raise SettingsError("추가 리밋 개수(N)는 숫자여야 합니다.")
    if not (1 <= gr["addon_n"] <= 10):
        raise SettingsError("추가 리밋 개수(N)는 1~10 이어야 합니다.")
    if gr.get("addon_lot") not in ("fill", "total"):
        raise SettingsError("추가 리밋 랏은 fill(체결 한 번 크기) 또는 total(첫 진입 합계) 이어야 합니다.")

    ev = settings["events"]
    hl = ev.get("highlight") or []
    if isinstance(hl, str):
        hl = hl.replace("\n", ",").split(",")
    if not isinstance(hl, list):
        raise SettingsError("강조할 단어는 쉼표로 나눠 넣어 주세요.")
    hl = list(dict.fromkeys(str(w).strip() for w in hl if str(w).strip()))
    if len(hl) > 60 or any(len(w) > 40 for w in hl):
        raise SettingsError("강조할 단어는 60개까지, 한 단어는 40자까지 넣을 수 있습니다.")
    ev["highlight"] = hl


# 지침 규칙별 숫자 범위 (key → (최소, 최대))
_GUIDE_RANGES = {
    "minutes": (1, 240), "repeat": (0, 240), "gap_min": (5, 1440), "gap_max": (5, 1440), "count": (1, 100), "seconds": (10, 3600), "lookback": (5, 200), "ratio": (1.2, 20), "pct": (0.01, 5),
}


def _validate_guidelines(g: dict) -> None:
    g["enabled"] = bool(g.get("enabled", True))
    cd = g.get("cooldown_min", 30)
    if not _int(cd) or not (1 <= cd <= 1440):
        raise SettingsError("지침 반복 방지 시간은 1~1440분이어야 합니다.")
    gap = g.get("entry_gap_sec", 60)
    if not _int(gap) or not (5 <= gap <= 900):
        raise SettingsError("나눠 진입 묶는 간격은 5~900초여야 합니다.")
    items = [str(x).strip() for x in (g.get("items") or []) if str(x).strip()]
    if len(items) > 100:
        raise SettingsError("지침은 100개까지 넣을 수 있습니다.")
    g["items"] = items
    rules = g.get("rules") or {}
    defaults = DEFAULTS["guidelines"]["rules"]
    for key, r in list(rules.items()):
        if key not in defaults or not isinstance(r, dict):
            rules.pop(key, None)
            continue
        r["enabled"] = bool(r.get("enabled", True))
        r["text"] = str(r.get("text") or "").strip()[:500]
        for k, (lo, hi) in _GUIDE_RANGES.items():
            if k in defaults[key]:
                v = r.get(k, defaults[key][k])
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo <= v <= hi):
                    raise SettingsError(f"지침 '{key}' 의 {k} 값은 {lo}~{hi} 사이여야 합니다. (입력: {v})")
                r[k] = float(v) if k in ("ratio", "pct") else int(v)
        if "weekdays" in defaults[key]:
            wd = r.get("weekdays") or []
            if not isinstance(wd, list) or any(not _int(x) or not (0 <= x <= 6) for x in wd):
                raise SettingsError("요일은 0(월)~6(일) 숫자 목록이어야 합니다.")
            r["weekdays"] = sorted(set(wd))
        for k, label in (("start", "시작 시각"), ("end", "끝 시각")):
            if k in defaults[key]:
                r[k] = _hhmm(r.get(k, defaults[key][k]), label)
        if "gap_min" in defaults[key] and r["gap_min"] > r["gap_max"]:
            raise SettingsError("무작위 지침의 간격은 '최소 ≤ 최대' 여야 합니다.")
        if "levels" in defaults[key]:       # 증거금 경고 단계 (%) - "10, 20, 30" 글자로 와도 됨
            lv = r.get("levels", defaults[key]["levels"])
            if isinstance(lv, str):
                lv = [x for x in lv.replace("%", "").replace("，", ",").replace(" ", ",").split(",") if x.strip()]
            try:
                lv = sorted({round(float(x), 1) for x in (lv or [])})
            except (TypeError, ValueError):
                raise SettingsError("증거금 경고 단계는 숫자(%)를 쉼표로 나눠 적어 주세요. 예: 10, 20, 30")
            if not lv or len(lv) > 10 or any(not (1 <= x <= 1000) for x in lv):
                raise SettingsError("증거금 경고 단계는 1~1000% 사이로 1~10개까지 넣을 수 있습니다.")
            r["levels"] = [int(x) if x == int(x) else x for x in lv]
        for b in ("event_days", "us_open", "asia_open", "euro_open"):
            if b in defaults[key]:
                r[b] = bool(r.get(b, defaults[key][b]))
    g["rules"] = rules


def _hhmm(v, label: str) -> str:
    """'9:5' / '09:05' → '09:05'"""
    try:
        h, m = str(v).strip().replace("시", ":").replace("분", "").split(":")[:2]
        h, m = int(h), int(m or 0)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}"
    except (ValueError, TypeError):
        pass
    raise SettingsError(f"{label}은 00:00 ~ 23:59 형식이어야 합니다. (입력: {v})")


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _validate_sessions(sessions) -> list[dict]:
    if not isinstance(sessions, list):
        raise SettingsError("집중 구간 목록 형식이 잘못됐습니다.")
    if len(sessions) > MAX_SESSIONS:
        raise SettingsError(f"집중 구간은 최대 {MAX_SESSIONS}개까지 만들 수 있습니다.")

    out, ids = [], set()
    for i, s in enumerate(sessions, 1):
        if not isinstance(s, dict):
            raise SettingsError(f"{i}번째 집중 구간 형식이 잘못됐습니다.")
        name = str(s.get("name") or "").strip()
        label = f"집중 구간 '{name or i}'"
        if not name or len(name) > 20:
            raise SettingsError(f"{i}번째 집중 구간 이름은 1~20자로 입력하세요.")

        sid = str(s.get("id") or "").strip().lower()
        if not sid or len(sid) > 24 or not all(c.isalnum() or c in "-_" for c in sid):
            raise SettingsError(f"{label}의 id가 잘못됐습니다.")
        if sid in ids:
            raise SettingsError(f"집중 구간 id '{sid}'가 중복됩니다.")
        if sid == "event":
            raise SettingsError("집중 구간 id 'event' 는 지표 발표일 전용 문구에 쓰여서 쓸 수 없습니다.")
        ids.add(sid)

        start, end = s.get("start_min"), s.get("end_min")
        if not (_int(start) and _int(end) and 0 <= start < end <= 1440 and start % 30 == 0 and end % 30 == 0):
            raise SettingsError(f"{label}의 시간은 30분 단위로, 시작이 끝보다 빨라야 합니다.")

        days = s.get("weekdays")
        if not isinstance(days, list) or any(not _int(d) or not 0 <= d <= 6 for d in days):
            raise SettingsError(f"{label}의 요일이 잘못됐습니다.")

        unlock = s.get("unlock_minutes")
        if not (_int(unlock) and 5 <= unlock <= 9 * 60):
            raise SettingsError(f"{label}의 해제 시간은 5~540분 사이여야 합니다.")

        for key in ("enabled", "event_days"):
            if not isinstance(s.get(key), bool):
                raise SettingsError(f"{label}의 {key} 값은 true/false여야 합니다.")

        out.append({
            "id": sid, "name": name, "enabled": s["enabled"],
            "start_min": start, "end_min": end,
            "weekdays": sorted(set(days)), "event_days": s["event_days"],
            "event_key_only": bool(s.get("event_key_only", True)),    # 지표일 중 ⭐ 주요 지표가 있는 날만 (기본 켜짐)
            "unlock_minutes": unlock,
        })
    return out


def _validate_interval(x, label: str) -> dict:
    if not isinstance(x, dict) or x.get("mode") not in ("minutes", "bars"):
        raise SettingsError(f"{label}의 재확인 방식이 잘못됐습니다.")
    every = x.get("every")
    if x["mode"] == "minutes" and not (_int(every) and 5 <= every <= 240):
        raise SettingsError(f"{label}의 재확인 간격은 5~240분이어야 합니다.")
    if x["mode"] == "bars" and every not in BAR_HOURS:
        raise SettingsError(f"{label}의 봉 단위는 {', '.join(f'{h}시간' for h in BAR_HOURS)} 중 하나여야 합니다.")
    return {"mode": x["mode"], "every": every}


def _ranges(start: int, end: int) -> list[tuple[int, int]]:
    """하루(0~1440분) 안의 구간 목록. 자정을 넘기면 둘로 나눔."""
    return [(start, end)] if start < end else [(start, 1440), (0, end)]


def _validate_checkin_rules(rules) -> list[dict]:
    if not isinstance(rules, list):
        raise SettingsError("정기 재확인 규칙 목록 형식이 잘못됐습니다.")
    if len(rules) > MAX_CHECKIN_RULES:
        raise SettingsError(f"정기 재확인 규칙은 최대 {MAX_CHECKIN_RULES}개까지 만들 수 있습니다.")

    out, ids = [], set()
    for i, r in enumerate(rules, 1):
        if not isinstance(r, dict):
            raise SettingsError(f"{i}번째 재확인 규칙 형식이 잘못됐습니다.")
        name = str(r.get("name") or "").strip()
        label = f"재확인 규칙 '{name or i}'"
        if not name or len(name) > 20:
            raise SettingsError(f"{i}번째 재확인 규칙 이름은 1~20자로 입력하세요.")
        rid = str(r.get("id") or "").strip().lower()
        if not rid or len(rid) > 24 or not all(c.isalnum() or c in "-_" for c in rid) or rid in ids:
            raise SettingsError(f"{label}의 id가 잘못됐거나 중복됩니다.")
        ids.add(rid)
        start, end = r.get("start_min"), r.get("end_min")
        if not (_int(start) and _int(end) and 0 <= start < 1440 and 0 <= end < 1440
                and start % 30 == 0 and end % 30 == 0 and start != end):
            raise SettingsError(f"{label}의 시간은 30분 단위로, 시작과 끝이 달라야 합니다.")
        if not isinstance(r.get("enabled"), bool):
            raise SettingsError(f"{label}의 사용 여부 값이 잘못됐습니다.")
        iv = _validate_interval(r, label)
        out.append({"id": rid, "name": name, "enabled": r["enabled"],
                    "start_min": start, "end_min": end, **iv})

    # 켜진 규칙끼리 시간대가 겹치면 어느 쪽을 따를지 모호하므로 막음
    on = [r for r in out if r["enabled"]]
    for a in range(len(on)):
        for b in range(a + 1, len(on)):
            if any(s1 < e2 and s2 < e1
                   for s1, e1 in _ranges(on[a]["start_min"], on[a]["end_min"])
                   for s2, e2 in _ranges(on[b]["start_min"], on[b]["end_min"])):
                raise SettingsError(f"재확인 규칙 '{on[a]['name']}'와(과) '{on[b]['name']}'의 시간대가 겹칩니다.")
    return out


def update_settings(patch: dict) -> dict:
    """일부 항목만 받아서 합친 뒤 검증하고 저장. 저장된 전체 설정을 돌려준다."""
    global _cache, _cache_mtime
    merged = _deep_merge(get_settings(), _migrate_legacy(copy.deepcopy(patch)))
    _validate(merged)

    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_PATH)  # 쓰는 도중 워커가 읽어도 깨진 파일을 보지 않게

    with _lock:
        _cache = None
        _cache_mtime = None
    return get_settings()
