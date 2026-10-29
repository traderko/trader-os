# services/addon.py
#
# 첫 진입 추가 리밋 - 첫 진입이 끝나면 평균가에서 N KTR 불리한 곳(매도는 위, 매수는 아래)에 같은 랏 리밋 1개.
# 설정: 관리 화면 텔레그램 탭 '구간매매' → 첫 진입 추가 리밋 (data/settings.json "grid" 의 addon_*)
#
#   첫 진입 = 같은 계좌·종목·방향으로 열린 직접 거래 포지션이 '한 번의 진입'뿐일 때
#             (한 번의 진입 = 직전 체결에서 '나눠 진입 묶기'(지침 탭, 기본 60초) 안에 이어진 체결 전부 - 0.5랏씩 다다다도 하나)
#             구간매매(magic 717xxxxx)·추가 리밋 자신(718xxxxx)은 빼고 봄
#   마지막 체결 뒤 묶기 간격 + 5초 동안 더 안 들어오면 → 다 들어간 걸로 보고
#     자동(addon_auto)      : 설정한 시간봉 KTR 로 바로 걸고 [❌ 취소] 버튼 알림
#     물어보기(addon_ask)   : 2분·5분·10분·1시간 KTR 표 + 시간봉 버튼 (누르면 걸림)
#   주기 확인 (20초마다)
#     첫 진입 포지션이 다 없어졌으면 → 리밋 취소 (포지션 없이 리밋만 남지 않게)
#     SL 따라가기(addon_sl_follow): 첫 진입의 SL 을 리밋에도. SL 이 리밋 가격 안쪽으로 오면(본전 SL 등) 리밋 취소
#
# 실제 주문은 계좌 워커(api/worker/worker_grid.py /grid/addon*)가 MT5 로 넣는다.

import asyncio
import html
import random
import secrets
import time

ADDON_LO, ADDON_HI = 71_800_000, 71_899_999
TFS = ["2m", "5m", "10m", "1h"]
TF_KR = {"2m": "2분", "5m": "5분", "10m": "10분", "1h": "1시간"}
LOOP_SEC = 20

_timers: dict[tuple, asyncio.Task] = {}     # (계좌, 종목, 방향) → 첫 진입이 끝나길 기다리는 중
_props: dict[str, dict] = {}                # 텔레그램 버튼 → 제안 내용 (6시간 보관)
_bg: set = set()


def cfg() -> dict:
    from services.settings_service import get_settings
    return get_settings().get("grid") or {}


def is_addon(magic) -> bool:
    try:
        return ADDON_LO <= int(magic or 0) <= ADDON_HI
    except (TypeError, ValueError):
        return False


def _manual(p: dict) -> bool:
    """직접 거래 포지션 (구간매매·추가 리밋 아님)"""
    from services.grid import kind_of
    return not kind_of(p.get("magic")) and not is_addon(p.get("magic"))


def _side(v) -> str:
    s = str(v).lower()
    return "buy" if s.startswith("b") or s == "0" else "sell"


def _fmt(v: float, d) -> str:
    d = d if isinstance(d, int) else 2
    return f"{v:,.{d}f}"


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)


def _send(title: str, body: str, buttons: list | None = None) -> None:
    try:
        from services.fcm_service import get_fcm_service
        get_fcm_service().send(title, body, '{"type":"addon"}', buttons=buttons)
    except Exception as e:
        print(f"[addon] 알림 실패: {e}")


# ── 체결이 들어올 때 (api/trade.py) ────────────────────────────────────
def on_open(account_id: int, account_number: str, symbol: str, side: str, magic) -> None:
    c = cfg()
    if not (c.get("addon_auto") or c.get("addon_ask", True)):
        return
    from services.grid import kind_of
    if kind_of(magic) or is_addon(magic):
        return
    key = (account_id, symbol, _side(side))
    old = _timers.get(key)
    if old and not old.done():
        old.cancel()                          # 아직 나눠 들어가는 중 - 마지막 체결부터 다시 기다림
    _timers[key] = asyncio.create_task(_after_quiet(account_id, account_number, symbol, _side(side)))


def on_close(account_id: int) -> None:
    """청산 체결 - 첫 진입이 다 없어졌으면 리밋을 바로 정리 (주기 확인을 기다리지 않게)"""
    _spawn(_later_sweep(account_id))


async def _later_sweep(account_id: int) -> None:
    await asyncio.sleep(2)
    try:
        await _sweep(account_id)
    except Exception as e:
        print(f"[addon] 정리 실패: {e}")


