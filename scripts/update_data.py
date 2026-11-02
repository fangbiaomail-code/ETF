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
    "sp500": {
        "name": "标普500", "kind": "us", "launch_date": "1957-03-04",
        "inception_date": "1928-01-03", "base_date": "1941–1943",
        "candidates": ["S&P 500", "S&P500", "SPX"],
    },
    "dividend": {
        "name": "中证红利低波 H30269", "kind": "csi", "code": "H30269",
        "launch_date": "2013-12-19", "inception_date": "2005-12-30",
        "base_date": "2005-12-30",
    },
    "nasdaq": {
        "name": "纳斯达克100", "kind": "us", "launch_date": "1985-01-31",
        "inception_date": "1985-01-31", "base_date": "1985-01-31",
        "candidates": ["NASDAQ 100", "NASDAQ-100", "NDX"],
    },
    "consumer": {
        "name": "中证主要消费 000932", "kind": "csi", "code": "000932",
        "launch_date": "2009-07-03", "inception_date": "2004-12-31",
        "base_date": "2004-12-31",
    },
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
        match = __import__("re").search(r"-?\d+(?:\.\d+)?", cleaned)
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

def merge_fields(preferred_rows, supplemental_rows, field):
    merged = {x["date"]: dict(x) for x in preferred_rows if x.get("date")}
    for point in supplemental_rows:
        dt = point.get("date")
        if not dt:
            continue
        current = merged.setdefault(dt, {"date": dt, "pe": None, "dividend_yield": None})
        if point.get(field) is not None:
            current[field] = point[field]
    return [merged[k] for k in sorted(merged)]

def funddb_rows(code, indicator):
    """Fetch long-run index valuation history from FundDB via AKShare (if available)."""
    import akshare as ak
    if not hasattr(ak, "index_value_name_funddb") or not hasattr(ak, "index_value_hist_funddb"):
        raise RuntimeError("当前 AKShare 版本不含 FundDB 历史估值接口")
    names = ak.index_value_name_funddb()
    if names is None or names.empty:
        raise RuntimeError("FundDB 指数名称目录为空")
    name_col = next((c for c in names.columns if "指数名称" in str(c) or str(c).strip() in ("名称", "指数")), None)
    if name_col is None:
        raise RuntimeError(f"FundDB 指数目录字段无法识别：{list(names.columns)}")
    tokens = ("红利低波", "红利低波动") if code == "H30269" else ("主要消费",)
    matched = []
    for _, row in names.iterrows():
        label = str(row[name_col]).strip()
        if any(token in label for token in tokens):
            matched.append(label)
    if not matched:
        raise RuntimeError(f"FundDB 指数目录中找不到 {code} 对应指数，目录字段={list(names.columns)}")
    symbol = sorted(matched, key=lambda x: (len(x), x))[0]
    frame = ak.index_value_hist_funddb(symbol=symbol, indicator=indicator)
    if frame is None or frame.empty:
        raise RuntimeError(f"FundDB {symbol} {indicator} 历史数据为空")
    date_col = next((c for c in frame.columns if "日期" in str(c) or str(c).lower() == "date"), None)
    value_col = next((c for c in frame.columns if indicator in str(c)), None)
    if value_col is None:
        aliases = ("PE", "P/E") if indicator == "市盈率" else ("DY", "股息率")
        value_col = next((c for c in frame.columns if any(a in str(c).upper() for a in aliases)), None)
    if date_col is None or value_col is None:
        raise RuntimeError(f"FundDB {symbol} 字段无法识别：{list(frame.columns)}")
    rows = []
    for _, row in frame.iterrows():
        dt, val = date_string(row[date_col]), number(row[value_col])
        if not dt or val is None:
            continue
        if indicator == "市盈率":
            if val <= 0:
                continue
            rows.append({"date": dt, "pe": val, "dividend_yield": None})
        else:
            if val < 0:
                continue
            # FundDB expresses dividend yield in percentage points (e.g. 4.7).
            dy = val / 100 if val > 1 else val
            rows.append({"date": dt, "pe": None, "dividend_yield": dy})
    rows = sorted({x["date"]: x for x in rows}.values(), key=lambda x: x["date"])
    if len(rows) < 30:
        raise RuntimeError(f"FundDB {symbol} {indicator} 有效历史样本不足：{len(rows)}")
    dates = pd.to_datetime([x["date"] for x in rows])
    median_days = dates.to_series().diff().dt.days.median()
    frequency = "月末" if median_days and median_days > 20 else ("周频" if median_days and median_days > 5 else "日频")
    print(f"FundDB {symbol} {indicator}: {len(rows)} samples from {rows[0]['date']} to {rows[-1]['date']}")
    return rows, f"FundDB 指数估值历史（{symbol}，{indicator}）", frequency

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

def trendonify_nasdaq_rows():
    """Monthly Nasdaq-100 PE history; public series currently begins in 1990."""
    urls = [
        "https://api.trendonify.com/united-states/stock-market/nasdaq-100/pe-ratio",
        "https://trendonify.com/united-states/stock-market/nasdaq-100/pe-ratio",
    ]
    last_error = None
    response = None
    for url in urls:
        try:
            candidate = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            candidate.raise_for_status()
            response = candidate
            break
        except Exception as exc:
            last_error = exc
    if response is None:
        raise RuntimeError(f"Trendonify history endpoints unavailable: {last_error}")
    tables = pd.read_html(StringIO(response.text))
    for frame in tables:
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = [" ".join(str(y) for y in x if str(y) != "nan") for x in frame.columns]
        cols = {str(c).strip().lower(): c for c in frame.columns}
        date_col = next((v for k, v in cols.items() if "date" in k), None)
        pe_col = next((v for k, v in cols.items() if "pe" in k and "ratio" in k and "forward" not in k), None)
        if date_col is None or pe_col is None:
            continue
        rows = []
        for _, row in frame.iterrows():
            dt, pe = date_string(row[date_col]), number(row[pe_col])
            if dt and pe is not None and pe > 0:
                rows.append({"date": dt, "pe": pe, "dividend_yield": None})
        rows = sorted({x["date"]: x for x in rows}.values(), key=lambda x: x["date"])
        if len(rows) >= 30:
            return rows, "Trendonify：纳斯达克100公开月度 PE 历史序列（最早可用数据约始于1990年；非 Nasdaq 官方估值文件）", "月末"
    raise RuntimeError("Trendonify 未返回足够的纳斯达克100历史 PE 样本")

