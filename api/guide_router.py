# api/guide_router.py
#
# 가이드 (docs/guide/*.md) 를 웹 화면 📖 도움말과 텔레그램 /도움말 에서 보여 주기 위한 읽기 전용 API
#   GET /guide          → 목차 [{name, title}] (docs/guide/README.md 에 나오는 순서, 나머지는 뒤에)
#   GET /guide/{name}   → {name, title, markdown}   name: README · start · margin … (확장자 없이)
# 문서는 저장소와 같이 업데이트됨 (github.com/traderko/trader-os/tree/main/docs/guide 와 같은 내용)

import os
import re

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/guide", tags=["guide"])
GUIDE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "guide")
GITHUB = "https://github.com/traderko/trader-os/blob/main/docs/guide/"
_NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def read(name: str) -> str | None:
    if not _NAME.match(name or ""):
        return None
    p = os.path.join(GUIDE_DIR, name + ".md")
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def title_of(text: str, name: str) -> str:
    return next((ln[2:].strip() for ln in text.splitlines() if ln.startswith("# ")), name)


def pages() -> list[dict]:
    """목차 순서: README 에서 처음 링크된 순서 → 남은 파일은 이름순"""
    try:
        files = sorted(f[:-3] for f in os.listdir(GUIDE_DIR) if f.endswith(".md"))
    except OSError:
        return []
    idx = read("README") or ""
    order = []
    for m in re.finditer(r"\]\(([A-Za-z0-9_-]+)\.md", idx):
        if m.group(1) in files and m.group(1) not in order:
            order.append(m.group(1))
    order += [f for f in files if f not in order and f != "README"]
    out = [{"name": "README", "title": "가이드 처음 화면"}] if "README" in files else []
    for n in order:
        out.append({"name": n, "title": title_of(read(n) or "", n)})
    return out


@router.get("")
def list_guide():
    return {"pages": pages(), "github": GITHUB}


@router.get("/{name}")
def get_guide(name: str):
    text = read(name)
    if text is None:
        raise HTTPException(404, "문서가 없습니다")
    return {"name": name, "title": title_of(text, name), "markdown": text, "github": GITHUB + name + ".md"}
