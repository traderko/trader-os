# services/updater.py
#
# 자동 업데이트 - GitHub Releases 에서 새 버전을 확인하고, 받아서 설치 준비까지.
#
#   release.json (배포 zip 안에만 있음 - .github/workflows/release.yml 이 만들어 넣음)
#       {"version": "0.1.2", "repo": "owner/name"}
#     → 이 파일이 없으면 (git 으로 받은 개발 폴더, 개인 서버) 업데이트 기능을 쓰지 않는다.
#
#   1) 확인: 서버 시작 때와 6시간마다 GitHub API(releases/latest) 조회. 새 버전이면 텔레그램으로 한 번 알림
#   2) 적용: 관리 화면 [업데이트] → zip 을 data/update/ 에 받아 풀고, 표시 파일(apply.flag)을 만든 뒤 서버를 끈다
#   3) start.bat 이 서버가 꺼진 걸 보고 util/apply_update.py 로 파일을 바꾸고(data·uploads·venv·.env 는 그대로)
#      패키지를 맞춘 다음 서버를 다시 켠다. DB 구조 변경은 서버가 켜질 때 자동 (db/migrate.py)
#
# 업데이트는 사용자가 버튼을 눌렀을 때만 한다 (매매 중에 서버가 갑자기 재시작되지 않게).

import asyncio
import hashlib
import json
import os
import shutil
import signal
import threading
import time
import zipfile

import httpx

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE_FILE = os.path.join(BASE_DIR, "release.json")
UPDATE_DIR = os.path.join(BASE_DIR, "data", "update")
STATE_FILE = os.path.join(UPDATE_DIR, "state.json")
FLAG_FILE = os.path.join(UPDATE_DIR, "apply.flag")
NEW_DIR = os.path.join(UPDATE_DIR, "new")
CHECK_EVERY_SEC = 6 * 3600
GITHUB_API = os.getenv("TRADEROS_UPDATE_API", "https://api.github.com")   # 시험할 때만 바꿈

status: dict = {"checking": False, "downloading": False, "error": None, "progress": None}
_lock = threading.Lock()


def release_info() -> dict | None:
    try:
        with open(RELEASE_FILE, encoding="utf-8") as f:
            info = json.load(f)
        if info.get("version") and info.get("repo"):
            return info
    except (OSError, ValueError):
        pass
    return None


def _git_commit() -> str | None:
    """git 으로 받은 폴더면 지금 커밋 (앞 7자리) - 관리 화면 버전 표시용"""
    git = os.path.join(BASE_DIR, ".git")
    try:
        head = open(os.path.join(git, "HEAD"), encoding="utf-8").read().strip()
        if not head.startswith("ref:"):
            return head[:7]
        ref = head[4:].strip()
        p = os.path.join(git, *ref.split("/"))
        if os.path.isfile(p):
            return open(p, encoding="utf-8").read().strip()[:7]
        with open(os.path.join(git, "packed-refs"), encoding="utf-8") as f:
            for line in f:
                if line.strip().endswith(" " + ref):
                    return line.split()[0][:7]
    except OSError:
        pass
    return None


def version_label() -> dict:
    """관리 화면 위쪽에 보여줄 버전. 배포판: release.json 버전 / git 폴더: 커밋"""
    info = release_info()
    if info:
        return {"version": info["version"], "label": f"v{info['version']}", "release": True}
    c = _git_commit()
    return {"version": None, "label": f"개발판 {c}" if c else "개발판", "release": False}


def _ver(v: str) -> tuple:
    v = (v or "").strip().lstrip("vV")
    out = []
    for part in v.split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out + [0] * (3 - len(out)))