def filter_from_inception(rows, inception_date, frequency):
    """Use the full index-history period from its base/first-value date."""
    if frequency in ("月末", "月频", "周频"):
        start_month = inception_date[:7]
        return [x for x in rows if x.get("date", "")[:7] >= start_month]
    return [x for x in rows if x.get("date", "") >= inception_date]

def months_between(start, end):
    try:
        sy, sm = (int(x) for x in start[:7].split("-"))
        ey, em = (int(x) for x in end[:7].split("-"))
        return max(0, (ey - sy) * 12 + em - sm)
    except Exception:
        return None

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
    # Rebuild PE from the selected provider on every run. Do not blend old
    # PE observations from a previous provider/methodology. Dividend-yield
    # history is already merged into CSI rows inside csi_rows().
    merged = {}
    for point in new_rows:
        dt = point["date"]
        current = merged.setdefault(dt, {"key": key, "date": dt})
        current["key"] = key
        current["date"] = dt
        for field in ("pe", "dividend_yield"):
            if point.get(field) is not None:
                current[field] = point[field]
    return [merged[k] for k in sorted(merged)]

def main():
    now = datetime.now(timezone.utc)
    old = load_old()
    old_assets = old.get("assets", {})
    old_history = old.get("history", [])
    payload = {
        "updatedAt": now.strftime("%Y-%m-%d %H:%M UTC"),
        "provider": "AKShare + CSI official data + public index valuation datasets",
        "notice": "PE 历史序列从指数基日或首个指数值日开始；指数基日早于正式发布日期的区间属于回溯历史，页面单独显示正式发布日期。历史百分位使用该起点以来全部可取得的有效 PE 样本。若数据源起点晚于指数历史起点，会明确显示缺口，不补造数据。PE 百分位至少需要 30 个有效样本。股息率只使用可核验的历史观测。",
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
                csi_fallback, csi_source, csi_frequency = csi_rows(cfg["code"], old_history)
                try:
                    funddb_pe, funddb_pe_source, funddb_frequency = funddb_rows(cfg["code"], "市盈率")
                    new_rows = merge_fields(funddb_pe, csi_fallback, "dividend_yield")
                    source = funddb_pe_source + "；近期股息率补充自中证官方估值文件"
                    frequency = funddb_frequency
                except Exception as funddb_pe_error:
                    print(f"FundDB PE history unavailable for {cfg['code']}: {funddb_pe_error}")
                    new_rows, source, frequency = csi_fallback, csi_source, csi_frequency
                if key == "dividend":
                    try:
                        funddb_dy, funddb_dy_source, _ = funddb_rows(cfg["code"], "股息率")
                        new_rows = merge_fields(new_rows, funddb_dy, "dividend_yield")
                        source += "；股息率历史来自 " + funddb_dy_source
                    except Exception as funddb_dy_error:
                        print(f"FundDB dividend-yield history unavailable: {funddb_dy_error}")
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
            elif key == "nasdaq":
                try:
                    new_rows, source, frequency = trendonify_nasdaq_rows()
                except Exception as exc:
                    print(f"Trendonify Nasdaq history failed: {exc}")
                    new_rows, source, frequency = siblis_rows("NDX")
            else:
                raise RuntimeError(f"未配置估值数据源：{key}")

            # Keep the complete index-history period from its base/first-value date.
            new_rows = filter_from_inception(new_rows, cfg["inception_date"], frequency)
            if len(new_rows) < 2:
                raise RuntimeError(f"{cfg['name']} 指数历史起点之后的 PE 样本不足")
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

            pe_start = pe_points[0]["date"] if pe_points else None
            inception_gap_months = months_between(cfg["inception_date"], pe_start) if pe_start else None
            payload["assets"][key] = {
                "name": cfg["name"],
                "inceptionDate": cfg["inception_date"],
                "launchDate": cfg["launch_date"],
                "baseDate": cfg["base_date"],
                "pe": current_pe,
                "pe_pct": pe_pct,
                "dividend_yield": current_dy,
                "dividend_pct": dy_pct,
                "date": latest_pe["date"] if latest_pe else (latest_dy["date"] if latest_dy else None),
                "peDate": latest_pe["date"] if latest_pe else None,
                "yieldDate": latest_dy["date"] if latest_dy else None,
                "historyStart": pe_start,
                "yieldHistoryStart": dy_points[0]["date"] if dy_points else None,
                "inceptionGapMonths": inception_gap_months,
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
                    "name": cfg["name"], "inceptionDate": cfg["inception_date"], "launchDate": cfg["launch_date"], "baseDate": cfg["base_date"],
                    "pe": None, "pe_pct": None, "dividend_yield": None, "dividend_pct": None,
                    "date": None, "peDate": None, "yieldDate": None,
                    "historyStart": None, "yieldHistoryStart": None, "inceptionGapMonths": None,
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
