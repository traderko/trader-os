from contextlib import asynccontextmanager
import os
import asyncio
import time
from datetime import datetime, timedelta
import shutil
import sys
import subprocess
from dotenv import load_dotenv
from fastapi.responses import PlainTextResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import selectinload
from db.models import *
from datetime import datetime
from fastapi import Depends, FastAPI, HTTPException
from api import admin_app, admin_router
from api import account_router, mt5_gateway_router, rates, trade, trade_image, trade_lock_phrase_router, trade_lock_router, trade_note, trade_tag
import importlib
from db import init_db
from db.models.account import Account
from db.models.trade import Trade
from services.account import get_account_or_default
from services.economic_event_service import EconomicEventService
from services.fcm_service import FCMService, get_fcm_service
from mt5.mt5Client import Mt5Client
from fastapi.middleware.cors import CORSMiddleware
from db.base import Base
from db.session import AsyncSessionLocal, engine, get_db
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from apscheduler.schedulers.background import BackgroundScheduler
from services.worker_client import worker_port_for
from util.generate_terminal_configs import GenerateTerminalConfig
from services import ea_installer
import httpx

load_dotenv()

origins = [
    "http://localhost",
    "https://localhost",
    "http://localhost:5173",  # 개발 중인 프론트엔드
    "http://127.0.0.1:5173",
    "http://localhost:52040", # Local Web
    "http://127.0.0.1:52040",
]

scheduler = BackgroundScheduler(timezone=Mt5Client.KOREA_TIMEZONE_STR)

fcm_service = get_fcm_service()

WATCHDOG_INTERVAL_SEC = 10
from util.paths import TERMINAL_BASE_DIR, SOURCE_MT5_INSTALL_DIR   # .env 로 바꿀 수 있음

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
 
# 개발중 특정 계좌 워커를 IDE에서 직접 디버깅하고 싶을 때:
#   환경변수 SKIP_WORKER_SPAWN=1,3  ← 이 계좌들은 main.py가 자동 스폰 안 함
#   그 계좌는 터미널에서 직접 실행:
#   python -m app.trade_lock_worker --account-id 1 --port 9001 --terminal-path "..."
#   (IDE 디버거로 그 명령어를 실행하면 브레이크포인트도 정상 동작함)
_SKIP_WORKER_IDS = {
    int(x) for x in os.environ.get("SKIP_WORKER_SPAWN", "").split(",") if x.strip()
}
 
_terminal_procs: dict[int, subprocess.Popen] = {}
_worker_procs: dict[int, subprocess.Popen] = {}
_account_configs: dict[int, dict] = {}  # account_id -> {terminal_path, worker_port, ...}

# 개인용 텔레그램 채널 중계 (services/telegram_relay.py 가 있을 때만 - 공개판에는 없음)
if not __debug__:
    try:
        import services.telegram_relay  # noqa: F401  (import 하면 바로 동작)
    except ModuleNotFoundError:
        pass
 
def _terminal_path_for(account_number: str) -> str:
    return rf"{TERMINAL_BASE_DIR}\{account_number}\terminal64.exe"
 
