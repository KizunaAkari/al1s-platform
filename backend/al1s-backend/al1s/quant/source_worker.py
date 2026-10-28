"""Single-request AKShare worker with fixed destinations and bounded responses."""

import contextlib
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta
from importlib.metadata import version
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

MAX_RESPONSE = 4 * 1024 * 1024
ALLOWED_HOSTS = {
    "quotes.sina.cn",
    "finance.sina.com.cn",
    "push2his.eastmoney.com",
    "webapi.cninfo.com.cn",
    "ifzq.gtimg.cn",
    "proxy.finance.qq.com",
    "api.tushare.pro",
    "api.gugudata.com",
}

EXTRA_PROVIDERS = {"tencent", "tushare", "gugudata"}


def configure_proxy(session, kind: str, proxy: str | None) -> None:
    if kind in {"minute_east", "daily"}:
        if not proxy:
            raise ValueError("quant_source_unavailable")
        session.proxies["https"] = proxy


def install_sina_guard(module) -> None:
    def blocked_daily(*_args, **_kwargs):
        raise ValueError("unsafe_adjustment_blocked")

    module.stock_zh_a_daily = blocked_daily


def validate_request(value: object) -> dict:
    extra_kinds = {
        f"{frequency}_{provider}"
        for frequency in ("minute", "daily")
        for provider in EXTRA_PROVIDERS
    }
    if not isinstance(value, dict) or value.get("kind") not in extra_kinds | {
        "minute",
        "minute_east",
        "daily",
        "daily_sina",
        "calendar",
        "corporate",
    }:
        raise ValueError("quant_source_request_invalid")
    kind = value["kind"]
    expected = (
        {"kind", "start", "end"}
        if kind == "calendar"
        else {"kind", "market", "code"}
        if kind in {"minute", "minute_east", "corporate"} or kind.startswith("minute_")
        else {"kind", "market", "code", "start", "end", "adjustment"}
    )
    if kind in {f"minute_{provider}" for provider in EXTRA_PROVIDERS} and (
        "start" in value or "end" in value
    ):
        expected |= {"start", "end"}
    if set(value) != expected:
        raise ValueError("quant_source_request_invalid")
    if kind != "calendar":
        if (
            value.get("market") not in {"sh", "sz", "bj"}
            or not isinstance(value.get("code"), str)
            or not re.fullmatch(r"[0-9]{6}", value["code"])
        ):
            raise ValueError("quant_source_request_invalid")
        prefixes = {"sh": ("6",), "sz": ("0", "3"), "bj": ("4", "8", "92")}
        if not value["code"].startswith(prefixes[value["market"]]):
            raise ValueError("quant_source_request_invalid")
    if (
        kind in {"daily", "daily_sina", "calendar"}
        or kind.startswith("daily_")
        or (kind.startswith("minute_") and "start" in value)
    ):
        try:
            start = datetime.strptime(value["start"], "%Y%m%d").date()
            end = datetime.strptime(value["end"], "%Y%m%d").date()
        except (ValueError, TypeError) as error:
            raise ValueError("quant_source_request_invalid") from error
        if (
            start > end
            or (end - start).days > 10000
            or (kind == "daily" and value["adjustment"] not in {"", "qfq", "hfq"})
            or (kind == "daily_sina" and value["adjustment"] != "")
            or (kind.startswith("daily_") and value["adjustment"] not in {"", "qfq", "hfq"})
            or (kind == "daily_tushare" and value["adjustment"] != "")
        ):
            raise ValueError("quant_source_request_invalid")
    return value


def limited_request(session, method: str, url: str, **kwargs):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_HOSTS
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
    ):
        raise ValueError("quant_source_destination_invalid")
    params = kwargs.get("params")
    response = session.request(
        method,
        url,
        params=params,
        headers=kwargs.get("headers"),
        json=kwargs.get("json"),
        timeout=(3, 8),
        allow_redirects=False,
        stream=True,
    )
    try:
        response.raise_for_status()
        if 300 <= response.status_code < 400:
            raise ValueError("quant_source_redirect_blocked")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE:
                raise ValueError("quant_source_response_too_large")
        response._content = bytes(body)
        response._content_consumed = True
        return response
    finally:
        response.close()


