import base64
import io
import math
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Lock

from al1s.trading_replies.render import reply

PERIODS = {
    "15分钟": (900, "1m", 60),
    "1小时": (3600, "1m", 60),
    "4小时": (14400, "5m", 300),
    "1天": (86400, "15m", 900),
    "7天": (604800, "1h", 3600),
}
BEIJING = timezone(timedelta(hours=8))


def parse_chart(arguments):
    if len(arguments) not in {1, 2}:
        raise ValueError("走势用法：币走势 BTC [15分钟/1小时/4小时/1天/7天]，默认1小时。")
    symbol = arguments[0].upper().removesuffix(":USDT").removesuffix("/USDT")
    period = arguments[1] if len(arguments) == 2 else "1小时"
    if symbol not in {"BTC", "ETH", "SOL"} or period not in PERIODS:
        raise ValueError("走势图支持BTC、ETH、SOL；范围为15分钟、1小时、4小时、1天、7天。")
    return symbol, period


def fetch_chart(exchange, arguments, market, *, now=None):
    symbol, period = parse_chart(arguments)
    now = now or datetime.now(UTC)
    seconds, interval, step = PERIODS[period]
    end = int(now.timestamp()) // step * step * 1000
    start = end - seconds * 1000
    method = exchange.fapiPublicGetKlines if market == "futures" else exchange.publicGetKlines
    rows = method(
        dict(symbol=symbol + "USDT", interval=interval, startTime=start, endTime=end - 1, limit=180)
    )
    expected = seconds // step
    if not isinstance(rows, list) or len(rows) != expected:
        raise ValueError("行情区间不完整或最新K线尚未就绪，请稍后重试。")
    times, prices = [], []
    try:
        for index, row in enumerate(rows):
            opened, closed, price = int(row[0]), int(row[6]), float(row[4])
            if (
                opened != start + index * step * 1000
                or closed != opened + step * 1000 - 1
                or not math.isfinite(price)
                or price <= 0
            ):
                raise ValueError
            times.append(datetime.fromtimestamp(closed / 1000, UTC))
            prices.append(price)
    except (ValueError, TypeError, IndexError, OverflowError) as error:
        raise ValueError("行情存在缺口或无效K线，请稍后重试。") from error
    return dict(
        symbol=symbol,
        market=market,
        period=period,
        interval=interval,
        times=times,
        prices=prices,
        collected_at=datetime.now(UTC),
    )


def render_chart(data) -> bytes:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    fonts = list((Path(__file__).parents[1] / "meme_workshop" / "assets").glob("*.otf"))
    if fonts:
        font_manager.fontManager.addfont(str(fonts[0]))
        family = font_manager.FontProperties(fname=str(fonts[0])).get_name()
    else:
        family = "DejaVu Sans"
    prices, times = data["prices"], data["times"]
    change = (prices[-1] / prices[0] - 1) * 100
    market = "USDT永续" if data["market"] == "futures" else "现货"
    color = "#149d79" if change >= 0 else "#dc5369"
    with plt.rc_context({"font.family": family, "axes.unicode_minus": False}):
        fig, ax = plt.subplots(figsize=(12, 6.8), dpi=100)
        try:
            fig.patch.set_facecolor("#f5f8fc")
            ax.set_facecolor("#ffffff")
            fig.subplots_adjust(left=0.09, right=0.96, top=0.77, bottom=0.23)
            fig.text(
                0.08,
                0.92,
                f"{data['symbol']}/USDT · {market} · 最近{data['period']}",
                fontsize=21,
                weight="bold",
                color="#17375d",
            )
            fig.text(
                0.08,
                0.85,
                f"{prices[-1]:,.6g} USDT     区间首尾 {change:+.2f}%",
                fontsize=17,
                color=color,
            )
            ax.plot(times, prices, color=color, linewidth=2.4)
            ax.scatter(times[-1:], prices[-1:], color=color, s=30, zorder=3)
            ax.grid(axis="y", color="#e6edf5", linewidth=0.8)
            ax.spines[["top", "right"]].set_visible(False)
            ax.spines[["left", "bottom"]].set_color("#d8e2ef")
            ax.tick_params(colors="#56708c", labelsize=10)
            ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=7))
            ax.xaxis.set_major_formatter(
                mdates.DateFormatter(
                    "%m-%d %H:%M" if data["period"] in {"1天", "7天"} else "%H:%M", tz=BEIJING
                )
            )
            ax.ticklabel_format(axis="y", style="plain", useOffset=False)
            ax.set_ylabel("USDT", color="#56708c")
            first, last = (value.astimezone(BEIJING) for value in (times[0], times[-1]))
            collected = data["collected_at"].astimezone(BEIJING)
            fig.text(
                0.08,
                0.12,
                f"收盘点区间：{first:%m-%d %H:%M} — {last:%m-%d %H:%M}  "
                f"周期：{data['interval']} · 北京时间",
                fontsize=11,
                color="#56708c",
            )
            fig.text(
                0.08,
                0.07,
                f"来源：Binance公开K线 · 仅完整K线收盘价  |  采集：{collected:%m-%d %H:%M:%S}",
                fontsize=10,
                color="#7489a1",
            )
            output = io.BytesIO()
            fig.savefig(output, format="png", facecolor=fig.get_facecolor())
            body = output.getvalue()
            if len(body) > 524288:
                raise ValueError("走势图图片过大，请稍后重试。")
            return body
        finally:
            plt.close(fig)