async def _after_quiet(aid: int, accnum: str, symbol: str, side: str) -> None:
    from services.guidelines import entry_gap
    try:
        await asyncio.sleep(entry_gap() + 5)
    except asyncio.CancelledError:
        return
    try:
        await _evaluate(aid, accnum, symbol, side)
    except Exception as e:
        print(f"[addon] 첫 진입 확인 실패: {e}")


def _common_sl(ps: list[dict]) -> float:
    """포지션들의 SL - 다르면 랏이 가장 많이 걸린 값, 없으면 0"""
    w: dict[float, float] = {}
    for p in ps:
        if p.get("sl"):
            w[p["sl"]] = w.get(p["sl"], 0) + p["volume"]
    return max(w, key=w.get) if w else 0.0


async def _ktrs(aid: int, symbol: str, src: str) -> dict:
    from services import grid
    from services.symbols import group_of
    g = group_of(symbol)
    group = g["key"] if g else symbol

    async def one(tf):
        try:
            k = await grid.compute_ktr(aid, group, symbol if src == "mt5" else group, src, tf)
            return tf, {"ktr": float(k["ktr"]), "session": k.get("session_name")}
        except Exception as e:
            return tf, {"error": str(getattr(e, "detail", e))[:80]}
    return dict(await asyncio.gather(*(one(tf) for tf in TFS)))


async def _evaluate(aid: int, accnum: str, symbol: str, side: str) -> None:
    from services.guidelines import entry_gap
    from services.worker_client import worker_get
    c = cfg()
    positions = await worker_get(aid, "/positions-all")
    same = [p for p in positions if p["symbol"] == symbol and _side(p["type"]) == side]
    base = [p for p in same if _manual(p)]
    if not base or any(is_addon(p.get("magic")) for p in same):
        return                                    # 포지션이 없어졌거나 추가 리밋이 이미 체결돼 있음
    times = sorted(int(p.get("time") or 0) for p in base)
    if any(b - a > entry_gap() for a, b in zip(times, times[1:])):
        return                                    # 이미 들고 있던 포지션에 더 들어간 것 - 첫 진입 아님
    addons = await worker_get(aid, "/grid/addons")
    if any(o["symbol"] == symbol and o["side"] == side for o in addons):
        return                                    # 이미 걸어 둠
    lots = round(sum(p["volume"] for p in base), 8)
    avg = sum(p["price_open"] * p["volume"] for p in base) / lots
    digits = base[0].get("digits")
    sl = _common_sl(base) if c.get("addon_sl_follow", True) else 0.0
    now = base[0].get("price_current") or avg
    mult = float(c.get("addon_mult") or 1.0)
    src = c.get("addon_src") or "mt5"
    ktrs = await _ktrs(aid, symbol, src)
    prop = {"aid": aid, "accnum": accnum, "symbol": symbol, "side": side, "lots": lots, "avg": avg, "sl": sl,
            "digits": digits, "mult": mult, "ktrs": ktrs, "src": src, "n": len(base), "at": time.time(), "now": now}
    for tf, k in ktrs.items():
        if "ktr" in k:
            k["price"] = round(avg + (mult * k["ktr"] if side == "sell" else -mult * k["ktr"]), digits if isinstance(digits, int) else 5)
            k["why"] = _invalid(prop, k["price"])
    head = (f"계좌 {accnum} · {symbol} {side.upper()} {lots:g}lot" + (f" ({len(base)}번 나눠)" if len(base) > 1 else "")
            + f" · 평균 {_fmt(avg, digits)}" + (f" · SL {_fmt(sl, digits)}" if sl else " · SL 없음"))
    if c.get("addon_auto"):
        tf = c.get("addon_tf") or "10m"
        k = ktrs.get(tf) or {}
        if "ktr" not in k:
            _send("⚠️ 추가 리밋 못 걸었음", f"{head}\n{TF_KR[tf]}봉 KTR 을 못 구했습니다: {k.get('error', '?')}")
            return
        if k["why"]:
            _send("⚠️ 추가 리밋 안 걸었음", f"{head}\n{TF_KR[tf]}봉 {mult:g}KTR → {_fmt(k['price'], digits)}: {k['why']}")
            return
        await _place_and_tell(prop, tf, title="➕ 추가 리밋 걸었음 (자동)")
        return
    # 물어보기: KTR 표 + 시간봉 버튼
    key = secrets.token_hex(3)
    _props[key] = prop
    for k_ in [k_ for k_, v in _props.items() if time.time() - v["at"] > 6 * 3600]:
        _props.pop(k_, None)
    _send("➕ 첫 진입 추가 리밋?", _table(prop), _buttons(key, prop))