def fetch_extra(session, value: dict) -> list[dict]:
    from al1s.quant.extra_sources import parse_gugudata, parse_tencent, parse_tushare

    kind = value["kind"]
    frequency, provider = kind.split("_", 1)
    market, code = value["market"], value["code"]
    symbol = market + code
    adjustment = value.get("adjustment", "")
    if provider == "tencent":
        if market == "bj":
            raise ValueError("quant_source_unavailable")
        if frequency == "minute":
            url = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
            param = f"{symbol},m1,,640"
        else:
            url = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
            start = datetime.strptime(value["start"], "%Y%m%d").date().isoformat()
            end = datetime.strptime(value["end"], "%Y%m%d").date().isoformat()
            param = f"{symbol},day,{start},{end},640,{adjustment}"
        payload = limited_request(session, "GET", url, params={"param": param}).json()
        rows = parse_tencent(payload, symbol, "1m" if frequency == "minute" else "1d", adjustment)
    else:
        secret = os.environ.get("AL1S_QUANT_SOURCE_KEY", "")
        if not secret:
            raise ValueError("quant_source_unavailable")
        if provider == "tushare":
            ts_code = f"{code}.{market.upper()}"
            history = frequency == "minute" and "start" in value
            api_name = (
                ("stk_mins" if history else "rt_min_daily") if frequency == "minute" else "daily"
            )
            params = {"ts_code": ts_code}
            if frequency == "minute":
                params["freq"] = "1min" if history else "1MIN"
                if history:
                    params.update(
                        start_date=datetime.strptime(value["start"], "%Y%m%d").strftime(
                            "%Y-%m-%d 00:00:00"
                        ),
                        end_date=datetime.strptime(value["end"], "%Y%m%d").strftime(
                            "%Y-%m-%d 23:59:59"
                        ),
                    )
            else:
                params.update(start_date=value["start"], end_date=value["end"])
            response = limited_request(
                session,
                "POST",
                "https://api.tushare.pro",
                json={"api_name": api_name, "token": secret, "params": params, "fields": ""},
            )
            rows = parse_tushare(response.json(), ts_code, "1m" if frequency == "minute" else "1d")
        else:
            now = datetime.now(ZoneInfo("Asia/Shanghai"))
            if frequency == "minute":
                url = "https://api.gugudata.com/stock/cn/stockcnperminute"
                if "start" in value:
                    begin = datetime.strptime(value["start"], "%Y%m%d")
                    end = datetime.strptime(value["end"], "%Y%m%d")
                    if (end - begin).days > 6:
                        raise ValueError("quant_source_unavailable")
                    begin_date, end_date = (
                        begin.strftime("%Y%m%d 00:00:00"),
                        end.strftime("%Y%m%d 23:59:59"),
                    )
                else:
                    begin_date, end_date = (
                        (now - timedelta(days=6)).strftime("%Y%m%d 00:00:00"),
                        now.strftime("%Y%m%d %H:%M:%S"),
                    )
                params = {
                    "symbol": code,
                    "beginDate": begin_date,
                    "endDate": end_date,
                    "period": 1,
                    "adjust": "",
                }
            else:
                url = "https://api.gugudata.com/stock/cn"
                params = {
                    "symbol": code,
                    "begindate": value["start"],
                    "enddate": value["end"],
                    "period": "daily",
                    "adjust": {"": "", "qfq": "pre", "hfq": "after"}[adjustment],
                }
            response = limited_request(
                session, "GET", url, params=params, headers={"X-GUGUDATA-APPKEY": secret}
            )
            rows = parse_gugudata(response.json(), code, "1m" if frequency == "minute" else "1d")
    if frequency == "daily":
        start, end = value["start"], value["end"]
        rows = [row for row in rows if start <= row["day"][:10].replace("-", "") <= end]
    return rows


