//+------------------------------------------------------------------+
//|                                              SessionReporter_EA |
//|   종목 거래 시간(세션)을 TraderOS 서버로 보냄 - 휴장 · 조기 종료 미리 알림용 |
//|   주문 · 청산 기능 없음 (읽기만 함)                                  |
//+------------------------------------------------------------------+
// 브로커는 휴일(추수감사절, 크리스마스 등)이 있으면 MT5 서버의 종목 거래 시간을 바꿔 둠.
// 이 EA가 10분마다 시장 감시 창 종목 · 보유 종목의 앞으로 3일 거래 시간을 읽어
// 서버(/market/sessions)로 보내면, 서버(services/market_watch.py)가 평소와 다른 종료를 찾아
// 보유 중일 때 텔레그램으로 미리 알려 줌.
// 이 계좌의 MT5에 차트를 하나 새로 열어(파일 → 새 차트) 거기에 한 번 붙여 두면 됨.
// MT5는 차트 하나에 EA 하나만 붙으므로 TradeLock EA가 붙은 차트에는 붙이지 말 것 (TradeLock이 빠짐).
#property strict
#define EA_VERSION "1.0.0"
#property version   "1.00"
#property description "TraderOS SessionReporter EA v1.0.0 - 종목 거래 시간을 서버로 보냄 (주문 기능 없음)"

input string InpServerUrl   = "http://127.0.0.1:8000";  // FastAPI 서버 주소 (TradeLock EA와 같게)
input string InpApiKey      = "";                       // API 키 (있으면 헤더에 포함)
input int    InpEverySec    = 600;                      // 보내는 주기(초)
input int    InpDays        = 3;                        // 오늘부터 며칠치

int OnInit()
{
   EventSetTimer(MathMax(60, InpEverySec));
   SendSessions();
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason) { EventKillTimer(); }
void OnTimer() { SendSessions(); }
void OnTick() {}

string EscapeJson(string s)
{
   StringReplace(s, "\\", "\\\\");
   StringReplace(s, "\"", "\\\"");
   return s;
}

bool InList(string &arr[], string s)
{
   for(int i = 0; i < ArraySize(arr); i++) if(arr[i] == s) return true;
   return false;
}

// 보낼 종목: 보유 포지션 종목 + 시장 감시 창 종목
void CollectSymbols(string &out[])
{
   ArrayResize(out, 0);
   for(int i = 0; i < PositionsTotal(); i++)
   {
      string s = PositionGetSymbol(i);
      if(s != "" && !InList(out, s)) { int n = ArraySize(out); ArrayResize(out, n + 1); out[n] = s; }
   }
   for(int i = 0; i < SymbolsTotal(true) && ArraySize(out) < 60; i++)
   {
      string s = SymbolName(i, true);
      if(s != "" && !InList(out, s)) { int n = ArraySize(out); ArrayResize(out, n + 1); out[n] = s; }
   }
}

void SendSessions()
{
   datetime srv = TimeTradeServer();
   datetime gmt = TimeGMT();
   long off = (long)srv - (long)gmt;
   off = (long)MathRound(off / 900.0) * 900;          // 브로커 시각 - UTC (15분 단위로 반올림)
   datetime day0 = (datetime)((long)srv - ((long)srv % 86400));   // 브로커 날짜 00:00

   string syms[];
   CollectSymbols(syms);
   string json = StringFormat("{\"ea\":\"%s\",\"account_number\":%I64d,\"server\":\"%s\",\"offset\":%I64d,\"now_utc\":%I64d,\"symbols\":{",
                              EA_VERSION, AccountInfoInteger(ACCOUNT_LOGIN), EscapeJson(AccountInfoString(ACCOUNT_SERVER)),
                              off, (long)gmt);
   for(int k = 0; k < ArraySize(syms); k++)
   {
      if(k > 0) json += ",";
      json += "\"" + EscapeJson(syms[k]) + "\":[";
      bool first = true;
      for(int d = 0; d < InpDays; d++)
      {
         datetime day = day0 + d * 86400;
         MqlDateTime st;
         TimeToStruct(day, st);
         datetime from, to;
         for(uint i = 0; i < 10; i++)
         {
            if(!SymbolInfoSessionTrade(syms[k], (ENUM_DAY_OF_WEEK)st.day_of_week, i, from, to)) break;
            long a = (long)day + (long)from - off, b = (long)day + (long)to - off;   // UTC epoch
            if(b <= a) continue;
            if(!first) json += ",";
            json += StringFormat("[%I64d,%I64d]", a, b);
            first = false;
         }
      }
      json += "]";
   }
   json += "}}";

   char post[], result[];
   StringToCharArray(json, post, 0, StringLen(json));
   string headers = "Content-Type: application/json\r\n";
   if(InpApiKey != "") headers += "Authorization: Bearer " + InpApiKey + "\r\n";
   string resultHeaders;
   ResetLastError();
   int res = WebRequest("POST", InpServerUrl + "/market/sessions", headers, 5000, post, result, resultHeaders);
   if(res != 200)
      PrintFormat("[SessionReporter] 전송 실패: 응답=%d err=%d (도구 → 옵션 → 전문가 조언자 → WebRequest 허용 URL에 %s 가 있는지 확인)",
                  res, GetLastError(), InpServerUrl);
}
