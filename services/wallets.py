"""출금한 돈이 간 곳 잔고 보기 - 바이낸스(읽기 전용 API 키) + BSC 개인 지갑(주소만, 여러 개)

  바이낸스 : 관리 화면 '지갑' 탭에서 넣은 API 키 (암호화해서 settings "wallets.binance_enc" 에 저장 -
             계좌 비밀번호와 같은 키 services/crypto.py). 없으면 .env 의 BINANCE_API_KEY · BINANCE_API_SECRET
             키는 '읽기'만 켜고 IP 제한 권장 - 출금·거래 권한 X
             현물(Spot) + 펀딩(Funding) 지갑 잔고, 최근 입금 내역
  BSC 지갑 : 주소만 있으면 공개 노드(JSON-RPC)로 BNB · USDT · USDC 잔고 조회 (키·개인키 필요 없음, 돈을 옮길 수 없음)
             주소 목록은 settings "wallets.bsc" = [{address, label}] (관리 화면 '지갑' 탭 · 텔레그램 /잔고 추가)
  환산     : 코인 → USDT 는 바이낸스 공개 시세, USDT → 원화는 업비트 공개 시세 (키 필요 없음)
  알림     : 5분마다 - 바이낸스 새 입금(완료), BSC 지갑 잔고가 늘면 텔레그램 (settings "wallets.notify")
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import time
from urllib.parse import urlencode

import httpx

BINANCE = "https://api.binance.com"
BSC_RPCS = ["https://bsc-dataseed.bnbchain.org", "https://bsc-dataseed1.binance.org", "https://bsc-rpc.publicnode.com"]
BSC_TOKENS = [  # (이름, 계약 주소, 소수 자릿수) - BSC 의 USDT·USDC 는 18자리
    ("USDT", "0x55d398326f99059fF775485246999027B3197955", 18),
    ("USDC", "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d", 18),
]
STABLE = {"USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI"}
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
CACHE_SEC = 60
NOTIFY_SEC = 300


# ── 설정 ──
def _binance_keys() -> tuple[str, str, str]:
    """(api key, secret, 어디서) - 관리 화면에서 넣은 것(암호화) 우선, 없으면 .env"""
    from services.settings_service import get_settings
    enc = (get_settings().get("wallets") or {}).get("binance_enc")
    if enc:
        try:
            from services.crypto import CryptoService
            d = json.loads(CryptoService().decrypt(enc))
            if d.get("key") and d.get("secret"):
                return d["key"], d["secret"], "admin"
        except Exception:
            pass                                       # 암호화 키가 바뀌어 못 풂 → .env 로
    k, sec = os.getenv("BINANCE_API_KEY", "").strip(), os.getenv("BINANCE_API_SECRET", "").strip()
    return (k, sec, "env") if k and sec else ("", "", "")


def save_binance(key: str | None, secret: str | None, clear: bool = False) -> None:
    from services.settings_service import get_settings, update_settings
    cur = dict(get_settings().get("wallets") or {})
    if clear:
        cur.pop("binance_enc", None)
    else:
        key, secret = (key or "").strip(), (secret or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9]{20,128}", key) or not re.fullmatch(r"[A-Za-z0-9]{20,128}", secret):
            raise ValueError("바이낸스 API Key · Secret Key 를 둘 다 넣어 주세요 (영문·숫자, 보통 64자)")
        from services.crypto import CryptoService
        cur["binance_enc"] = CryptoService().encrypt(json.dumps({"key": key, "secret": secret}))
    update_settings({"wallets": cur})
    _cache.clear()


def cfg() -> dict:
    from services.settings_service import get_settings
    raw = get_settings().get("wallets") or {}
    bsc = []
    for w in raw.get("bsc") or []:
        a = str((w or {}).get("address") or "").strip()
        if ADDR_RE.match(a):
            bsc.append({"address": a, "label": str(w.get("label") or "").strip()[:30]})
    k, _, src = _binance_keys()
    return {"bsc": bsc, "notify": bool(raw.get("notify", True)), "binance": bool(k), "binance_src": src,
            "binance_key": f"{k[:4]}…{k[-4:]}" if k else ""}    # 화면에는 앞뒤 4자만 (Secret 은 절대 안 보냄)


def save_cfg(bsc: list[dict] | None = None, notify: bool | None = None) -> dict:
    from services.settings_service import get_settings, update_settings
    cur = dict(get_settings().get("wallets") or {})
    if bsc is not None:
        clean, seen = [], set()
        for w in bsc:
            a = str((w or {}).get("address") or "").strip()
            if not ADDR_RE.match(a):
                raise ValueError(f"BSC 주소가 아닙니다: {a[:20]}… (0x 로 시작하는 42자)")
            if a.lower() in seen:
                continue
            seen.add(a.lower())
            clean.append({"address": a, "label": str(w.get("label") or "").strip()[:30]})
        cur["bsc"] = clean
    if notify is not None:
        cur["notify"] = bool(notify)
    update_settings({"wallets": cur})
    _cache.clear()
    return cfg()


def short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 12 else addr


# ── 시세 ──
async def _prices(client: httpx.AsyncClient) -> dict[str, float]:
    """{'BNBUSDT': 600.1, ...} - 바이낸스 공개 시세 (키 필요 없음)"""
    r = await client.get(f"{BINANCE}/api/v3/ticker/price", timeout=10)
    r.raise_for_status()
    return {x["symbol"]: float(x["price"]) for x in r.json()}


def _usdt(asset: str, amount: float, prices: dict) -> float | None:
    if asset in STABLE:
        return amount
    p = prices.get(asset + "USDT")
    return amount * p if p else None


async def _krw_rate(client: httpx.AsyncClient) -> float | None:
    try:
        r = await client.get("https://api.upbit.com/v1/ticker", params={"markets": "KRW-USDT"}, timeout=10)
        r.raise_for_status()
        return float(r.json()[0]["trade_price"])
    except Exception:
        return None


# ── 바이낸스 (읽기 전용 키) ──
_time_offset = 0


async def _signed(client: httpx.AsyncClient, method: str, path: str, params: dict | None = None):
    global _time_offset
    key, secret, _ = _binance_keys()
    for attempt in range(2):
        p = dict(params or {}, timestamp=int(time.time() * 1000) + _time_offset, recvWindow=10000)
        q = urlencode(p)
        sig = hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        r = await client.request(method, f"{BINANCE}{path}?{q}&signature={sig}", headers={"X-MBX-APIKEY": key}, timeout=15)
        if r.status_code == 200:
            return r.json()
        body = r.text[:300]
        if attempt == 0 and '"code":-1021' in body:          # 이 PC 시계가 어긋남 → 바이낸스 시각에 맞춰 다시
            t = (await client.get(f"{BINANCE}/api/v3/time", timeout=10)).json()["serverTime"]
            _time_offset = int(t - time.time() * 1000)
            continue
        if '"code":-2015' in body or '"code":-2014' in body:
            raise RuntimeError("API 키가 맞지 않거나, 이 서버 IP 가 키의 허용 IP 에 없습니다")
        raise RuntimeError(f"바이낸스 응답 {r.status_code}: {body}")
    raise RuntimeError("바이낸스 시각 맞추기 실패")


async def binance(client: httpx.AsyncClient, prices: dict) -> dict:
    out = {"configured": cfg()["binance"], "ok": False, "error": "", "assets": [], "total_usdt": 0.0, "deposits": []}
    if not out["configured"]:
        out["error"] = "키 없음 (관리 화면 '지갑' 탭에서 넣으세요)"
        return out
    try:
        spot, funding, deps = await asyncio.gather(
            _signed(client, "GET", "/api/v3/account", {"omitZeroBalances": "true"}),
            _signed(client, "POST", "/sapi/v1/asset/get-funding-asset"),
            _signed(client, "GET", "/sapi/v1/capital/deposit/hisrec", {"limit": 10}),
        )
    except Exception as e:
        out["error"] = str(e)[:200]
        return out
    amounts: dict[str, dict] = {}
    for where, rows, keys in (("현물", spot.get("balances") or [], ("free", "locked")),
                              ("펀딩", funding or [], ("free", "locked", "freeze", "withdrawing"))):
        for b in rows:
            amt = sum(float(b.get(k) or 0) for k in keys)
            if amt > 0:
                a = amounts.setdefault(b["asset"], {"asset": b["asset"], "amount": 0.0, "where": []})
                a["amount"] += amt
                a["where"].append(where)
    for a in amounts.values():
        a["usdt"] = _usdt(a["asset"], a["amount"], prices)
    out["assets"] = sorted(amounts.values(), key=lambda a: -(a["usdt"] or 0))
    out["total_usdt"] = sum(a["usdt"] or 0 for a in out["assets"])
    out["deposits"] = [{"id": str(d.get("id") or d.get("txId")), "coin": d.get("coin"), "network": d.get("network"),
                        "amount": float(d.get("amount") or 0), "time": int(d.get("insertTime") or 0) // 1000,
                        "done": int(d.get("status", 0)) == 1} for d in (deps or [])]
    out["ok"] = True
    return out


# ── BSC 지갑 (주소만) ──
async def _rpc(client: httpx.AsyncClient, calls: list[tuple[str, list]]) -> list:
    """JSON-RPC 여러 개를 한 번에 (노드가 안 되면 다음 노드)"""
    body = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)]
    err = None
    for url in BSC_RPCS:
        try:
            r = await client.post(url, json=body, timeout=10)
            r.raise_for_status()
            res = sorted(r.json(), key=lambda x: x["id"])
            if any("error" in x for x in res):
                raise RuntimeError(str(next(x["error"] for x in res if "error" in x))[:120])
            return [x["result"] for x in res]
        except Exception as e:
            err = e
    raise RuntimeError(f"BSC 노드 연결 실패: {err}")


async def bsc_wallet(client: httpx.AsyncClient, w: dict, prices: dict) -> dict:
    addr = w["address"]
    out = {"address": addr, "label": w.get("label") or short(addr), "ok": False, "error": "", "assets": [], "total_usdt": 0.0}
    calls = [("eth_getBalance", [addr, "latest"])]
    for _, token, _ in BSC_TOKENS:
        calls.append(("eth_call", [{"to": token, "data": "0x70a08231" + addr[2:].lower().rjust(64, "0")}, "latest"]))
    try:
        res = await _rpc(client, calls)
    except Exception as e:
        out["error"] = str(e)[:200]
        return out
    vals = [("BNB", int(res[0], 16) / 1e18)] + [(name, int(res[i + 1] or "0x0", 16) / 10 ** dec)
                                              for i, (name, _, dec) in enumerate(BSC_TOKENS)]
    for asset, amt in vals:
        if amt > 0:
            out["assets"].append({"asset": asset, "amount": amt, "usdt": _usdt(asset, amt, prices)})
    out["total_usdt"] = sum(a["usdt"] or 0 for a in out["assets"])
    out["ok"] = True
    return out


# ── 전체 ──
_cache: dict = {}


async def summary(refresh: bool = False) -> dict:
    if not refresh and _cache.get("at", 0) > time.time() - CACHE_SEC:
        return _cache["data"]
    c = cfg()
    async with httpx.AsyncClient() as client:
        try:
            prices = await _prices(client)
        except Exception:
            prices = {}
        bn, krw, *bsc = await asyncio.gather(binance(client, prices), _krw_rate(client),
                                             *(bsc_wallet(client, w, prices) for w in c["bsc"]))
    total = (bn["total_usdt"] if bn["ok"] else 0) + sum(w["total_usdt"] for w in bsc if w["ok"])
    data = {"binance": bn, "bsc": bsc, "total_usdt": total, "krw_rate": krw,
            "total_krw": total * krw if krw else None, "notify": c["notify"], "at": int(time.time())}
    _cache.update(at=time.time(), data=data)
    return data


def _n(v: float, d: int = 2) -> str:
    return f"{v:,.{d}f}"


def _amt(asset: str, v: float) -> str:
    return f"{_n(v, 2 if asset in STABLE else 4)} {asset}"


def text(d: dict) -> str:
    import html
    from datetime import datetime
    L = ["💰 <b>지갑 잔고</b>", ""]
    bn = d["binance"]
    if bn["ok"]:
        L.append(f"<b>바이낸스</b>  {_n(bn['total_usdt'])} USDT")
        L += [f"   {_amt(a['asset'], a['amount'])}" + (f" ≈ {_n(a['usdt'])}" if a["usdt"] is not None and a["asset"] not in STABLE else "")
              for a in bn["assets"][:6]]
    else:
        L.append(f"<b>바이낸스</b>  {html.escape(bn['error'])}")
    for w in d["bsc"]:
        L.append("")
        name = html.escape(w["label"]) + (f" ({short(w['address'])})" if w["label"] != short(w["address"]) else "")
        if w["ok"]:
            L.append(f"<b>BSC · {name}</b>  {_n(w['total_usdt'])} USDT")
            L += [f"   {_amt(a['asset'], a['amount'])}" for a in w["assets"]] or ["   비어 있음"]
        else:
            L.append(f"<b>BSC · {name}</b>  {html.escape(w['error'])}")
    L += ["", "━━━━━━━━━━━━━━", f"합계 <b>{_n(d['total_usdt'])} USDT</b>"
          + (f" ≈ <b>{d['total_krw']:,.0f}원</b> (1 USDT = {d['krw_rate']:,.0f}원)" if d.get("total_krw") else "")]
    deps = [x for x in (bn.get("deposits") or []) if x["done"]][:3]
    if deps:
        L.append("")
        L.append("최근 바이낸스 입금")
        L += [f"   {datetime.fromtimestamp(x['time']):%m/%d %H:%M}  +{_amt(x['coin'], x['amount'])} ({x['network']})" for x in deps]
    if not d["bsc"]:
        L.append("\n<i>BSC 지갑 추가: /잔고 추가 0x주소 이름</i>")
    return "\n".join(L)[:4000]


# ── 입금 알림 ──
def _state_path() -> str:
    from services.settings_service import DATA_DIR
    return os.path.join(DATA_DIR, "wallets_state.json")


async def check_once(send) -> None:
    """send(text) - 바이낸스 새 입금 · BSC 잔고 늘어남 → 알림. 첫 실행은 기준만 저장"""
    if not cfg()["notify"]:
        return
    d = await summary(refresh=True)
    try:
        with open(_state_path(), encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    first = not st
    msgs = []
    bn = d["binance"]
    if bn["ok"]:
        seen = set(st.get("bn_seen") or [])
        for x in bn["deposits"]:
            if x["done"] and x["id"] not in seen:
                seen.add(x["id"])
                if not first and "bn_seen" in st:
                    msgs.append(f"📥 <b>바이낸스 입금 도착</b>  +{_amt(x['coin'], x['amount'])} ({x['network']})")
        st["bn_seen"] = list(seen)[-50:]
    bal = st.get("bsc") or {}
    for w in d["bsc"]:
        if not w["ok"]:
            continue
        now = {a["asset"]: a["amount"] for a in w["assets"]}
        old = bal.get(w["address"].lower())
        if old is not None:
            for asset, amt in now.items():
                up = amt - (old.get(asset) or 0)
                if up > (0.01 if asset in STABLE else 0.0001):
                    msgs.append(f"📥 <b>BSC 지갑 입금</b> · {w['label']}  +{_amt(asset, up)} (잔고 {_amt(asset, amt)})")
        bal[w["address"].lower()] = now
    st["bsc"] = bal
    try:
        tmp = _state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, _state_path())
    except OSError:
        pass
    for m in msgs:
        await send(m)


async def loop(live, client) -> None:
    from services.settings_service import telegram_cfg

    async def send(t: str) -> None:
        c = telegram_cfg()
        if c["bot_token"] and c["chat_id"]:
            await live._send(client, c["bot_token"], c["chat_id"], t)

    await asyncio.sleep(90)
    while True:
        try:
            c = cfg()
            if c["binance"] or c["bsc"]:
                await check_once(send)
        except Exception as e:
            print(f"[wallets] 확인 실패: {e}")
        await asyncio.sleep(NOTIFY_SEC)


# ── 텔레그램 /잔고 (/wallet) ──
async def cmd(live, client, token, cid: str, arg: str) -> None:
    import html
    w = (arg or "").split()
    try:
        if w and w[0] in ("추가", "add"):
            if len(w) < 2:
                raise ValueError("예) /잔고 추가 0x1234…abcd 개인지갑1")
            cur = cfg()["bsc"]
            cur.append({"address": w[1], "label": " ".join(w[2:]) or f"지갑{len(cur) + 1}"})
            save_cfg(bsc=cur)
            await live._send(client, token, cid, f"➕ BSC 지갑 추가: {html.escape(short(w[1]))}")
        elif w and w[0] in ("삭제", "del", "remove"):
            cur = cfg()["bsc"]
            i = int(w[1]) - 1 if len(w) > 1 and w[1].isdigit() else -1
            if not 0 <= i < len(cur):
                lst = "\n".join(f"{n + 1}. {html.escape(x['label'] or '')} {short(x['address'])}" for n, x in enumerate(cur)) or "(없음)"
                raise ValueError(f"지울 번호를 붙여 주세요. 예) /잔고 삭제 1\n{lst}")
            gone = cur.pop(i)
            save_cfg(bsc=cur)
            await live._send(client, token, cid, f"🗑 BSC 지갑 삭제: {html.escape(gone['label'])} {short(gone['address'])}")
        elif w and w[0] in ("목록", "list"):
            cur = cfg()["bsc"]
            await live._send(client, token, cid, "BSC 지갑\n" + ("\n".join(
                f"{n + 1}. {html.escape(x['label'] or '')} <code>{x['address']}</code>" for n, x in enumerate(cur)) or "(없음)"))
            return
        elif w and w[0] in ("알림", "notify"):
            on = not (len(w) > 1 and w[1] in ("끄기", "off"))
            save_cfg(notify=on)
            await live._send(client, token, cid, f"입금 알림 {'켬' if on else '끔'} (바이낸스 입금 · BSC 지갑 잔고 늘어남, 5분마다 확인)")
            return
    except ValueError as e:
        await live._send(client, token, cid, html.escape(str(e)))
        return
    try:
        d = await summary(refresh=True)
    except Exception as e:
        await live._send(client, token, cid, f"잔고 조회 실패: {html.escape(str(e))[:200]}")
        return
    await live._send(client, token, cid, text(d) + "\n\n<i>/잔고 추가 0x주소 이름 · /잔고 삭제 번호 · /잔고 목록 · /잔고 알림 끄기</i>")
