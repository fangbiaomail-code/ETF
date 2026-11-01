"""Weekly ETF valuation collector.

Uses official CSI valuation files for mainland-China indices and a no-key,
non-commercial public index-valuation feed for U.S. indices. Missing data is
never inferred from price; the last valid snapshot is retained on source errors.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from io import StringIO

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "data.json"
BASE_SIBLIS = "https://siblisresearch.supabase.co/functions/v1/free-data-api"
TIMEOUT = 35
HEADERS = {"User-Agent": "ETF-Valuation-Watch/1.0 (personal research dashboard)"}

ASSETS = {
    "sp500": {"name": "标普500", "kind": "us", "candidates": ["S&P 500", "S&P500", "SPX"]},
    "dividend": {"name": "中证红利低波 H30269", "kind": "csi", "code": "H30269"},
    "nasdaq": {"name": "纳斯达克100", "kind": "us", "candidates": ["NASDAQ 100", "NASDAQ-100", "NDX"]},
    "consumer": {"name": "中证主要消费 000932", "kind": "csi", "code": "000932"},
    "star50": {"name": "科创50 000688", "kind": "csi", "code": "000688"},
}

def get_json(url):
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()

def number(value):
    try:
        result = float(str(value).replace(",", "").replace("%", "").strip())
        return result if pd.notna(result) else None
    except Exception:
        return None

def date_string(value):
    try:
        return pd.to_datetime(value, errors="raise").strftime("%Y-%m-%d")
    except Exception:
        return None

def percentile(values, current=None):
    clean_values = [number(x) for x in values]
    clean_values = [x for x in clean_values if x is not None]
    if len(clean_values) < 2:
        return None
    target = current if current is not None else clean_values[-1]
    return sum(x <= target for x in clean_values) / len(clean_values)

def csi_rows(code):
    # This AKShare endpoint reads the CSI index indicator XLS, not price data.
    import akshare as ak
    frame = ak.stock_zh_index_value_csindex(symbol=code)
    if frame is None or frame.empty:
        raise RuntimeError(f"中证指数估值文件为空：{code}")
    date_col = next((c for c in frame.columns if "日期" in str(c)), None)
    pe_col = next((c for c in frame.columns if str(c).strip() in ("市盈率2", "市盈率2（计算用股本）P/E2")), None)
    dy_col = next((c for c in frame.columns if str(c).strip() in ("股息率2", "股息率2（计算用股本）D/P2")), None)
    if date_col is None:
        raise RuntimeError(f"中证估值文件缺少日期列：{code} / {list(frame.columns)}")
    rows = []
    for _, row in frame.iterrows():
        dt = date_string(row[date_col])
        if not dt:
            continue
        pe = number(row[pe_col]) if pe_col else None
        # CSI dividend yield columns are expressed in percentage points, e.g. 4.8 means 4.8%.
        raw_dy = number(row[dy_col]) if dy_col and code == "H30269" else None
        dy = raw_dy / 100 if raw_dy is not None else None
        if pe is None and dy is None:
            continue
        rows.append({"date": dt, "pe": pe if pe and pe > 0 else None, "dividend_yield": dy})
    rows.sort(key=lambda x: x["date"])
    if len(rows) < 2:
        raise RuntimeError(f"中证估值历史样本不足：{code}，仅 {len(rows)} 条")
    return rows, "中证指数有限公司官方估值文件（AKShare 接口）", "日频"

def siblis_indices():
    payload = get_json(f"{BASE_SIBLIS}/v1/indices")
    return payload.get("indices", [])

def pick_siblis_ticker(index_list, key):
    candidates = ASSETS[key]["candidates"]
    for item in index_list:
        name = str(item.get("index_name", "")).lower().replace("–", "-")
        ticker = str(item.get("ticker", "")).upper()
        if key == "sp500" and ("s&p 500" in name or "s&p500" in name or ticker in ("SPX", "SP500")):
            return ticker
        if key == "nasdaq" and ("nasdaq 100" in name or "nasdaq-100" in name or ticker == "NDX"):
            return ticker
    # Probe exact documented aliases only after checking the provider's live catalogue.
    available = {str(x.get("ticker", "")).upper() for x in index_list}
    for candidate in candidates:
        alias = candidate.upper()
        if alias in available:
            return alias
    return None

def siblis_rows(ticker):
    payload = get_json(f"{BASE_SIBLIS}/v1/{ticker}/pe-trailing")
    data = payload.get("data", [])
    rows = []
    for item in data:
        dt = date_string(item.get("trading_day (EOD)") or item.get("date") or item.get("trading_day"))
        value = number(item.get("value"))
        if dt and value is not None and value > 0:
            rows.append({"date": dt, "pe": value, "dividend_yield": None})
    rows.sort(key=lambda x: x["date"])
    if len(rows) < 2:
        raise RuntimeError(f"Siblis {ticker} 的 TTM PE 历史样本不足")
    return rows, "Siblis Research 免费历史估值接口（需注明来源；月末历史样本）", "月末"

def hom_sp500_rows():
    # Public CC BY 4.0 dataset; JSON includes a trailing P/E series.
    payload = get_json("https://historyofmarket.com/api/sp500/forward-pe.json")
    found = []
    date_keys = ("date", "week", "week_ending", "trading_day", "observation_date", "period")
    pe_keys = ("ttm_pe", "pe_ttm", "trailing_pe", "trailingpe", "pe_trailing", "ttmpe")
    def walk(node):
        if isinstance(node, dict):
            dt = next((date_string(node.get(k)) for k in date_keys if node.get(k) is not None and date_string(node.get(k))), None)
            if dt:
                for k, v in node.items():
                    key = str(k).lower().replace("-", "_")
                    if any(term in key for term in pe_keys):
                        val = number(v)
                        if val is not None and val > 0:
                            found.append({"date": dt, "pe": val, "dividend_yield": None})
                            break
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(payload)
    unique = {x["date"]: x for x in found}
    rows = sorted(unique.values(), key=lambda x: x["date"])
    if len(rows) < 2:
        raise RuntimeError("History of Market JSON 中未识别到可用的 S&P 500 TTM PE 序列")
    return rows, "History of Market（CC BY 4.0，标普500 TTM PE）", "周频"

def dqydj_sp500_rows():
    # Secondary fallback: published monthly S&P 500 trailing P/E table.
    url = "https://dqydj.com/sp-500-pe-ratio/"
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    tables = pd.read_html(StringIO(response.text))
    for frame in tables:
        cols = {str(c).lower(): c for c in frame.columns}
        date_col = next((v for k, v in cols.items() if "date" in k or "month" in k), None)
        value_col = next((v for k, v in cols.items() if "pe" in k or "value" in k), None)
        if date_col is None or value_col is None:
            continue
        rows = []
        for _, row in frame.iterrows():
            dt, pe = date_string(row[date_col]), number(row[value_col])
            if dt and pe and pe > 0:
                rows.append({"date": dt, "pe": pe, "dividend_yield": None})
        if len(rows) >= 2:
            return sorted(rows, key=lambda x: x["date"]), "DQYDJ（月度标普500 TTM PE）", "月末"
    raise RuntimeError("DQYDJ 页面没有返回可识别的 PE 历史表")

def load_old():
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:
        return {}

def main():
    now = datetime.now(timezone.utc)
    old = load_old()
    old_assets = old.get("assets", {})
    old_history = old.get("history", [])
    payload = {
        "updatedAt": now.strftime("%Y-%m-%d %H:%M UTC"),
        "provider": "AKShare + official CSI + public index valuation datasets",
        "notice": "PE 使用指数层面 TTM 口径；百分位按页面所列完整可用历史样本计算。缺少可核验估值时不以价格推算。",
        "assets": {},
        "history": [],
    }

    try:
        index_list = siblis_indices()
    except Exception as exc:
        index_list = []
        print(f"Siblis index catalogue unavailable: {exc}")

    for key, cfg in ASSETS.items():
        try:
            if cfg["kind"] == "csi":
                rows, source, frequency = csi_rows(cfg["code"])
            else:
                ticker = pick_siblis_ticker(index_list, key) if index_list else None
                if ticker:
                    try:
                        rows, source, frequency = siblis_rows(ticker)
                    except Exception as exc:
                        print(f"Siblis {key} ({ticker}) failed: {exc}")
                        if key != "sp500":
                            raise
                        rows, source, frequency = hom_sp500_rows()
                elif key == "sp500":
                    try:
                        rows, source, frequency = hom_sp500_rows()
                    except Exception as exc:
                        print(f"History of Market S&P fallback failed: {exc}")
                        rows, source, frequency = dqydj_sp500_rows()
                else:
                    raise RuntimeError("免费数据目录暂未提供 NASDAQ 100 的可核验 TTM PE 序列")

            pe_values = [r["pe"] for r in rows if r.get("pe") is not None]
            dy_values = [r["dividend_yield"] for r in rows if r.get("dividend_yield") is not None]
            latest = rows[-1]
            latest_pe = latest.get("pe")
            latest_dy = latest.get("dividend_yield") if key == "dividend" else None
            payload["assets"][key] = {
                "name": cfg["name"],
                "pe": latest_pe,
                "pe_pct": percentile(pe_values, latest_pe) if latest_pe is not None else None,
                "dividend_yield": latest_dy,
                "dividend_pct": percentile(dy_values, latest_dy) if latest_dy is not None and len(dy_values) > 1 else None,
                "date": latest["date"],
                "historyStart": rows[0]["date"],
                "sampleCount": len(rows),
                "source": source,
                "frequency": frequency,
                "status": "ok",
            }
            for i, point in enumerate(rows):
                hist = {"key": key, "date": point["date"], "pe": point.get("pe")}
                if point.get("pe") is not None:
                    hist["pe_pct"] = percentile(pe_values[:i + 1], point["pe"])
                if key == "dividend":
                    hist["dividend_yield"] = point.get("dividend_yield")
                    if point.get("dividend_yield") is not None:
                        hist["dividend_pct"] = percentile(dy_values[:max(1, len([x for x in rows[:i + 1] if x.get("dividend_yield") is not None]))], point["dividend_yield"])
                payload["history"].append(hist)
            print(f"{key}: {len(rows)} observations from {rows[0]['date']} to {latest['date']} ({source})")
        except Exception as exc:
            print(f"{key} valuation fetch failed: {exc}")
            previous = old_assets.get(key)
            if previous and previous.get("pe") is not None:
                retained = dict(previous)
                retained["status"] = "stale"
                retained["error"] = str(exc)[:240]
                payload["assets"][key] = retained
                payload["history"].extend([x for x in old_history if x.get("key") == key])
            else:
                payload["assets"][key] = {
                    "name": cfg["name"], "pe": None, "pe_pct": None,
                    "dividend_yield": None, "dividend_pct": None,
                    "date": None, "historyStart": None, "sampleCount": 0,
                    "source": None, "frequency": None, "status": "unavailable",
                    "error": str(exc)[:240],
                }

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {OUT}; history rows={len(payload['history'])}")

if __name__ == "__main__":
    main()
