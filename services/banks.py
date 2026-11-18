"""은행 잔고 - CODEF API(데모, 지원 은행) + 직접 입력 (토스뱅크처럼 지원 안 되는 곳)

  직접 입력 : settings "banks.manual" = [{name, amount(원), at(입력 시각)}]
              관리 화면 '지갑' 탭 · 텔레그램 /잔고 은행 토스 1234000
  CODEF     : codef.io 데모 키 (Client ID · Secret · 공개키) 를 관리 화면에서 넣음 → 암호화해서 settings "banks.codef_enc"
              은행 연결은 인터넷뱅킹 아이디·비밀번호로 한 번 → CODEF 가 주는 연결 아이디(connectedId)만 암호화해 저장
              (은행 비밀번호는 CODEF 공개키로 암호화해서 CODEF 로만 보내고, 이 서버에는 남기지 않음)
              데모는 하루 100회까지라 은행 조회는 1시간에 한 번 (손으로 새로고침해도 10분 안에는 다시 안 부름)
              결과는 data/banks_state.json 에 저장해 두고 그걸 보여 줌
"""
from __future__ import annotations

import base64
import json
import os
import time
from urllib.parse import unquote_plus

import httpx

CODEF_OAUTH = "https://oauth.codef.io/oauth/token"
CODEF_API = "https://development.codef.io"          # 데모 (정식은 api.codef.io - 사업자 승인 필요)
REFRESH_SEC = 3600
MIN_GAP_SEC = 600
BANKS = {  # CODEF 기관 코드 (지원 여부는 CODEF 문서 '은행 개인 보유계좌' 에서 확인)
    "0004": "국민", "0088": "신한", "0020": "우리", "0081": "하나", "0011": "농협", "0003": "기업",
    "0089": "케이뱅크", "0090": "카카오뱅크", "0071": "우체국", "0045": "새마을금고", "0048": "신협",
    "0023": "SC제일", "0027": "씨티", "0031": "iM뱅크(대구)", "0032": "부산", "0034": "광주", "0035": "제주",
    "0037": "전북", "0039": "경남", "0007": "수협", "0002": "산업",
}


# ── 설정 ──
def _raw() -> dict:
    from services.settings_service import get_settings
    return dict(get_settings().get("banks") or {})


def _put(cur: dict) -> None:
    from services.settings_service import update_settings
    update_settings({"banks": cur})


def _enc(obj) -> str:
    from services.crypto import CryptoService
    return CryptoService().encrypt(json.dumps(obj))


def _dec(tok: str):
    from services.crypto import CryptoService
    try:
        return json.loads(CryptoService().decrypt(tok)) if tok else None
    except Exception:
        return None


def cfg() -> dict:
    """화면용 - 키·연결 아이디는 보내지 않음"""
    raw = _raw()
    keys = _dec(raw.get("codef_enc")) or {}
    return {
        "manual": [{"name": m.get("name", ""), "amount": float(m.get("amount") or 0), "at": int(m.get("at") or 0)}
                   for m in raw.get("manual") or []],
        "codef": bool(keys.get("client_id")),
        "codef_id": (keys.get("client_id", "")[:6] + "…") if keys.get("client_id") else "",
        "orgs": [{"organization": o, "name": BANKS.get(o, o)} for o in raw.get("orgs") or []],
        "banks": BANKS,
    }


def set_manual(items: list[dict]) -> None:
    cur, old = _raw(), {m.get("name"): m for m in _raw().get("manual") or []}
    out = []
    for m in items:
        name = str(m.get("name") or "").strip()[:20]
        if not name:
            continue
        try:
            amt = float(str(m.get("amount")).replace(",", ""))
        except (TypeError, ValueError):
            raise ValueError(f"{name}: 금액이 숫자가 아닙니다")
        prev = old.get(name)
        at = prev["at"] if prev and float(prev.get("amount") or 0) == amt else int(time.time())   # 금액이 바뀐 것만 시각 갱신
        out.append({"name": name, "amount": amt, "at": at})
    cur["manual"] = out
    _put(cur)