async def _load_accounts() -> dict[int, dict]:
    """lock_enabled=True인 계좌만 골라서, 경로/포트는 규칙대로 계산해 채움."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Account).where(Account.lock_enabled.is_(True)))
        accounts = result.scalars().all()
 
    return {
        acc.id: {
            "terminal_path": _terminal_path_for(acc.account_number),
            "worker_port": worker_port_for(acc.id),
        }
        for acc in accounts
    }

def _ensure_terminal_installed(terminal_path: str):
    """터미널 exe가 없으면(계좌 최초 세팅) 기본 설치본을 통째로 복사해서 포터블 인스턴스를 만든다."""
    if os.path.exists(terminal_path):
        return

    target_dir = os.path.dirname(terminal_path)
    if not os.path.exists(SOURCE_MT5_INSTALL_DIR):
        raise FileNotFoundError(
            f"기본 설치본을 찾을 수 없음: {SOURCE_MT5_INSTALL_DIR} "
            f"(MT5가 이 경로에 설치돼 있는지 확인 필요)"
        )

    print(f"[lifespan] {terminal_path} 없음 - {SOURCE_MT5_INSTALL_DIR}에서 복사 시작 (파일 많아서 시간 걸릴 수 있음)")
    os.makedirs(target_dir, exist_ok=True)
    shutil.copytree(SOURCE_MT5_INSTALL_DIR, target_dir, dirs_exist_ok=True)
    print(f"[lifespan] 복사 완료 -> {target_dir}")
 
def _spawn_terminal(account_id: int, cfg: dict) -> subprocess.Popen:
    terminal_path = cfg["terminal_path"]

    _ensure_terminal_installed(terminal_path)

    if __debug__:
        args = ''
    else:
        args = '' #'/portable'

    config_ini = os.path.join(os.path.dirname(terminal_path), "config.ini")
    proc = subprocess.Popen(
        [
            terminal_path, 
            f"{args} /config:{config_ini}"
        ]
    )
    print(f"[lifespan] account={account_id} MT5 터미널 실행 (pid={proc.pid})")
    return proc
 
 
def _launch_terminal_with_ea(account_id: int, cfg: dict):
    """EA(tradeLock 등)와 브로커 서버 목록을 이 터미널의 데이터 폴더에 깔고 터미널을 띄운다. (블로킹 - 스레드에서 호출)
    처음 띄우는 터미널은 데이터 폴더가 아직 없으므로: 띄움 → 폴더 생길 때까지 대기 → 종료 → 복사 → 다시 띄움
    (시작 EA는 터미널이 켜질 때만 붙고, 서버 목록은 꺼져 있을 때 바꿔야 해서 재시작이 필요)"""
    terminal_dir = os.path.dirname(cfg["terminal_path"])

    def prepare(data_dir: str, running: bool = False) -> None:
        ea_installer.build_if_changed(terminal_dir, data_dir)   # mt5_ea/*.mq5 가 바뀌었으면 먼저 컴파일
        changed = ea_installer.install(data_dir, terminal_dir)  # 떠 있는 MT5는 바뀐 .ex5를 알아서 다시 불러옴
        if changed:
            print(f"[ea] account={account_id} EA/프리셋 설치: {changed}")
        if running:
            return   # 아래 작업은 터미널이 꺼져 있어야 의미가 있음 (종료할 때 MT5가 덮어씀)
        donor = ea_installer.ensure_servers(data_dir, terminal_dir)
        if donor:
            print(f"[ea] account={account_id} 브로커 서버 목록 복사: {donor}")
        src = ea_installer.ensure_webrequest(data_dir)     # EA가 서버에 물어볼 수 있게 WebRequest 허용 목록
        if src:
            print(f"[ea] account={account_id} WebRequest 허용 목록 복사: {src}")
        removed = ea_installer.remove_ea_charts(data_dir, terminal_dir)   # 시작 차트와 겹치지 않게 지난번 EA 차트 정리
        if removed:
            print(f"[ea] account={account_id} 이전 EA 차트 {len(removed)}개 정리")

    data_dir = ea_installer.find_data_dir(terminal_dir)

    # 이미 떠 있으면 다시 실행하지 않음 - 다시 실행하면 MT5가 EA 차트를 하나 더 연다
    running = ea_installer.find_running(cfg["terminal_path"])
    if running is not None and data_dir and not ea_installer.webrequest_ready(data_dir) \
            and ea_installer.has_webrequest_donor(data_dir):
        # WebRequest 허용 목록이 비어 있으면 EA가 서버에 잠금 상태를 물어볼 수 없다.
        # 목록은 MT5가 꺼져 있을 때만 바꿀 수 있어서 한 번 정상 종료 → 목록 복사 → 다시 실행.
        # (포지션은 브로커 서버에 있으므로 터미널을 껐다 켜도 그대로)
        print(f"[ea] account={account_id} WebRequest 허용 목록이 비어 있어 MT5를 재시작합니다 (pid={running.pid})")
        if ea_installer.close_terminal(running):
            running = None
    if running is not None:
        if data_dir:
            prepare(data_dir, running=True)
        print(f"[lifespan] account={account_id} MT5 터미널이 이미 실행 중 (pid={running.pid}) - 그대로 사용")
        return ea_installer.RunningTerminal(running)

    if data_dir:
        prepare(data_dir)
        return _spawn_terminal(account_id, cfg)

    proc = _spawn_terminal(account_id, cfg)
    data_dir = ea_installer.wait_data_dir(terminal_dir, timeout=90)
    if not data_dir:
        print(f"[ea] account={account_id} 데이터 폴더를 찾지 못해 EA를 설치하지 못했습니다. 차트에 직접 붙여 주세요.")
        return proc

    print(f"[ea] account={account_id} 첫 실행 - EA·서버 목록 설치를 위해 터미널 재시작")
    time.sleep(5)          # 첫 실행 초기화(MQL5 폴더 채우기·컴파일)가 끝나도록 잠깐 대기
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    prepare(data_dir)      # 꺼진 상태에서 복사해야 MT5가 덮어쓰지 않음
    return _spawn_terminal(account_id, cfg)


WORKER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_lock_worker.py")

STDOUT_LOG_MAX_BYTES = 2 * 1024 * 1024  # 2MB 넘으면 마지막 절반만 남기고 자름
 
 
def _cap_log_file(path: str, max_bytes: int):
    """서브프로세스 stdout 캡처 파일이 무한정 커지는 걸 방지 - 초과분은 앞부분부터 잘라냄."""
    if not os.path.exists(path):
        return
    if os.path.getsize(path) <= max_bytes:
        return
    with open(path, "rb") as f:
        f.seek(-max_bytes // 2, os.SEEK_END)  # 뒤쪽 절반만 유지
        tail = f.read()
    with open(path, "wb") as f:
        f.write(b"===== (log truncated - size limit) =====\n")
        f.write(tail)
 
 
def _spawn_worker(account_id: int, cfg: dict) -> subprocess.Popen:
    # worker_{id}.log는 워커 자체의 RotatingFileHandler가 관리하는 운영 로그이므로
    # 이름이 겹치지 않게 여기서는 _stdout.log로 분리 (크래시 트레이스백 등 안전망 용도)
    log_path = os.path.join(LOG_DIR, f"worker_{account_id}_stdout.log")
    _cap_log_file(log_path, STDOUT_LOG_MAX_BYTES)
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)  # line-buffered
    log_file.write(f"\n===== worker start {account_id} =====\n")
 
    proc = subprocess.Popen(
        [
            sys.executable, WORKER_SCRIPT,
            "--account-id", str(account_id),
            "--port", str(cfg["worker_port"]),
            "--terminal-path", cfg["terminal_path"],
        ],
        stdout=log_file,
        stderr=subprocess.STDOUT,  # 에러도 같은 파일에 (트레이스백 포함)
    )
    print(f"[lifespan] account={account_id} 워커 시작 (pid={proc.pid}, port={cfg['worker_port']}, log={log_path})")
    return proc
 
 
def _tail_log(account_id: int, n_lines: int = 20) -> str:
    """워커가 죽었을 때 원인 파악용으로 로그 마지막 부분을 바로 보여줌."""
    log_path = os.path.join(LOG_DIR, f"worker_{account_id}_stdout.log")
    if not os.path.exists(log_path):
        return "(로그 파일 없음)"
    with open(log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    return "".join(lines[-n_lines:])
 
 
async def _watchdog_loop():
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL_SEC)
        for account_id, proc in list(_worker_procs.items()):
            if proc.poll() is not None:
                if account_id not in _account_configs:
                    continue   # 관리 화면에서 멈춘 계좌
                print(f"[watchdog] account={account_id} 워커 죽음 감지 (exitcode={proc.returncode})")
                print(f"[watchdog] 마지막 로그:\n{_tail_log(account_id)}")
                print(f"[watchdog] account={account_id} 재시작")
                _worker_procs[account_id] = _spawn_worker(account_id, _account_configs[account_id])
 
 
# ── 관리 화면에서 계좌를 추가하거나 잠금을 켜고 끌 때 (서버 재시작 없이) ──
_runtime_lock = asyncio.Lock()   # 동시에 여러 계좌를 켜고 끌 때 설정 파일 생성이 겹치지 않게


async def start_account_runtime(account_id: int) -> None:
    """설정 파일(config.ini) 생성 → MT5 터미널 실행 → 로그인 대기 → 워커 실행."""
    async with _runtime_lock:
        proc = _worker_procs.get(account_id)
        if proc is not None and proc.poll() is None:
            return  # 이미 실행 중

        await GenerateTerminalConfig.main()   # lock_enabled 계좌들의 config.ini / .set 다시 생성
        cfg = (await _load_accounts()).get(account_id)
        if cfg is None:
            raise RuntimeError(f"account_id={account_id}: 잠금이 꺼져 있거나 없는 계좌입니다.")

        _account_configs[account_id] = cfg
        mt5_gateway_router.WORKER_PORTS[account_id] = cfg["worker_port"]

        term = _terminal_procs.get(account_id)
        if term is None or term.poll() is not None:
            # 처음 추가한 계좌는 MT5 설치본 복사가 오래 걸릴 수 있어서 스레드에서
            _terminal_procs[account_id] = await asyncio.to_thread(_launch_terminal_with_ea, account_id, cfg)
            await asyncio.sleep(5)   # 터미널 로그인 대기 (기동 때와 같은 값)

        if account_id in _SKIP_WORKER_IDS:
            print(f"[runtime] account={account_id} 워커 자동 스폰 스킵 (수동 실행 대상)")
            return
        _worker_procs[account_id] = _spawn_worker(account_id, cfg)


async def stop_account_runtime(account_id: int) -> None:
    """워커만 멈춤. MT5 터미널은 직접 거래에 쓰고 있을 수 있어서 그대로 둔다."""
    async with _runtime_lock:
        _account_configs.pop(account_id, None)          # watchdog이 다시 띄우지 않게 먼저 제거
        mt5_gateway_router.WORKER_PORTS.pop(account_id, None)
        proc = _worker_procs.pop(account_id, None)
        if proc is None or proc.poll() is not None:
            return
        print(f"[runtime] account={account_id} 워커 종료 (pid={proc.pid})")
        proc.terminate()
        try:
            await asyncio.to_thread(proc.wait, 5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _restart_terminal_blocking(account_id: int, cfg: dict):
    """떠 있는 MT5를 정상 종료(창 닫기)한 뒤 EA를 다시 깔고 새로 띄운다. (블로킹 - 스레드에서 호출)
    포지션은 브로커 서버에 있으므로 터미널을 껐다 켜도 그대로다."""
    running = ea_installer.find_running(cfg["terminal_path"])
    if running is not None:
        print(f"[runtime] account={account_id} MT5 종료 중 (pid={running.pid})")
        if not ea_installer.close_terminal(running):
            raise RuntimeError("MT5가 꺼지지 않습니다. 작업 관리자에서 terminal64.exe 를 직접 끈 뒤 다시 해 보세요.")
        time.sleep(2)   # 종료 직후 바로 띄우면 이전 프로세스가 파일을 잡고 있을 수 있음
    return _launch_terminal_with_ea(account_id, cfg)


async def restart_terminal_runtime(account_id: int) -> None:
    """관리 화면 [MT5 재시작] - 워커 멈춤 → MT5 껐다 켜기(EA 다시 설치) → 워커 다시 실행.
    MT5가 멈췄거나, 실수로 껐거나, EA가 차트에서 빠졌을 때 서버를 재시작하지 않고 복구."""
    async with _runtime_lock:
        await GenerateTerminalConfig.main()   # config.ini 를 최신 설정으로 (시작 차트 종목 등)
        cfg = (await _load_accounts()).get(account_id)
        if cfg is None:
            raise RuntimeError("MT5 실행이 꺼진 계좌입니다. 계좌 탭에서 MT5 실행을 먼저 켜세요.")

        # 워커가 MT5에 붙어 있으므로 먼저 멈춤 (watchdog이 그 사이 다시 띄우지 않게 목록에서 뺌)
        _account_configs.pop(account_id, None)
        proc = _worker_procs.pop(account_id, None)
        if proc is not None and proc.poll() is None:
            print(f"[runtime] account={account_id} MT5 재시작 - 워커 종료 (pid={proc.pid})")
            proc.terminate()
            try:
                await asyncio.to_thread(proc.wait, 5)
            except subprocess.TimeoutExpired:
                proc.kill()

        _terminal_procs.pop(account_id, None)
        _terminal_procs[account_id] = await asyncio.to_thread(_restart_terminal_blocking, account_id, cfg)
        await asyncio.sleep(5)   # 터미널 로그인 대기 (기동 때와 같은 값)

        _account_configs[account_id] = cfg
        mt5_gateway_router.WORKER_PORTS[account_id] = cfg["worker_port"]
        if account_id in _SKIP_WORKER_IDS:
            print(f"[runtime] account={account_id} 워커 자동 스폰 스킵 (수동 실행 대상)")
            return
        _worker_procs[account_id] = _spawn_worker(account_id, cfg)
        print(f"[runtime] account={account_id} MT5 재시작 완료")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # DB 구조를 최신으로 (처음 설치면 만들고, 업데이트면 새 마이그레이션 적용 - db/migrate.py)
    from db import migrate as db_migrate
    try:
        await asyncio.to_thread(db_migrate.run)
    except Exception as e:
        print(f"[db] ⚠️ DB 구조 업데이트(마이그레이션) 실패 - 서버를 멈춥니다: {e}")
        raise
    await init_db.init_db()

    # 확인 문구 기본값 (종류별로 처음 한 번만 - 이미 있는 문구는 건드리지 않음)
    from services.phrase_defaults import seed_default_phrases
    async with AsyncSessionLocal() as db:
        added = await seed_default_phrases(db)
    if any(added.values()):
        print(f"[lifespan] 기본 확인 문구 추가: {added}")

    fcm_service = get_fcm_service()

    economic_event_service = EconomicEventService(
        scheduler,
        fcm_service
    )

    scheduler.start()

    scheduler.add_job(
        economic_event_service.check,
        trigger = "interval",
        minutes = 10,  # 10분마다 새 이벤트 체크
        next_run_time = datetime.now(Mt5Client.KOREA_TIMEZONE),  # 기동 직후 1회 바로 (지표일 잠금이 10분 비지 않게)
    )

    # 지표일 판단용 두 번째 출처 (Forex Factory). 요청 제한이 있어 1시간 간격.
    scheduler.add_job(
        economic_event_service.check_forexfactory,
        trigger = "interval",
        hours = 1,
        next_run_time = datetime.now(Mt5Client.KOREA_TIMEZONE),
    )

    await GenerateTerminalConfig.main()


    # 1) DB에서 터미널 정보가 세팅된 계좌 목록 조회
    _account_configs.update(await _load_accounts())
 
    # 게이트웨이 라우터가 account_id -> port 매핑을 알아야 포워딩 가능
    mt5_gateway_router.WORKER_PORTS.update(
        {aid: cfg["worker_port"] for aid, cfg in _account_configs.items()}
    )
 
    # 2) 터미널 실행
    for account_id, cfg in _account_configs.items():
        _terminal_procs[account_id] = await asyncio.to_thread(_launch_terminal_with_ea, account_id, cfg)
 
    # 3) 터미널 로그인 시간 대기 후 워커 스폰 (SKIP_WORKER_SPAWN 대상은 제외)
    await asyncio.sleep(5)
    for account_id, cfg in _account_configs.items():
        if account_id in _SKIP_WORKER_IDS:
            print(f"[lifespan] account={account_id} 워커 자동 스폰 스킵 (수동 실행 대상)")
            continue
        _worker_procs[account_id] = _spawn_worker(account_id, cfg)
 
    watchdog_task = asyncio.create_task(_watchdog_loop())

    # 4) 웹 관리 화면 (http://127.0.0.1:8100/) - 워커 목록·로그 폴더를 넘겨줌
    #    _worker_procs는 같은 dict 객체라 watchdog이 워커를 재시작해도 화면에 그대로 반영됨
    admin_router.RUNTIME.update(
        worker_procs=_worker_procs, log_dir=LOG_DIR,
        # 관리 화면의 "앱" 탭 - 플러터 웹 빌드가 있을 때만 (메인 서버 포트는 .env TRADEROS_PORT, 기본 8000)
        web_app={"port": int(os.getenv("TRADEROS_PORT", "8000")), "path": APP_PATH + "/"},
        start_account=start_account_runtime, stop_account=stop_account_runtime,
        restart_terminal=restart_terminal_runtime,
        terminal_dir_for=lambda number: os.path.dirname(_terminal_path_for(number)),
    )
    admin_server, admin_task = admin_app.start_admin_server()

    # 5) 텔레그램 실시간 현황 (고정 메시지를 계속 고쳐 씀 + 봇 버튼·/start 처리)
    from services.telegram_live import live as telegram_live
    telegram_live_task = asyncio.create_task(telegram_live.run())

    # 6) 새 버전 확인 (배포판만 - release.json 이 있을 때, services/updater.py)
    from services import updater
    update_task = asyncio.create_task(updater.loop())
 
    yield
 
    # ── shutdown ──
    telegram_live_task.cancel()
    update_task.cancel()
    await admin_app.stop_admin_server(admin_server, admin_task)
    watchdog_task.cancel()
    
    try:
        await watchdog_task
    except asyncio.CancelledError:
        pass
 
    for account_id, proc in _worker_procs.items():
        print(f"[lifespan] account={account_id} 워커 종료 요청 (pid={proc.pid})")
        proc.terminate()
    for account_id, proc in _worker_procs.items():
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print(f"[lifespan] account={account_id} 워커 강제 종료 (kill)")
            proc.kill()
 
app = FastAPI(root_path="/trading-api", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,  # 허용할 도메인
    allow_credentials=True,
    allow_methods=["*"],    # 모든 HTTP 메소드 허용
    allow_headers=["*"],    # 모든 헤더 허용
)

@app.get("/debug-check")
def debug_check():
    return {"debug_mode": __debug__}

app.include_router(trade_lock_router.router)
app.include_router(trade_lock_phrase_router.router)
app.include_router(mt5_gateway_router.router)

app.include_router(trade.router)
app.include_router(trade_note.router)
app.include_router(trade_image.router)
app.include_router(trade_tag.router)
app.include_router(account_router.router)
app.include_router(rates.router)


# 개인용 기능 (파일이 있을 때만 - 공개판에는 없음)
#   trade_report: MT5 리포트 HTML 모으기 / signal_edge_sheet: 전략 기록표 / trade_ktr: 분할 주문
#   server: Vultr 서버 요금 조회 / test: 시험용
for _name in ("trade_report", "signal_edge_sheet", "trade_ktr", "server", "test"):
    try:
        app.include_router(importlib.import_module(f"api.{_name}").router)
    except ModuleNotFoundError as _e:
        if _e.name != f"api.{_name}":
            raise


# 종목 묶음 (웹 화면 종목 버튼용 - services/symbols.py)
@app.get("/symbols")
def list_symbols():
    from services import symbols
    return symbols.groups()


# ── 웹 앱 (플러터 웹 빌드) ─────────────────────────────────────────
# 앱과 같은 화면을 브라우저에서: http://<이 PC 주소>:8000/trader-os/
# (Tailscale 주소로 폰·다른 PC에서도 접속 가능)
# 빌드: trader-os 폴더에서  flutter build web --release --base-href /trader-os/
# 빌드 결과 폴더를 순서대로 찾는다:
#   1) 환경변수 TRADEROS_WEB_DIR
#   2) 이 서버 폴더의 web_app\ (배포판에 빌드 결과를 넣어 줄 때)
#   3) 옆 폴더 ..\trader-os\build\web (개발 PC·배포 서버의 기본 배치)
WEB_APP_PATH = "/trader-os"


def _find_web_app_dir() -> str | None:
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.getenv("TRADEROS_WEB_DIR"),
              os.path.join(here, "web_app"),
              os.path.join(os.path.dirname(here), "trader-os", "build", "web")):
        if d and os.path.isfile(os.path.join(d, "index.html")):
            return d
    return None


class _WebAppMiddleware:
    """/trader-os/… 요청을 플러터 빌드 폴더에서 바로 내려준다.
    (app.mount 를 쓰지 않는 이유: 이 앱은 root_path="/trading-api" 라서
     마운트된 정적 파일 경로 계산이 어긋나 404가 난다)"""

    def __init__(self, app, directory: str, prefix: str):
        self.app = app
        self.prefix = prefix
        self.static = StaticFiles(directory=directory, html=True)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if path == self.prefix:
            return await RedirectResponse(self.prefix + "/")(scope, receive, send)
        if path.startswith(self.prefix + "/"):
            sub = dict(scope, root_path="", path=path[len(self.prefix):])
            try:
                return await self.static(sub, receive, send)
            except StarletteHTTPException as e:   # 없는 파일 → 404 (그냥 두면 500)
                return await PlainTextResponse(e.detail, status_code=e.status_code)(scope, receive, send)
        return await self.app(scope, receive, send)


# ── 웹 화면 (플러터 대신 새로 만든 웹 - 폰에서 Tailscale로 보는 용도) ──
# http://<이 PC 주소>:8000/app/   파일: 이 서버 폴더의 web_ui\
APP_PATH = "/app"
_app_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_ui")
if os.path.isfile(os.path.join(_app_dir, "index.html")):
    app.add_middleware(_WebAppMiddleware, directory=_app_dir, prefix=APP_PATH)
    print(f"[web] 웹 화면: {APP_PATH}/")

# 매매 기록에 첨부한 스크린샷 (api/trade_image.py 가 uploads\trade_<id>\ 에 저장)
# 라이브는 nginx가 내려주고, 내 PC 서버에서는 여기서 내려준다.
_uploads_dir = os.path.abspath("uploads")
os.makedirs(_uploads_dir, exist_ok=True)
app.add_middleware(_WebAppMiddleware, directory=_uploads_dir, prefix="/uploads")

_web_dir = _find_web_app_dir()
if _web_dir:
    app.add_middleware(_WebAppMiddleware, directory=_web_dir, prefix=WEB_APP_PATH)
    print(f"[web] 웹 앱: {WEB_APP_PATH}/  ({_web_dir})")
else:
    print("[web] 웹 앱 빌드를 찾지 못했습니다 - trader-os 폴더에서 "
          "flutter build web --release --base-href /trader-os/ 를 실행하면 /trader-os/ 로 열 수 있습니다.")


# ── 바깥 접속 제한 (관리 화면 '접속' 탭: open / tailscale / public) ──
# 이 PC 안에서 온 요청(MT5 EA·워커·관리 화면)은 항상 통과. 자세한 규칙은 services/access_service.py
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel as _BaseModel
from services import access_service as access


class _AccessGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            ok, code, why = access.decide(scope)
            if not ok:
                return await JSONResponse({"detail": why, "login": code == 401}, status_code=code)(scope, receive, send)
        return await self.app(scope, receive, send)


class _LoginIn(_BaseModel):
    password: str


@app.get("/auth/status")
async def auth_status(request: Request):
    cfg = access.access_cfg()
    return {"mode": cfg["mode"], "need_login": cfg["mode"] == "public",
            "logged_in": access.is_local(request.scope) or access.check_session(access.session_from_scope(request.scope))}


@app.post("/auth/login")
async def auth_login(body: _LoginIn, request: Request):
    cfg = access.access_cfg()
    if cfg["mode"] != "public":
        return {"ok": True}
    ip = access.client_ip(request.scope)
    wait = access.login_blocked(ip)
    if wait:
        raise HTTPException(429, f"비밀번호를 여러 번 틀렸습니다. {wait // 60 + 1}분 뒤에 다시 시도하세요.")
    if not cfg["password_hash"] or not access.verify_password(body.password, cfg["password_hash"]):
        access.login_failed(ip)
        await asyncio.sleep(1)   # 대입 공격을 느리게
        raise HTTPException(401, "비밀번호가 틀렸습니다.")
    access.login_ok(ip)
    res = JSONResponse({"ok": True})
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    res.set_cookie(access.COOKIE, access.make_session(cfg["session_days"]), max_age=cfg["session_days"] * 86400,
                   httponly=True, samesite="strict", secure=https, path="/")
    return res


@app.post("/auth/logout")
async def auth_logout():
    res = JSONResponse({"ok": True})
    res.delete_cookie(access.COOKIE, path="/")
    return res


app.add_middleware(_AccessGuard)   # 마지막에 추가 = 가장 바깥에서 먼저 검사 (웹 화면·사진·API 모두)


# ── 관리 화면을 :8000/admin/ 으로도 (8100 포트를 안 쳐도 되게) ──
# 관리 앱(api/admin_app.py)을 그대로 붙인다. 접속 제한은 위 _AccessGuard 대신 관리 화면 규칙(services/admin_remote.py):
#   '관리 화면 원격 접속'이 켜져 있을 때만, 같은 네트워크·Tailscale 에서, 관리자 비밀번호로.
#   이 경로는 리버스 프록시를 거칠 수 있어 이 PC에서 열어도 비밀번호를 묻는다.
ADMIN_PATH = "/admin"


class _AdminMount:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = access.normalize_path(scope.get("path", ""))
            if path == ADMIN_PATH:
                return await RedirectResponse(scope.get("path", "") + "/")(scope, receive, send)
            if path.startswith(ADMIN_PATH + "/"):
                sub = path[len(ADMIN_PATH):]
                sub_scope = dict(scope, root_path="", path=sub, raw_path=sub.encode("utf-8"), traderos_via="main")
                return await admin_app.admin_app(sub_scope, receive, send)
        return await self.app(scope, receive, send)


app.add_middleware(_AdminMount)   # _AccessGuard 보다 바깥 - /admin 은 관리 화면 규칙으로만 검사