def _read_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_state(st: dict) -> None:
    os.makedirs(UPDATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def summary() -> dict:
    """관리 화면에 보여줄 상태"""
    info = release_info()
    st = _read_state()
    latest = st.get("latest") or {}
    current = info["version"] if info else None
    return {
        "enabled": info is not None,
        "current": current,
        "repo": info["repo"] if info else None,
        "latest": latest or None,
        "available": bool(info and latest.get("version") and _ver(latest["version"]) > _ver(current)),
        "checked_at": st.get("checked_at"),
        "pending_restart": os.path.exists(FLAG_FILE),
        **status,
    }


async def check(notify: bool = True) -> dict:
    """GitHub 에서 최신 릴리스를 확인"""
    info = release_info()
    if info is None:
        return summary()
    status.update(checking=True, error=None)
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.get(f"{GITHUB_API}/repos/{info['repo']}/releases/latest",
                            headers={"Accept": "application/vnd.github+json", "User-Agent": "TraderOS-updater"})
        if r.status_code == 404:
            raise RuntimeError("릴리스가 아직 없습니다.")
        r.raise_for_status()
        rel = r.json()
        asset = next((a for a in rel.get("assets") or []
                      if a.get("name", "").lower().startswith("traderos") and a["name"].lower().endswith(".zip")), None)
        latest = {
            "version": (rel.get("tag_name") or "").lstrip("vV"),
            "name": rel.get("name") or rel.get("tag_name"),
            "notes": (rel.get("body") or "")[:4000],
            "url": rel.get("html_url"),
            "published_at": rel.get("published_at"),
            "asset_url": asset.get("browser_download_url") if asset else None,
            "asset_size": asset.get("size") if asset else None,
            "asset_digest": asset.get("digest") if asset else None,   # "sha256:..." (GitHub 가 알려주면)
        }
        st = _read_state()
        st.update(latest=latest, checked_at=time.time())
        newer = _ver(latest["version"]) > _ver(info["version"])
        if notify and newer and st.get("notified") != latest["version"]:
            st["notified"] = latest["version"]
            try:
                from services.telegram_service import TelegramService
                title, body, buttons = offer_message(info["version"], latest)
                TelegramService().send(title, body, buttons)
            except Exception:
                pass
        _write_state(st)
    except Exception as e:
        status["error"] = f"업데이트 확인 실패: {e}"
    finally:
        status["checking"] = False
    return summary()


def _plain_notes(notes: str, limit: int = 1500) -> str:
    """릴리스 노트(마크다운)를 텔레그램용 일반 글로"""
    lines = []
    for ln in (notes or "").replace("\r", "").split("\n"):
        t = ln.strip()
        if not t or t.startswith("<!--"):
            continue
        t = t.lstrip("#").strip() if t.startswith("#") else t
        if t.startswith(("- ", "* ")):
            t = "• " + t[2:]
        t = t.replace("**", "").replace("__", "").replace("`", "")
        lines.append(t)
    out = "\n".join(lines)
    if len(out) > limit:
        out = out[:limit].rsplit("\n", 1)[0] + "\n…(더 보기는 아래 '변경 내용' 버튼)"
    return out


def offer_message(current: str, latest: dict) -> tuple[str, str, list]:
    """새 버전 알림 (제목, 본문, 버튼) - 텔레그램 [업데이트] 버튼 포함"""
    body = [f"현재 {current} → 새 버전 {latest['version']}"]
    if latest.get("name") and latest["name"].lstrip("vV") != latest["version"]:
        body.append(latest["name"])
    if latest.get("published_at"):
        body.append(f"공개: {latest['published_at'][:10]}")
    notes = _plain_notes(latest.get("notes") or "")
    if notes:
        body += ["", "📝 릴리스 노트", notes]
    body += ["", "업데이트하면 서버가 1~2분 꺼졌다 켜집니다 (그동안 잠금 감시도 멈춤).",
             "설정·DB·사진은 그대로 남습니다."]
    row = [{"text": "⬆️ 업데이트", "callback_data": f"upd:ask:{latest['version']}"}]
    if latest.get("url"):
        row.append({"text": "📄 변경 내용", "url": latest["url"]})
    return f"⬆️ TraderOS 새 버전 {latest['version']}", "\n".join(body), [row]


def _notify(title: str, body: str, buttons: list | None = None, wait: bool = False) -> None:
    try:
        from services.telegram_service import TelegramService
        t = TelegramService()
        if wait:
            t.send_now(title, body, buttons)      # 서버가 곧 꺼질 때 - 보내고 나서 끄도록
        else:
            t.send(title, body, buttons)
    except Exception:
        pass


def _report_after_restart() -> None:
    """업데이트로 다시 켜졌으면 결과를 텔레그램으로 알림"""
    st = _read_state()
    job = st.pop("applying", None)
    if not job:
        return
    _write_state(st)
    info = release_info() or {}
    now = info.get("version")
    if now and _ver(now) >= _ver(job.get("to") or ""):
        _notify("✅ TraderOS 업데이트 완료", f"{job.get('from')} → {now}\n서버가 다시 켜졌습니다.")
    else:
        _notify("⚠️ TraderOS 업데이트 확인 필요",
                f"{job.get('from')} → {job.get('to')} 업데이트 후 버전이 {now} 입니다.\n"
                f"start.bat 창의 [update] 메시지를 확인하거나, 관리 화면 '업데이트' 탭에서 다시 시도하세요.")


async def loop() -> None:
    """서버 시작 때와 6시간마다 확인"""
    if release_info() is None:
        return
    await asyncio.sleep(10)
    _report_after_restart()
    await asyncio.sleep(20)    # 서버가 다 켜진 뒤에
    while True:
        await check()
        await asyncio.sleep(CHECK_EVERY_SEC)


def _download_and_extract(latest: dict) -> None:
    url = latest.get("asset_url")
    if not url:
        raise RuntimeError("이 릴리스에 설치 파일(zip)이 없습니다.")
    if os.path.exists(NEW_DIR):
        shutil.rmtree(NEW_DIR)
    os.makedirs(NEW_DIR, exist_ok=True)
    zpath = os.path.join(UPDATE_DIR, f"TraderOS-{latest['version']}.zip")
    h = hashlib.sha256()
    done = 0
    with httpx.stream("GET", url, timeout=120, follow_redirects=True,
                      headers={"User-Agent": "TraderOS-updater"}) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or latest.get("asset_size") or 0)
        with open(zpath, "wb") as f:
            for chunk in r.iter_bytes(65536):
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if total:
                    status["progress"] = round(done / total * 100)
    digest = latest.get("asset_digest") or ""
    if digest.startswith("sha256:") and digest[7:].lower() != h.hexdigest():
        os.remove(zpath)
        raise RuntimeError("받은 파일이 손상됐습니다 (해시 불일치). 다시 시도하세요.")

    with zipfile.ZipFile(zpath) as z:
        root = os.path.realpath(NEW_DIR)
        for m in z.infolist():
            target = os.path.realpath(os.path.join(NEW_DIR, m.filename))
            if not target.startswith(root + os.sep) and target != root:
                raise RuntimeError(f"zip 안의 경로가 이상합니다: {m.filename}")
        z.extractall(NEW_DIR)
    os.remove(zpath)

    # zip 은 TraderOS/ 폴더 하나로 감싸져 있음
    inner = os.path.join(NEW_DIR, "TraderOS")
    src = inner if os.path.isdir(inner) else NEW_DIR
    for must in ("main.py", "release.json"):
        if not os.path.isfile(os.path.join(src, must)):
            raise RuntimeError(f"받은 파일에 {must} 가 없습니다. 릴리스 파일을 확인하세요.")
    new_info = json.load(open(os.path.join(src, "release.json"), encoding="utf-8"))
    with open(FLAG_FILE, "w", encoding="utf-8") as f:
        json.dump({"source": src, "version": new_info.get("version"), "at": time.time()}, f)


