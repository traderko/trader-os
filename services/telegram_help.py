"""텔레그램 /도움말 (/help) - 가이드(docs/guide)를 텔레그램에서 보기

  /도움말              : 목차 (문서마다 버튼) + [🌐 GitHub 에서 보기]
  /도움말 증거금       : 제목 · 파일 이름 · 명령으로 찾아서 바로 그 문서
  /증거금 도움말       : 명령 뒤에 '도움말' 을 붙이면 그 명령을 설명한 문서 (모든 명령에서)
  버튼 hp:<문서>       : 같은 메시지를 그 문서로 고침 · [◀ 목차]

문서는 웹 📖 도움말 · GitHub 과 같은 파일 (api/guide_router.py). 마크다운을 텔레그램 HTML 로 간단히 바꿈
텔레그램 한 메시지는 4096자까지라 길면 앞부분만 보여 주고 'GitHub 에서 전체 보기' 버튼
"""
from __future__ import annotations

import html
import re

LIMIT = 3600
# 한글 명령 → 영어 명령 (가이드 문서 첫 줄 commands= 에는 영어로 적혀 있음)
ALIAS = {"잠금": "lock", "출금": "withdraw", "잔고": "wallet", "증거금": "margin", "돌파": "bob", "원웨이": "oneway",
         "손익": "pnl", "통계": "stats", "근거": "reasons", "지표": "events", "문구": "phrases", "지침": "guide",
         "무작위": "random", "계좌": "accounts", "서버": "server", "표시": "show", "봉": "bars"}
HELP_WORDS = ("도움말", "help")


def _pages() -> list[dict]:
    from api.guide_router import pages
    return pages()


def _meta_commands(text: str) -> list[str]:
    m = re.match(r"\s*<!--\s*guide:(.*?)-->", text)
    if not m:
        return []
    for part in m.group(1).split():
        k, _, v = part.partition("=")
        if k == "commands":
            return [x.strip().lstrip("/").lower() for x in v.split(",") if x.strip()]
    return []


def page_for_command(cmd: str) -> str | None:
    """명령(앞 / 빼고, 한글 가능) → 그 명령을 설명한 문서 이름"""
    from api.guide_router import read
    c = ALIAS.get(cmd, cmd).lower()
    for p in _pages():
        if p["name"] in ("README", "commands"):
            continue
        if c in _meta_commands(read(p["name"]) or ""):
            return p["name"]
    return None


def find(q: str) -> str | None:
    q = q.strip().lstrip("/").lower()
    if not q:
        return None
    ps = _pages()
    for p in ps:
        if q == p["name"].lower():
            return p["name"]
    hit = page_for_command(q)
    if hit:
        return hit
    for p in ps:
        if q in p["title"].lower():
            return p["name"]
    return None


def _inline(t: str) -> str:
    x = html.escape(t, quote=False)
    x = re.sub(r"`([^`]+)`", r"<code>\1</code>", x)
    x = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", x)
    x = re.sub(r"(^|[^*])\*([^*\s][^*]*)\*", r"\1<i>\2</i>", x)

    def link(m):
        txt, url = m.group(1), m.group(2)
        if re.match(r"^[A-Za-z0-9_-]+\.md(#.*)?$", url):
            return f"<b>{txt}</b>"                      # 다른 가이드 문서 - 아래 버튼으로
        if url.startswith("http"):
            return f'<a href="{html.escape(url)}">{txt}</a>'
        return txt
    return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, x)


