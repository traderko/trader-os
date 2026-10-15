# tools/check_secrets.py - 커밋하기 전에 비밀 정보로 보이는 문자열 검사 (tools/publish.ps1 이 부름)
#
#   python tools/check_secrets.py <파일> [<파일> ...]     걸리면 목록을 찍고 2 로 끝남
#
# 공개 저장소라 텔레그램 봇 토큰·DB 비밀번호·개인 키 등이 들어가면 안 된다.

import re
import sys

SECRET_PATTERNS = [
    (r"postgres(ql)?(\+\w+)?://[^:\s/]+:[^@\s]+@", "DB 비밀번호가 들어간 주소"),
    (r"\b\d{8,10}:AA[\w-]{30,}", "텔레그램 봇 토큰"),
    (r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----", "개인 키"),
    (r"firebase" r"-adminsdk", "Firebase 키 파일 이름"),   # 나눠 써서 이 파일 자신은 안 걸리게
    (r"-100\d{10}", "텔레그램 채널 id"),
]
PLACEHOLDERS = ("비밀번호", "password", "PASSWORD", "(.*?)", "<", "USER:PASS")


def check(paths: list[str]) -> list[str]:
    problems = []
    for path in paths:
        try:
            raw = open(path, "rb").read()
        except OSError:
            continue                      # 지운 파일
        if b"\0" in raw[:8000]:
            continue                      # 바이너리 (ex5 등)
        text = raw.decode("utf-8", errors="ignore")
        for pat, what in SECRET_PATTERNS:
            if any(not any(x in m.group(0) for x in PLACEHOLDERS) for m in re.finditer(pat, text)):
                problems.append(f"{path}: {what}")
    return problems


if __name__ == "__main__":
    found = check(sys.argv[1:])
    if found:
        print("   비밀 정보로 보이는 내용이 있어 멈춥니다. 고친 뒤 다시 실행하세요:")
        for p in found:
            print("     -", p)
        sys.exit(2)