def _invalid(prop: dict, price: float) -> str:
    """이 가격에 걸 수 없는 이유 (없으면 '')"""
    sell = prop["side"] == "sell"
    now = prop.get("now") or prop["avg"]
    if (price <= now) if sell else (price >= now):
        return "이미 지나감"
    sl = prop.get("sl")
    if sl and ((sl <= price) if sell else (sl >= price)):
        return "SL 너머"
    return ""


def _table(prop: dict) -> str:
    d = prop["digits"]
    sess = next((k.get("session") for k in prop["ktrs"].values() if k.get("session")), "")
    lines = [f"계좌 {prop['accnum']} · {prop['symbol']} {prop['side'].upper()} {prop['lots']:g}lot"
             + (f" ({prop['n']}번 나눠)" if prop["n"] > 1 else ""),
             f"평균 {_fmt(prop['avg'], d)}" + (f" · SL {_fmt(prop['sl'], d)}" if prop["sl"] else " · SL 없음"),
             f"\n{'위' if prop['side'] == 'sell' else '아래'}로 {prop['mult']:g}KTR 에 {prop['lots']:g}lot 리밋 · KTR ({sess} · {'OANDA' if prop['src'] == 'oanda' else 'MT5'})"]
    for tf in TFS:
        k = prop["ktrs"].get(tf) or {}
        if "ktr" in k:
            lines.append(f"{TF_KR[tf]:>4}  {_fmt(k['ktr'], d)} → {_fmt(k['price'], d)}" + (f"  ✕ {k['why']}" if k["why"] else ""))
        else:
            lines.append(f"{TF_KR[tf]:>4}  - ({k.get('error', '?')})")
    lines.append("\n시간봉을 누르면 그 가격에 겁니다. 첫 진입을 다 청산하면 리밋은 저절로 취소됩니다.")
    return "\n".join(lines)


def _buttons(key: str, prop: dict) -> list:
    d = prop["digits"]
    row, rows = [], []
    for tf in TFS:
        k = prop["ktrs"].get(tf) or {}
        if "ktr" in k and not k["why"]:
            row.append({"text": f"{TF_KR[tf]} {_fmt(k['price'], d)}", "callback_data": f"ad:p:{key}:{tf}"})
            if len(row) == 2:
                rows.append(row)
                row = []
    if row:
        rows.append(row)
    rows.append([{"text": "안 걸기", "callback_data": f"ad:n:{key}"}])
    return rows


async def _place(prop: dict, tf: str) -> dict:
    from services.worker_client import worker_get, worker_post
    # 누르는 사이 바뀌었을 수 있어 다시 확인
    positions = await worker_get(prop["aid"], "/positions-all")
    base = [p for p in positions if p["symbol"] == prop["symbol"] and _side(p["type"]) == prop["side"] and _manual(p)]
    if not base:
        raise RuntimeError("첫 진입 포지션이 이미 없습니다.")
    addons = await worker_get(prop["aid"], "/grid/addons")
    if any(o["symbol"] == prop["symbol"] and o["side"] == prop["side"] for o in addons):
        raise RuntimeError("이미 추가 리밋이 걸려 있습니다.")
    k = prop["ktrs"][tf]
    sl = _common_sl(base) if cfg().get("addon_sl_follow", True) else 0.0
    return await worker_post(prop["aid"], "/grid/addon", {
        "symbol": prop["symbol"], "side": prop["side"], "price": k["price"], "lot": prop["lots"], "sl": sl,
        "magic": ADDON_LO + random.randint(0, 99_999), "comment": f"TOS addon {TF_KR[tf]}"})


async def _place_and_tell(prop: dict, tf: str, title: str) -> str:
    try:
        r = await _place(prop, tf)
    except Exception as e:
        msg = str(getattr(e, "detail", e))[:200]
        _send("⚠️ 추가 리밋 실패", f"계좌 {prop['accnum']} · {prop['symbol']} {prop['side'].upper()}\n{msg}")
        return f"실패: {msg}"[:190]
    _send(title, _placed_text(prop, tf, r), [[{"text": "❌ 리밋 취소", "callback_data": f"ad:c:{prop['aid']}:{r['ticket']}"}]])
    return "걸었습니다"


def _placed_text(prop: dict, tf: str, r: dict) -> str:
    d = r.get("digits", prop["digits"])
    k = prop["ktrs"][tf]
    return (f"계좌 {prop['accnum']} · {prop['symbol']} {prop['side'].upper()} {r['lot']:g}lot 리밋 @{_fmt(r['price'], d)}"
            f"\n{TF_KR[tf]}봉 KTR {_fmt(k['ktr'], d)} × {prop['mult']:g} · 첫 진입 평균 {_fmt(prop['avg'], d)}"
            + (f" · SL {_fmt(r['sl'], d)}" if r.get("sl") else " · SL 없음")
            + "\n첫 진입을 다 청산하면 저절로 취소됩니다.")


