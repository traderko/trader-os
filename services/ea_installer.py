# services/ea_installer.py
#
# 계좌별 MT5 터미널에 EA(tradeLock 등)를 자동으로 깔아준다.
#
# 왜 필요한가
#   터미널을 포터블 모드 없이 띄우면 MT5가 설치 폴더(C:\mt5\<계좌>)마다
#   %APPDATA%\MetaQuotes\Terminal\<해시>\ 에 별도 데이터 폴더를 만든다.
#   새로 생긴 데이터 폴더에는 우리 EA가 없어서 config.ini 의 시작 EA를 못 찾는다.
#   ("expert 'Experts\api' not found from start config")
#
# 데이터 폴더 찾기
#   각 데이터 폴더의 origin.txt(UTF-16)에 그 폴더를 만든 설치 경로가 적혀 있다.
#
# 복사하는 것
#   mt5_ea/*.ex5                         → <데이터>\MQL5\Experts\
#   <설치폴더>\MQL5\presets\*.set         → <데이터>\MQL5\Presets\   (config.ini 의 ExpertParameters)

import filecmp
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EA_SOURCE_DIR = os.path.join(BASE_DIR, "mt5_ea")


def _terminal_root() -> str:
    appdata = os.getenv("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
    return os.path.join(appdata, "MetaQuotes", "Terminal")


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path.strip().rstrip("\\/")))


def _read_origin(path: str) -> str | None:
    try:
        raw = open(path, "rb").read()
    except OSError:
        return None
    for enc in ("utf-16", "utf-8-sig", "mbcs" if os.name == "nt" else "latin-1"):
        try:
            return raw.decode(enc).strip("\x00").strip()
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def find_data_dir(terminal_dir: str, root: str | None = None) -> str | None:
    """설치 폴더(C:\\mt5\\<계좌>)에 해당하는 MT5 데이터 폴더. 아직 한 번도 안 띄웠으면 None."""
    target = _norm(terminal_dir)
    for origin in glob.glob(os.path.join(root or _terminal_root(), "*", "origin.txt")):
        text = _read_origin(origin)
        if text and _norm(text) == target:
            return os.path.dirname(origin)
    return None


