//+------------------------------------------------------------------+
//| PrevDayHL - plots yesterday's high / low on intraday charts      |
//+------------------------------------------------------------------+
#property indicator_chart_window
#property indicator_buffers 2
#property indicator_plots   2
#property indicator_type1   DRAW_LINE
#property indicator_color1  clrLimeGreen
#property indicator_type2   DRAW_LINE
#property indicator_color2  clrTomato

double H[], L[];

int OnInit()
{
   SetIndexBuffer(0, H, INDICATOR_DATA);
   SetIndexBuffer(1, L, INDICATOR_DATA);
   return INIT_SUCCEEDED;
}

int OnCalculate(const int total, const int prev, const datetime &time[], const double &open[],
                const double &high[], const double &low[], const double &close[],
                const long &tv[], const long &vol[], const int &spread[])
{
   for(int i = MathMax(prev - 1, 0); i < total; i++)
   {
      int d = iBarShift(_Symbol, PERIOD_D1, time[i]) + 1;
      H[i] = iHigh(_Symbol, PERIOD_D1, d);
      L[i] = iLow(_Symbol, PERIOD_D1, d);
   }
   return total;
}