# ── 텔레그램 버튼 (services/telegram_live.py 가 ad: 로 시작하는 버튼을 넘김) ──
async def button(live, client, token, chat_id: str, data: str, message_id) -> str:
    parts = data.split(":")
    act = parts[1] if len(parts) > 1 else ""
    if act == "c":                                # 걸어 둔 리밋 취소
        from services.worker_client import worker_post
        aid, ticket = int(parts[2]), int(parts[3])
        try:
            r = await worker_post(aid, "/grid/addon-cancel", {"ticket": ticket})
        except Exception as e:
            return f"취소 실패: {getattr(e, 'detail', e)}"[:190]
        note = "이미 체결됐거나 취소된 주문입니다" if r.get("gone") else "리밋을 취소했습니다"
        await live._call(client, token, "editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                         reply_markup={"inline_keyboard": []})
        await live._send(client, token, chat_id, f"❌ 추가 리밋 #{ticket}: {note}")
        return note
    prop = _props.get(parts[2]) if len(parts) > 2 else None
    if prop is None:
        await live._call(client, token, "editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                         reply_markup={"inline_keyboard": []})
        return "오래된 버튼입니다 (서버 재시작 등)"
    if act == "n":
        _props.pop(parts[2], None)
        await live._call(client, token, "editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                         reply_markup={"inline_keyboard": []})
        return "안 걸었습니다"
    if act == "p":
        tf = parts[3]
        if tf not in prop["ktrs"] or "price" not in prop["ktrs"][tf]:
            return "그 시간봉 KTR 이 없습니다"
        try:
            r = await _place(prop, tf)
        except Exception as e:
            return f"실패: {getattr(e, 'detail', e)}"[:190]
        _props.pop(parts[2], None)
        await live._call(client, token, "editMessageText", chat_id=chat_id, message_id=message_id, parse_mode="HTML",
                         text="✅ <b>추가 리밋 걸었음</b>\n" + html.escape(_placed_text(prop, tf, r)),
                         reply_markup={"inline_keyboard": [[{"text": "❌ 리밋 취소", "callback_data": f"ad:c:{prop['aid']}:{r['ticket']}"}]]})
        return "걸었습니다"
    return ""


# ── 주기 확인: 첫 진입이 없어지면 취소 · SL 따라가기 ───────────────────
async def _sweep(aid: int) -> None:
    from services.worker_client import worker_get, worker_post
    addons = await worker_get(aid, "/grid/addons")
    if not addons:
        return
    positions = await worker_get(aid, "/positions-all")
    follow = cfg().get("addon_sl_follow", True)
    for o in addons:
        base = [p for p in positions if p["symbol"] == o["symbol"] and _side(p["type"]) == o["side"] and _manual(p)]
        d = o.get("digits")
        what = f"{o['symbol']} {o['side'].upper()} {o['volume']:g}lot @{_fmt(o['price'], d)}"
        reason = ""
        if not base:
            reason = "첫 진입 포지션을 다 청산해서"
        elif follow:
            sl = _common_sl(base)
            sell = o["side"] == "sell"
            if sl and ((sl <= o["price"]) if sell else (sl >= o["price"])):
                reason = f"첫 진입 SL({_fmt(sl, d)})이 리밋 가격 안쪽으로 와서 (닿기 전에 손절됨)"
            elif sl and abs(sl - (o.get("sl") or 0)) > 1e-9:
                try:
                    await worker_post(aid, "/grid/addon-sl", {"ticket": o["ticket"], "sl": sl})
                    print(f"[addon] #{o['ticket']} SL {o.get('sl')} → {sl}")
                except Exception as e:
                    print(f"[addon] #{o['ticket']} SL 따라가기 실패: {getattr(e, 'detail', e)}")
        if reason:
            try:
                await worker_post(aid, "/grid/addon-cancel", {"ticket": o["ticket"]})
                _send("🗑 추가 리밋 취소", f"{what}\n{reason} 취소했습니다.")
            except Exception as e:
                print(f"[addon] #{o['ticket']} 취소 실패: {getattr(e, 'detail', e)}")


async def loop() -> None:
    from sqlalchemy import select
    from db.models.account import Account
    from db.session import AsyncSessionLocal
    await asyncio.sleep(40)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                ids = (await db.execute(select(Account.id).where(Account.lock_enabled == True))).scalars().all()
            for aid in ids:
                try:
                    await asyncio.wait_for(_sweep(aid), timeout=30)
                except Exception:
                    pass                          # 워커가 꺼져 있는 계좌 등
        except Exception as e:
            print(f"[addon] 주기 확인 실패: {e}")
        await asyncio.sleep(LOOP_SEC)