def to_html(md: str) -> str:
    """마크다운 → 텔레그램 HTML (b · i · code · pre · a 만 씀)"""
    lines = re.sub(r"<!--.*?-->", "", md, flags=re.S).replace("\r", "").split("\n")
    out, i = [], 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("```"):
            buf = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append("<pre>" + html.escape("\n".join(buf), quote=False) + "</pre>")
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", ln)
        if m:
            out.append(("\n" if out else "") + ("📖 " if len(m.group(1)) == 1 else "▪️ ") + f"<b>{_inline(m.group(2))}</b>")
            i += 1
            continue
        if ln.lstrip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(lines[i])
                i += 1
            rows = [r for r in rows if not re.match(r"^\s*\|?\s*:?-{2,}", r)]
            for k, r in enumerate(rows):
                cells = [c.strip() for c in r.strip().strip("|").split("|")]
                if k == 0 and len(rows) > 1:
                    continue                                 # 머리줄은 빼고 내용만
                out.append("• " + " — ".join(_inline(c) for c in cells if c))
            continue
        if ln.lstrip().startswith(">"):
            out.append("<i>" + _inline(ln.lstrip()[1:].strip()) + "</i>")
            i += 1
            continue
        m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", ln)
        if m:
            mark = m.group(2) if m.group(2)[0].isdigit() else "•"
            out.append(("   ◦ " if m.group(1) else f"{mark} ") + _inline(m.group(3)))
            i += 1
            continue
        if ln.strip():
            out.append(_inline(ln.strip()))
        i += 1
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _cut(text: str) -> tuple[str, bool]:
    if len(text) <= LIMIT:
        return text, False
    cut = text.rfind("\n\n", 0, LIMIT)
    cut = cut if cut > LIMIT // 2 else text.rfind("\n", 0, LIMIT)
    body = text[:cut]
    if body.count("<pre>") > body.count("</pre>"):         # 코드 상자 중간에서 자르지 않게
        body = body[:body.rfind("<pre>")]
    return body.rstrip() + "\n\n<i>… 길어서 여기까지. 나머지는 아래 'GitHub 에서 전체 보기'</i>", True


def view(name: str) -> tuple[str, dict]:
    from api.guide_router import read, GITHUB
    md = read(name)
    if md is None:
        return index()
    text, _ = _cut(to_html(md))
    links = re.findall(r"\]\(([A-Za-z0-9_-]+)\.md", md)          # 이 문서가 가리키는 다른 문서 → 버튼
    seen, row, rows = set(), [], []
    titles = {p["name"]: p["title"] for p in _pages()}
    for n in links:
        if n in seen or n == name or n not in titles:
            continue
        seen.add(n)
        row.append({"text": titles[n][:28], "callback_data": f"hp:{n}"})
        if len(row) == 2:
            rows.append(row)
            row = []
        if len(seen) >= 8:
            break
    if row:
        rows.append(row)
    rows.append([{"text": "◀ 목차", "callback_data": "hp:README"},
                 {"text": "🌐 GitHub 에서 전체 보기", "url": GITHUB + name + ".md"}])
    return text, {"inline_keyboard": rows}


def index() -> tuple[str, dict]:
    from api.guide_router import GITHUB
    ps = [p for p in _pages() if p["name"] != "README"]
    rows, row = [], []
    for p in ps:
        row.append({"text": p["title"][:28], "callback_data": f"hp:{p['name']}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "🌐 GitHub 에서 보기 (카페 공유용)", "url": GITHUB + "README.md"}])
    text = ("📖 <b>TraderOS 가이드</b>\n기능마다 무엇인지 · 켜고 끄는 법 · 실제 알림 예시를 적었어요.\n"
            "처음이라면 <b>처음 시작하기 → 거래 잠금 → 실시간 현황</b> 순서로 보세요.\n\n"
            "<i>명령 뒤에 '도움말'을 붙이면 그 기능 설명이 바로 나와요. 예: /증거금 도움말</i>")
    return text, {"inline_keyboard": rows}


async def cmd(bot, client, token, cid: str, arg: str = "") -> None:
    name = find(arg) if arg.strip() else None
    if arg.strip() and not name:
        text, kb = index()
        text = f"'{html.escape(arg.strip())}' 에 맞는 문서를 못 찾았어요. 목차에서 골라 주세요.\n\n" + text
    else:
        text, kb = view(name) if name else index()
    await bot._send(client, token, cid, text, reply_markup=kb)


async def for_command(bot, client, token, cid: str, command: str) -> bool:
    """'/증거금 도움말' 처럼 명령 뒤 도움말 - 문서를 찾으면 보내고 True"""
    name = page_for_command(command.lstrip("/").lower())
    if not name:
        return False
    text, kb = view(name)
    await bot._send(client, token, cid, text, reply_markup=kb)
    return True


async def button(bot, client, token, cid: str, data: str, message_id: int | None) -> str:
    name = data.split(":", 1)[1] if ":" in data else "README"
    text, kb = index() if name == "README" else view(name)
    r = await bot._call(client, token, "editMessageText", chat_id=cid, message_id=message_id, text=text,
                        parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb)
    if not r.get("ok") and "not modified" not in str(r.get("description")):
        await bot._send(client, token, cid, text, reply_markup=kb)
    return ""
