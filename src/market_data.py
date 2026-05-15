"""市场数据模块

从Binance公开API获取行情数据，免费无需Key。
提供给Agent讨论时作为实时市场背景。
"""

import httpx
from datetime import datetime, timezone
from typing import Optional

from .config import MarketDataConfig


# 支持的交易对映射（用户输入 → Binance交易对）
SYMBOL_MAP = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "BNB": "BNBUSDT",
    "XRP": "XRPUSDT",
    "DOGE": "DOGEUSDT",
    "ADA": "ADAUSDT",
    "AVAX": "AVAXUSDT",
    "DOT": "DOTUSDT",
    "MATIC": "MATICUSDT",
    "LINK": "LINKUSDT",
    "UNI": "UNIUSDT",
    "ARB": "ARBUSDT",
    "OP": "OPUSDT",
    "SUI": "SUIUSDT",
    "APT": "APTUSDT",
    "PEPE": "PEPEUSDT",
    "WIF": "WIFUSDT",
    "ORDI": "ORDIUSDT",
}


class MarketDataClient:
    """Binance公开行情数据客户端"""

    def __init__(self, config: MarketDataConfig):
        self.base_url = config.base_url.rstrip("/")

    def _resolve_symbol(self, coin: str) -> str:
        """将用户输入的币种转换为Binance交易对"""
        coin = coin.upper().strip()
        if coin in SYMBOL_MAP:
            return SYMBOL_MAP[coin]
        # 如果用户直接输入了交易对
        if coin.endswith("USDT"):
            return coin
        # 默认拼接USDT
        return f"{coin}USDT"

    async def get_market_context(self, coin: str) -> str:
        """
        获取某个币种的完整市场上下文（供Agent讨论用）

        Returns:
            格式化的中文市场数据摘要
        """
        symbol = self._resolve_symbol(coin)

        try:
            ticker = await self._get_ticker_24h(symbol)
            klines_4h = await self._get_klines(symbol, "4h", limit=30)
            klines_1d = await self._get_klines(symbol, "1d", limit=14)
            funding = await self._get_funding_rate(symbol)
        except httpx.ConnectError:
            return (
                f"⚠️ 无法连接Binance API（{self.base_url}），请检查网络连接或API地址配置。\n"
                f"请基于你的经验和记忆进行分析。"
            )
        except httpx.TimeoutException:
            return (
                f"⚠️ 获取{coin}行情数据超时，Binance API响应过慢。\n"
                f"请基于你的经验和记忆进行分析。"
            )
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 400:
                return (
                    f"⚠️ 交易对 {symbol} 不存在或已下架，请确认币种名称是否正确。\n"
                    f"支持的币种示例: BTC, ETH, SOL, BNB, ARB, OP 等。\n"
                    f"请基于你的经验和记忆进行分析。"
                )
            elif status == 418 or status == 403:
                return (
                    f"⚠️ Binance API访问被限制（状态码{status}），可能是IP被临时封禁。\n"
                    f"建议稍后再试，或检查是否需要更换API地址。\n"
                    f"请基于你的经验和记忆进行分析。"
                )
            elif status == 429:
                return (
                    f"⚠️ Binance API请求频率过高（限流），请稍后再试。\n"
                    f"请基于你的经验和记忆进行分析。"
                )
            else:
                return (
                    f"⚠️ 获取{coin}行情数据失败（状态码{status}）。\n"
                    f"请基于你的经验和记忆进行分析。"
                )
        except Exception as e:
            return f"⚠️ 获取{coin}行情数据失败: {e}\n请基于你的经验和记忆进行分析。"

        # 构建市场摘要
        context = self._format_context(coin, symbol, ticker, klines_4h, klines_1d, funding)
        return context

    async def _get_ticker_24h(self, symbol: str) -> dict:
        """24小时行情"""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{self.base_url}/api/v3/ticker/24hr",
                params={"symbol": symbol},
            )
            resp.raise_for_status()
            return resp.json()

    async def _get_klines(self, symbol: str, interval: str, limit: int = 30) -> list:
        """K线数据"""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{self.base_url}/api/v3/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit},
            )
            resp.raise_for_status()
            return resp.json()

    async def _get_funding_rate(self, symbol: str) -> Optional[dict]:
        """合约资金费率（使用合约API）"""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://fapi.binance.com/fapi/v1/fundingRate",
                    params={"symbol": symbol, "limit": 1},
                )
                resp.raise_for_status()
                data = resp.json()
                return data[0] if data else None
        except Exception:
            return None

    def _format_context(
        self, coin: str, symbol: str,
        ticker: dict, klines_4h: list, klines_1d: list,
        funding: Optional[dict],
    ) -> str:
        """格式化市场数据为中文摘要"""

        # 基本行情
        price = float(ticker.get("lastPrice", 0))
        change_pct = float(ticker.get("priceChangePercent", 0))
        high_24h = float(ticker.get("highPrice", 0))
        low_24h = float(ticker.get("lowPrice", 0))
        volume_usdt = float(ticker.get("quoteVolume", 0))

        # 计算技术指标
        closes_4h = [float(k[4]) for k in klines_4h]
        closes_1d = [float(k[4]) for k in klines_1d]

        ma7_4h = sum(closes_4h[-7:]) / 7 if len(closes_4h) >= 7 else price
        ma20_4h = sum(closes_4h[-20:]) / 20 if len(closes_4h) >= 20 else price

        ma7_1d = sum(closes_1d[-7:]) / 7 if len(closes_1d) >= 7 else price
        ma14_1d = sum(closes_1d[-14:]) / 14 if len(closes_1d) >= 14 else price

        # RSI (14周期，日线)
        rsi_1d = self._calc_rsi(closes_1d, 14)

        # 近7天涨跌
        if len(closes_1d) >= 7:
            week_change = (closes_1d[-1] - closes_1d[-7]) / closes_1d[-7] * 100
        else:
            week_change = 0

        # 最近的支撑阻力（简单取近期高低点）
        recent_high = max(closes_4h[-20:]) if len(closes_4h) >= 20 else high_24h
        recent_low = min(closes_4h[-20:]) if len(closes_4h) >= 20 else low_24h

        # 资金费率
        funding_str = "无数据"
        if funding:
            rate = float(funding.get("fundingRate", 0))
            funding_str = f"{rate*100:.4f}%（{'多头付费' if rate > 0 else '空头付费'}）"

        # 趋势判断
        if price > ma7_4h > ma20_4h:
            trend_4h = "上升趋势（价格>MA7>MA20）"
        elif price < ma7_4h < ma20_4h:
            trend_4h = "下降趋势（价格<MA7<MA20）"
        else:
            trend_4h = "震荡/趋势不明"

        # 成交量（亿USDT）
        vol_display = volume_usdt / 1e8

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        context = f"""## {coin} 实时市场数据
*更新时间: {now_str}*

### 价格概况
- 当前价格: ${price:,.2f}
- 24h涨跌: {change_pct:+.2f}%
- 24h最高: ${high_24h:,.2f}
- 24h最低: ${low_24h:,.2f}
- 7日涨跌: {week_change:+.2f}%
- 24h成交量: {vol_display:.2f}亿USDT

### 技术指标
- 4h趋势: {trend_4h}
- 4h MA7: ${ma7_4h:,.2f}（{'上方' if price > ma7_4h else '下方'}）
- 4h MA20: ${ma20_4h:,.2f}（{'上方' if price > ma20_4h else '下方'}）
- 日线MA7: ${ma7_1d:,.2f}
- 日线MA14: ${ma14_1d:,.2f}
- 日线RSI(14): {rsi_1d:.1f}（{'超买' if rsi_1d > 70 else '超卖' if rsi_1d < 30 else '中性'}）
- 近期阻力位: ${recent_high:,.2f}
- 近期支撑位: ${recent_low:,.2f}

### 衍生品数据
- 资金费率: {funding_str}

### 市场氛围简评
- RSI {rsi_1d:.0f} {'偏强' if rsi_1d > 55 else '偏弱' if rsi_1d < 45 else '中性'}，{'量能充足' if vol_display > 50 else '量能一般' if vol_display > 20 else '缩量'}
"""
        return context

    @staticmethod
    def _calc_rsi(closes: list[float], period: int = 14) -> float:
        """计算RSI"""
        if len(closes) < period + 1:
            return 50.0

        gains = []
        losses = []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            if diff > 0:
                gains.append(diff)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(diff))

        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi
