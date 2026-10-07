//+------------------------------------------------------------------+
//|                                                    TradeLock_EA |
//|      FastAPI 락 서버 연동 - 시간대락/연속손절락 + 즉시청산       |
//+------------------------------------------------------------------+
#property strict
#include <Trade\Trade.mqh>
#include <Controls\Dialog.mqh>
#include <Controls\Edit.mqh>
#include <Controls\Button.mqh>
#include <Controls\Label.mqh>

//--- 설정
input group "< 서버 연동 설정 >"
input string InpServerUrl     = "http://127.0.0.1:8000";  // FastAPI 서버 주소
input int    InpPollSeconds   = 3;                           // 상태 확인 주기(초)
input string InpApiKey        = "";                          // API 키 (있으면 헤더에 포함)

//--- 즉시청산 대상
input group "< 청산 설정 >"
input bool   InpCloseAllSymbols = true;   // true: 전체종목 청산, false: 현재종목만
                                            // (특정 종목만 감시하고 싶은 게 아니면 true 권장 -
                                            //  이 시스템은 계좌 전체의 충동매매를 막는 게 취지)
input long   InpLockCloseMagic  = 990099; // 락에 의한 강제청산 식별용 매직넘버
                                            // (백엔드 LOCK_CLOSE_MAGIC 상수와 반드시 일치해야 함)

//--- 체결 기록 (예전 api EA 기능을 합침 - MT5 시작 설정으로는 EA를 하나만 붙일 수 있어서)
input group "< 체결 기록 >"
input bool   InpSendTrades = true;        // 진입·청산 체결을 서버(/trades)로 보내 거래내역에 기록
                                            // (api EA를 따로 붙여 쓰고 있다면 둘 중 하나만 켜도 됨 -
                                            //  서버가 같은 체결을 두 번 받아도 한 번만 처리함)

//==================================================================
#define CONFIRM_PHRASE "나는 규칙을 확인했고, 뇌동매매를 하지 않겠습니다"

CTrade trade;
bool   g_locked = false;         // 서버가 "잠김"이라고 답한 경우에만 true.
                                 // 서버 응답을 못 받은 상태(시작 직후, 서버 재시작 중 등)에서는 절대 청산하지 않음 -
                                 // 예전엔 true로 시작해서, 첫 조회가 실패하면 보호 스냅샷도 없이
                                 // 기존 포지션까지 전부 청산될 수 있었다.
string g_lockReason = "unknown";
int    g_remainSec = 0;
datetime g_lastPollTime = 0;
bool   g_pollFailed = false;
bool   g_everPolled = false;     // 시작 후 조회에 한 번이라도 성공했는지 (시작 직후 알림 중복 방지)
datetime g_lockStartTime = 0;    // 참고용으로만 유지 (진단 로그 표시용) - 청산 판단엔 더 이상 안 씀
ulong  g_protectedTickets[];     // 락 걸리는 "그 순간" 이미 존재하던 티켓 목록 - 얘내는 절대 안 건드림
bool   g_hasSnapshot = false;    // 스냅샷을 한 번이라도 떴는지

//--- 다이얼로그
class CLockDialog : public CAppDialog
{
private:
   CLabel m_lblStatus;
   CLabel m_lblReason;
   CEdit  m_edtPhrase;
   CButton m_btnSubmit;
   CLabel m_lblResult;
public:
   bool Create(long chart, string name, int subwin, int x1, int y1, int x2, int y2);
   void UpdateStatus(bool locked, string reason, int remain);
   void ShowResult(string msg, color clr);
   virtual bool OnEvent(const int id, const long &lparam, const double &dparam, const string &sparam);
   string GetPhraseText() { return m_edtPhrase.Text(); }
   void ClearPhrase() { m_edtPhrase.Text(""); }
};

