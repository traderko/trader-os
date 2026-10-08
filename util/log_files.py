# util/log_files.py
#
# logs\ 폴더 크기 관리.
#
#   워커 출력(worker_<id>_stdout.log): 워커가 파일에 직접 쓰지 않고 메인 서버가 파이프로 받아서 쓴다.
#     → 서버가 켜져 있는 동안에도 크기를 지킬 수 있음 (예전엔 워커를 다시 켤 때만 잘랐음)
#     파일이 MAX_BYTES 를 넘으면 지금 파일을 <이름>.prev.log 로 바꾸고 새로 시작 → 워커 하나당 최대 약 2배
#   정리: 서버 시작 때와 하루마다 - 30일 동안 안 바뀐 .log 는 지우고(지운 계좌 등), 너무 큰 .log 는 뒤쪽만 남김
#
# 표준 라이브러리만 쓴다.

import os
import threading
import time

MAX_BYTES = int(os.getenv("TRADEROS_LOG_MAX_MB", "5")) * 1024 * 1024   # 파일 하나 최대 (기본 5MB)
KEEP_DAYS = int(os.getenv("TRADEROS_LOG_KEEP_DAYS", "30"))


class CappedLog:
    """크기 제한이 있는 로그 파일 (여러 스레드에서 써도 됨)"""

    def __init__(self, path: str, max_bytes: int = MAX_BYTES):
        self.path = path
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._f = None
        self._size = 0

    def _open(self) -> None:
        self._f = open(self.path, "ab")
        self._size = self._f.tell()

    def _rotate(self) -> None:
        old = self.path[:-4] + ".prev.log" if self.path.endswith(".log") else self.path + ".prev"
        self._f.close()
        self._f = None
        try:
            os.replace(self.path, old)
        except OSError:
            # 관리 화면이 마침 읽는 중이면 이름을 못 바꿈 (Windows) → 뒤쪽 절반만 남기고 계속
            cap_file(self.path, self.max_bytes)
        self._open()

    def write(self, data: bytes) -> None:
        with self._lock:
            try:
                if self._f is None:
                    self._open()
                if self._size + len(data) > self.max_bytes and self._size > 0:
                    self._rotate()
                self._f.write(data)
                self._f.flush()
                self._size += len(data)
            except OSError as e:
                print(f"[log] {os.path.basename(self.path)} 쓰기 실패: {e}")

    def close(self) -> None:
        with self._lock:
            if self._f:
                self._f.close()
                self._f = None


_logs: dict[str, CappedLog] = {}
_logs_lock = threading.Lock()


def get(path: str) -> CappedLog:
    """같은 파일은 같은 객체 (워커를 다시 켜도 한 곳에서만 씀)"""
    path = os.path.abspath(path)
    with _logs_lock:
        if path not in _logs:
            _logs[path] = CappedLog(path)
        return _logs[path]


def pipe_to(stream, path: str) -> threading.Thread:
    """프로세스 출력(stdout 파이프)을 로그 파일로 옮겨 쓰는 스레드. 프로세스가 끝나면 스레드도 끝남"""
    log = get(path)

    def run():
        try:
            for line in iter(stream.readline, b""):
                log.write(line)
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    t = threading.Thread(target=run, daemon=True, name=f"log:{os.path.basename(path)}")
    t.start()
    return t


def cap_file(path: str, max_bytes: int = MAX_BYTES) -> None:
    """파일이 max_bytes 를 넘으면 뒤쪽 절반만 남김 (줄 단위로 맞춤)"""
    try:
        if os.path.getsize(path) <= max_bytes:
            return
        with open(path, "rb") as f:
            f.seek(-(max_bytes // 2), os.SEEK_END)
            tail = f.read()
        nl = tail.find(b"\n")
        if 0 <= nl < 4096:
            tail = tail[nl + 1:]
        with open(path, "wb") as f:
            f.write(b"===== (log truncated - size limit) =====\n")
            f.write(tail)
    except OSError:
        pass


def cleanup(log_dir: str) -> None:
    """오래된 로그 지우기 + 너무 큰 로그 줄이기 (지금 쓰는 파일은 건드리지 않음)"""
    if not os.path.isdir(log_dir):
        return
    active = set(_logs)
    now = time.time()
    removed = 0
    for name in os.listdir(log_dir):
        p = os.path.abspath(os.path.join(log_dir, name))
        if not name.endswith(".log") or not os.path.isfile(p) or p in active:
            continue
        try:
            if now - os.path.getmtime(p) > KEEP_DAYS * 86400:
                os.remove(p)
                removed += 1
            else:
                cap_file(p)
        except OSError:
            pass
    if removed:
        print(f"[log] {KEEP_DAYS}일 넘은 로그 {removed}개를 지웠습니다")


def dir_size(log_dir: str) -> int:
    total = 0
    for name in os.listdir(log_dir) if os.path.isdir(log_dir) else []:
        p = os.path.join(log_dir, name)
        if os.path.isfile(p):
            total += os.path.getsize(p)
    return total
