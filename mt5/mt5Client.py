from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
import os
import shutil
import glob
import time
from pathlib import Path
from zoneinfo import ZoneInfo
import MetaTrader5 as mt5
from dotenv import load_dotenv
from fastapi import HTTPException
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
import json
import os
import psutil
import pyautogui
from services.crypto import CryptoService
from db.models.account import Account
import win32gui
import win32con
import win32com.client
import win32api
import time
import ctypes
import win32process

class Mt5Client:
    HANTEC_BROKER_ID = 1
    INFINOX_BROKER_ID = 2

    # 브로커 서버시간 = 뉴욕 17:00 마감 기준 (미국 서머타임 중 UTC+3, 그 외 UTC+2).
    # 예전엔 전환 시각을 표(BROKER_TIME)로 손으로 적었는데 매년 추가해야 해서
    # 미국 DST 규칙으로 자동 계산하도록 바꿈 → _get_broker_offset 참고.

    TIMEFRAMES = {
        "1m": mt5.TIMEFRAME_M1,
        "2m": mt5.TIMEFRAME_M2,
        "5m": mt5.TIMEFRAME_M5,
        "10m": mt5.TIMEFRAME_M10,
        "1h": mt5.TIMEFRAME_H1,
        "2h": mt5.TIMEFRAME_H2,
        "4h": mt5.TIMEFRAME_H4,
        "1d": mt5.TIMEFRAME_D1,
        "1w": mt5.TIMEFRAME_W1,
        "1M": mt5.TIMEFRAME_MN1
    }

    KOREA_TIMEZONE_STR = "Asia/Seoul"
    KOREA_TIMEZONE = ZoneInfo(KOREA_TIMEZONE_STR)

    total_volume: float = 0.

    accounts = []

    main_account: int = 0

    crypto_service = CryptoService()

    image_dir_name = './mt5_pyautogui_images'
    system_trading_btn_image_name = 'system_trading_btn'
    tools_image_name = 'tools'
    options_image_name = 'options'
    system_trading_image_name = 'system_trading'
    confirm_image_name = 'confirm'
    favorite_image_name = 'favorite'
    api_image_name = 'api'
    api_deactive_image_name = 'api_deactive'
    api_dialog_image_name = 'api_dialog'
    yes_image_name = 'yes'
    download_image_name = 'download'
    html_image_name = 'html'
    
    def __init__(self):
        load_dotenv()
        main_account = int(os.getenv("MAIN_ACCOUNT") or 0)   # 예전 GUI 자동화용 - 없어도 됨
        Mt5Client.main_account = main_account
        self.main_account = main_account

        if __debug__:
            self.system_trading_btn_image_name += '_debug'
            self.tools_image_name += '_debug'
            self.options_image_name += '_debug'
            self.system_trading_image_name += '_debug'
            self.confirm_image_name += '_debug'

            self.favorite_image_name += '_debug'
            self.api_image_name += '_debug'
            self.api_deactive_image_name += '_debug'
            self.api_dialog_image_name += '_debug'
            self.yes_image_name += '_debug'

            self.download_image_name += '_debug'
            self.html_image_name += '_debug'

    def collect_trade(
            self, 
            account: Account,
            from_dt = datetime(2025, 1, 1, tzinfo=ZoneInfo(KOREA_TIMEZONE_STR)), 
            to_dt = datetime(2027, 1, 1, tzinfo=ZoneInfo(KOREA_TIMEZONE_STR))
        ):

        if not self.__init_mt5__(account):
            raise SystemError("__init_mt5__ failed.")
        
        if account.broker.id == self.HANTEC_BROKER_ID or self.INFINOX_BROKER_ID:
            from_broker_time = self.__kst_to_broker_time(from_dt)
            to_broker_time = self.__kst_to_broker_time(to_dt)
        
        df = self.__fetch_deals(from_broker_time, to_broker_time)
        
        if df is None:
            return []

        df = self.__process_time(df)

        df.to_csv('./df_test.csv')

        trades_df = self.__build_trades(df)

        df.to_csv('./trades_df_test.csv')

        records = []

        for _, row in trades_df.iterrows():
            records.append({
                "ticket": row["position_id"],
                "symbol": row["symbol"],
                "position": row["position"],
                "volume": row["volume"],
                "open_time": row["entry"]["time"],
                "price_open": row["entry"]["price"],
                "close_time": row["exit"]["time"],
                "price_close": row["exit"]["price"],
                "profit": row["exit"]["profit"],
                "commission": row["commission"],
                "account_id": account.id
            })

        return records
    
    def get_trade_pnl_commission(
        self,
        account: Account,
        start_time: datetime,
        end_time: datetime
    ) -> float:

        # if not self.__init_mt5__(account):
        #     raise SystemError("__init_mt5__ failed.")

        start_hantec = self.__kst_to_broker_time(start_time)
        end_hantec = self.__kst_to_broker_time(end_time)

        deals = mt5.history_deals_get(start_hantec, end_hantec)

        pnl = 0.0
        commission = 0.0

        for d in deals:
            if d.type in [mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL]:
                pnl += d.profit + d.commission + d.swap
                commission += d.commission

        return pnl, commission

    def get_balances(
        self,
        account: Account,
        start_time: datetime,
        end_time: datetime
    ):
        # if not self.__init_mt5__(account):
        #     raise SystemError("__init_mt5__ failed.")
        
        start_hantec_time = self.__kst_to_broker_time(start_time)
        end_hantec_time = self.__kst_to_broker_time(end_time)
        
        deals = mt5.history_deals_get(start_hantec_time, end_hantec_time)

        if (len(deals) == 0 or deals is None):
            return [], []

        df = pd.DataFrame(list(deals), columns=deals[0]._asdict().keys())

        balance_df = df[df["type"] == mt5.DEAL_TYPE_BALANCE]

        balance_df = self.__process_time(balance_df)

        deposits, withdrawals = [], []

        for _, row in balance_df.iterrows():
            item = {
                "ticket": int(row["ticket"]),
                "time": row["time_kst"],
                "amount": row["profit"],
                "comment": row["comment"]
            }

            if row["profit"] > 0:
                deposits.append(item)
            elif row["profit"] < 0:
                withdrawals.append(item)

        return deposits, withdrawals
    
    def get_balance_at(
        self, 
        account: Account,
        before_time: datetime
    ) -> float:
        # if not self.__init_mt5__(account):
        #     raise SystemError("__init_mt5__ failed.")
        
        hantec_time = self.__kst_to_broker_time(before_time)
        
        deals = mt5.history_deals_get(datetime(2025,1,1), hantec_time)

        balance = 0.0
        deposits, withdrawals = 0.0, 0.0

        for d in deals:
            if d.type == mt5.DEAL_TYPE_BALANCE:
                balance += d.profit

                if (d.profit > 0):
                    deposits += d.profit
                else:
                    withdrawals += d.profit

            # 거래
            elif d.type in [mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL]:
                balance += d.profit + d.commission + d.swap

        return balance
    
    def get_real_leverage(
        self,
        account: Account,
        equity: float
    ):
        if not self.__init_mt5__(account):
            raise SystemError("mt5 login failed.")
            
        def _notional_value(pos) -> float:
            """포지션 1건의 명목가치(계약 통화 기준) 계산"""
            symbol_info = mt5.symbol_info(pos.symbol)
            if symbol_info is None:
                return 0.0
            contract_size = symbol_info.trade_contract_size
            return pos.volume * contract_size * pos.price_current
        
        positions = mt5.positions_get()
        total_notional = sum(_notional_value(p) for p in positions) if positions else 0.0
        real_leverage = (total_notional / equity) if equity > 0 else 0.0

        return real_leverage
    
    def get_positions(
        self,
        account: Account,
        symbol: str,
    ) -> list[dict]:
        if not self.__init_mt5__(account):
            raise SystemError("mt5 login failed.")

        # symbol 은 종목 이름(XAUUSD+) 또는 종목 묶음 key(gold - services/symbols.py)
        from services import symbols as sym_groups
        group = sym_groups.group_by_key(symbol)
        if group is not None:
            positions = [p for p in (mt5.positions_get() or [])
                         if (sym_groups.group_of(p.symbol) or {}).get("key") == group["key"]]
        else:
            positions = mt5.positions_get(symbol=symbol)

        if positions is None:
            return []

        sizes: dict[str, float | None] = {}

        def contract_size(name: str):
            # 1랏이 가격 1만큼 움직일 때 손익 계산용 (브로커마다 다를 수 있어 MT5에 물어봄)
            if name not in sizes:
                info = mt5.symbol_info(name)
                sizes[name] = getattr(info, "trade_contract_size", None) if info else None
            return sizes[name]

        return [
            {
                "contract_size": contract_size(p.symbol),
                "ticket": p.ticket,
                "symbol": p.symbol,
                "volume": p.volume,
                "type": p.type,
                "price_open": p.price_open,
                "price_current": p.price_current,
                "profit": p.profit,
                "swap": p.swap,
                "sl": p.sl,   # 추가 - 0.0이면 미설정
                "tp": p.tp,   # 추가 - 0.0이면 미설정
                "time": p.time,  # mt5는 unix timestamp(int) 반환
                "comment": p.comment,
            }
            for p in positions
        ]

    def get_lots(
        self, 
        account: Account, 
        symbol: str,
        start_price: float = 0.0,
        end_price: float = 0.0,
    ) -> int:
        if not self.__init_mt5__(account):
            raise SystemError("mt5 login failed.")
        
        positions = mt5.positions_get(symbol=symbol)

        if positions is None:
            return 0.0

        if start_price == 0.0 and end_price == 0.0:
            filtered = positions
        else:
            lo, hi = min(start_price, end_price), max(start_price, end_price)
            filtered = [p for p in positions if lo <= p.price_open <= hi]

        total_lots = sum(p.volume for p in filtered)

        return total_lots
    
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def set_stop_loss(
        self,
        account: Account,
        tickets: list[int],
        sl_price: float,
    ) -> dict:
        if not self.__init_mt5__(account):
            raise SystemError("mt5 login failed.")

        results = {"success": [], "failed": []}

        def _modify_one(ticket: int) -> tuple[int, bool, str]:
            position = mt5.positions_get(ticket=ticket)
            if not position:
                return ticket, False, "포지션을 찾을 수 없음"

            pos = position[0]

            # 이미 원하는 SL이면 스킵 (불필요한 요청 줄이기)
            if pos.sl == sl_price:
                return ticket, True, "already set"

            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": pos.symbol,
                "position": ticket,
                "sl": sl_price,
                "tp": pos.tp,
            }
            result = mt5.order_send(request)

            if result.retcode != mt5.TRADE_RETCODE_DONE:
                return ticket, False, f"retcode {result.retcode}: {result.comment}"
            return ticket, True, ""

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(_modify_one, t): t for t in tickets}
            for future in as_completed(futures):
                ticket, success, reason = future.result()
                if success:
                    results["success"].append(ticket)
                else:
                    results["failed"].append({"ticket": ticket, "reason": reason})

        return results
    
    def set_take_profit(
        self,
        account: Account,
        tickets: list[int],
        tp_price: float,
    ) -> dict:
        if not self.__init_mt5__(account):
            raise SystemError("mt5 login failed.")

        results = {"success": [], "failed": []}

        def _modify_one(ticket: int) -> tuple[int, bool, str]:
            position = mt5.positions_get(ticket=ticket)
            if not position:
                return ticket, False, "포지션을 찾을 수 없음"

            pos = position[0]

            if pos.tp == tp_price:
                return ticket, True, "already set"

            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": pos.symbol,
                "position": ticket,
                "sl": pos.sl,  # 기존 SL은 유지
                "tp": tp_price,
            }
            result = mt5.order_send(request)

            if result.retcode != mt5.TRADE_RETCODE_DONE:
                return ticket, False, f"retcode {result.retcode}: {result.comment}"
            return ticket, True, ""

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(_modify_one, t): t for t in tickets}
            for future in as_completed(futures):
                ticket, success, reason = future.result()
                if success:
                    results["success"].append(ticket)
                else:
                    results["failed"].append({"ticket": ticket, "reason": reason})

        return results
    
    def collect(self, 
                from_dt = datetime(2025, 1, 1, tzinfo=ZoneInfo(KOREA_TIMEZONE_STR)), 
                to_dt = datetime(2027, 1, 1, tzinfo=ZoneInfo(KOREA_TIMEZONE_STR))
        ):

        accounts = self.__load_accounts()

        for acc in accounts:
            login = acc["login"]

            if not self.__init_mt5(acc):
                continue

            df = self.__fetch_deals(from_dt, to_dt)
            
            if df is None:
                continue

            df = self.__process_time(df)

            self.__save_raw_csv(df, login)

            base_folder = f"output/{login}"
            os.makedirs(base_folder, exist_ok=True)

            self.__extract_balance(df, f"{base_folder}/balance")

            trades_df = self.__build_trades(df)

            self.__save_trades_by_month(trades_df, f"{base_folder}/trades")

            stats_folder = f"{base_folder}/stats"

            self.__save_stats(trades_df, stats_folder)

            self.__save_symbol_stats(trades_df, stats_folder)

            curve = self.__build_equity_curve(df, stats_folder)

            mdd = self.__calculate_mdd(curve)

            print(f"{login} MDD:", mdd)

        print(f'total_volume: {self.total_volume}')

        main_account = self.__get_main_account()

        if not self.__init_mt5(main_account):
            raise SystemError("main_Account login failed.")

    def rates(self,
              symbol: str,
              timeframe: str,
              from_dt = datetime(2026, 4, 2, tzinfo=ZoneInfo(KOREA_TIMEZONE_STR)),
              to_dt = datetime.now(tz=ZoneInfo(KOREA_TIMEZONE_STR)),
              is_kst = True
        ):

        acc = self.__get_main_account()

        if not self.__init_mt5(acc):
            raise SystemError("mt5 login failed.")
        
        from_dt_hantec = self.__kst_to_broker_time(from_dt, is_kst)
        to_dt_hantec = self.__kst_to_broker_time(to_dt, is_kst)

        if not mt5.symbol_select(symbol, True):
            raise SystemError("mt5 symbol_select failed.")
        
        tf_const = self.TIMEFRAMES[timeframe]
        
        rates = mt5.copy_rates_range(symbol, tf_const, from_dt_hantec, to_dt_hantec)

        if rates is not None and len(rates) > 0:
            df = pd.DataFrame(rates)
            
            if df is None:
                return pd.DataFrame()

            df = self.__process_time(df)

            df["time"] = df["time"].astype("int64")
            df["time_kst"] = df["time_kst"].astype("int64") // 10**3

            return df
        else:
            return pd.DataFrame()

    def prices(self,
               symbol = "XAUUSD",
               from_dt = datetime(2026, 4, 2, tzinfo=ZoneInfo(KOREA_TIMEZONE_STR)), 
               to_dt = datetime.now(tz=ZoneInfo(KOREA_TIMEZONE_STR))
        ):
        accounts = self.__load_accounts()

        acc = accounts[0]

        # login = acc["login"]

        if not self.__init_mt5(acc):
            return
        
        symbols = mt5.symbols_get()

        for s in symbols:
            if "XAU" in s.name:
                print(s.name)

        if not mt5.symbol_select(symbol, True):
            print("심볼 선택 실패")
            return
        
        from_dt_hantec = self.__kst_to_broker_time(from_dt)
        to_dt_hantec = self.__kst_to_broker_time(to_dt)

        all_data = {}

        for tf_name, tf_const in self.TIMEFRAMES.items():
            rates = mt5.copy_rates_range(symbol, tf_const, from_dt_hantec, to_dt_hantec)
            
            #print(f"{tf_name}: {len(rates)}")
            
            if rates is not None and len(rates) > 0:
                df = pd.DataFrame(rates)
                
                if df is None:
                    continue

                df = self.__process_time(df)

                # df['time'] = pd.to_datetime(df['time'], unit='s')

                all_data[tf_name] = df

                print(f"{tf_name} 데이터 개수: {len(df)}")
                print(df.head())

                self.__save_raw_csv(df, tf_name)

        print(all_data['1m'])

    def account_info(
        self,
        account: Account,
    ):
        if not self.__init_mt5__(account):
            raise SystemError("mt5 login failed.")
        
        return mt5.account_info()

    def __get_main_account(self):
        for acc in self.accounts:
            if int(acc['login']) == self.main_account:
                return acc
        return None

    def __load_accounts(self, path="accounts.json"):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def __init_mt5__(self, acc: Account) -> bool:
        """
        워커 프로세스 안에서 호출된다는 전제. 여기서는 재로그인을 시도하지 않고
        "정말 연결이 살아있고, 원하는 계좌가 맞는지"만 확인한다.
 
        mt5.initialize(login=..., password=..., server=...)를 다시 부르면
        이미 인증된 세션에 재인증을 강제로 트리거해서 재접속 충돌(retcode 10027류)이
        날 수 있으므로 여기서는 절대 호출하지 않는다. 로그인/AutoTrading 허용은
        전부 config.ini + 워커의 lifespan이 프로세스 시작 시점에 이미 끝내놓는다.
        """
        term_info = mt5.terminal_info()
        acc_info = mt5.account_info()
 
        if term_info is None or acc_info is None:
            print(f"계정 {acc.account_number}: MT5 연결이 없습니다. 워커가 정상 기동됐는지 확인 필요.")
            return False
 
        if str(acc_info.login) != str(acc.account_number):
            print(
                f"경고: 이 프로세스는 계정 {acc_info.login}에 연결돼 있는데 "
                f"{acc.account_number} 요청이 들어옴 - 워커/계좌 매핑을 확인하세요."
            )
            return False
 
        if not term_info.trade_allowed:
            print(f"경고: 계정 {acc.account_number} AutoTrading이 꺼져 있습니다. config.ini의 AllowLiveTrading을 확인하세요.")
            return False
 
        return True
        
    # def __init_mt5__(self, acc: Account):
    #     password = self.crypto_service.decrypt(acc.password_encrypted)

    #     if __debug__:
    #         if not mt5.initialize(
    #             login = int(acc.account_number), 
    #             password = password,
    #             server = acc.broker.server
    #         ):
    #             print(f"계정 {acc.account_number} 연결 실패:", mt5.last_error())
    #             return False
    #     else:
    #         if not mt5.initialize(
    #             login = int(acc.account_number), 
    #             password = password, 
    #             server = acc.broker.server,
    #             path = r"C:\Program Files\MetaTrader 5\terminal64.exe"
    #         ):
    #             print(f"계정 {acc.account_number} 연결 실패:", mt5.last_error())
    #             return False
        
    #     print(f"계정 {acc.account_number} 연결 성공.")

    #     term_info = mt5.terminal_info()
    #     acc_info = mt5.account_info()

    #     print(term_info)
    #     print(acc_info)

    #     # 2. 이미 원하는 계좌로 로그인되어 있는지 확인
    #     already_logged_in = (acc_info is not None and str(acc_info.login) == acc.account_number)

    #     # 3. AutoTrading(알고리즘 매매) 허용 상태 확인
    #     algo_allowed = term_info is not None and term_info.trade_allowed

    #     if not already_logged_in or not algo_allowed:
    #         self.allow_system_trading(acc)

    #     return True
    
    
    def find_mt5_window(self, account_number: str | None = None):
        result = []
 
        def callback(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            if "MT5" not in title and "MetaTrader" not in title and "Infinox" not in title:
                return
            if account_number is not None and str(account_number) not in title:
                return  # 계좌번호가 지정됐는데 타이틀에 없으면 스킵
            result.append(hwnd)
 
        win32gui.EnumWindows(callback, None)
 
        if account_number is not None and not result:
            print(f"경고: 계좌 {account_number}가 타이틀에 포함된 MT5 창을 못 찾음")
 
        return result[0] if result else None

    def _force_foreground_impl(self, hwnd):
        # 현재 포그라운드 윈도우의 스레드
        fg_hwnd = win32gui.GetForegroundWindow()
        fg_thread = win32process.GetWindowThreadProcessId(fg_hwnd)[0]
        my_thread = win32api.GetCurrentThreadId()
        
        # 스레드 입력 붙이기
        win32process.AttachThreadInput(fg_thread, my_thread, True)
        
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)

        time.sleep(0.3)
        
        # 떼기
        win32process.AttachThreadInput(fg_thread, my_thread, False)

    def _force_foreground_v2(self, hwnd):
        # Alt 키 눌렀다 떼면 포커스 제한 풀림
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        
        win32gui.SetForegroundWindow(hwnd)

        time.sleep(0.3)
 
    def force_foreground(self, account_number: str | None = None):
        hwnd = self.find_mt5_window(account_number)
 
        if hwnd is None:
            raise SystemError(f"계좌 {account_number}의 MT5 창을 찾을 수 없습니다.")
 
        try:
            self._force_foreground_impl(hwnd)       # AttachThreadInput
        except Exception:
            self._force_foreground_v2(hwnd)    # keybd_event 폴백
 
        time.sleep(0.5)
 
    def save_mt5_report(self, account_number: str | None = None):
        self.force_foreground(account_number)
 
        pyautogui.keyDown('alt')
        pyautogui.press('e')
        pyautogui.keyUp('alt')
 
        time.sleep(0.5)
 
        location = pyautogui.locateOnScreen(
            f'{self.image_dir_name}/{self.download_image_name}.png',
            confidence=0.7
        )
        if location:
            pyautogui.click(location)
 
        time.sleep(0.5)
 
        location = pyautogui.locateOnScreen(
            f'{self.image_dir_name}/{self.html_image_name}.png',
            confidence=0.7
        )
        if location:
            pyautogui.click(location)
 
        time.sleep(0.5)
        pyautogui.press('enter')
 
        # ── HTML 파일 탐지 및 이동 (기존 로직 그대로) ──
        search_dirs = [Path.home() / "Downloads", Path.home() / "Documents"]
        dest_dir = Path("./trade-report-html")
        dest_dir.mkdir(parents=True, exist_ok=True)
 
        moved_file = None
        timeout, poll_interval, elapsed = 10, 0.5, 0
 
        before: dict[Path, float] = {}
        for d in search_dirs:
            if d.exists():
                for f in d.glob("*.htm*"):
                    before[f] = f.stat().st_mtime
 
        while elapsed < timeout:
            time.sleep(poll_interval)
            elapsed += poll_interval
            for d in search_dirs:
                if not d.exists():
                    continue
                for f in d.glob("*.htm*"):
                    if f not in before:
                        dest_path = dest_dir / f.name
                        shutil.move(str(f), str(dest_path))
                        moved_file = dest_path
                        break
            if moved_file:
                break
 
        self.kill_all_chrome()
 
        if moved_file:
            print(f"[MT5] 리포트 저장 완료: {moved_file}")
        else:
            print(f"[MT5] 경고: {timeout}초 내에 HTML 파일을 찾지 못했습니다.")
 
        return moved_file

    def add_script(self):
        location = pyautogui.locateOnScreen(
            f'{self.image_dir_name}/{self.favorite_image_name}.png', 
            confidence=0.7
        )
            
        if location:
            pyautogui.click(location)

        time.sleep(0.3)

        pyautogui.press('down')

        time.sleep(0.1)

        try:
            location = pyautogui.locateOnScreen(
                f'{self.image_dir_name}/{self.api_image_name}.png', 
                confidence=0.9
            )
        except:
            location = pyautogui.locateOnScreen(
                f'{self.image_dir_name}/{self.api_deactive_image_name}.png', 
                confidence=0.9
            )
            
        if location:
            pyautogui.doubleClick(location)

        time.sleep(0.3)
        
        try:
            location = pyautogui.locateOnScreen(
                f'{self.image_dir_name}/{self.yes_image_name}.png', 
                confidence=0.99
            )
                
            if location:
                pyautogui.click(location)
        except:
            pass

        time.sleep(0.3)

        location = pyautogui.locateOnScreen(
            f'{self.image_dir_name}/{self.api_dialog_image_name}.png', 
            confidence = 0.9
        )
            
        if location:
            pyautogui.press('enter')

    def allow_system_trading(self, account: Account):
        Mt5Client.force_foreground()

        try:
            location = pyautogui.locateOnScreen(
                f'{self.image_dir_name}/{self.system_trading_btn_image_name}.png', 
                confidence=0.99
            )
                
            if location:
                pyautogui.click(location)
        except:
            pass

        self.add_script()

        return
        
        location = pyautogui.locateOnScreen(
            f'{image_dir_name}/{tools_image_name}.png', 
            confidence=0.8
        )

        if location:
            pyautogui.click(location)

            time.sleep(0.5)

            location = pyautogui.locateOnScreen(
                f'{image_dir_name}/{options_image_name}.png', 
                confidence=0.8
            )
            
            if location:
                pyautogui.click(location)

                time.sleep(0.5)

                try:
                    location = pyautogui.locateOnScreen(
                        f'{image_dir_name}/{system_trading_image_name}.png', 
                        confidence=0.99
                    )
                        
                    if location:
                        pyautogui.click(location)
                except:
                    pass

                location = pyautogui.locateOnScreen(
                    f'{image_dir_name}/{confirm_image_name}.png', 
                    confidence=0.8
                )

                if location:
                    pyautogui.click(location)

    def kill_all_chrome(self):
        for proc in psutil.process_iter(['pid', 'name']):
            if proc.info['name'] == 'chrome.exe':
                try:
                    proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

    def __disconnect_mt5__(self):
        mt5.shutdown()
    
    def __init_mt5(self, acc):
        if not mt5.initialize(
            login=acc["login"], 
            password=acc["password"], 
            server=acc["server"],
            path = r"C:\Program Files\MetaTrader 5\terminal64.exe"
        ):
            print(f"계정 {acc['login']} 연결 실패:", mt5.last_error())
            return False
        
        print(f"계정 {acc['login']} 연결 성공.")

        return True
    
    def __fetch_deals(self, from_date, to_date):
        deals = mt5.history_deals_get(from_date, to_date)

        if deals is None or len(deals) == 0:
            print("거래내역 없음")
            return None

        df = pd.DataFrame(list(deals), columns=deals[0]._asdict().keys())

        return df
    
    def __process_time(self, df):
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)

        #df["time_kst"] = df["time"] - pd.Timedelta(hours=offset)
        df["time_hantec"] = df["time"].apply(self.__apply_mt5_to_hantec_time)

        df["time_kst"] = df["time_hantec"].dt.tz_convert(self.KOREA_TIMEZONE_STR)

        # 3. UTC → KST
        #df["time_kst"] = df["time_kst"].dt.tz_convert("Asia/Seoul")

        return df
    
    def __save_raw_csv(self, df, login):
        df.to_csv(f"{login}.csv", index=False)

    def __extract_balance(self, df, balance_folder):
        os.makedirs(balance_folder, exist_ok=True)

        balance_df = df[df["type"] == mt5.DEAL_TYPE_BALANCE]

        deposits, withdrawals = [], []

        for _, row in balance_df.iterrows():
            item = {
                "ticket": int(row["ticket"]),
                "time": row["time_kst"],
                "amount": row["profit"],
                "comment": row["comment"]
            }

            if row["profit"] > 0:
                deposits.append(item)
            elif row["profit"] < 0:
                withdrawals.append(item)

        with open(f"{balance_folder}/deposits.json", "w", encoding="utf-8") as f:
            json.dump(deposits, f, indent=2, default=str)

        with open(f"{balance_folder}/withdrawals.json", "w", encoding="utf-8") as f:
            json.dump(withdrawals, f, indent=2, default=str)

        print(f"입금 {len(deposits)}개 / 출금 {len(withdrawals)}개 저장")

        return deposits, withdrawals
    
    def __build_trades(self, df):

        entry_df = df[df["entry"] == 0]
        exit_df = df[df["entry"] == 1]

        # entry/exit 각각 position_id로 집계
        entry_agg = entry_df.groupby("position_id").agg(
            time_kst=("time_kst", "first"),
            price=("price", "first"),        # 첫 진입가
            volume=("volume", "sum"),         # 총 볼륨 합산
            commission=("commission", "sum"), # 커미션 합산
            symbol=("symbol", "first"),
            type=("type", "first"),
        ).reset_index()

        exit_agg = exit_df.groupby("position_id").agg(
            time_kst=("time_kst", "last"),   # 마지막 청산 시간
            price=("price", "mean"),          # 평균 청산가
            profit=("profit", "sum"),         # 수익 합산
            volume=("volume", "sum"),
        ).reset_index()

        merged = entry_agg.merge(exit_agg, on="position_id", suffixes=("_entry", "_exit"), how="inner")
        # 이후 기존 로직 동일

        # # entry / exit 분리
        # entry_df = df[df["entry"] == 0]
        # exit_df = df[df["entry"] == 1]

        # # position_id 기준으로 첫 값만 사용
        # entry_df = entry_df.sort_values("time_kst").drop_duplicates("position_id")
        # exit_df = exit_df.sort_values("time_kst").drop_duplicates("position_id")

        # # inner join → entry + exit 둘 다 있는 것만 유지
        # merged = entry_df.merge(
        #     exit_df,
        #     on="position_id",
        #     suffixes=("_entry", "_exit"),
        #     how="inner"
        # )

        # 최종 구조 생성
        trades_df = pd.DataFrame({
            "position_id": merged["position_id"].astype(int),
            # "symbol": merged["symbol_entry"],
            # "position": np.where(merged["type_entry"] == mt5.ORDER_TYPE_BUY, "BUY", "SELL"),
            # "volume": merged["volume_entry"],
            "symbol": merged["symbol"],       # symbol_entry → symbol
            "position": np.where(merged["type"] == mt5.ORDER_TYPE_BUY, "BUY", "SELL"),
            "volume": merged["volume_entry"],  # volume은 양쪽에 있으니 suffix 붙음
            "commission": merged["commission"],
            "entry": list(zip(
                merged["time_kst_entry"],
                merged["price_entry"]
            )),
            "exit": list(zip(
                merged["time_kst_exit"],
                merged["price_exit"],
                merged["profit"]
            ))
        })

        # dict 형태로 다시 변환 (원래 구조 유지)
        trades_df["entry"] = trades_df["entry"].apply(lambda x: {
            "time": x[0],
            "price": x[1]
        })
        trades_df["exit"] = trades_df["exit"].apply(lambda x: {
            "time": x[0],
            "price": x[1],
            "profit": x[2],
        })

        account_volume = trades_df["volume"].sum()
        print(f'account_volume: {account_volume}')

        self.total_volume += account_volume

        return trades_df

    def __save_trades_by_month(self, trades_df, folder):
        os.makedirs(folder, exist_ok=True)

        trades_df["month"] = pd.to_datetime(
            trades_df["entry"].apply(lambda x: x["time"])
        ).dt.strftime("%Y-%m")

        for month, g in trades_df.groupby("month"):
            with open(f"{folder}/{month}.json", "w", encoding="utf-8") as f:
                json.dump(g.drop(columns=["month"]).to_dict("records"), f, indent=2, default=str)

                print(f"{month} 저장 완료")

    def __save_stats(self, trades_df : pd.DataFrame, stats_folder):
        #stats_folder = f"{base_folder}/stats"
        os.makedirs(stats_folder, exist_ok=True)

        monthly = []

        for month, g in trades_df.groupby("month"):
            trades_count = len(g)
            profit = g["exit"].apply(lambda x: x["profit"]).sum()
            wins = g["exit"].apply(lambda x: x["profit"] > 0).sum()

            monthly.append({
                "month": month,
                "trades": trades_count,
                "profit": profit,
                "winrate": wins / trades_count if trades_count else 0
            })

        with open(f"{stats_folder}/monthly_stats.json","w") as f:
            json.dump(monthly, f, indent=2)
    
    def __save_symbol_stats(self, trades_df : pd.DataFrame, stats_folder):
        symbol_stats = []

        for symbol, g in trades_df.groupby("symbol"):

            trades_count = len(g)

            profit = g["exit"].apply(lambda x: x["profit"]).sum()

            volume = g["volume"].sum()

            wins = g["exit"].apply(lambda x: x["profit"] > 0).sum()

            winrate = wins / trades_count if trades_count else 0

            symbol_stats.append({
                "symbol": symbol,
                "trades": trades_count,
                "profit": profit,
                "volume": volume,
                "winrate": winrate
            })

        with open(f"{stats_folder}/symbol_stats.json","w") as f:
            json.dump(symbol_stats, f, indent=2)

    def __build_equity_curve(self, df : pd.DataFrame, stats_folder):
        events = []

        for _, row in df.iterrows():

            # 입출금
            if row["type"] == mt5.DEAL_TYPE_BALANCE:

                events.append({
                    "time": row["time_kst"],
                    "value": row["profit"],
                    "type": "balance",
                    "comment": row["comment"]
                })

            # 거래
            elif row["type"] in [mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL]:

                value = row["profit"] + row["commission"] + row["swap"]

                events.append({
                    "time": row["time_kst"],
                    "value": value,
                    "type": "trade"
                })

        events.sort(key=lambda x: x["time"])

        balance = 0
        curve = []

        for e in events:

            balance += e["value"]

            curve.append({
                "time": e["time"],
                "balance": balance,
                "type": e["type"]
            })

        with open(f"{stats_folder}/equity_curve.json","w") as f:
            json.dump(curve, f, indent=2, default=str)

        trade_curve = []
        equity = 0

        for p in events:

            if p["type"] != "trade":
                continue

            equity += p["value"]

            trade_curve.append(equity)

        return trade_curve
    
    def __calculate_mdd(self, curve):
        peak, mdd = 0, 0

        for v in curve:
            peak = max(peak, v)
            mdd = max(mdd, peak - v)

        return mdd
    
    def __kst_to_broker_time(self, kst: datetime, is_kst = True) -> datetime:
        if is_kst:
            utc_dt = kst.astimezone(ZoneInfo("UTC"))
        else:
            utc_dt = kst

        offset = self._get_broker_offset(utc_dt)

        server_dt = utc_dt + timedelta(hours = offset)

        return server_dt
    
    def __apply_mt5_to_kst(self, dt_utc):
        dt = self.__apply_mt5_to_hantec_time(dt_utc)

        return dt.astimezone(ZoneInfo(self.KOREA_TIMEZONE_STR))
    
    def __apply_mt5_to_hantec_time(self, dt_utc):
        offset = Mt5Client._get_broker_offset(dt_utc)

        dt = dt_utc + pd.Timedelta(hours = - offset)

        return dt
    
    @staticmethod
    def _us_dst_bounds_utc(year: int) -> tuple[datetime, datetime]:
        """미국 서머타임 구간 (UTC).
        시작: 3월 둘째 일요일 02:00 EST = 07:00 UTC
        종료: 11월 첫째 일요일 02:00 EDT = 06:00 UTC"""
        utc = ZoneInfo("UTC")
        march1 = datetime(year, 3, 1, tzinfo=utc)
        start_day = 1 + (6 - march1.weekday()) % 7 + 7      # 둘째 일요일
        nov1 = datetime(year, 11, 1, tzinfo=utc)
        end_day = 1 + (6 - nov1.weekday()) % 7              # 첫째 일요일
        return (datetime(year, 3, start_day, 7, tzinfo=utc),
                datetime(year, 11, end_day, 6, tzinfo=utc))

    @staticmethod
    def _get_broker_offset(utc_time: datetime) -> int:
        """브로커 서버시간의 UTC 오프셋(시간). 미국 서머타임 중 3, 아니면 2.
        naive datetime은 UTC로 간주."""
        if utc_time.tzinfo is None:
            utc_time = utc_time.replace(tzinfo=ZoneInfo("UTC"))
        start, end = Mt5Client._us_dst_bounds_utc(utc_time.year)
        return 3 if start <= utc_time < end else 2
    
    def _get_mt5_data(self, symbol, timeframe, start, end):
        """
        symbol: 티커 이름, 예: "XAUUSD"
        timeframe: TIMEFRAMES 딕셔너리 key, 예: "H1"
        start: datetime 객체, 시작 시점
        end: datetime 객체, 종료 시점
        """
        # MT5 데이터 가져오기
        rates = mt5.copy_rates_range(symbol, self.TIMEFRAMES[timeframe], start, end)
        if rates is None:
            print(f"{symbol} {timeframe} 데이터 가져오기 실패")
            return None
        
        # DataFrame 변환
        df = pd.DataFrame(rates)
        # timestamp → datetime
        df['time'] = pd.to_datetime(df['time'], unit='s')
        return df