bool CLockDialog::Create(long chart, string name, int subwin, int x1, int y1, int x2, int y2)
{
   if(!CAppDialog::Create(chart, name, subwin, x1, y1, x2, y2)) return false;

   m_lblStatus.Create(chart, name+"lblStatus", subwin, 10, 25, 280, 45);
   m_lblStatus.Text("상태 확인중...");
   m_lblStatus.Color(clrOrange);
   Add(m_lblStatus);

   m_lblReason.Create(chart, name+"lblReason", subwin, 10, 50, 280, 70);
   m_lblReason.Text("");
   Add(m_lblReason);

   m_edtPhrase.Create(chart, name+"edtPhrase", subwin, 10, 80, 280, 105);
   m_edtPhrase.Text("");
   Add(m_edtPhrase);

   m_btnSubmit.Create(chart, name+"btnSubmit", subwin, 10, 110, 280, 135);
   m_btnSubmit.Text("확인 및 잠금해제 요청");
   Add(m_btnSubmit);

   m_lblResult.Create(chart, name+"lblResult", subwin, 10, 140, 280, 160);
   m_lblResult.Text("");
   Add(m_lblResult);

   return true;
}

void CLockDialog::UpdateStatus(bool locked, string reason, int remain)
{
   if(locked)
   {
      m_lblStatus.Text("🔒 거래 잠금 상태");
      m_lblStatus.Color(clrRed);
      string reasonKr = "";
      if(reason == "restricted_window") reasonKr = "정기 재확인 (00-07시 30분 / 그 외 2시간 단위)";
      else if(StringFind(reason, "consec_loss") == 0) reasonKr = "연속 손절 감지: " + reason;
      else if(StringFind(reason, "session_window") == 0) reasonKr = "집중 구간 - 전용 문구";
      else if(reason == "manual") reasonKr = "수동 잠금 (시간이 지나야 해제)";
      else reasonKr = reason;
      m_lblReason.Text("사유: " + reasonKr);
      m_edtPhrase.Show();
      m_btnSubmit.Show();
   }
   else
   {
      m_lblStatus.Text("🔓 거래 가능");
      m_lblStatus.Color(clrLime);
      if(remain > 0)
         m_lblReason.Text(StringFormat("재잠금까지 %d분 %d초", remain/60, remain%60));
      else
         m_lblReason.Text("");
      m_edtPhrase.Hide();
      m_btnSubmit.Hide();
   }
}

void CLockDialog::ShowResult(string msg, color clr)
{
   m_lblResult.Text(msg);
   m_lblResult.Color(clr);
}

bool CLockDialog::OnEvent(const int id, const long &lparam, const double &dparam, const string &sparam)
{
   if(id == CHARTEVENT_OBJECT_CLICK && sparam == m_btnSubmit.Name())
   {
      OnSubmitPressed();
      return true;
   }
   return CAppDialog::OnEvent(id, lparam, dparam, sparam);
}

CLockDialog g_dialog;

//==================================================================
void OnSubmitPressed()
{
   string phrase = g_dialog.GetPhraseText();
   long accountLogin = AccountInfoInteger(ACCOUNT_LOGIN);
   string url = InpServerUrl + "/lock/confirm";
   string body = StringFormat("{\"account_number\":%d,\"phrase\":\"%s\"}",
                               accountLogin, EscapeJson(phrase));

   char post[], result[];
   StringToCharArray(body, post, 0, StringLen(body));
   string headers = "Content-Type: application/json\r\n";
   if(InpApiKey != "") headers += "Authorization: Bearer " + InpApiKey + "\r\n";
   string resultHeaders;

   ResetLastError();
   int res = WebRequest("POST", url, headers, 5000, post, result, resultHeaders);

   if(res == -1)
   {
      g_dialog.ShowResult("서버 연결 실패 (URL 허용목록 확인: " + IntegerToString(GetLastError()) + ")", clrRed);
      return;
   }
   if(res != 200)
   {
      string resp = CharArrayToString(result);
      g_dialog.ShowResult("거부됨: " + resp, clrRed);
      g_dialog.ClearPhrase();
      return;
   }

   g_dialog.ShowResult("잠금 해제 완료.", clrLime);
   g_dialog.ClearPhrase();
   PollLockStatus(); // 즉시 상태 갱신
}

//==================================================================
string EscapeJson(string s)
{
   string r = s;
   StringReplace(r, "\\", "\\\\");
   StringReplace(r, "\"", "\\\"");
   return r;
}

