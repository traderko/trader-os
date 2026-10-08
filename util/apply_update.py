# util/apply_update.py
#
# start.bat 이 서버가 꺼진 뒤 실행한다 (data/update/apply.flag 가 있을 때만).
# services/updater.py 가 받아 둔 새 버전 파일로 이 폴더를 바꾼다.
#
#   그대로 두는 것: data/ (설정·DB·키), uploads/ (사진), logs/, venv/, .env, secrets/, .git/
#   start.bat 은 실행 중인 배치 파일이라 바로 못 바꿔서 start.bat.new 로 두고, 다음 실행 때 start.bat 이 스스로 바꿈
#   지난 버전에만 있던 파일은 지운다 (data/update/manifest.json 에 지난 버전 파일 목록을 기록)
#   마지막에 requirements.txt 대로 패키지를 맞춘다
#
# 표준 라이브러리만 쓴다 (프로젝트 파일이 바뀌는 중이라 import 하지 않음).

import json
import os
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPDATE_DIR = os.path.join(BASE, "data", "update")
FLAG = os.path.join(UPDATE_DIR, "apply.flag")
MANIFEST = os.path.join(UPDATE_DIR, "manifest.json")

KEEP_DIRS = {"data", "uploads", "logs", "venv", ".venv", "secrets", ".git", "__pycache__"}
KEEP_FILES = {".env"}


def _files(root: str) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            dirnames[:] = [d for d in dirnames if d not in KEEP_DIRS]
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for f in filenames:
            rel = os.path.normpath(os.path.join(rel_dir, f)) if rel_dir != "." else f
            if rel in KEEP_FILES:
                continue
            out.append(rel.replace("\\", "/"))
    return out


def main() -> int:
    if not os.path.isfile(FLAG):
        return 0
    flag = json.load(open(FLAG, encoding="utf-8"))
    src = flag.get("source") or ""
    if not os.path.isfile(os.path.join(src, "main.py")):
        print(f"[update] 새 버전 파일을 찾지 못했습니다: {src}")
        os.remove(FLAG)
        return 1

    print(f"[update] {flag.get('version')} 로 업데이트합니다...")
    new_files = _files(src)

    # 지난 버전에만 있던 파일 지우기
    try:
        old_files = json.load(open(MANIFEST, encoding="utf-8"))
    except (OSError, ValueError):
        old_files = []
    for rel in set(old_files) - set(new_files):
        p = os.path.join(BASE, rel)
        if os.path.isfile(p) and rel != "start.bat":
            try:
                os.remove(p)
                print(f"  - 삭제 {rel}")
            except OSError as e:
                print(f"  ! 삭제 실패 {rel}: {e}")

    # 새 파일 복사
    changed = 0
    for rel in new_files:
        s = os.path.join(src, rel)
        d = os.path.join(BASE, rel)
        if rel == "start.bat":
            if os.path.isfile(d) and open(s, "rb").read() == open(d, "rb").read():
                continue
            d = d + ".new"          # 실행 중인 배치 파일 - 다음 실행 때 start.bat 이 바꿈
        os.makedirs(os.path.dirname(d) or BASE, exist_ok=True)
        if os.path.isfile(d) and open(s, "rb").read() == open(d, "rb").read():
            continue
        shutil.copyfile(s, d)
        changed += 1
    print(f"[update] 파일 {changed}개를 바꿨습니다.")

    os.makedirs(UPDATE_DIR, exist_ok=True)
    json.dump(new_files, open(MANIFEST, "w", encoding="utf-8"))

    # 패키지 맞추기
    req = os.path.join(BASE, "requirements.txt")
    if os.path.isfile(req):
        print("[update] 패키지를 확인합니다...")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", req])
        if r.returncode != 0:
            print("[update] ⚠️ 패키지 설치 중 오류 - install.bat 을 한 번 실행해 보세요.")

    os.remove(FLAG)
    shutil.rmtree(os.path.join(UPDATE_DIR, "new"), ignore_errors=True)
    print(f"[update] 완료 - {flag.get('version')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