class Charts:
    def __init__(self):
        self.lock = Lock()
        self.exchange = None

    def execute_for_ai(self, arguments, market):
        if market not in {"spot", "futures"}:
            raise ValueError("ai_chart_market_invalid")
        if not self.lock.acquire(blocking=False):
            return {"status": "busy", "code": "chart_busy"}
        try:
            if self.exchange is None:
                import ccxt

                self.exchange = ccxt.binance({"enableRateLimit": True, "timeout": 8000})
            data = fetch_chart(self.exchange, arguments, market)
            body = render_chart(data)
            return {"status": "success", "source": "Binance", "market": market,
                    "symbol": data["symbol"], "period": data["period"], "interval": data["interval"],
                    "last_close": str(Decimal(str(data["prices"][-1]))),
                    "last_bar_time": data["times"][-1].isoformat(),
                    "collected_at": data["collected_at"].isoformat(),
                    "image": base64.b64encode(body).decode("ascii")}
        except (OSError, ValueError):
            return {"status": "failed", "code": "chart_unavailable"}
        finally:
            self.lock.release()

    def execute(self, command):
        if not self.lock.acquire(blocking=False):
            return {"content": "走势图正在生成，请稍后重试。"}
        try:
            if command.market == "futures" and not command.settings.futures_enabled:
                return {"content": "合约模拟当前未启用。"}
            if not 0 <= datetime.now(UTC).timestamp() - command.event_timestamp <= 120:
                return {"content": "消息已过期，请重新查询走势。"}
            if self.exchange is None:
                import ccxt

                self.exchange = ccxt.binance({"enableRateLimit": True, "timeout": 8000})
            data = fetch_chart(self.exchange, command.arguments, command.market)
            body = render_chart(data)
            label = "合约" if command.market == "futures" else "现货"
            return {
                "content": reply(command, "main", dict(symbol=data["symbol"],
                    pair=data["symbol"]+"/USDT"+(":USDT" if command.market == "futures" else ""),
                    market_label=label, period=data["period"], interval=data["interval"],
                    range_start=data["times"][0].astimezone(BEIJING).strftime("%m-%d %H:%M"),
                    range_end=data["times"][-1].astimezone(BEIJING).strftime("%m-%d %H:%M"),
                    latest_close=Decimal(str(data["prices"][-1])),
                    change_percent=Decimal(str((data["prices"][-1]/data["prices"][0]-1)*100)).quantize(Decimal("0.01")),
                    latest_bar_time=data["times"][-1].astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S"),
                    collected_at=data["collected_at"].astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S"), provider="Binance"),
                    engine="freqtrade"),
                "image": base64.b64encode(body).decode("ascii"),
            }
        except ValueError as error:
            return {"content": str(error)}
        except Exception:
            return {"content": "Binance行情暂不可用，未生成走势图，请稍后重试。"}
        finally:
            self.lock.release()