//==================================================================
int OnInit()
{
   EventSetTimer(1);
   trade.SetDeviationInPoints(30);
   // 이 EA는 락 강제청산만 수행하므로, 모든 청산 딜에 항상 이 매직넘버가 붙음.
   // test.mq5 웹훅이 DEAL_MAGIC을 읽어 백엔드에 전달 -> 백엔드가 이 값으로
   // "몰래 진입했다 강제청산됨" 알림을 판단함.
   trade.SetExpertMagicNumber(InpLockCloseMagic);
   g_lockStartTime = TimeCurrent();  // 재시작 시점 이전 포지션은 항상 보호 (기본값)

   if(!g_dialog.Create(0, "TradeLockDlg", 0, 20, 20, 320, 190))
   {
      Print("다이얼로그 생성 실패");
      return INIT_FAILED;
   }
   g_dialog.Run();

   PollLockStatus();
   Print("TradeLock_EA 시작.");
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   g_dialog.Destroy();
}

//==================================================================
void OnTimer()
{
   static int counter = 0;
   counter++;
   if(counter >= InpPollSeconds)
   {
      counter = 0;
      PollLockStatus();
   }

   // 청산 조건: 마지막 조회가 성공했고, 그 응답이 "잠김"이고, 보호 스냅샷을 이미 떠 둔 상태.
   // 조회 실패(서버 꺼짐·재시작·네트워크 문제) 중에는 청산을 멈춘다.
   if(g_locked && !g_pollFailed && g_hasSnapshot)
      CloseAllOpenPositions("[TradeLock] 잠금 상태 - 즉시청산");

   if(counter == 0)
      FlushTradeQueue();   // 체결 기록 재전송 (상태 확인 주기마다)
}

//==================================================================
void PollLockStatus()
{
   long accountLogin = AccountInfoInteger(ACCOUNT_LOGIN);
   string url = StringFormat("%s/lock/status?account_number=%d", InpServerUrl, accountLogin);
   char post[], result[];
   string headers = "";
   if(InpApiKey != "") headers = "Authorization: Bearer " + InpApiKey + "\r\n";
   string resultHeaders;

   ResetLastError();
   int res = WebRequest("GET", url, headers, 4000, post, result, resultHeaders);

   if(res != 200)
   {
      // 통신 실패: 잠금 여부를 모르므로 청산하지 않음 (OnTimer가 g_pollFailed를 보고 멈춤)
      if(!g_pollFailed)
         PrintFormat("[TradeLock] 서버 통신 실패 (code=%d, err=%d) - 응답이 올 때까지 자동청산 중지", res, GetLastError());
      g_pollFailed = true;
      Comment(StringFormat("[TradeLock] 서버 통신 실패 (code=%d, err=%d) - 자동청산 중지됨", res, GetLastError()));
      g_dialog.ShowResult("서버 연결 끊김 - 자동청산 중지", clrOrange);
      return;
   }
   if(g_pollFailed)
      g_dialog.ShowResult("", clrWhite);
   g_pollFailed = false;

   string json = CharArrayToString(result);
   bool locked = (StringFind(json, "\"locked\":true") >= 0 || StringFind(json, "\"locked\": true") >= 0);
   string reason = ExtractJsonString(json, "reason");
   int remain = (int)ExtractJsonInt(json, "remaining_sec");

   bool wasLocked = g_locked;
   g_locked = locked;
   g_lockReason = reason;
   g_remainSec = remain;

   if(locked)
   {
      long sinceEpoch = (long)ExtractJsonInt(json, "locked_since_broker_epoch");
      PrintFormat("[TradeLock 진단] 서버 응답 raw sinceEpoch=%d (reason=%s)", sinceEpoch, reason);
      if(sinceEpoch > 0)
         g_lockStartTime = (datetime)sinceEpoch;  // 참고용 (진단 로그 표시용)
      else if(!wasLocked)
         g_lockStartTime = TimeCurrent();

      // 핵심: "새로 락 걸린 순간"(재시작 중 이미 락 상태였던 경우 포함, g_hasSnapshot로 판단)에만
      // 딱 한 번 티켓 스냅샷을 뜬다. 이후 폴링에서는 절대 다시 안 뜸 -
      // 매번 다시 뜨면 "락 걸린 동안 새로 연 포지션"도 다음 스냅샷에 포함돼서
      // 보호 대상으로 오분류될 수 있기 때문.
      if(!wasLocked || !g_hasSnapshot)
      {
         TakeProtectedTicketsSnapshot();
         g_hasSnapshot = true;
      }
   }
   else
   {
      g_hasSnapshot = false;  // 잠금 풀리면 스냅샷 무효화 - 다음 락 걸릴 때 새로 떠야 함
   }

   g_dialog.UpdateStatus(locked, reason, remain);

   bool firstPoll = !g_everPolled;
   g_everPolled = true;
   if(!wasLocked && locked && !firstPoll)
      Alert("[TradeLock] 거래가 잠겼습니다: " + reason);

   Comment(StringFormat("[TradeLock] %s | %s", locked ? "LOCKED" : "UNLOCKED", reason));
}

