"""잠금 풀기 확인 문구 고르기 - 중요도(★1~5)가 높을수록 자주 (지침 목록과 같은 방식, services/guidelines.py)

문구 묶음(잠금 종류마다 다름)마다 '한 바퀴'를 만들어 하나씩 꺼냄: 문구마다 중요도만큼 넣고 고르게 흩음.
★5 · ★1 · ★1 이면 7번 중 5 · 1 · 1번. 묶음이나 중요도가 바뀌면 바퀴를 새로 만듦
"""
from __future__ import annotations

import random

_bags: dict[tuple, list[str]] = {}


def weight_of(w) -> int:
    try:
        return max(1, min(5, int(w or 1)))
    except (TypeError, ValueError):
        return 1


def _cycle(items: list[tuple[str, int]]) -> list[str]:
    order = random.sample(items, len(items))
    total = sum(w for _, w in order)
    cur = {p: random.random() for p, _ in order}
    seq = []
    for _ in range(total):
        for p, w in order:
            cur[p] += w
        pick = max(order, key=lambda x: cur[x[0]])[0]
        cur[pick] -= total
        seq.append(pick)
    return seq


def pick(rows) -> str:
    """rows: (문구, 중요도) 묶음. 문구 하나"""
    items = {}
    for p, w in rows:
        items[p] = weight_of(w)
    items = sorted(items.items())
    if not items:
        raise ValueError("문구가 없습니다")
    sig = tuple(items)
    bag = _bags.get(sig)
    if not bag:
        bag = _bags[sig] = _cycle(items)
    if len(_bags) > 50:                                   # 오래된 묶음 정리
        for k in list(_bags)[:-20]:
            if k != sig:
                _bags.pop(k, None)
    return bag.pop(0)
