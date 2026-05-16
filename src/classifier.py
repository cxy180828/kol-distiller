"""推文分类模块

两级AI分类架构：
  第一级（粗筛）：用便宜模型快速判断推文是否与加密货币交易/市场相关
  第二级（精分类）：只对相关推文做详细分类和信息提取

分类类别：
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


# === 第一级：粗筛 Prompt ===

PRE_FILTER_SYSTEM_PROMPT = """你是一个加密货币内容筛选器。判断每条推文是否与加密货币交易、市场分析、行情判断相关。

规则：
- Y = 与加密货币交易/市场/行情/仓位/技术分析相关（哪怕只是提到币种价格走势）
- N = 完全无关（闲聊、日常生活、广告、抽奖、纯表情、纯转发无内容）

只输出一个JSON数组，元素为 "Y" 或 "N"，顺序对应输入推文顺序。
例如输入3条推文：["Y", "N", "Y"]

注意：宁可多放过（标Y），也不要误杀。有任何加密/金融相关内容就标Y。"""


# === 第二级：精分类 Prompt ===

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
    """推文两级分类器：粗筛 + 精分类"""

    def __init__(self, config: AppConfig):
        self.config = config
        self.llm = LLMClient(config.llm)
        self.batch_size = config.distill.batch_size
        # 预筛批次大小（小模型可以处理更多）
        self.pre_filter_batch_size = 50

        # 预筛模型：如果配置了单独的模型就用它，否则跳过预筛直接全量分类
        self.pre_filter_model = config.llm.pre_filter_model.strip() if config.llm.pre_filter_model else ""

    async def classify_tweets(self, tweets: list[dict]) -> list[dict]:
        """
        对推文列表进行两级分类标注

        流程：
          1. 第一级（粗筛）：用小模型快速判断是否交易相关
          2. 第二级（精分类）：只对相关推文做详细分类

        Args:
            tweets: 原始推文列表

        Returns:
            标注后的推文列表（在原数据上增加category和extracted字段）
        """
        if not tweets:
            return []

        total = len(tweets)

        # === 第一级：粗筛 ===
        if self.pre_filter_model:
            print(f"  第一级粗筛（{self.pre_filter_model}）...")
            relevant_mask = await self._pre_filter_all(tweets)
            relevant_tweets = [t for t, is_rel in zip(tweets, relevant_mask) if is_rel]
            noise_tweets = [t for t, is_rel in zip(tweets, relevant_mask) if not is_rel]
            print(f"  粗筛结果：{len(relevant_tweets)} 条相关 / {len(noise_tweets)} 条噪音（跳过）")
        else:
            # 没有配置预筛模型，全量走精分类
            relevant_tweets = tweets
            noise_tweets = []
            relevant_mask = [True] * total

        # === 第二级：精分类 ===
        if relevant_tweets:
            print(f"  第二级精分类（{self.config.llm.model}）...")
            tagged_relevant = await self._classify_detailed(relevant_tweets)
        else:
            tagged_relevant = []

        # === 合并结果 ===
        # 噪音推文直接标记为noise
        tagged_noise = []
        for tweet in noise_tweets:
            tagged_tweet = tweet.copy()
            tagged_tweet["category"] = "noise"
            tagged_tweet["extracted"] = None
            tagged_noise.append(tagged_tweet)

        # 按原始顺序重组结果
        result = []
        rel_idx = 0
        noise_idx = 0
        for is_rel in relevant_mask:
            if is_rel:
                if rel_idx < len(tagged_relevant):
                    result.append(tagged_relevant[rel_idx])
                rel_idx += 1
            else:
                if noise_idx < len(tagged_noise):
                    result.append(tagged_noise[noise_idx])
                noise_idx += 1

        # 统计
        categories_count = {}
        for t in result:
            cat = t.get("category", "noise")
            categories_count[cat] = categories_count.get(cat, 0) + 1
        stats = " / ".join(f"{k}:{v}" for k, v in sorted(categories_count.items()))
        print(f"  分类完成：{stats}")

        return result

    # === 第一级：粗筛实现 ===

    async def _pre_filter_all(self, tweets: list[dict]) -> list[bool]:
        """对所有推文进行粗筛，返回每条推文是否相关的布尔列表"""
        results = [True] * len(tweets)  # 默认为相关（保守策略）

        batches = [
            tweets[i:i + self.pre_filter_batch_size]
            for i in range(0, len(tweets), self.pre_filter_batch_size)
        ]

        for batch_idx, batch in enumerate(batches):
            offset = batch_idx * self.pre_filter_batch_size
            batch_results = await self._pre_filter_batch(batch)

            for i, is_relevant in enumerate(batch_results):
                if offset + i < len(results):
                    results[offset + i] = is_relevant

            # 批次间间隔
            if batch_idx < len(batches) - 1:
                await asyncio.sleep(0.3)

        return results

    async def _pre_filter_batch(self, batch: list[dict]) -> list[bool]:
        """用小模型粗筛一批推文"""

        # 构建推文文本
        tweets_text = ""
        for i, tweet in enumerate(batch):
            text = tweet["text"].replace("\n", " ").strip()[:200]  # 截断长推文节省token
            tweets_text += f"[{i}] {text}\n"

        messages = [
            {"role": "system", "content": PRE_FILTER_SYSTEM_PROMPT},
            {"role": "user", "content": f"判断以下{len(batch)}条推文是否与加密货币交易/市场相关：\n\n{tweets_text}"},
        ]

        try:
            # 使用预筛模型
            result = await self.llm.chat_with_model(
                messages,
                model=self.pre_filter_model,
                temperature=0.0,
                max_tokens=512,
            )

            # 解析结果
            content = result.strip()
            if content.startswith("```"):
                lines = content.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                content = "\n".join(lines)

            parsed = json.loads(content)
            if isinstance(parsed, list):
                # 转换为布尔列表
                bool_results = []
                for item in parsed:
                    if isinstance(item, str):
                        bool_results.append(item.upper().startswith("Y"))
                    elif isinstance(item, bool):
                        bool_results.append(item)
                    else:
                        bool_results.append(True)  # 无法判断的保留

                # 补齐长度
                while len(bool_results) < len(batch):
                    bool_results.append(True)

                return bool_results[:len(batch)]

        except Exception as e:
            print(f"  ⚠️ 粗筛批次失败: {e}，保守处理（全部标为相关）")

        # 失败时保守策略：全部标为相关
        return [True] * len(batch)

    # === 第二级：精分类实现 ===

    async def _classify_detailed(self, tweets: list[dict]) -> list[dict]:
        """对筛选后的推文做详细分类"""
        tagged = []
        total = len(tweets)
        batches = [
            tweets[i:i + self.batch_size]
            for i in range(0, total, self.batch_size)
        ]

        for batch_idx, batch in enumerate(batches):
            print(f"    精分类中... ({batch_idx * self.batch_size + len(batch)}/{total})")

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
        """精分类一个批次的推文"""

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
                        # 推文时间格式多样，尝试多种解析
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