def set_manual_one(name: str, amount: float) -> None:
    items = [m for m in cfg()["manual"] if m["name"] != name] + [{"name": name, "amount": amount}]
    set_manual(items)


def set_codef_keys(client_id: str, client_secret: str, public_key: str) -> None:
    client_id, client_secret, public_key = (client_id or "").strip(), (client_secret or "").strip(), "".join((public_key or "").split())
    if not (client_id and client_secret and public_key):
        raise ValueError("CODEF Client ID · Client Secret · 공개키(Public Key)를 모두 넣어 주세요 (codef.io 마이페이지 → 키 관리, 데모)")
    cur = _raw()
    cur["codef_enc"] = _enc({"client_id": client_id, "client_secret": client_secret, "public_key": public_key})
    _put(cur)
    _token.clear()


def clear_codef() -> None:
    cur = _raw()
    for k in ("codef_enc", "connected_enc", "orgs"):
        cur.pop(k, None)
    _put(cur)
    _token.clear()
    _save_state({})


# ── CODEF 호출 ──
_token: dict = {}


async def _get_token(client: httpx.AsyncClient) -> str:
    if _token.get("exp", 0) > time.time() + 60:
        return _token["tok"]
    keys = _dec(_raw().get("codef_enc")) or {}
    if not keys.get("client_id"):
        raise RuntimeError("CODEF 키가 없습니다")
    auth = base64.b64encode(f"{keys['client_id']}:{keys['client_secret']}".encode()).decode()
    r = await client.post(CODEF_OAUTH, data={"grant_type": "client_credentials", "scope": "read"},
                          headers={"Authorization": f"Basic {auth}"}, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"CODEF 토큰 실패 ({r.status_code}) - Client ID · Secret 을 확인하세요")
    j = r.json()
    _token.update(tok=j["access_token"], exp=time.time() + int(j.get("expires_in") or 600000))
    return _token["tok"]


async def _call(client: httpx.AsyncClient, path: str, body: dict) -> dict:
    tok = await _get_token(client)
    r = await client.post(CODEF_API + path, json=body, headers={"Authorization": f"Bearer {tok}"}, timeout=60)
    text = unquote_plus(r.text)                       # CODEF 응답은 URL 인코딩된 JSON
    try:
        j = json.loads(text)
    except ValueError:
        raise RuntimeError(f"CODEF 응답을 읽지 못함 ({r.status_code})")
    res = j.get("result") or {}
    if res.get("code") != "CF-00000":
        extra = ""
        for e in ((j.get("data") or {}).get("errorList") or []):
            extra = f" · {e.get('message') or e.get('code')}"
        raise RuntimeError(f"{res.get('message') or res.get('code')}{extra}")
    return j.get("data") or {}


def _rsa(password: str) -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    keys = _dec(_raw().get("codef_enc")) or {}
    pub = serialization.load_der_public_key(base64.b64decode(keys["public_key"]))
    return base64.b64encode(pub.encrypt(password.encode(), padding.PKCS1v15())).decode()


