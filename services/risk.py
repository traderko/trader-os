# services/risk.py
#
# 강제청산(스탑아웃)까지 몇 포인트 - 차트에서 바로 판단할 수 있게 (텔레그램 실시간 현황 · 증거금 경고 · 웹 포지션 탭)
#
#   스탑아웃 = 마진 레벨(평가금 ÷ 사용 증거금 × 100)이 브로커가 정한 % (account_info.margin_so_so) 아래로
#   견딜 수 있는 손실 = 평가금 − 사용 증거금 × 스탑아웃% / 100
#   종목별 1포인트 손익 = Σ 랏 × 계약 크기 × (BUY +1 / SELL −1)   → 포인트 = 견딜 손실 ÷ |1포인트 손익|
#   ※ 그 종목만 움직이고 나머지는 그대로라고 볼 때 · 가격이 움직이면 증거금도 조금 바뀌므로 대략값 · 스왑·수수료 제외

def stop_out_room(info: dict) -> tuple[float, float] | None:
    """(견딜 수 있는 손실, 스탑아웃 %) - 증거금을 안 쓰고 있으면 None"""
    eq, used = float(info.get("equity") or 0), float(info.get("margin") or 0)
    if used <= 0 or eq <= 0:
        return None
    so = float(info.get("margin_so_so") or 0)
    if int(info.get("margin_so_mode") or 0) == 1:       # 금액으로 정한 브로커 (드묾)
        return eq - so, so
    so = so or 50.0                                      # 못 받으면 흔한 값 50%
    return eq - used * so / 100, so


def liquidation(info: dict, positions: list[dict]) -> list[dict]:
    """종목별 강제청산까지 거리 [{symbol, side, lots, points, pct, price, now, so}] (|1포인트 손익| 큰 순)"""
    r = stop_out_room(info)
    if not r:
        return []
    room, so = r
    by: dict[str, dict] = {}
    for p in positions:
        cs = p.get("contract_size")
        if not cs:
            continue
        d = by.setdefault(p["symbol"], {"pp": 0.0, "lots": 0.0, "now": p.get("price_current") or p.get("price_open"),
                                        "digits": p.get("digits")})
        sign = 1 if p.get("type") == 0 else -1
        d["pp"] += sign * float(p["volume"]) * float(cs)
        d["lots"] += sign * float(p["volume"])
    out = []
    for sym, d in by.items():
        if abs(d["pp"]) < 1e-12 or not d["now"]:
            continue                                     # 양방향으로 같은 양이면 그 종목만으로는 청산 안 됨
        pts = max(0.0, room) / abs(d["pp"])
        long_ = d["pp"] > 0
        price = d["now"] - pts if long_ else d["now"] + pts
        out.append({"symbol": sym, "side": "BUY" if long_ else "SELL", "lots": abs(d["lots"]), "per_point": abs(d["pp"]),
                    "points": pts, "pct": pts / d["now"] * 100, "price": price, "now": d["now"],
                    "digits": d["digits"], "so": so, "room": room})
    return sorted(out, key=lambda x: -x["per_point"])


def liq_line(x: dict) -> str:
    """'NAS100+ SELL 30lot · ▲232pt(0.8%) 가면 강제청산 (29,382)'"""
    d = x["digits"] if isinstance(x.get("digits"), int) else 2
    arrow = "▼" if x["side"] == "BUY" else "▲"
    return (f"{x['symbol']} {x['side']} {x['lots']:g}lot · {arrow}{x['points']:,.{min(d, 1) if x['points'] >= 10 else d}f}pt"
            f"({x['pct']:.1f}%) 가면 강제청산 ({x['price']:,.{d}f})")
