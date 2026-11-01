"""Weekly ETF valuation collector based on public AKShare endpoints.
No PE or dividend yield is inferred from index price. Missing sources remain null.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
import akshare as ak

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/"data"/"data.json"
ASSETS={
 "sp500":{"name":"标普500","funddb_candidates":["标普500","标普 500","S&P 500"]},
 "nasdaq":{"name":"纳斯达克100","funddb_candidates":["纳斯达克100","纳斯达克 100","纳指100"]},
 "dividend":{"name":"中证红利低波 H30269","funddb_candidates":["中证红利低波","红利低波","中证红利低波动"]},
 "consumer":{"name":"中证主要消费 000932","funddb_candidates":["中证主要消费","主要消费"]},
 "star50":{"name":"科创50 000688","funddb_candidates":["科创50","科创板50"]},
}
def percentile(s):
    s=pd.to_numeric(s,errors="coerce").dropna()
    if len(s)<2:return None
    return float((s<=s.iloc[-1]).mean())
def clean(v):
    try:
        x=float(v)
        return x if pd.notna(x) else None
    except:return None
def fetch_funddb(key,cfg):
    result=[]
    try:
        names=ak.index_value_name_funddb()
        name_col=next((c for c in names.columns if "指数名称" in str(c) or "指数"==str(c)),None)
        code_col=next((c for c in names.columns if "代码" in str(c)),None)
        chosen=None
        if name_col:
            for candidate in cfg["funddb_candidates"]:
                m=names[names[name_col].astype(str).str.contains(candidate,case=False,regex=False)]
                if len(m):
                    chosen=str(m.iloc[0][name_col]);break
        if not chosen:return []
        for metric,col in [("市盈率","pe"),("股息率","dividend_yield")]:
            if metric=="股息率" and key!="dividend":continue
            try:
                df=ak.index_value_hist_funddb(symbol=chosen,indicator=metric)
                date_col=next((c for c in df.columns if "日期" in str(c)),None)
                value_col=metric if metric in df.columns else next((c for c in df.columns if c not in ["日期","平均值","最低30","最低10","最高30","最高10"]),None)
                if not date_col or not value_col:continue
                rows=[]
                for _,r in df.iterrows():
                    val=clean(r[value_col])
                    if val is None:continue
                    # FundDB dividend yield is commonly expressed in percentage points.
                    if metric=="股息率": val=val/100 if val>1 else val
                    rows.append({"date":pd.to_datetime(r[date_col]).strftime("%Y-%m-%d"),col:val})
                if rows:
                    if not result:result=rows
                    else:
                        mp={x["date"]:x for x in result}
                        for x in rows:mp.setdefault(x["date"],{}).update(x)
                        result=list(mp.values())
            except Exception as e: print(f"{key} {metric}: {e}")
    except Exception as e: print(f"{key} name lookup: {e}")
    return sorted(result,key=lambda x:x["date"])
def main():
    now=datetime.now(timezone.utc).astimezone()
    payload={"updatedAt":now.strftime("%Y-%m-%d %H:%M %Z"),"provider":"AKShare / FundDB public endpoints","notice":"估值数据仅在公开接口返回可核验历史序列时展示；价格行情不用于反推 PE。","assets":{},"history":[],"priceHistory":[]}
    for key,cfg in ASSETS.items():
        rows=fetch_funddb(key,cfg)
        if rows:
            df=pd.DataFrame(rows).sort_values("date")
            if "pe" in df:
                s=pd.to_numeric(df.pe,errors="coerce")
                payload["history"] += [{"key":key,"date":r["date"],"pe":clean(r["pe"]),"pe_pct":float((s.iloc[:i+1]<=s.iloc[i]).mean()) if pd.notna(s.iloc[i]) else None} for i,r in df.iterrows()]
            if key=="dividend" and "dividend_yield" in df:
                s=pd.to_numeric(df.dividend_yield,errors="coerce")
                for i,r in df.reset_index(drop=True).iterrows():
                    payload["history"].append({"key":key,"date":r["date"],"dividend_yield":clean(r["dividend_yield"]),"dividend_pct":float((s.iloc[:i+1]<=s.iloc[i]).mean()) if pd.notna(s.iloc[i]) else None})
        df=pd.DataFrame(rows)
        latest=rows[-1] if rows else {}
        pe_series=pd.to_numeric(df["pe"],errors="coerce").dropna() if "pe" in df else pd.Series(dtype=float)
        div_series=pd.to_numeric(df["dividend_yield"],errors="coerce").dropna() if "dividend_yield" in df else pd.Series(dtype=float)
        payload["assets"][key]={"name":cfg["name"],"pe":clean(latest.get("pe")),"pe_pct":percentile(pe_series) if len(pe_series)>1 else None,"dividend_yield":clean(latest.get("dividend_yield")) if key=="dividend" else None,"dividend_pct":percentile(div_series) if key=="dividend" and len(div_series)>1 else None,"date":latest.get("date"),"historyStart":rows[0]["date"] if rows else None,"status":"ok" if rows else "unavailable"}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    print("Updated",OUT,"history rows",len(payload["history"]))
if __name__=="__main__":main()