async def connect_bank(organization: str, login_id: str, password: str) -> None:
    """은행 하나 연결 (인터넷뱅킹 아이디·비밀번호). 비밀번호는 여기서 암호화해 CODEF 로만 보내고 저장하지 않음"""
    if organization not in BANKS:
        raise ValueError("은행을 고르세요")
    if not login_id or not password:
        raise ValueError("인터넷뱅킹 아이디와 비밀번호를 넣어 주세요")
    acc = {"countryCode": "KR", "businessType": "BK", "clientType": "P", "organization": organization,
           "loginType": "1", "id": login_id, "password": _rsa(password)}
    cur = _raw()
    connected = (_dec(cur.get("connected_enc")) or {}).get("id")
    async with httpx.AsyncClient() as client:
        if connected:
            d = await _call(client, "/v1/account/add", {"connectedId": connected, "accountList": [acc]})
        else:
            d = await _call(client, "/v1/account/create", {"accountList": [acc]})
            connected = d.get("connectedId")
            if not connected:
                raise RuntimeError("CODEF 가 연결 아이디를 주지 않았습니다")
    cur = _raw()
    cur["connected_enc"] = _enc({"id": connected})
    cur["orgs"] = sorted(set((cur.get("orgs") or []) + [organization]))
    _put(cur)
    _save_state(dict(_load_state(), at=0))            # 다음 조회를 바로


async def disconnect_bank(organization: str) -> None:
    cur = _raw()
    connected = (_dec(cur.get("connected_enc")) or {}).get("id")
    if connected:
        try:
            async with httpx.AsyncClient() as client:
                await _call(client, "/v1/account/delete", {"connectedId": connected, "accountList": [
                    {"countryCode": "KR", "businessType": "BK", "clientType": "P", "organization": organization, "loginType": "1"}]})
        except Exception:
            pass
    cur["orgs"] = [o for o in cur.get("orgs") or [] if o != organization]
    _put(cur)
    st = _load_state()
    st["banks"] = [b for b in st.get("banks") or [] if b.get("organization") != organization]
    _save_state(st)


# ── 조회 결과 (하루 100회 제한 때문에 저장해 두고 씀) ──
def _state_path() -> str:
    from services.settings_service import DATA_DIR
    return os.path.join(DATA_DIR, "banks_state.json")


def _load_state() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    try:
        tmp = _state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, _state_path())
    except OSError:
        pass


async def refresh_codef(force: bool = False) -> dict:
    st = _load_state()
    raw = _raw()
    orgs = raw.get("orgs") or []
    connected = (_dec(raw.get("connected_enc")) or {}).get("id")
    if not (orgs and connected and (_dec(raw.get("codef_enc")) or {}).get("client_id")):
        return st
    gap = MIN_GAP_SEC if force else REFRESH_SEC
    if time.time() - st.get("at", 0) < gap:
        return st
    banks = []
    async with httpx.AsyncClient() as client:
        for o in orgs:
            try:
                d = await _call(client, "/v1/kr/bank/p/account/account-list", {"connectedId": connected, "organization": o})
                accs = [{"account": a.get("resAccountDisplay") or a.get("resAccount", ""), "name": a.get("resAccountName", ""),
                         "balance": float(a.get("resAccountBalance") or 0)} for a in d.get("resDepositTrust") or []]
                banks.append({"organization": o, "name": BANKS.get(o, o), "ok": True, "accounts": accs,
                              "amount": sum(a["balance"] for a in accs)})
            except Exception as e:
                old = next((b for b in st.get("banks") or [] if b.get("organization") == o), None)
                banks.append(dict(old or {"organization": o, "name": BANKS.get(o, o), "accounts": [], "amount": 0.0},
                                  ok=False, error=str(e)[:200]))
    st = {"at": int(time.time()), "banks": banks}
    _save_state(st)
    return st


async def summary(force: bool = False) -> dict:
    """{'banks': [{name, amount, source, at, ok, error, accounts}], 'total_krw'}"""
    st = await refresh_codef(force)
    out = []
    for b in st.get("banks") or []:
        out.append({"name": b["name"], "amount": b.get("amount", 0.0), "source": "codef", "at": st.get("at", 0),
                    "ok": b.get("ok", True), "error": b.get("error", ""), "accounts": b.get("accounts") or []})
    for m in cfg()["manual"]:
        out.append({"name": m["name"], "amount": m["amount"], "source": "manual", "at": m["at"], "ok": True, "error": "", "accounts": []})
    return {"banks": out, "total_krw": sum(b["amount"] for b in out)}
