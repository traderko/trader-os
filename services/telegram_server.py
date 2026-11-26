"""텔레그램 /서버 (/server) - TraderOS 가 도는 PC 사양 · 상태

CPU(이름 · 코어 · 사용률) · 램 · 저장공간(TraderOS 가 있는 드라이브) · OS · 켜진 지 · IP(내부 · Tailscale · 외부) ·
TraderOS 버전 · 파이썬 · 이 프로그램이 쓰는 메모리 · MT5 개수. 등록된 대화에서만 (telegram_live 가 확인)
"""
from __future__ import annotations

import asyncio
import html
import os
import platform
import socket
import sys
import time


def _gb(b: float) -> str:
    return f"{b / 1024 ** 3:,.1f}GB" if b >= 1024 ** 3 else f"{b / 1024 ** 2:,.0f}MB"


def _cpu_name() -> str:
    if sys.platform == "win32":
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return " ".join(str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).split())
        except OSError:
            pass
    return platform.processor() or platform.machine()


def _ips() -> tuple[list[str], list[str]]:
    """(내부 IP들, Tailscale IP들)"""
    import psutil
    lan, ts = [], []
    for name, addrs in psutil.net_if_addrs().items():
        for a in addrs:
            if a.family != socket.AF_INET or a.address.startswith(("127.", "169.254.")):
                continue
            if a.address.startswith("100.") or "tailscale" in name.lower():
                ts.append(a.address)
            else:
                lan.append(a.address)
    return lan, ts


async def _public_ip() -> str:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=4) as c:
            return (await c.get("https://api.ipify.org")).text.strip()[:45]
    except Exception:
        return "확인 못 함"


def _uptime(sec: float) -> str:
    d, r = divmod(int(sec), 86400)
    h, r = divmod(r, 3600)
    return (f"{d}일 " if d else "") + f"{h}시간 {r // 60}분"


async def text() -> str:
    import psutil
    from services.updater import version_label
    cpu_pct = await asyncio.to_thread(psutil.cpu_percent, 1.0)        # 1초 재서 사용률
    vm = psutil.virtual_memory()
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    du = psutil.disk_usage(os.path.splitdrive(base)[0] + os.sep if sys.platform == "win32" else "/")
    lan, ts = _ips()
    pub = await _public_ip()
    me = psutil.Process()
    try:
        mt5 = sum(1 for p in psutil.process_iter(["name"]) if (p.info.get("name") or "").lower() == "terminal64.exe")
    except Exception:
        mt5 = None
    freq = psutil.cpu_freq()
    L = [
        "🖥 <b>서버</b> · " + html.escape(socket.gethostname()),
        f"TraderOS {html.escape(version_label()['label'])} · 켜진 지 {_uptime(time.time() - me.create_time())} (PC {_uptime(time.time() - psutil.boot_time())})",
        "",
        f"<b>CPU</b> {html.escape(_cpu_name())}",
        f"   {psutil.cpu_count(logical=False) or '?'}코어 {psutil.cpu_count()}스레드"
        + (f" · {freq.current / 1000:.1f}GHz" if freq and freq.current else "") + f" · 사용 {cpu_pct:.0f}%",
        f"<b>램</b> {_gb(vm.used)} / {_gb(vm.total)} ({vm.percent:.0f}%) · 남음 {_gb(vm.available)}",
        f"<b>저장공간</b> {os.path.splitdrive(base)[0] or '/'} {_gb(du.used)} / {_gb(du.total)} ({du.percent:.0f}%) · 남음 {_gb(du.free)}",
        f"<b>OS</b> {html.escape(platform.system())} {html.escape(platform.release())} ({html.escape(platform.version())}) · {html.escape(platform.machine())}",
        "",
        f"<b>IP</b> 내부 {', '.join(lan) or '-'}",
        f"   Tailscale {', '.join(ts) or '-'} · 외부 {html.escape(pub)}",
        "",
        f"TraderOS 메모리 {_gb(me.memory_info().rss)} · 파이썬 {platform.python_version()}"
        + (f" · MT5 {mt5}개 실행 중" if mt5 is not None else ""),
    ]
    if du.free < 5 * 1024 ** 3:
        L.append("⚠️ 저장공간이 5GB 아래입니다")
    if vm.percent >= 90:
        L.append("⚠️ 램을 90% 넘게 쓰고 있습니다")
    return "\n".join(L)


async def cmd(live, client, token, cid: str, arg: str = "") -> None:
    try:
        t = await text()
    except Exception as e:
        t = f"서버 정보를 못 읽었습니다: {html.escape(str(e)[:200])}"
    await live._send(client, token, cid, t)
