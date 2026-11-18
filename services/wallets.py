"""출금한 돈이 간 곳 잔고 보기 - 바이낸스(읽기 전용 API 키) + 개인 지갑(주소만, 여러 네트워크 · 여러 개)

  바이낸스 : 관리 화면 '지갑' 탭에서 넣은 API 키 (암호화해서 settings "wallets.binance_enc" 에 저장 -
             계좌 비밀번호와 같은 키 services/crypto.py). 없으면 .env 의 BINANCE_API_KEY · BINANCE_API_SECRET
             키는 '읽기'만 켜고 IP 제한 권장 - 출금·거래 권한 X
             현물(Spot) + 펀딩(Funding) 지갑 잔고, 최근 입금 내역
  개인 지갑 : 주소만 있으면 공개 노드로 잔고 조회 (키·개인키 필요 없음, 주소로는 돈을 옮길 수 없음)
             네트워크: BSC · Ethereum · TRON · Polygon · Arbitrum · Base · Optimism · Avalanche · Solana · Bitcoin
             (EVM 은 기본 코인 + USDT · USDC, TRON 은 TRX + USDT · USDC, Solana 는 SOL + USDT · USDC, Bitcoin 은 BTC)
             목록은 settings "wallets.list" = [{network, address, label}] (예전 "wallets.bsc" 도 읽음)
  환산     : 코인 → USDT 는 바이낸스 공개 시세, USDT → 원화는 업비트 공개 시세 (키 필요 없음)
  알림     : 5분마다 - 바이낸스 새 입금(완료), 개인 지갑 잔고가 늘면 텔레그램 (settings "wallets.notify")
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
# EVM 네트워크: (이름, 공개 노드들, 기본 코인, 토큰 [(이름, 계약, 소수 자릿수)])
EVM = {
    "bsc": ("BSC (BEP20)", ["https://bsc-dataseed.bnbchain.org", "https://bsc-dataseed1.binance.org", "https://bsc-rpc.publicnode.com"], "BNB",
            [("USDT", "0x55d398326f99059fF775485246999027B3197955", 18), ("USDC", "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d", 18)]),
    "eth": ("Ethereum (ERC20)", ["https://ethereum-rpc.publicnode.com", "https://eth.llamarpc.com", "https://cloudflare-eth.com"], "ETH",
            [("USDT", "0xdAC17F958D2ee523a2206206994597C13D831ec7", 6), ("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6)]),
    "polygon": ("Polygon", ["https://polygon-bor-rpc.publicnode.com", "https://polygon-rpc.com"], "POL",
                [("USDT", "0xc2132D05D31c914a87C6611C10748AEb04B58e8F", 6), ("USDC", "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", 6)]),
    "arbitrum": ("Arbitrum One", ["https://arb1.arbitrum.io/rpc", "https://arbitrum-one-rpc.publicnode.com"], "ETH",
                 [("USDT", "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9", 6), ("USDC", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 6)]),
    "base": ("Base", ["https://mainnet.base.org", "https://base-rpc.publicnode.com"], "ETH",
             [("USDC", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 6)]),
    "optimism": ("Optimism", ["https://mainnet.optimism.io", "https://optimism-rpc.publicnode.com"], "ETH",
                 [("USDT", "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58", 6), ("USDC", "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85", 6)]),
    "avax": ("Avalanche C", ["https://api.avax.network/ext/bc/C/rpc", "https://avalanche-c-chain-rpc.publicnode.com"], "AVAX",
             [("USDT", "0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7", 6), ("USDC", "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E", 6)]),
}
TRON_API = "https://api.trongrid.io"
TRON_TOKENS = [("USDT", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", 6), ("USDC", "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8", 6)]
SOL_RPCS = ["https://api.mainnet-beta.solana.com", "https://solana-rpc.publicnode.com"]
SOL_TOKENS = [("USDT", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"), ("USDC", "EPjFWdd5AufqSSqeM2qJLdkuZRZ4pR4D3pTz7cDhGWv")]
NETWORKS = {k: v[0] for k, v in EVM.items()} | {"tron": "TRON (TRC20)", "sol": "Solana", "btc": "Bitcoin"}
ADDR_RES = {
    "evm": re.compile(r"^0x[0-9a-fA-F]{40}$"),
    "tron": re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$"),
    "sol": re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"),
    "btc": re.compile(r"^(bc1[02-9ac-hj-np-z]{11,71}|[13][1-9A-HJ-NP-Za-km-z]{25,34})$"),
}
ALIASES = {"bep20": "bsc", "bnb": "bsc", "erc20": "eth", "ethereum": "eth", "trc20": "tron", "trx": "tron",
           "matic": "polygon", "pol": "polygon", "arb": "arbitrum", "op": "optimism", "avalanche": "avax",
           "solana": "sol", "bitcoin": "btc"}


def net_key(x: str) -> str:
    x = (x or "").strip().lower()
    return x if x in NETWORKS else ALIASES.get(x, "")


def valid_addr(net: str, addr: str) -> bool:
    kind = "evm" if net in EVM else net
    return bool(ADDR_RES.get(kind) and ADDR_RES[kind].match(addr or ""))


def guess_net(addr: str) -> str:
    """주소 모양으로 짐작 (0x… 는 어느 EVM 인지 알 수 없어 BSC 로)"""
    for k in ("tron", "btc", "sol"):
        if valid_addr(k, addr):
            return k
    return "bsc" if valid_addr("bsc", addr) else ""


STABLE = {"USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI"}
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
    items = raw.get("list")
    if items is None:                                   # 예전 형식 (BSC 만)
        items = [dict(w or {}, network="bsc") for w in raw.get("bsc") or []]
    lst = []
    for w in items:
        net, a = net_key((w or {}).get("network")), str((w or {}).get("address") or "").strip()
        if net and valid_addr(net, a):
            lst.append({"network": net, "address": a, "label": str(w.get("label") or "").strip()[:30]})
    k, _, src = _binance_keys()
    return {"list": lst, "notify": bool(raw.get("notify", True)), "binance": bool(k), "binance_src": src,
            "binance_key": f"{k[:4]}…{k[-4:]}" if k else "",     # 화면에는 앞뒤 4자만 (Secret 은 절대 안 보냄)
            "networks": NETWORKS}


def save_cfg(lst: list[dict] | None = None, notify: bool | None = None) -> dict:
    from services.settings_service import get_settings, update_settings
    cur = dict(get_settings().get("wallets") or {})
    if lst is not None:
        clean, seen = [], set()
        for w in lst:
            net, a = net_key((w or {}).get("network")) or guess_net(str((w or {}).get("address") or "").strip()), str((w or {}).get("address") or "").strip()
            if not net:
                raise ValueError(f"네트워크를 고르세요: {a[:20]}")
            if not valid_addr(net, a):
                raise ValueError(f"{NETWORKS[net]} 주소가 아닙니다: {a[:24]}…")
            if (net, a.lower()) in seen:
                continue
            seen.add((net, a.lower()))
            clean.append({"network": net, "address": a, "label": str(w.get("label") or "").strip()[:30]})
        cur["list"] = clean
        cur.pop("bsc", None)
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


# ── 개인 지갑 (주소만) ──
async def _rpc(client: httpx.AsyncClient, urls: list[str], calls: list[tuple[str, list]]) -> list:
    """JSON-RPC 여러 개를 한 번에 (노드가 안 되면 다음 노드)"""
    body = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)]
    err = None
    for url in urls:
        try:
            r = await client.post(url, json=body, timeout=10)
            r.raise_for_status()
            res = sorted(r.json(), key=lambda x: x["id"])
            if any("error" in x for x in res):
                raise RuntimeError(str(next(x["error"] for x in res if "error" in x))[:120])
            return [x["result"] for x in res]
        except Exception as e:
            err = e
    raise RuntimeError(f"노드 연결 실패: {err}")


async def _evm(client, net: str, addr: str) -> list[tuple[str, float]]:
    _, urls, native, tokens = EVM[net]
    calls = [("eth_getBalance", [addr, "latest"])]
    for _, token, _ in tokens:
        calls.append(("eth_call", [{"to": token, "data": "0x70a08231" + addr[2:].lower().rjust(64, "0")}, "latest"]))
    res = await _rpc(client, urls, calls)
    return [(native, int(res[0], 16) / 1e18)] + [(name, int(res[i + 1] or "0x0", 16) / 10 ** dec)
                                                for i, (name, _, dec) in enumerate(tokens)]


async def _tron(client, addr: str) -> list[tuple[str, float]]:
    r = await client.get(f"{TRON_API}/v1/accounts/{addr}", timeout=10)
    r.raise_for_status()
    data = (r.json().get("data") or [{}])[0]               # 한 번도 안 쓴 주소면 비어 있음
    trc = {}
    for d in data.get("trc20") or []:
        trc.update(d)
    return [("TRX", int(data.get("balance") or 0) / 1e6)] + [(name, int(trc.get(c) or 0) / 10 ** dec) for name, c, dec in TRON_TOKENS]


async def _sol(client, addr: str) -> list[tuple[str, float]]:
    calls = [("getBalance", [addr])] + [("getTokenAccountsByOwner", [addr, {"mint": mint}, {"encoding": "jsonParsed"}])
                                         for _, mint in SOL_TOKENS]
    res = await _rpc(client, SOL_RPCS, calls)
    out = [("SOL", int(res[0]["value"]) / 1e9)]
    for (name, _), r in zip(SOL_TOKENS, res[1:]):
        out.append((name, sum(float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0) for a in r["value"])))
    return out


async def _btc(client, addr: str) -> list[tuple[str, float]]:
    r = await client.get(f"https://mempool.space/api/address/{addr}", timeout=10)
    r.raise_for_status()
    j = r.json()
    sat = sum(j[k]["funded_txo_sum"] - j[k]["spent_txo_sum"] for k in ("chain_stats", "mempool_stats"))
    return [("BTC", sat / 1e8)]


async def wallet(client: httpx.AsyncClient, w: dict, prices: dict) -> dict:
    net, addr = w["network"], w["address"]
    out = {"network": net, "network_name": NETWORKS.get(net, net), "address": addr, "label": w.get("label") or short(addr),
           "ok": False, "error": "", "assets": [], "total_usdt": 0.0}
    try:
        vals = await (_evm(client, net, addr) if net in EVM else _tron(client, addr) if net == "tron"
                      else _sol(client, addr) if net == "sol" else _btc(client, addr))
    except Exception as e:
        out["error"] = str(e)[:200]
        return out
    for asset, amt in vals:
        if amt > (0.005 if asset in STABLE else 1e-8):       # 먼지(0.00 으로 보이는 양)는 빼고
            out["assets"].append({"asset": asset, "amount": amt, "usdt": _usdt(asset, amt, prices)})
    out["total_usdt"] = sum(a["usdt"] or 0 for a in out["assets"])
    out["ok"] = True
    return out


# ── 전체 ──
_cache: dict = {}


async def _mt5() -> list[dict]:
    """잠금을 켠 MT5 계좌의 평가금(증거금) - 계좌 통화는 USD 로 보고 USDT 와 1:1 로 더함"""
    from sqlalchemy import select
    from db.models.account import Account
    from db.session import AsyncSessionLocal
    from services.worker_client import worker_get
    async with AsyncSessionLocal() as db:
        accs = (await db.execute(select(Account).where(Account.lock_enabled == True).order_by(Account.id))).scalars().all()

    async def one(a):
        try:
            info = await asyncio.wait_for(worker_get(a.id, "/account-info"), 5)
            return {"account_number": a.account_number, "ok": True, "equity": float(info.get("equity") or 0),
                    "balance": float(info.get("balance") or 0), "margin": float(info.get("margin") or 0),
                    "currency": info.get("currency") or "USD"}
        except Exception:
            return {"account_number": a.account_number, "ok": False, "equity": 0.0, "currency": ""}
    return list(await asyncio.gather(*(one(a) for a in accs)))


async def summary(refresh: bool = False, bank_force: bool | None = None) -> dict:
    """refresh: 지갑·바이낸스 새로 조회. bank_force: 은행(CODEF, 하루 100회)도 10분 넘었으면 새로 - 기본은 refresh 와 같음"""
    if not refresh and _cache.get("at", 0) > time.time() - CACHE_SEC:
        return _cache["data"]
    c = cfg()
    async with httpx.AsyncClient() as client:
        try:
            prices = await _prices(client)
        except Exception:
            prices = {}
        bn, krw, mt5, *ws = await asyncio.gather(binance(client, prices), _krw_rate(client), _mt5(),
                                                 *(wallet(client, w, prices) for w in c["list"]))
    try:                                                                                          # 은행 (원화)
        from services import banks
        bk = await banks.summary(force=refresh if bank_force is None else bank_force)
    except Exception as e:
        bk = {"banks": [], "total_krw": 0.0, "error": str(e)[:200]}
    total = (bn["total_usdt"] if bn["ok"] else 0) + sum(w["total_usdt"] for w in ws if w["ok"])   # 지갑 합계
    mt5_total = sum(m["equity"] for m in mt5 if m["ok"])                                         # MT5 평가금 합계
    bank_krw = bk["total_krw"]
    bank_usdt = bank_krw / krw if krw else 0.0
    grand = total + mt5_total + bank_usdt
    data = {"binance": bn, "wallets": ws, "mt5": mt5, "banks": bk["banks"], "total_usdt": total, "mt5_total": mt5_total,
            "bank_krw": bank_krw, "bank_usdt": bank_usdt,
            "wallet_pct": total / mt5_total * 100 if mt5_total > 0 else None,      # 지갑이 MT5 평가금의 몇 %
            "grand_usdt": grand, "krw_rate": krw,
            "total_krw": total * krw if krw else None,
            "grand_krw": (total + mt5_total) * krw + bank_krw if krw else None,
            "notify": c["notify"], "at": int(time.time())}
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
    for w in d["wallets"]:
        L.append("")
        name = html.escape(w["label"]) + (f" ({short(w['address'])})" if w["label"] != short(w["address"]) else "")
        if w["ok"]:
            L.append(f"<b>{html.escape(w['network_name'])} · {name}</b>  {_n(w['total_usdt'])} USDT")
            L += [f"   {_amt(a['asset'], a['amount'])}" + (f" ≈ {_n(a['usdt'])}" if a["usdt"] is not None and a["asset"] not in STABLE else "")
                  for a in w["assets"]] or ["   비어 있음"]
        else:
            L.append(f"<b>{html.escape(w['network_name'])} · {name}</b>  {html.escape(w['error'])}")
    mt5 = d.get("mt5") or []
    if mt5:
        L.append("")
        L.append(f"<b>MT5 평가금(증거금)</b>  {_n(d['mt5_total'])}")
        L += [f"   {html.escape(m['account_number'])}  " + (f"{_n(m['equity'])} {html.escape(m['currency'])}" if m["ok"] else "MT5 연결 안 됨")
              for m in mt5]
    bks = d.get("banks") or []
    if bks:
        from datetime import datetime as _dt
        L.append("")
        L.append(f"<b>은행 (원화)</b>  {d['bank_krw']:,.0f}원")
        for x in bks:
            age = int(time.time() - x["at"]) if x.get("at") else None
            when = "" if age is None else (f"{age // 60}분 전" if age < 3600 else f"{age // 3600}시간 전" if age < 86400 else f"{age // 86400}일 전")
            tag = "직접 입력" if x["source"] == "manual" else "자동"
            L.append(f"   {html.escape(x['name'])}  {x['amount']:,.0f}원  <i>({tag}{' · ' + when if when else ''})</i>"
                     + (f"\n   ⚠️ {html.escape(x['error'])}" if not x["ok"] else ""))
    krw = d.get("krw_rate")
    won = lambda v: f" ≈ {v * krw:,.0f}원" if krw else ""
    L += ["", "━━━━━━━━━━━━━━",
          f"지갑 합계 <b>{_n(d['total_usdt'])} USDT</b>{won(d['total_usdt'])}"
          + (f"\n   = MT5 평가금의 <b>{d['wallet_pct']:.1f}%</b>" if d.get("wallet_pct") is not None else "")]
    if mt5:
        L.append(f"MT5 평가금 <b>{_n(d['mt5_total'])}</b>{won(d['mt5_total'])}")
    if bks:
        L.append(f"은행 <b>{d['bank_krw']:,.0f}원</b>")
    if mt5 or bks:
        parts = " + ".join(x for x, ok in (("지갑", True), ("MT5", bool(mt5)), ("은행", bool(bks))) if ok)
        L.append(f"전체 ({parts}) <b>{_n(d['grand_usdt'])}</b>" + (f" ≈ <b>{d['grand_krw']:,.0f}원</b>" if d.get("grand_krw") else ""))
    if krw:
        L.append(f"<i>1 USDT = {krw:,.0f}원 · MT5 계좌 달러는 USDT 와 1:1 로 계산</i>")
    deps = [x for x in (bn.get("deposits") or []) if x["done"]][:3]
    if deps:
        L.append("")
        L.append("최근 바이낸스 입금")
        L += [f"   {datetime.fromtimestamp(x['time']):%m/%d %H:%M}  +{_amt(x['coin'], x['amount'])} ({x['network']})" for x in deps]
    if not d["wallets"]:
        L.append("\n<i>개인 지갑 추가: /잔고 추가 네트워크 주소 이름 (예: /잔고 추가 tron T… 지갑1)</i>")
    return "\n".join(L)[:4000]


# ── 입금 알림 ──
def _state_path() -> str:
    from services.settings_service import DATA_DIR
    return os.path.join(DATA_DIR, "wallets_state.json")


async def check_once(send) -> None:
    """send(text) - 바이낸스 새 입금 · 개인 지갑 잔고 늘어남 → 알림. 첫 실행은 기준만 저장"""
    if not cfg()["notify"]:
        return
    d = await summary(refresh=True, bank_force=False)       # 은행은 1시간마다만 (CODEF 데모 하루 100회)
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
    bal = st.get("wallets") or {f"bsc:{k}": v for k, v in (st.get("bsc") or {}).items()}
    for w in d["wallets"]:
        if not w["ok"]:
            continue
        now = {a["asset"]: a["amount"] for a in w["assets"]}
        wkey = f"{w['network']}:{w['address'].lower()}"
        old = bal.get(wkey)
        if old is not None:
            for asset, amt in now.items():
                up = amt - (old.get(asset) or 0)
                if up > (0.01 if asset in STABLE else 0.0001):
                    msgs.append(f"📥 <b>{w['network_name']} 지갑 입금</b> · {w['label']}  +{_amt(asset, up)} (잔고 {_amt(asset, amt)})")
        bal[wkey] = now
    st["wallets"] = bal
    st.pop("bsc", None)
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
            if c["binance"] or c["list"]:
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
            # /잔고 추가 [네트워크] 주소 [이름] - 네트워크를 빼면 주소 모양으로 짐작 (0x… 는 BSC)
            if len(w) < 2:
                raise ValueError("예) /잔고 추가 tron T… 지갑1 · /잔고 추가 eth 0x… 메타마스크\n네트워크: " + " · ".join(NETWORKS))
            net = net_key(w[1])
            addr, rest = (w[2], w[3:]) if net and len(w) > 2 else (w[1], w[2:])
            net = net or guess_net(addr)
            if not net:
                raise ValueError("주소 모양을 모르겠습니다. 네트워크를 앞에 붙여 주세요. 예) /잔고 추가 sol 주소\n네트워크: " + " · ".join(NETWORKS))
            cur = cfg()["list"]
            cur.append({"network": net, "address": addr, "label": " ".join(rest) or f"지갑{len(cur) + 1}"})
            save_cfg(lst=cur)
            await live._send(client, token, cid, f"➕ {NETWORKS[net]} 지갑 추가: {html.escape(short(addr))}")
        elif w and w[0] in ("삭제", "del", "remove"):
            cur = cfg()["list"]
            i = int(w[1]) - 1 if len(w) > 1 and w[1].isdigit() else -1
            if not 0 <= i < len(cur):
                lst = "\n".join(f"{n + 1}. {NETWORKS[x['network']]} {html.escape(x['label'] or '')} {short(x['address'])}" for n, x in enumerate(cur)) or "(없음)"
                raise ValueError(f"지울 번호를 붙여 주세요. 예) /잔고 삭제 1\n{lst}")
            gone = cur.pop(i)
            save_cfg(lst=cur)
            await live._send(client, token, cid, f"🗑 지갑 삭제: {NETWORKS[gone['network']]} {html.escape(gone['label'])} {short(gone['address'])}")
        elif w and w[0] in ("목록", "list"):
            cur = cfg()["list"]
            await live._send(client, token, cid, "개인 지갑\n" + ("\n".join(
                f"{n + 1}. {NETWORKS[x['network']]} · {html.escape(x['label'] or '')}\n<code>{x['address']}</code>" for n, x in enumerate(cur)) or "(없음)"))
            return
        elif w and w[0] in ("은행", "bank"):
            # /잔고 은행 토스 1234000 (직접 입력) · /잔고 은행 토스 삭제
            from services import banks
            if len(w) < 3:
                lst = "\n".join(f"{m['name']}  {m['amount']:,.0f}원" for m in banks.cfg()["manual"]) or "(없음)"
                raise ValueError(f"예) /잔고 은행 토스 1234000 · /잔고 은행 토스 삭제\n직접 입력한 은행:\n{lst}")
            name, val = w[1], w[2]
            if val in ("삭제", "del"):
                banks.set_manual([m for m in banks.cfg()["manual"] if m["name"] != name])
                await live._send(client, token, cid, f"🗑 {html.escape(name)} 지웠습니다")
            else:
                try:
                    amt = float(val.replace(",", "").replace("원", ""))
                except ValueError:
                    raise ValueError("금액은 숫자로. 예) /잔고 은행 토스 1234000")
                banks.set_manual_one(name, amt)
                _cache.clear()
                await live._send(client, token, cid, f"🏦 {html.escape(name)} {amt:,.0f}원 저장")
        elif w and w[0] in ("알림", "notify"):
            on = not (len(w) > 1 and w[1] in ("끄기", "off"))
            save_cfg(notify=on)
            await live._send(client, token, cid, f"입금 알림 {'켬' if on else '끔'} (바이낸스 입금 · 개인 지갑 잔고 늘어남, 5분마다 확인)")
            return
    except ValueError as e:
        await live._send(client, token, cid, html.escape(str(e)))
        return
    try:
        d = await summary(refresh=True)
    except Exception as e:
        await live._send(client, token, cid, f"잔고 조회 실패: {html.escape(str(e))[:200]}")
        return
    await live._send(client, token, cid, text(d) + "\n\n<i>/잔고 추가 네트워크 주소 이름 · /잔고 삭제 번호 · /잔고 목록 · /잔고 은행 토스 1234000 · /잔고 알림 끄기</i>")