def fetch(value: dict) -> list[dict[str, str]]:
    import akshare
    import akshare.stock.stock_zh_a_sina as sina
    import requests

    if version("akshare") != "1.19.3":
        raise ValueError("quant_source_version_unsupported")
    value = validate_request(value)
    with requests.Session() as session:
        session.trust_env = False
        configure_proxy(session, value["kind"], os.environ.get("AL1S_QUANT_EASTMONEY_PROXY"))
        if value["kind"] in {
            f"{frequency}_{provider}"
            for frequency in ("minute", "daily")
            for provider in EXTRA_PROVIDERS
        }:
            return fetch_extra(session, value)
        requests.get = lambda url, **kwargs: limited_request(session, "GET", url, **kwargs)
        requests.post = lambda url, **kwargs: limited_request(session, "POST", url, **kwargs)
        install_sina_guard(sina)
        with contextlib.redirect_stdout(io.StringIO()):
            if value["kind"] == "minute":
                frame = akshare.stock_zh_a_minute(
                    symbol=value["market"] + value["code"], period="1", adjust=""
                )
            elif value["kind"] == "minute_east":
                frame = akshare.stock_zh_a_hist_min_em(symbol=value["code"], period="1", adjust="")
            elif value["kind"] == "daily_sina":
                from py_mini_racer import py_mini_racer

                response = requests.get(
                    sina.zh_sina_a_stock_hist_url.format(value["market"] + value["code"])
                )
                payload = response.text.split("=", 1)[1].split(";", 1)[0].replace('"', "")
                if not re.fullmatch(r"[A-Za-z0-9+/=_-]+", payload):
                    raise ValueError("quant_source_invalid")
                decoder = py_mini_racer.MiniRacer()
                decoder.eval(sina.hk_js_decode)
                data = decoder.call("d", payload)
                start = datetime.strptime(value["start"], "%Y%m%d").date().isoformat()
                end = datetime.strptime(value["end"], "%Y%m%d").date().isoformat()
                rows = [
                    {
                        "date": str(row["date"])[:10],
                        **{
                            key: str(row[key]) for key in ("open", "high", "low", "close", "volume")
                        },
                    }
                    for row in data
                    if start <= str(row["date"])[:10] <= end
                ]
                if len(rows) > 8000:
                    raise ValueError("quant_source_response_too_large")
                return rows
            elif value["kind"] == "corporate":
                frame = akshare.stock_dividend_cninfo(symbol=value["code"])
            elif value["kind"] == "daily":
                frame = akshare.stock_zh_a_hist(
                    symbol=value["code"],
                    period="daily",
                    start_date=value["start"],
                    end_date=value["end"],
                    adjust=value["adjustment"],
                    timeout=8,
                )
            else:
                frame = akshare.tool_trade_date_hist_sina()
                start = datetime.strptime(value["start"], "%Y%m%d").date()
                end = datetime.strptime(value["end"], "%Y%m%d").date()
                if (
                    frame.empty
                    or start < frame["trade_date"].min()
                    or end > frame["trade_date"].max()
                ):
                    raise ValueError("quant_calendar_coverage_missing")
                frame = frame[(frame["trade_date"] >= start) & (frame["trade_date"] <= end)]
        if len(frame) > 8000:
            raise ValueError("quant_source_response_too_large")
        return [
            {str(key): str(item) for key, item in row.items()} for row in frame.to_dict("records")
        ]


def run() -> None:
    try:
        raw = sys.stdin.buffer.read(4097)
        if len(raw) > 4096:
            raise ValueError("quant_source_request_invalid")
        rows = fetch(json.loads(raw))
        encoded = json.dumps({"rows": rows}, ensure_ascii=False).encode()
        if len(encoded) > MAX_RESPONSE:
            raise ValueError("quant_source_response_too_large")
        sys.stdout.buffer.write(encoded)
    except Exception as error:
        sys.stdout.write(json.dumps({"error": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    run()
