"""가이드(docs/guide) 검사 · 명령 목록 자동 생성

  python tools/guide_check.py            : docs/guide/commands.md 를 새로 만들고, 문서가 없는 텔레그램 명령을 알려 줌
  python tools/guide_check.py --strict   : 위와 같고, 빠진 게 있으면 실패(종료 코드 1) - 배포 전에 씀

텔레그램 명령은 services/telegram_live.py 의 setMyCommands 목록에서 읽음 (봇 메뉴에 나오는 것과 같음)
문서는 각 파일 첫 줄의 <!-- guide: commands=/a,/b settings=... --> 로 어떤 명령을 설명하는지 밝힘
새 명령을 만들었는데 어느 문서에도 없으면 경고 → 그 기능 문서를 쓰거나 기존 문서 commands= 에 넣기
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUIDE = os.path.join(ROOT, "docs", "guide")
SKIP = {"README.md", "commands.md"}


def bot_commands() -> list[tuple[str, str]]:
    src = open(os.path.join(ROOT, "services", "telegram_live.py"), encoding="utf-8").read()
    return re.findall(r'\{"command":\s*"([^"]+)",\s*"description":\s*"([^"]*)"\}', src)


def pages() -> dict[str, dict]:
    out = {}
    for f in sorted(os.listdir(GUIDE)):
        if not f.endswith(".md") or f in SKIP:
            continue
        text = open(os.path.join(GUIDE, f), encoding="utf-8").read()
        m = re.match(r"<!--\s*guide:(.*?)-->", text.strip())
        meta = {}
        if m:
            for part in m.group(1).split():
                k, _, v = part.partition("=")
                meta[k] = [x.strip().lstrip("/") for x in v.split(",") if x.strip()]
        title = next((ln[2:].strip() for ln in text.splitlines() if ln.startswith("# ")), f)
        out[f] = {"title": title, "commands": meta.get("commands", []), "text": text}
    return out


def main() -> int:
    strict = "--strict" in sys.argv
    cmds, ps = bot_commands(), pages()
    where = {}
    for f, p in ps.items():
        for c in p["commands"]:
            where.setdefault(c, f)
    rows, missing = [], []
    for c, desc in cmds:
        f = where.get(c)
        if not f:
            missing.append(c)
        rows.append(f"| `/{c}` | {desc} | " + (f"[{ps[f]['title']}]({f})" if f else "—") + " |")
    md = ["<!-- 자동으로 만들어지는 파일 - 직접 고치지 마세요 (tools/guide_check.py) -->",
          "# 텔레그램 명령 전체 목록", "",
          "봇 메뉴(☰)에 나오는 명령과 같습니다. 한글 명령(`/증거금`, `/출금` 등)도 같은 기능입니다.", "",
          "| 명령 | 하는 일 | 자세한 설명 |", "|---|---|---|", *rows, ""]
    with open(os.path.join(GUIDE, "commands.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(md))
    # 목차(README)에서 가리키는 파일이 다 있는지
    idx = open(os.path.join(GUIDE, "README.md"), encoding="utf-8").read()
    broken = [l for l in set(re.findall(r"\]\(([a-z0-9-]+\.md)", idx)) if not os.path.exists(os.path.join(GUIDE, l))]
    for c in missing:
        print(f"[guide] 문서 없는 명령: /{c} - docs/guide 의 어떤 문서에도 commands= 로 적혀 있지 않음")
    for l in broken:
        print(f"[guide] 목차에 있는데 파일이 없음: {l}")
    print(f"[guide] 명령 {len(cmds)}개 · 문서 {len(ps)}개 · 빠진 명령 {len(missing)}개")
    return 1 if strict and (missing or broken) else 0


if __name__ == "__main__":
    sys.exit(main())
