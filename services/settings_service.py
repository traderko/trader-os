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
            },
        ],
        # 수동 잠금
        "manual_lock_max_minutes": 24 * 60,
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
    }


def _validate(settings: dict) -> None:
    lock = settings["lock"]
    for key, (lo, hi) in _LOCK_RANGES.items():
        v = lock.get(key)
        if not isinstance(v, int) or isinstance(v, bool) or not (lo <= v <= hi):
            raise SettingsError(f"{_LABELS.get(key, key)}은(는) {lo}~{hi} 사이 정수여야 합니다. (입력: {v})")

    lock["daily_loss_enabled"] = bool(lock.get("daily_loss_enabled", False))
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

    ac = settings["access"]
    if ac.get("mode") not in ("open", "tailscale", "lan", "public"):
        raise SettingsError("접속 방식은 open, tailscale, lan, public 중 하나여야 합니다.")
    if ac["mode"] == "public" and not ac.get("password_hash"):
        raise SettingsError("공개 서버 방식은 비밀번호를 먼저 정해야 합니다.")
    sd = ac.get("session_days", 30)
    if not _int(sd) or not (1 <= sd <= 365):
        raise SettingsError("로그인 유지 기간은 1~365일이어야 합니다.")

    ar = settings["admin_remote"]
    ar["enabled"] = bool(ar.get("enabled"))
    if ar["enabled"] and not ar.get("password_hash"):
        raise SettingsError("관리 화면 원격 접속을 켜려면 관리자 비밀번호를 먼저 정하세요.")
    sd = ar.get("session_days", 7)
    if not _int(sd) or not (1 <= sd <= 90):
        raise SettingsError("관리 화면 로그인 유지 기간은 1~90일이어야 합니다.")


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
            "weekdays": sorted(set(days)), "event_days": s["event_days"], "unlock_minutes": unlock,
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