//==================================================================
string ExtractJsonString(string json, string key)
{
   string pattern = "\"" + key + "\":\"";
   int p = StringFind(json, pattern);
   if(p < 0) return "";
   p += StringLen(pattern);
   int e = StringFind(json, "\"", p);
   if(e < 0) return "";
   return StringSubstr(json, p, e - p);
}

double ExtractJsonInt(string json, string key)
{
   string pattern = "\"" + key + "\":";
   int p = StringFind(json, pattern);
   if(p < 0) return 0;
   p += StringLen(pattern);
   int e = p;
   while(e < StringLen(json) && (StringGetCharacter(json, e) >= '0' && StringGetCharacter(json, e) <= '9'))
      e++;
   if(e == p) return 0;
   return StringToDouble(StringSubstr(json, p, e - p));
}

//==================================================================
// 락 걸리는 "그 순간" 존재하던 모든 포지션 티켓을 스냅샷.
// 넷팅 계좌에서 POSITION_TIME이 반전(reversal) 등으로 흔들릴 수 있어서,
// 시각 비교보다 "그 순간 존재했는가"라는 티켓 단위 판단이 훨씬 안전함.
void TakeProtectedTicketsSnapshot()
{
   int total = PositionsTotal();
   ArrayResize(g_protectedTickets, total);
   for(int i = 0; i < total; i++)
      g_protectedTickets[i] = PositionGetTicket(i);

   PrintFormat("[TradeLock] 보호 티켓 스냅샷 %d개 저장", total);
}

bool IsProtectedTicket(ulong ticket)
{
   for(int i = 0; i < ArraySize(g_protectedTickets); i++)
      if(g_protectedTickets[i] == ticket) return true;
   return false;
}

//==================================================================
void CloseAllOpenPositions(string tag)
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(!PositionSelectByTicket(ticket)) continue;
      string sym = PositionGetString(POSITION_SYMBOL);
      if(!InpCloseAllSymbols && sym != _Symbol) continue;

      // 락 걸리는 순간 스냅샷에 있던 티켓(=락 걸리기 전부터 존재)은 절대 안 건드림.
      // POSITION_TIME 비교 방식은 넷팅 반전 등으로 흔들릴 수 있어서 폐기함 -
      // 티켓 자체는 넷팅 반전에도 유지된다고 MT5 공식 문서에 명시돼있어 더 안전함.
      bool isProtected = IsProtectedTicket(ticket);

      PrintFormat("[TradeLock 진단] 티켓=%d 스냅샷보호=%s 비교=%s",
                  ticket, isProtected ? "예" : "아니오",
                  isProtected ? "보호(스킵)" : "청산대상");

      if(isProtected) continue;

      if(trade.PositionClose(ticket))
      {
         string msg = StringFormat("%s: %s 청산 (티켓:%d)", tag, sym, ticket);
         Print(msg);
         Alert(msg);
      }
   }
}
//+------------------------------------------------------------------+

