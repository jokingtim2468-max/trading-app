//+------------------------------------------------------------------+
//| TrendBot v2 - sample EA used by the simulator (magic 555123)     |
//| Trades NAS100 / US500 / XAUUSD with a trailing stop.             |
//+------------------------------------------------------------------+
#property strict
#include <Trade/Trade.mqh>

input long   InpMagic     = 555123;
input double InpLots      = 1.0;
input int    InpFastMA    = 20;
input int    InpSlowMA    = 50;
input double InpRiskATR   = 1.5;   // stop distance in ATRs
input double InpRR        = 2.0;   // take profit = RR x stop

CTrade trade;
int hFast, hSlow, hATR;

int OnInit()
{
   trade.SetExpertMagicNumber(InpMagic);
   hFast = iMA(_Symbol, PERIOD_CURRENT, InpFastMA, 0, MODE_EMA, PRICE_CLOSE);
   hSlow = iMA(_Symbol, PERIOD_CURRENT, InpSlowMA, 0, MODE_EMA, PRICE_CLOSE);
   hATR  = iATR(_Symbol, PERIOD_CURRENT, 14);
   return (hFast == INVALID_HANDLE || hSlow == INVALID_HANDLE || hATR == INVALID_HANDLE) ? INIT_FAILED : INIT_SUCCEEDED;
}

void OnTick()
{
   double f[2], s[2], a[1];
   if(CopyBuffer(hFast, 0, 1, 2, f) < 2 || CopyBuffer(hSlow, 0, 1, 2, s) < 2 || CopyBuffer(hATR, 0, 1, 1, a) < 1)
      return;
   double stop = a[0] * InpRiskATR;
   bool hasPos = PositionSelectByMagic();
   if(!hasPos)
   {
      if(f[0] < s[0] && f[1] > s[1])
      {
         double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
         trade.Buy(InpLots, _Symbol, ask, ask - stop, ask + stop * InpRR, "TrendBot v2");
      }
      else if(f[0] > s[0] && f[1] < s[1])
      {
         double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
         trade.Sell(InpLots, _Symbol, bid, bid + stop, bid - stop * InpRR, "TrendBot v2");
      }
      return;
   }
   Trail(stop);
}

bool PositionSelectByMagic()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
      if(PositionGetSymbol(i) == _Symbol && PositionGetInteger(POSITION_MAGIC) == InpMagic)
         return true;
   return false;
}

void Trail(double dist)
{
   ulong ticket = PositionGetInteger(POSITION_TICKET);
   double sl = PositionGetDouble(POSITION_SL), tp = PositionGetDouble(POSITION_TP);
   if(PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY)
   {
      double nsl = SymbolInfoDouble(_Symbol, SYMBOL_BID) - dist;
      if(nsl > sl + _Point * 10) trade.PositionModify(ticket, nsl, tp);
   }
   else
   {
      double nsl = SymbolInfoDouble(_Symbol, SYMBOL_ASK) + dist;
      if(sl == 0 || nsl < sl - _Point * 10) trade.PositionModify(ticket, nsl, tp);
   }
}
