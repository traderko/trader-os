# TraderOS 작업 규칙

## 가이드 (docs/guide)
- 사용자가 쓰는 기능을 새로 만들거나 동작을 바꾸면 **같은 커밋에서 `docs/guide/` 문서도 고친다.**
  - 새 기능이면 기존 문서에 절을 추가하거나 새 문서를 만들고 `docs/guide/README.md` 목차에 넣는다.
  - 문서 틀: 무엇인지 → 언제 쓰는지 → 켜고 끄는 법(명령 · 화면 위치 · 기본값) → 실제 알림 예시(코드의 실제 문구 그대로) → 주의할 점.
  - 각 문서 첫 줄 `<!-- guide: commands=/a,/b settings=... -->` 에 그 문서가 설명하는 텔레그램 명령을 적는다.
- 텔레그램 명령을 추가하면 `services/telegram_live.py` 의 setMyCommands 목록에 넣고, `python tools/guide_check.py --strict` 가 통과해야 한다.
  `docs/guide/commands.md` 는 이 스크립트가 만든다 (직접 고치지 않음). 배포(`tools/publish.ps1`) 때도 자동으로 다시 만든다.
- 처음 쓰는 사람이 읽는다고 생각하고 쉬운 한국어로. 용어는 처음 나올 때 풀어 쓴다.

## 커밋 · 릴리스
- 커밋 메시지와 릴리스 노트(publish.msg)는 한국어로 쓴다.

## 주의
- `mt5_ea/tradeLock.mq5` 는 잠금 중 진입을 강제청산하는 EA 다. 고칠 때는 특히 조심하고, 바꾼 동작을 가이드에 반드시 적는다.
- 새 알림을 만들기 전에 같은 알림이 이미 있는지 먼저 찾는다 (특히 `services/guidelines.py` 의 지침 규칙).
