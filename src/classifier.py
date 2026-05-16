"""推文分类模块

对原始推文进行批量分类标注：
- trade_opinion: 交易观点（喊单、仓位操作）
- market_analysis: 市场分析（技术面、链上数据解读）
- macro: 宏观判断（美联储、监管、行业趋势）
- review: 复盘/马后炮（回顾之前的操作结果）
- noise: 无关内容（闲聊、广告、转发评论）
"""

import json
import asyncio
from pathlib import Path
from typing import Optional

from .config import AppConfig, get_kol_dir
from .llm_client import LLMClient


CLASSIFY_SYSTEM_PROMPT = """你是一个加密货币推文分类助手。你的任务是对KOL的推文进行分类和关键信息提取。

对每条推文，你需要：
1. 判断分类（category）：
   - trade_opinion: 包含明确的交易观点或操作（做多/做空/买入/卖出/加仓/减仓/止盈/止损）
   - market_analysis: 市场分析（技术指标解读、K线形态、链上数据分析、资金流向）
   - macro: 宏观判断（美联储政策、监管动态、行业趋势、大事件预判）
   - review: 复盘（回顾之前的交易结果、总结经验教训）
   - noise: 无关内容（闲聊、广告、抽奖、日常、表情包、纯转发评论）

2. 如果不是noise，提取关键信息（extracted）：
   - coin: 涉及的币种（BTC/ETH/SOL等），没有则为null
   - direction: 方向（long/short/neutral），没有明确方向则为neutral
   - key_point: 一句话概括核心观点（中文）

输出格式为JSON数组，每个元素对应一条推文：
[
  {
    "index": 0,
    "category": "trade_opinion",
    "extracted": {
      "coin": "BTC",
      "direction": "long",
      "key_point": "认为6.5万是强支撑位，在此加仓"
    }
  },
  {
    "index": 1,
    "category": "noise",
    "extracted": null
  }
]

注意：
- 严格按照推文顺序输出，index从0开始
- 只输出JSON数组，不要有其他文字
- key_point用中文概括，简短精炼（20字以内）
- 模棱两可的归为noise，宁可漏掉也不要误判"""


class TweetClassifier:
    """推文批量分类器"""

    def __init__(self, config: AppConfig):
        self.config = config
        self.llm = LLMClient(config.llm)
        self.batch_size = config.distill.batch_size

    async def classify_tweets(self, tweets: list[dict]) -> list[dict]:
        """
        对推文列表进行分类标注

        Args:
            tweets: 原始推文列表

        Returns:
            标注后的推文列表（在原数据上增加category和extracted字段）
        """
        if not tweets:
            return []

        tagged = []
        total = len(tweets)
        batches = [
            tweets[i:i + self.batch_size]
            for i in range(0, total, self.batch_size)
        ]

        for batch_idx, batch in enumerate(batches):
            print(f"  分类中... ({batch_idx * self.batch_size + len(batch)}/{total})")

            results = await self._classify_batch(batch)

            for i, tweet in enumerate(batch):
                tagged_tweet = tweet.copy()
                if i < len(results):
                    tagged_tweet["category"] = results[i].get("category", "noise")
                    tagged_tweet["extracted"] = results[i].get("extracted")
                else:
                    tagged_tweet["category"] = "noise"
                    tagged_tweet["extracted"] = None
                tagged.append(tagged_tweet)

            # 批次间间隔，避免API限流
            if batch_idx < len(batches) - 1:
                await asyncio.sleep(0.5)

        return tagged

    async def _classify_batch(self, batch: list[dict]) -> list[dict]:
        """分类一个批次的推文"""

        # 构建推文文本列表
        tweets_text = ""
        for i, tweet in enumerate(batch):
            text = tweet["text"].replace("\n", " ").strip()
            tweets_text += f"[{i}] {text}\n"

        messages = [
            {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
            {"role": "user", "content": f"请对以下{len(batch)}条推文进行分类：\n\n{tweets_text}"},
        ]

        try:
            result = await self.llm.chat_json(
                messages,
                temperature=self.config.llm.temperature_classify,
                max_tokens=2048,
            )
            if isinstance(result, list):
                return result
            else:
                return [{"category": "noise", "extracted": None}] * len(batch)
        except Exception as e:
            print(f"  ⚠️ 分类批次失败: {e}，标记为noise")
            return [{"category": "noise", "extracted": None}] * len(batch)


def save_tagged_tweets(handle: str, tagged_tweets: list[dict]):
    """保存分类后的推文"""
    handle = handle.lstrip("@")
    kol_dir = get_kol_dir(handle)
    tagged_path = kol_dir / "tweets_tagged.jsonl"

    with open(tagged_path, "a", encoding="utf-8") as f:
        for tweet in tagged_tweets:
            f.write(json.dumps(tweet, ensure_ascii=False) + "\n")


def load_tagged_tweets(
    handle: str,
    categories: Optional[list[str]] = None,
    days: Optional[int] = None,
) -> list[dict]:
    """
    加载已标注的推文

    Args:
        handle: KOL handle
        categories: 只加载这些分类的推文，None则全部
        days: 只加载最近N天的，None则全部
    """
    handle = handle.lstrip("@")
    kol_dir = get_kol_dir(handle)
    tagged_path = kol_dir / "tweets_tagged.jsonl"

    if not tagged_path.exists():
        return []

    tweets = []
    parse_errors = 0
    with open(tagged_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                tweet = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue

            # 分类过滤
            if categories and tweet.get("category") not in categories:
                continue

            # 时间过滤 - 优先用推文本身的发布时间(time)，而不是抓取时间(fetched_at)
            if days is not None:
                from datetime import datetime, timezone, timedelta
                CN_TZ = timezone(timedelta(hours=8))
                # 优先用推文发布时间
                time_str = tweet.get("time", "") or tweet.get("fetched_at", "")
                if time_str:
                    try:
                        # twikit返回的时间格式多样，尝试多种解析
                        if "+" in time_str or time_str.endswith("Z"):
                            t = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
                        else:
                            # 尝试解析 "Wed Oct 10 20:19:24 +0000 2018" 格式
                            try:
                                t = datetime.strptime(time_str, "%a %b %d %H:%M:%S %z %Y")
                            except ValueError:
                                # 尝试解析 "2025-05-15 23:10:30" 格式（北京时间）
                                try:
                                    t = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN_TZ)
                                except ValueError:
                                    # 回退到fetched_at
                                    fetched = tweet.get("fetched_at", "")
                                    if fetched:
                                        try:
                                            t = datetime.fromisoformat(fetched.replace("Z", "+00:00"))
                                        except ValueError:
                                            t = datetime.strptime(fetched, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN_TZ)
                                    else:
                                        continue
                        cutoff = datetime.now(CN_TZ) - timedelta(days=days)
                        if t < cutoff:
                            continue
                    except (ValueError, TypeError):
                        pass  # 解析失败就不过滤，保留该推文

            tweets.append(tweet)

    if parse_errors > 0:
        print(f"  ⚠️ @{handle} 的标注文件中有 {parse_errors} 行数据损坏（已跳过）")

    return tweets


def count_recent_trade_tweets(handle: str, days: int = 7) -> int:
    """统计最近N天的交易观点推文数量（用于判断是否触发提前蒸馏）"""
    tweets = load_tagged_tweets(
        handle,
        categories=["trade_opinion"],
        days=days,
    )
    return len(tweets)