//==================================================================
// 체결 기록: 진입(OPEN)·청산(CLOSE) 체결을 서버 POST /trades 로 보낸다.
// 서버는 이 기록으로 거래내역·뇌동매매 경고·"잠금 중 진입 강제청산" 알림을 만든다.
void OnTradeTransaction(const MqlTradeTransaction& trans,
                        const MqlTradeRequest& request,
                        const MqlTradeResult& result)
{
   if(!InpSendTrades) return;
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD) return;

   ulong deal = trans.deal;
   if(!HistoryDealSelect(deal)) return;

   long type = HistoryDealGetInteger(deal, DEAL_TYPE);
   if(type != DEAL_TYPE_BUY && type != DEAL_TYPE_SELL) return;   // 입출금·크레딧 등 제외

   long entry = HistoryDealGetInteger(deal, DEAL_ENTRY);
   string entryType;
   if(entry == DEAL_ENTRY_IN)       entryType = "OPEN";
   else if(entry == DEAL_ENTRY_OUT) entryType = "CLOSE";
   else return;                                                   // 반대매매(INOUT) 등은 기록 안 함 (기존 api EA와 동일)

   string json = StringFormat(
      "{\"ticket\":%I64d,\"deal\":%I64u,\"position\":\"%s\",\"commission\":%.2f,"
      "\"account_number\":%I64d,\"broker_server\":\"%s\",\"symbol\":\"%s\","
      "\"volume\":%.2f,\"price\":%.5f,\"profit\":%.2f,\"entry_type\":\"%s\","
      "\"time\":%I64d,\"magic\":%I64d}",
      HistoryDealGetInteger(deal, DEAL_POSITION_ID), deal,
      type == DEAL_TYPE_BUY ? "BUY" : "SELL",
      HistoryDealGetDouble(deal, DEAL_COMMISSION),
      AccountInfoInteger(ACCOUNT_LOGIN), EscapeJson(AccountInfoString(ACCOUNT_SERVER)),
      EscapeJson(HistoryDealGetString(deal, DEAL_SYMBOL)),
      HistoryDealGetDouble(deal, DEAL_VOLUME), HistoryDealGetDouble(deal, DEAL_PRICE),
      HistoryDealGetDouble(deal, DEAL_PROFIT), entryType,
      (long)HistoryDealGetInteger(deal, DEAL_TIME), HistoryDealGetInteger(deal, DEAL_MAGIC));

   // 바로 보내고, 실패하면 대기열에 넣어 타이머에서 다시 보낸다 (서버 재시작 중이어도 기록이 사라지지 않게)
   if(!PostJson("/trades", json))
      QueueTrade(json);
}

string g_tradeQueue[];
int    g_tradeTries[];

void QueueTrade(string json)
{
   int n = ArraySize(g_tradeQueue);
   if(n >= 200) return;   // 서버가 오래 꺼져 있으면 너무 쌓이지 않게
   ArrayResize(g_tradeQueue, n + 1);
   ArrayResize(g_tradeTries, n + 1);
   g_tradeQueue[n] = json;
   g_tradeTries[n] = 1;
}

// OnTimer에서 호출 - 대기 중인 체결 기록을 한 건씩 다시 보낸다
void FlushTradeQueue()
{
   if(ArraySize(g_tradeQueue) == 0) return;
   if(PostJson("/trades", g_tradeQueue[0]))
   {
      ArrayRemove(g_tradeQueue, 0, 1);
      ArrayRemove(g_tradeTries, 0, 1);
      return;
   }
   g_tradeTries[0]++;
   if(g_tradeTries[0] > 100)   // 약 5분 넘게 실패하면 포기 (서버에서 MT5 기록으로 다시 모을 수 있음)
   {
      Print("[TradeLock] 체결 기록 전송 포기: ", g_tradeQueue[0]);
      ArrayRemove(g_tradeQueue, 0, 1);
      ArrayRemove(g_tradeTries, 0, 1);
   }
}

bool PostJson(string path, string json)
{
   // 잠금 해제 요청(OnSubmitPressed)과 같은 방식으로 본문을 만든다 (끝의 NULL 문자 제외)
   char post[], result[];
   StringToCharArray(json, post, 0, StringLen(json));
   string headers = "Content-Type: application/json\r\n";
   if(InpApiKey != "") headers += "Authorization: Bearer " + InpApiKey + "\r\n";
   string resultHeaders;
   ResetLastError();
   int res = WebRequest("POST", InpServerUrl + path, headers, 5000, post, result, resultHeaders);
   int err = GetLastError();   // 다른 함수를 부르기 전에 읽어야 정확함
   if(res == 200) return true;
   string body = ArraySize(result) > 0 ? CharArrayToString(result) : "";
   PrintFormat("[TradeLock] %s 실패: 응답=%d err=%d 본문=%s 보낸값=%s", path, res, err, body, json);
   return false;
}
//+------------------------------------------------------------------+