def _shutdown_server() -> None:
    """서버를 정상 종료 (워커 정리 포함). start.bat 이 apply.flag 를 보고 업데이트 후 다시 켬"""
    time.sleep(1.5)            # 응답이 먼저 나가게
    try:
        signal.raise_signal(signal.SIGINT)     # Ctrl+C 와 같음 → uvicorn 이 정상 종료 (워커도 정리)
    except Exception as e:
        print(f"[update] 정상 종료 신호 실패: {e}")
    # 20초 안에 안 꺼지면 워커를 직접 끄고 프로세스를 끝냄
    time.sleep(20)
    print("[update] 서버가 꺼지지 않아 강제로 끕니다")
    try:
        from api.admin_router import RUNTIME
        for p in list((RUNTIME.get("worker_procs") or {}).values()):
            try:
                p.terminate()
            except Exception:
                pass
    except Exception:
        pass
    os._exit(0)


def apply_in_background(source: str = "관리 화면") -> dict:
    """[업데이트] 버튼 (관리 화면·텔레그램) - 뒤에서 받아서 풀고 서버를 끔"""
    s = summary()
    if not s["enabled"]:
        raise RuntimeError("이 설치본은 자동 업데이트를 쓰지 않습니다 (git 으로 받은 폴더 등).")
    if not s["available"]:
        raise RuntimeError("이미 최신 버전입니다.")
    if not _lock.acquire(blocking=False):
        raise RuntimeError("이미 업데이트 중입니다.")

    def job():
        try:
            status.update(downloading=True, error=None, progress=0)
            _notify("⬇️ 업데이트 받는 중", f"{s['current']} → {s['latest']['version']} ({source}에서 시작)")
            _download_and_extract(s["latest"])
            status.update(downloading=False, progress=100)
            st = _read_state()
            st["applying"] = {"from": s["current"], "to": s["latest"]["version"], "at": time.time()}
            _write_state(st)
            print(f"[update] {s['current']} → {s['latest']['version']} 준비 완료 - 서버를 껐다가 업데이트 후 다시 켭니다")
            _notify("🔄 서버를 다시 켭니다", "새 버전으로 바꾸는 중입니다. 1~2분 뒤 완료 메시지가 옵니다.\n"
                    "(10분이 지나도 안 오면 PC의 start.bat 창을 확인하세요)", wait=True)
            _shutdown_server()
        except Exception as e:
            status.update(downloading=False, error=f"업데이트 실패: {e}")
            print(f"[update] 실패: {e}")
            _notify("❌ 업데이트 실패", f"{e}\n서버는 그대로 켜져 있습니다.")
        finally:
            _lock.release()

    threading.Thread(target=job, daemon=True).start()
    return summary()
