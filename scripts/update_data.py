"""Weekly ETF valuation collector with honest history coverage.

China PE history: CSI historical index-performance endpoint (rolling PE).
China dividend yield: CSI official indicator XLS; currently only recent observations,
which are accumulated locally over weekly runs and clearly date-stamped.
U.S. PE history: public TTM valuation feeds. No price-derived PE is fabricated.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from io import StringIO

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "data.json"
BASE_SIBLIS = "https://siblisresearch.supabase.co/functions/v1/free-data-api"
TIMEOUT = 35
MIN_PERCENTILE_SAMPLES = 30
HEADERS = {"User-Agent": "ETF-Valuation-Watch/1.1 (personal research dashboard)"}

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
        if value is None:
            return None
        cleaned = str(value).replace(",", "").replace("%", "").strip()
        match = __import__("re").search(r"-?\\d+(?:\\.\\d+)?", cleaned)
        if not match:
            return None
        result = float(match.group(0))
        return result if pd.notna(result) else None
    except Exception:
        return None

def date_string(value):
    try:
        if isinstance(value, (int, float)):
            if value > 10**12:
                return pd.to_datetime(value, unit="ms", errors="raise").strftime("%Y-%m-%d")
            if value > 10**9:
                return pd.to_datetime(value, unit="s", errors="raise").strftime("%Y-%m-%d")
        return pd.to_datetime(value, errors="raise").strftime("%Y-%m-%d")
    except Exception:
        return None

def percentile(values, current=None):
    clean_values = [number(x) for x in values]
    clean_values = [x for x in clean_values if x is not None]
    if len(clean_values) < MIN_PERCENTILE_SAMPLES:
        return None
    target = current if current is not None else clean_values[-1]
    return sum(x <= target for x in clean_values) / len(clean_values)

def csi_rows(code, old_history):
    import akshare as ak
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    merged = {}

    # Long-run daily PE history from CSI's index-performance endpoint.
    try:
        frame = ak.stock_zh_index_hist_csindex(
            symbol=code, start_date="20000101", end_date=today
        )
        if frame is not None and not frame.empty:
            date_col = next((c for c in frame.columns if "日期" in str(c)), None)
            pe_col = next((c for c in frame.columns if "滚动市盈率" in str(c)), None)
            if date_col and pe_col:
                for _, row in frame.iterrows():
                    dt, pe = date_string(row[date_col]), number(row[pe_col])
                    if dt:
                        merged[dt] = {"date": dt, "pe": pe if pe and pe > 0 else None, "dividend_yield": None}
    except Exception as exc:
        print(f"CSI long PE history {code} unavailable: {exc}")

    # Official indicator XLS currently contains a recent rolling window.
    # Merge it with saved observations so the yield history accumulates over time.
    try:
        recent = ak.stock_zh_index_value_csindex(symbol=code)
        if recent is not None and not recent.empty:
            date_col = next((c for c in recent.columns if "日期" in str(c)), None)
            pe_col = next((c for c in recent.columns if str(c).strip() == "市盈率2"), None)
            dy_col = next((c for c in recent.columns if str(c).strip() == "股息率2"), None)
            for _, row in recent.iterrows():
                dt = date_string(row[date_col]) if date_col else None
                if not dt:
                    continue
                current = merged.setdefault(dt, {"date": dt, "pe": None, "dividend_yield": None})
                pe = number(row[pe_col]) if pe_col else None
                raw_dy = number(row[dy_col]) if dy_col and code == "H30269" else None
                if pe and pe > 0:
                    current["pe"] = pe
                if raw_dy is not None:
                    current["dividend_yield"] = raw_dy / 100
    except Exception as exc:
        print(f"CSI recent indicator file {code} unavailable: {exc}")

    # Keep previously collected dividend-yield observations; CSI's XLS is a rolling window.
    if code == "H30269":
        for point in old_history:
            if point.get("key") != "dividend" or not point.get("date"):
                continue
            current = merged.setdefault(point["date"], {"date": point["date"], "pe": None, "dividend_yield": None})
            if current.get("dividend_yield") is None and point.get("dividend_yield") is not None:
                current["dividend_yield"] = point["dividend_yield"]

    rows = sorted(merged.values(), key=lambda x: x["date"])
    pe_count = sum(x.get("pe") is not None for x in rows)
    if pe_count < 2:
        raise RuntimeError(f"中证指数历史 PE 样本不足：{code}，仅 {pe_count} 条")
    source = "中证指数有限公司：历史行情滚动 PE；股息率来自官方估值 XLS（目前仅提供近期窗口）"
    return rows, source, "日频"

def siblis_indices():
    payload = get_json(f"{BASE_SIBLIS}/v1/indices")
    return payload.get("indices", [])

def pick_siblis_ticker(index_list, key):
    for item in index_list:
        name = str(item.get("index_name", "")).lower().replace("–", "-")
        ticker = str(item.get("ticker", "")).upper()
        if key == "sp500" and ("s&p 500" in name or "s&p500" in name or ticker in ("SPX", "SP500")):
            return ticker
        if key == "nasdaq" and ("nasdaq 100" in name or "nasdaq-100" in name or ticker == "NDX"):
            return ticker
    available = {str(x.get("ticker", "")).upper() for x in index_list}
    for candidate in ASSETS[key]["candidates"]:
        if candidate.upper() in available:
            return candidate.upper()
    return None

def siblis_rows(ticker):
    payload = get_json(f"{BASE_SIBLIS}/v1/{ticker}/pe-trailing")
    rows = []
    for item in payload.get("data", []):
        dt = date_string(item.get("trading_day (EOD)") or item.get("date") or item.get("trading_day"))
        value = number(item.get("value"))
        if dt and value is not None and value > 0:
            rows.append({"date": dt, "pe": value, "dividend_yield": None})
    rows.sort(key=lambda x: x["date"])
    if len(rows) < 2:
        raise RuntimeError(f"Siblis {ticker} 的 TTM PE 历史样本不足")
    return rows, "Siblis Research 免费历史估值接口（非商业使用；月末样本）", "月末"

def parse_valuation_payload(payload):
    found = []
    date_keys = ("date", "d", "week", "week_ending", "weekending", "trading_day",
                 "observation_date", "period", "as_of", "asof", "timestamp", "x")
    pe_tokens = ("ttm_pe", "pe_ttm", "trailing_pe", "trailingpe", "pe_trailing", "ttmpe")
    def add_row(row, path=""):
        if not isinstance(row, dict):
            return
        dt = None
        for k in date_keys:
            if row.get(k) is not None:
                dt = date_string(row.get(k))
                if dt:
                    break
        if not dt:
            return
        selected = None
        for k, v in row.items():
            key = str(k).lower().replace("-", "_").replace(" ", "_")
            if any(token in key for token in pe_tokens) and "forward" not in key:
                selected = number(v)
                if selected is not None:
                    break
        if selected is None and any(token in path.lower() for token in ("trailing", "ttm")):
            for k in ("value", "y", "pe", "multiple"):
                if row.get(k) is not None:
                    selected = number(row[k])
                    if selected is not None:
                        break
        if selected is not None and selected > 0:
            found.append({"date": dt, "pe": selected, "dividend_yield": None})
    def walk(node, path=""):
        if isinstance(node, dict):
            # Handle columnar arrays: {"dates":[...], "trailing":[...]}.
            date_array = next((node[k] for k in date_keys if isinstance(node.get(k), list)), None)
            if date_array:
                for k, vals in node.items():
                    if isinstance(vals, list) and len(vals) == len(date_array) and any(t in str(k).lower() for t in ("ttm", "trailing")):
                        for dt, val in zip(date_array, vals):
                            ds, n = date_string(dt), number(val)
                            if ds and n is not None and n > 0:
                                found.append({"date": ds, "pe": n, "dividend_yield": None})
            for k, v in node.items():
                if isinstance(v, dict):
                    add_row(v, path + "/" + str(k))
                    walk(v, path + "/" + str(k))
                elif isinstance(v, list):
                    walk(v, path + "/" + str(k))
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, dict):
                    add_row(item, path)
                    walk(item, path)
                elif isinstance(item, (list, tuple)) and len(item) >= 2 and any(t in path.lower() for t in ("ttm", "trailing")):
                    ds, n = date_string(item[0]), number(item[1])
                    if ds and n is not None and n > 0:
                        found.append({"date": ds, "pe": n, "dividend_yield": None})
    walk(payload)
    unique = {x["date"]: x for x in found}
    return sorted(unique.values(), key=lambda x: x["date"])

def hom_sp500_rows():
    payload = get_json("https://historyofmarket.com/api/sp500/forward-pe.json")
    rows = parse_valuation_payload(payload)
    print(f"History of Market payload keys: {list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__}; extracted rows={len(rows)}")
    if len(rows) >= 2:
        return rows, "History of Market（CC BY 4.0；标普500 TTM PE）", "周频"
    raise RuntimeError("History of Market JSON 中未识别到可用的 S&P 500 TTM PE 序列")

def table_rows_from_url(url, source_name):
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    tables = pd.read_html(StringIO(response.text))
    for frame in tables:
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = [" ".join(str(y) for y in x if str(y) != "nan") for x in frame.columns]
        cols = {str(c).strip().lower(): c for c in frame.columns}
        date_col = next((v for k, v in cols.items() if "date" in k or "month" in k), None)
        value_col = next((v for k, v in cols.items() if "value" in k or "pe" in k or "ratio" in k), None)
        if date_col is None or value_col is None:
            continue
        rows = []
        for _, row in frame.iterrows():
            dt, pe = date_string(row[date_col]), number(row[value_col])
            if dt and pe is not None and pe > 0:
                rows.append({"date": dt, "pe": pe, "dividend_yield": None})
        if len(rows) >= 2:
            return sorted({x["date"]: x for x in rows}.values(), key=lambda x: x["date"]), source_name, "月末"
    raise RuntimeError(f"{source_name} 页面未返回可识别的 PE 历史表")

def multpl_sp500_rows():
    return table_rows_from_url(
        "https://www.multpl.com/s-p-500-pe-ratio/table/by-month",
        "Multpl（标普500月度 trailing PE）"
    )

def dqydj_sp500_rows():
    return table_rows_from_url(
        "https://dqydj.com/sp-500-pe-ratio/",
        "DQYDJ（标普500月度 TTM PE）"
    )

def load_old():
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:
        return {}

def merge_history(key, new_rows, old_history):
    merged = {}
    for point in old_history:
        if point.get("key") == key and point.get("date"):
            merged[point["date"]] = dict(point)
    for point in new_rows:
        dt = point["date"]
        old = merged.setdefault(dt, {"key": key, "date": dt})
        old["key"] = key
        old["date"] = dt
        for field in ("pe", "dividend_yield"):
            if point.get(field) is not None:
                old[field] = point[field]
    return [merged[k] for k in sorted(merged)]

def main():
    now = datetime.now(timezone.utc)
    old = load_old()
    old_assets = old.get("assets", {})
    old_history = old.get("history", [])
    payload = {
        "updatedAt": now.strftime("%Y-%m-%d %H:%M UTC"),
        "provider": "AKShare + CSI official data + public index valuation datasets",
        "notice": "PE-TTM 百分位仅在至少 30 个有效历史样本时计算；红利低波股息率官方文件目前只提供近期窗口，本网站会逐周积累，不伪造上市以来历史。",
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
                new_rows, source, frequency = csi_rows(cfg["code"], old_history)
            else:
                ticker = pick_siblis_ticker(index_list, key) if index_list else None
                if ticker:
                    try:
                        new_rows, source, frequency = siblis_rows(ticker)
                    except Exception as exc:
                        print(f"Siblis {key} ({ticker}) failed: {exc}")
                        if key != "sp500":
                            raise
                        new_rows, source, frequency = hom_sp500_rows()
                elif key == "sp500":
                    try:
                        new_rows, source, frequency = multpl_sp500_rows()
                    except Exception as exc:
                        print(f"Multpl S&P history failed: {exc}")
                        try:
                            new_rows, source, frequency = hom_sp500_rows()
                        except Exception as exc2:
                            print(f"History of Market S&P fallback failed: {exc2}")
                            new_rows, source, frequency = dqydj_sp500_rows()
                else:
                    raise RuntimeError("免费数据目录暂未提供纳斯达克100可核验的 TTM PE 历史序列")

            merged_history = merge_history(key, new_rows, old_history)
            pe_points = [x for x in merged_history if x.get("pe") is not None]
            dy_points = [x for x in merged_history if x.get("dividend_yield") is not None]
            latest_pe = pe_points[-1] if pe_points else None
            latest_dy = dy_points[-1] if dy_points and key == "dividend" else None
            current_pe = latest_pe.get("pe") if latest_pe else None
            current_dy = latest_dy.get("dividend_yield") if latest_dy else None
            pe_pct = percentile([x["pe"] for x in pe_points], current_pe) if current_pe is not None else None
            dy_pct = percentile([x["dividend_yield"] for x in dy_points], current_dy) if current_dy is not None else None

            # Keep daily observations for percentile calculations, but publish monthly PE
            # chart points to keep the static JSON small and fast to load.
            output_points = {}
            month_last = {}
            for point in pe_points:
                month_last[point["date"][:7]] = point
            for point in month_last.values():
                pe_seen = [x["pe"] for x in pe_points if x["date"] <= point["date"]]
                out = output_points.setdefault(point["date"], {"key": key, "date": point["date"]})
                out["pe"] = point["pe"]
                out["pe_pct"] = percentile(pe_seen, point["pe"])
            # Keep the daily official dividend-yield snapshots as they accumulate.
            if key == "dividend":
                dy_seen = []
                for point in dy_points:
                    dy_seen.append(point["dividend_yield"])
                    out = output_points.setdefault(point["date"], {"key": key, "date": point["date"]})
                    out["dividend_yield"] = point["dividend_yield"]
                    out["dividend_pct"] = percentile(dy_seen, point["dividend_yield"])
            payload["history"].extend(output_points[k] for k in sorted(output_points))

            payload["assets"][key] = {
                "name": cfg["name"],
                "pe": current_pe,
                "pe_pct": pe_pct,
                "dividend_yield": current_dy,
                "dividend_pct": dy_pct,
                "date": latest_pe["date"] if latest_pe else (latest_dy["date"] if latest_dy else None),
                "historyStart": pe_points[0]["date"] if pe_points else None,
                "yieldHistoryStart": dy_points[0]["date"] if dy_points else None,
                "sampleCount": len(pe_points),
                "yieldSampleCount": len(dy_points),
                "source": source,
                "frequency": frequency,
                "status": "ok" if current_pe is not None or current_dy is not None else "unavailable",
            }
            print(f"{key}: PE samples={len(pe_points)}; dividend-yield samples={len(dy_points)}; latest PE date={latest_pe['date'] if latest_pe else 'n/a'}; source={source}")
        except Exception as exc:
            print(f"{key} valuation fetch failed: {exc}")
            previous = old_assets.get(key)
            if previous and (previous.get("pe") is not None or previous.get("dividend_yield") is not None):
                retained = dict(previous)
                retained["status"] = "stale"
                retained["error"] = str(exc)[:240]
                payload["assets"][key] = retained
                payload["history"].extend([x for x in old_history if x.get("key") == key])
            else:
                payload["assets"][key] = {
                    "name": cfg["name"], "pe": None, "pe_pct": None,
                    "dividend_yield": None, "dividend_pct": None,
                    "date": None, "historyStart": None, "yieldHistoryStart": None,
                    "sampleCount": 0, "yieldSampleCount": 0,
                    "source": None, "frequency": None, "status": "unavailable",
                    "error": str(exc)[:240],
                }

    payload["history"].sort(key=lambda x: (x.get("key", ""), x.get("date", "")))
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {OUT}; history rows={len(payload['history'])}")

if __name__ == "__main__":
    main()
