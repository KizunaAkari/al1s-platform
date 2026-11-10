import html
import http.client
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

CHINA = timezone(timedelta(hours=8))
MAX_BODY = 262144


def _get(host: str, path: str) -> bytes:
    connection = http.client.HTTPSConnection(host, timeout=6)
    try:
        connection.request("GET", path, headers={"User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn/"})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError("market_source_unavailable")
        body = response.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            raise ValueError("market_response_too_large")
        return body
    except (http.client.HTTPException, OSError) as error:
        raise ValueError("market_source_unavailable") from error
    finally:
        connection.close()


def _result(symbol, name, unit, price, time, source, now):
    try:
        value = Decimal(str(price))
        if not value.is_finite() or value <= 0:
            raise ValueError
        observed = datetime.strptime(time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CHINA)
        if observed > now + timedelta(minutes=2):
            raise ValueError
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("market_quote_invalid") from error
    return {"status": "success", "symbol": symbol, "name": name, "unit": unit,
            "price": str(value), "source": source, "price_type": "来源最新报价",
            "quote_time": observed.isoformat(), "collected_at": now.isoformat(),
            "stale": now - observed > timedelta(minutes=15)}


def _ashare(symbol: str, now):
    value = symbol.strip().lower()
    match = re.fullmatch(r"(sh|sz|bj)?([0-9]{6})", value)
    if not match:
        raise ValueError("market_symbol_invalid")
    prefix, code = match.groups()
    expected = "sh" if code.startswith("6") else "sz" if code.startswith(("0", "3")) else \
        "bj" if code.startswith(("4", "8", "92")) else None
    if expected is None or (prefix and prefix != expected):
        raise ValueError("market_symbol_invalid")
    identity = expected + code
    path = "/appstock/app/kline/mkline?" + urlencode({"param": identity + ",m1,,1"})
    data = json.loads(_get("ifzq.gtimg.cn", path))
    row = data["data"][identity]["qt"][identity]
    if not isinstance(row, list) or len(row) <= 30 or row[2] != code:
        raise ValueError("market_quote_invalid")
    try:
        timestamp = datetime.strptime(row[30], "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError) as error:
        raise ValueError("market_quote_invalid") from error
    return _result(identity, row[1], "CNY", row[3], timestamp, "腾讯", now)


def public_quote(market: str, symbol: str, *, now=None):
    now = (now or datetime.now(CHINA)).astimezone(CHINA)
    if market == "ashare":
        try:
            return _ashare(symbol, now)
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError("market_quote_invalid") from error
    normalized = symbol.strip().upper().replace("/", "")
    choices = {"gold": ("XAUUSD", "hf_XAU"), "forex": ("USDJPY", "fx_susdjpy")}
    if market not in choices or normalized != choices[market][0]:
        raise ValueError("market_symbol_invalid")
    name = choices[market][1]
    raw = _get("hq.sinajs.cn", "/?list=" + name).decode("gb18030")
    match = re.fullmatch(r'\s*var hq_str_' + name + r'="([^"]*)";\s*', raw)
    row = match.group(1).split(",") if match else []
    try:
        if market == "gold" and len(row) >= 14 and row[13].startswith("伦敦金"):
            return _result("XAU/USD", "伦敦现货黄金", "USD/盎司", row[0],
                           row[12] + " " + row[6], "新浪财经", now)
        if market == "forex" and len(row) >= 18 and row[9].startswith("美元兑日元"):
            return _result("USD/JPY", "美元兑日元", "日元/1美元", row[1],
                           row[17] + " " + row[0], "新浪财经", now)
    except (IndexError, TypeError) as error:
        raise ValueError("market_quote_invalid") from error
    raise ValueError("market_quote_invalid")


def search_feeds(query: str, *, now=None):
    now = (now or datetime.now(CHINA)).astimezone(CHINA)
    match = re.fullmatch(r"\s*var\s+newest\s*=\s*(\[.*\])\s*;?\s*",
                         _get("www.jin10.com", "/flash_newest.js").decode("utf-8"), re.DOTALL)
    if not match:
        raise ValueError("feed_invalid")
    entries = json.loads(match.group(1))
    if not isinstance(entries, list):
        raise ValueError("feed_invalid")
    needle = query.strip().casefold()
    groups = (("btc", "比特币"), ("eth", "以太坊"), ("sol", "索拉纳"),
              ("xau", "xauusd", "xau/usd", "黄金"),
              ("usd/jpy", "usdjpy", "美元兑日元", "美元/日元"))
    terms = next((group for group in groups if needle in group), (needle,)) if needle else ()
    items = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != 0:
            continue
        data = entry.get("data")
        if not isinstance(data, dict) or data.get("lock"):
            continue
        text, identity = data.get("content"), entry.get("id")
        if (not isinstance(text, str) or not isinstance(identity, str)
                or not re.fullmatch(r"[0-9]{16,24}", identity)):
            continue
        text = html.unescape(re.sub(r"<[^>]*>", "", text)).strip()
        if not text or (terms and not any(term in text.casefold() for term in terms)):
            continue
        try:
            published = datetime.strptime(entry["time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CHINA)
        except (KeyError, TypeError, ValueError):
            continue
        if not now - timedelta(hours=48) <= published <= now + timedelta(minutes=2):
            continue
        items.append({"content": text[:250], "published_at": published.isoformat(),
                      "url": "https://flash.jin10.com/detail/" + identity})
    items.sort(key=lambda item: item["published_at"], reverse=True)
    return {"status": "success", "source": "金十数据", "query": query,
            "collected_at": now.isoformat(), "items": items[:5]}