def wait_data_dir(terminal_dir: str, timeout: float = 60, root: str | None = None) -> str | None:
    """터미널을 처음 띄운 직후 데이터 폴더와 MQL5 폴더가 생길 때까지 기다림."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        d = find_data_dir(terminal_dir, root)
        if d and os.path.isdir(os.path.join(d, "MQL5", "Experts")):
            return d
        time.sleep(1)
    return None


def _copy_if_changed(src: str, dst_dir: str) -> bool:
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, os.path.basename(src))
    if os.path.exists(dst) and filecmp.cmp(src, dst, shallow=False):
        return False
    try:
        shutil.copy2(src, dst)
    except OSError as e:   # 파일이 잠겨 있거나 권한 문제 - 터미널 실행 자체는 막지 않음
        print(f"[ea] 복사 실패 {src} → {dst}: {e}")
        return False
    return True


# ── EA 자동 컴파일 ─────────────────────────────────────────────────
# mt5_ea/*.mq5 를 고치면 서버가 다음에 터미널을 띄울 때 MetaEditor 명령줄로 컴파일해서
# mt5_ea/*.ex5 를 새로 만든다. (MetaEditor를 열어 F7 누를 필요 없음)
#   metaeditor64.exe /compile:<소스> /inc:<MQL5 폴더> /log:<로그>
# 마지막으로 컴파일에 성공한 소스의 해시를 data/ea_build.json 에 적어 두고, 해시가 바뀐 것만 다시 컴파일.
# 컴파일이 실패하면 기존 .ex5 를 그대로 쓴다 (터미널 실행은 막지 않음).

BUILD_DIR = os.path.join(BASE_DIR, "data", "ea_build")
BUILD_STAMP = os.path.join(BASE_DIR, "data", "ea_build.json")
_build_lock = threading.Lock()
_build_failed: set[str] = set()   # 이번 실행에서 이미 실패한 소스 해시 - 계좌마다 반복 시도하지 않게


def _sha(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def _load_stamp() -> dict:
    try:
        return json.load(open(BUILD_STAMP, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_stamp(stamp: dict) -> None:
    os.makedirs(os.path.dirname(BUILD_STAMP), exist_ok=True)
    tmp = BUILD_STAMP + ".tmp"
    json.dump(stamp, open(tmp, "w", encoding="utf-8"), indent=2)
    os.replace(tmp, BUILD_STAMP)


def find_metaeditor(terminal_dir: str | None = None) -> str | None:
    cands = []
    if terminal_dir:
        cands.append(os.path.join(terminal_dir, "metaeditor64.exe"))
    for pf in (os.getenv("ProgramFiles"), os.getenv("ProgramW6432"), r"C:\Program Files"):
        if pf:
            cands += glob.glob(os.path.join(pf, "*", "metaeditor64.exe"))
    if terminal_dir:   # 다른 계좌 설치 폴더 (C:\mt5\*)
        cands += glob.glob(os.path.join(os.path.dirname(terminal_dir), "*", "metaeditor64.exe"))
    return next((c for c in cands if os.path.isfile(c)), None)


def _include_root(data_dir: str | None, terminal_dir: str | None) -> str | None:
    """표준 라이브러리(Include\Trade\Trade.mqh)가 있는 MQL5 폴더"""
    for base in (data_dir, terminal_dir):
        if base and os.path.isfile(os.path.join(base, "MQL5", "Include", "Trade", "Trade.mqh")):
            return os.path.join(base, "MQL5")
    for mql5 in glob.glob(os.path.join(_terminal_root(), "*", "MQL5")):
        if os.path.isfile(os.path.join(mql5, "Include", "Trade", "Trade.mqh")):
            return mql5
    return None


def _read_log(path: str) -> str:
    try:
        raw = open(path, "rb").read()
    except OSError:
        return ""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", "replace")
    return raw.decode("utf-8", "replace")


def compile_one(src: str, metaeditor: str, inc: str) -> tuple[bool, str]:
    """소스 하나 컴파일. 성공하면 mt5_ea/<이름>.ex5 를 교체. (성공 여부, 로그 요약)"""
    name = os.path.splitext(os.path.basename(src))[0]
    work = os.path.join(BUILD_DIR, name)
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    wsrc = os.path.join(work, name + ".mq5")
    shutil.copy2(src, wsrc)
    wlog = os.path.join(work, name + ".log")
    try:
        subprocess.run([metaeditor, f"/compile:{wsrc}", f"/inc:{inc}", f"/log:{wlog}"],
                       timeout=180, capture_output=True)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"MetaEditor 실행 실패: {e}"

    log = _read_log(wlog)
    m = re.search(r"(\d+)\s+errors?,\s*(\d+)\s+warnings?", log)
    out = os.path.join(work, name + ".ex5")
    errors = int(m.group(1)) if m else None
    if errors != 0 or not os.path.isfile(out):
        lines = [l.strip() for l in log.splitlines() if "error" in l.lower()]
        return False, "\n".join(lines[-10:]) or "컴파일 결과를 확인하지 못했습니다 (로그 없음)"
    dst = os.path.join(EA_SOURCE_DIR, name + ".ex5")
    tmp = dst + ".new"
    shutil.copy2(out, tmp)
    os.replace(tmp, dst)
    return True, m.group(0)


def build_if_changed(terminal_dir: str | None = None, data_dir: str | None = None) -> list[str]:
    """mt5_ea/*.mq5 중 마지막 컴파일 이후 바뀐 것만 컴파일. 새로 만든 .ex5 이름 목록을 돌려준다."""
    sources = sorted(glob.glob(os.path.join(EA_SOURCE_DIR, "*.mq5")))
    if not sources:
        return []
    with _build_lock:
        stamp = _load_stamp()
        todo = []
        for src in sources:
            name = os.path.basename(src)
            h = _sha(src)
            ex5 = os.path.splitext(src)[0] + ".ex5"
            if stamp.get(name) == h and os.path.isfile(ex5):
                continue
            if h in _build_failed:
                continue
            todo.append((src, name, h))
        if not todo:
            return []

        metaeditor = find_metaeditor(terminal_dir)
        inc = _include_root(data_dir, terminal_dir)
        if not metaeditor or not inc:
            print(f"[ea] EA 소스가 바뀌었지만 컴파일하지 못했습니다 "
                  f"({'MetaEditor(metaeditor64.exe)' if not metaeditor else 'MQL5 Include 폴더'}를 찾지 못함). 기존 .ex5를 사용합니다.")
            _build_failed.update(h for _, _, h in todo)
            return []

        built = []
        for src, name, h in todo:
            ok, msg = compile_one(src, metaeditor, inc)
            if ok:
                stamp[name] = h
                built.append(os.path.splitext(name)[0] + ".ex5")
                print(f"[ea] {name} 컴파일 완료 ({msg})")
            else:
                _build_failed.add(h)
                print(f"[ea] {name} 컴파일 실패 - 기존 .ex5를 사용합니다:\n{msg}")
        if built:
            _save_stamp(stamp)
        return built


def install(data_dir: str, terminal_dir: str) -> list[str]:
    """EA·프리셋을 데이터 폴더에 복사. 새로 복사(또는 갱신)한 파일 이름 목록을 돌려준다."""
    changed = []
    experts = os.path.join(data_dir, "MQL5", "Experts")
    for src in glob.glob(os.path.join(EA_SOURCE_DIR, "*.ex5")):
        if _copy_if_changed(src, experts):
            changed.append(os.path.basename(src))

    presets_src = os.path.join(terminal_dir, "MQL5", "presets")
    presets_dst = os.path.join(data_dir, "MQL5", "Presets")
    for src in glob.glob(os.path.join(presets_src, "*.set")):
        if _copy_if_changed(src, presets_dst):
            changed.append(os.path.basename(src))
    return changed


# ── 브로커 서버 목록 (servers.dat) ─────────────────────────────────
# MetaQuotes 기본 설치본을 복사해 만든 터미널은 서버 목록에 브로커 서버가 없어서
# config.ini 의 Server= 로 접속을 시도조차 하지 못한다 (로그에 Network 줄이 아예 안 나옴).
# 같은 서버에 이미 로그인해 본 다른 터미널의 config\servers.dat 를 복사해 주면 바로 접속된다.

def _read_ini(path: str) -> dict[str, dict[str, str]]:
    try:
        raw = open(path, "rb").read()
    except OSError:
        return {}
    text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8", "replace")
    out, section = {}, ""
    for line in text.splitlines():
        line = line.strip().lstrip("﻿")
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].lower()
            out.setdefault(section, {})
        elif "=" in line and not line.startswith(";"):
            k, v = line.split("=", 1)
            out.setdefault(section, {})[k.strip().lower()] = v.strip()
    return out


def configured_server(terminal_dir: str) -> str | None:
    """서버가 만든 config.ini 의 [Common] Server="""
    return _read_ini(os.path.join(terminal_dir, "config.ini")).get("common", {}).get("server") or None


def has_logged_in(data_dir: str) -> bool:
    """한 번이라도 로그인에 성공하면 common.ini 에 Login 이 남는다."""
    return bool(_read_ini(os.path.join(data_dir, "config", "common.ini")).get("common", {}).get("login"))


def find_servers_donor(server: str, exclude: str | None = None, root: str | None = None) -> str | None:
    """같은 서버(Server=)에 로그인했던 다른 터미널의 servers.dat. 여러 개면 가장 큰 파일."""
    want = server.strip().lower()
    best = None
    for common in glob.glob(os.path.join(root or _terminal_root(), "*", "config", "common.ini")):
        data_dir = os.path.dirname(os.path.dirname(common))
        if exclude and _norm(data_dir) == _norm(exclude):
            continue
        if _read_ini(common).get("common", {}).get("server", "").strip().lower() != want:
            continue
        dat = os.path.join(data_dir, "config", "servers.dat")
        if os.path.isfile(dat) and (best is None or os.path.getsize(dat) > os.path.getsize(best)):
            best = dat
    return best


def ensure_servers(data_dir: str, terminal_dir: str, root: str | None = None) -> str | None:
    """아직 로그인한 적 없는 터미널이면 서버 목록을 복사. 복사했으면 원본 경로를 돌려준다.
    (터미널이 꺼져 있을 때 호출해야 함 - 켜져 있으면 종료할 때 MT5가 자기 목록으로 덮어씀)"""
    if has_logged_in(data_dir):
        return None
    server = configured_server(terminal_dir)
    if not server:
        return None
    donor = find_servers_donor(server, exclude=data_dir, root=root)
    if not donor:
        print(f"[ea] '{server}' 서버에 로그인했던 터미널이 없어 서버 목록을 복사하지 못했습니다. "
              f"MT5에서 파일 → 거래 계좌 로그인으로 서버를 검색해 한 번 로그인해 주세요.")
        return None
    dst = os.path.join(data_dir, "config", "servers.dat")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        if os.path.exists(dst) and not os.path.exists(dst + ".bak"):
            shutil.copy2(dst, dst + ".bak")
        shutil.copy2(donor, dst)
    except OSError as e:
        print(f"[ea] 서버 목록 복사 실패: {e}")
        return None
    return donor


# ── 실행 중인 터미널 / 차트 정리 ────────────────────────────────────
# 서버를 재시작할 때마다 terminal64.exe /config:... 를 다시 실행하면, 이미 떠 있는 MT5에
# [StartUp] 차트(EA 붙은 차트)가 하나씩 더 열린다. 또 MT5를 껐다 켜면 지난번 프로필에 저장된
# EA 차트 + 시작 차트가 겹친다. 그래서
#   - 이미 떠 있으면 다시 실행하지 않고 (find_running)
#   - 꺼져 있을 때 띄우기 전에 프로필에서 우리 EA가 붙은 차트를 지운다 (remove_ea_charts)
#     → 시작 차트 하나만 남는다. 사용자가 따로 연 차트(EA 없는 차트)는 건드리지 않는다.

def find_running(terminal_path: str):
    """이 설치 경로의 terminal64.exe 가 떠 있으면 psutil.Process, 아니면 None.
    exe 경로를 못 읽는 경우(권한·에뮬레이션 등)를 대비해 명령줄 첫 항목도 본다."""
    try:
        import psutil
    except ImportError:
        return None
    target = _norm(terminal_path)
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info.get("name") or "").lower() != "terminal64.exe":
                continue
            paths = []
            try:
                paths.append(p.exe())
            except (psutil.AccessDenied, OSError):
                pass
            try:
                cmd = p.cmdline()
                if cmd:
                    paths.append(cmd[0].strip('"'))
            except (psutil.AccessDenied, OSError):
                pass
            if any(x and _norm(x) == target for x in paths):
                return p
            if not paths:
                print(f"[ea] terminal64.exe(pid={p.pid}) 경로를 읽지 못했습니다 - 같은 터미널이면 차트가 하나 더 열릴 수 있습니다")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


class RunningTerminal:
    """이미 떠 있던 터미널을 Popen 처럼 다루기 위한 얇은 래퍼 (poll()만 씀)."""
    def __init__(self, proc):
        self._p = proc
        self.pid = proc.pid

    def poll(self):
        try:
            return None if self._p.is_running() and self._p.status() != "zombie" else 0
        except Exception:
            return 0


def _decode(raw: bytes) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", "replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("utf-16-le", "replace")


_PERIOD_RE = re.compile(r"^(M|H|D|W|MN)(\d+)$", re.I)
_PERIOD_TYPE = {"m": 0, "h": 1, "d": 2, "w": 3, "mn": 4}


def _startup_chart(terminal_dir: str) -> tuple[str, int, int] | None:
    """config.ini [StartUp] 의 (종목, period_type, period_size). 예) XAUUSD+, H1 → ("xauusd+", 1, 1)"""
    st = _read_ini(os.path.join(terminal_dir, "config.ini")).get("startup", {})
    sym, per = st.get("symbol"), (st.get("period") or "").strip()
    m = _PERIOD_RE.match(per)
    if not sym or not m:
        return None
    return sym.strip().lower(), _PERIOD_TYPE[m.group(1).lower()], int(m.group(2))


def _is_our_chart(text: str, ea_names: set[str], startup: tuple[str, int, int] | None) -> bool:
    """우리가 띄운 차트인지.
    - tradeLock EA가 붙은 차트
    - 또는 시작 차트와 같은 종목·주기이면서 EA·지표·그림이 없는 차트
      (config.ini [StartUp]으로 붙인 EA는 프로필에 저장되지 않아서, 시작 차트가 EA 없는
       차트로 저장된 뒤 다음 실행 때 또 하나 열려 쌓인다. MT5가 자동으로 그리는 체결 표시는
       그림으로 치지 않음. 사용자가 지표나 선을 그려 둔 차트는 건드리지 않음)"""
    for block in re.findall(r"<expert>(.*?)</expert>", text, flags=re.S | re.I):
        m = re.search(r"^\s*name\s*=\s*(.+?)\s*$", block, flags=re.M | re.I)
        if m and m.group(1).lower() in ea_names:
            return True
    if startup is None or re.search(r"<expert>", text, flags=re.I):
        return False
    # MT5가 자동으로 그리는 체결 내역 표시("autotrade #티켓 ...")는 사용자 그림이 아님
    for obj in re.findall(r"<object>(.*?)</object>", text, flags=re.S | re.I):
        m = re.search(r"^\s*name\s*=\s*(.*?)\s*$", obj, flags=re.M | re.I)
        if not (m and m.group(1).lower().startswith("autotrade ")):
            return False
    ind = [n.strip().lower() for n in re.findall(r"^\s*name\s*=\s*(.+?)\s*$",
           "".join(re.findall(r"<indicator>(.*?)</indicator>", text, flags=re.S | re.I)), flags=re.M)]
    if any(n != "main" for n in ind):
        return False
    def val(key):
        m = re.search(rf"^\s*{key}\s*=\s*(.+?)\s*$", text, flags=re.M | re.I)
        return m.group(1).strip().lower() if m else None
    try:
        return (val("symbol"), int(val("period_type") or -1), int(val("period_size") or -1)) == startup
    except ValueError:
        return False


def remove_ea_charts(data_dir: str, terminal_dir: str | None = None,
                     ea_names: tuple[str, ...] = ("tradeLock",)) -> list[str]:
    """터미널이 꺼져 있을 때 호출. 마지막 프로필에서 우리가 띄운 차트(_is_our_chart)를 지운다.
    → 다음 실행 때 시작 차트 하나만 남는다."""
    profile = _read_ini(os.path.join(data_dir, "config", "common.ini")).get("charts", {}).get("profilelast") or "Default"
    folder = os.path.join(data_dir, "MQL5", "Profiles", "Charts", profile)
    names = {n.lower() for n in ea_names}
    startup = _startup_chart(terminal_dir) if terminal_dir else None
    removed = []
    for chr_path in sorted(glob.glob(os.path.join(folder, "*.chr"))):
        try:
            text = _decode(open(chr_path, "rb").read())
        except OSError:
            continue
        if _is_our_chart(text, names, startup):
            try:
                os.remove(chr_path)
                removed.append(os.path.basename(chr_path))
            except OSError as e:
                print(f"[ea] 차트 파일 삭제 실패 {chr_path}: {e}")
    if removed:
        _rewrite_order(folder)
    return removed


def _rewrite_order(folder: str) -> None:
    """order.wnd(차트 탭 순서)에서 지운 차트를 뺀다. 없는 파일을 가리키면 MT5가 헷갈릴 수 있음."""
    path = os.path.join(folder, "order.wnd")
    try:
        raw = open(path, "rb").read()
    except OSError:
        return
    text = _decode(raw)
    keep = [l for l in text.splitlines() if l.strip() and os.path.isfile(os.path.join(folder, l.strip()))]
    out = "".join(l + "\r\n" for l in keep)
    utf16 = raw[:2] in (b"\xff\xfe", b"\xfe\xff")
    try:
        with open(path, "wb") as f:
            f.write(out.encode("utf-16") if utf16 else out.encode("utf-8"))
    except OSError:
        pass


# ── WebRequest 허용 URL ───────────────────────────────────────────
# EA가 서버(http://127.0.0.1:8000)에 잠금 상태를 물으려면 MT5 옵션 → 전문가 조언자 →
# "다음 URL에 WebRequest 허용"에 주소가 있어야 한다. 이 목록은 common.ini [Experts] 의
# WebRequestUrl 에 암호화돼 저장돼서 직접 만들 수는 없고, 이미 허용 목록을 설정해 둔 다른 터미널의
# 값을 그대로 복사한다. (같은 PC의 터미널끼리는 그대로 통한다)
# 이 터미널에 목록이 비어 있을 때만 채운다 - 사용자가 따로 넣은 목록은 덮어쓰지 않음.

def _ini_set(path: str, section: str, values: dict[str, str]) -> None:
    """INI 파일의 한 섹션 값을 바꾼다. 원래 인코딩(UTF-16/UTF-8)과 줄바꿈을 유지."""
    try:
        raw = open(path, "rb").read()
    except FileNotFoundError:
        raw = b""
    utf16 = raw[:2] in (b"\xff\xfe", b"\xfe\xff") or not raw
    text = (raw.decode("utf-16") if raw and utf16 else raw.decode("utf-8", "replace")).lstrip("\ufeff")
    nl = "\r\n" if "\r\n" in text or not text else "\n"
    lines = text.splitlines()

    want = section.lower()
    start = next((i for i, l in enumerate(lines) if l.strip().lower() == f"[{want}]"), None)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"[{section}]")
        start = len(lines) - 1
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("[")), len(lines))

    pending = dict(values)
    for i in range(start + 1, end):
        if "=" in lines[i]:
            k = lines[i].split("=", 1)[0].strip()
            for key in list(pending):
                if key.lower() == k.lower():
                    lines[i] = f"{k}={pending.pop(key)}"
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    for k, v in pending.items():
        lines.insert(insert_at, f"{k}={v}")
        insert_at += 1

    out = nl.join(lines) + nl
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "wb") as f:
        f.write(out.encode("utf-16") if utf16 else out.encode("utf-8"))   # utf-16 은 BOM 포함
    os.replace(tmp, path)


def _webrequest(data_dir: str) -> tuple[bool, str]:
    ex = _read_ini(os.path.join(data_dir, "config", "common.ini")).get("experts", {})
    return ex.get("webrequest") == "1", ex.get("webrequesturl", "")


def ensure_webrequest(data_dir: str, root: str | None = None) -> str | None:
    """이 터미널의 WebRequest 허용 목록이 비어 있으면 다른 터미널의 목록을 복사. 복사했으면 원본 폴더를 돌려준다.
    (터미널이 꺼져 있을 때 호출해야 함)"""
    enabled, urls = _webrequest(data_dir)
    if enabled and urls:
        return None
    best, best_urls, best_mtime = None, "", 0.0
    for common in glob.glob(os.path.join(root or _terminal_root(), "*", "config", "common.ini")):
        d = os.path.dirname(os.path.dirname(common))
        if _norm(d) == _norm(data_dir):
            continue
        on, u = _webrequest(d)
        if not (on and u):
            continue
        mt = os.path.getmtime(common)
        if len(u) > len(best_urls) or (len(u) == len(best_urls) and mt > best_mtime):
            best, best_urls, best_mtime = d, u, mt
    if not best:
        print("[ea] WebRequest 허용 목록을 복사할 터미널이 없습니다. MT5 도구 → 옵션 → 전문가 조언자에서 "
              "'다음 URL에 WebRequest 허용'에 http://127.0.0.1:8000 을 추가해 주세요.")
        return None
    try:
        _ini_set(os.path.join(data_dir, "config", "common.ini"), "Experts",
                 {"WebRequest": "1", "WebRequestUrl": best_urls})
    except OSError as e:
        print(f"[ea] WebRequest 허용 목록 복사 실패: {e}")
        return None
    return best


def webrequest_ready(data_dir: str) -> bool:
    on, urls = _webrequest(data_dir)
    return on and bool(urls)


def has_webrequest_donor(data_dir: str, root: str | None = None) -> bool:
    for common in glob.glob(os.path.join(root or _terminal_root(), "*", "config", "common.ini")):
        d = os.path.dirname(os.path.dirname(common))
        if _norm(d) != _norm(data_dir) and webrequest_ready(d):
            return True
    return False


def close_terminal(proc, timeout: float = 30) -> bool:
    """떠 있는 MT5를 창 닫기(WM_CLOSE)로 정상 종료 - 그래야 MT5가 설정·차트를 저장하고 끝난다.
    시간 안에 안 꺼지면 강제 종료. 꺼졌으면 True."""
    try:
        import win32con, win32gui, win32process
        def cb(hwnd, _):
            if win32gui.IsWindowVisible(hwnd) and win32process.GetWindowThreadProcessId(hwnd)[1] == proc.pid:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            return True
        win32gui.EnumWindows(cb, None)
    except Exception as e:
        print(f"[ea] MT5 창 닫기 실패 ({e}) - 강제 종료합니다")
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout)
        return True
    except Exception:
        try:
            proc.kill()
            proc.wait(10)
            return True
        except Exception:
            return False
