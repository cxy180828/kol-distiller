"""蒸馏模块

从分类标注后的推文中，提炼KOL的交易人格Profile。
输出为Markdown格式的人格文件，同时作为Agent的System Prompt基础。
"""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .config import AppConfig, get_kol_dir
from .llm_client import LLMClient
from .classifier import load_tagged_tweets


DISTILL_PROMPT = """你是一位资深量化策略分析师。你的任务是从一位加密货币KOL的历史推文中，蒸馏提炼出他的「交易人格画像」。

这份画像将被用来创建一个AI Agent，让它能以这位KOL的视角和思维方式来分析市场。所以你的总结必须：
1. 足够具体——不是泛泛而谈"他看好BTC"，而是他在什么条件下看好、逻辑是什么
2. 捕捉风格——他说话的方式、确信度的表达、犹豫时的表现
3. 突出差异——相比普通交易者，他的独特之处在哪

请严格按照以下Markdown模板输出（保留所有标题和结构）：

# KOL交易画像: @{handle}

## 身份定位
（一句话描述：他是什么类型的交易者？偏技术/偏宏观/偏链上/偏情绪？时间框架？）

## 核心交易哲学
（他反复强调的交易信念是什么？比如"趋势为王"、"逆势抄底"、"只做确定性高的"等）

## 分析框架
### 主要依据（权重最高）
- （列出他最看重的分析维度，如：关键价位、资金费率、链上大额转账...）

### 辅助参考
- （次要参考因素）

### 明确不看/不信的
- （他表达过不认可的分析方法或指标）

## 入场模式
### 模式一：（命名，如"支撑位反弹做多"）
- 触发条件：
- 确认信号：
- 典型案例：

### 模式二：（如有）
- 触发条件：
- 确认信号：
- 典型案例：

### 模式三：（如有）
- 触发条件：
- 确认信号：
- 典型案例：

## 风控与退出
- 止损习惯：（几个点？什么条件下认错？）
- 止盈方式：（分批出？一次性？目标位怎么定？）
- 仓位管理：（重仓还是轻仓试探？加仓逻辑？）

## 偏好与倾向
- 币种偏好：（主做大饼还是山寨？偏好什么类型？）
- 时间偏好：（日内/波段/中长线？）
- 情绪特征：（激进冒险/谨慎保守/中性理性？）

## 弱点与盲区
（从他亏损的案例或自我反思中总结）

## 典型发言风格
（摘录2-3条最能代表他风格的推文原文或概述，让Agent模仿他说话的方式）

## 代表性案例
### 成功案例
- （具体描述1-2个他明确赚钱的操作逻辑）

### 失败案例
- （如有，描述他认错或亏损的情况）

---
*蒸馏时间: {timestamp}*
*数据范围: 最近{days}天，共{count}条有效推文*"""


class ProfileDistiller:
    """KOL交易人格蒸馏器"""

    def __init__(self, config: AppConfig):
        self.config = config
        self.llm = LLMClient(config.llm)

    async def distill(self, handle: str) -> str:
        """
        对一个KOL执行蒸馏，生成Profile

        Args:
            handle: KOL的handle

        Returns:
            生成的Profile Markdown文本
        """
        handle = handle.lstrip("@")
        lookback_days = self.config.distill.lookback_days

        # 加载有效推文（排除noise）
        tweets = load_tagged_tweets(
            handle,
            categories=["trade_opinion", "market_analysis", "macro", "review"],
            days=lookback_days,
        )

        if not tweets:
            raise ValueError(
                f"@{handle} 没有有效的标注推文，请先抓取并分类"
            )

        # 构建推文摘要给LLM
        tweets_summary = self._build_tweets_summary(tweets)

        # 调用LLM蒸馏
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        prompt = DISTILL_PROMPT.format(
            handle=handle,
            timestamp=timestamp,
            days=lookback_days,
            count=len(tweets),
        )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"以下是 @{handle} 最近{lookback_days}天的{len(tweets)}条有效推文（已过滤无关内容）：\n\n{tweets_summary}"},
        ]

        print(f"  正在蒸馏 @{handle} 的交易画像（{len(tweets)}条有效推文）...")

        profile = await self.llm.chat(
            messages,
            temperature=self.config.llm.temperature_distill,
            max_tokens=4096,
        )

        print(f"  蒸馏完成")
        return profile

    def _build_tweets_summary(self, tweets: list[dict]) -> str:
        """将推文列表构建为LLM可读的摘要文本"""
        lines = []
        for tweet in tweets:
            time_str = tweet.get("time", "未知时间")
            category = tweet.get("category", "")
            text = tweet.get("text", "").replace("\n", " ").strip()

            # 提取的关键信息
            extracted = tweet.get("extracted")
            extra = ""
            if extracted:
                coin = extracted.get("coin", "")
                direction = extracted.get("direction", "")
                key_point = extracted.get("key_point", "")
                if key_point:
                    extra = f" [分类:{category} | 币:{coin or '无'} | 方向:{direction} | 要点:{key_point}]"

            lines.append(f"[{time_str}] {text}{extra}")

        return "\n\n".join(lines)


def save_profile(handle: str, profile_text: str):
    """保存Profile并备份旧版本"""
    handle = handle.lstrip("@")
    kol_dir = get_kol_dir(handle)
    profile_path = kol_dir / "profile.md"
    history_dir = kol_dir / "history"
    history_dir.mkdir(exist_ok=True)

    # 如果已有旧profile，备份到history
    if profile_path.exists():
        date_str = datetime.now().strftime("%Y-%m-%d")
        backup_path = history_dir / f"profile_{date_str}.md"
        # 如果今天已经有备份，加序号
        if backup_path.exists():
            i = 2
            while True:
                backup_path = history_dir / f"profile_{date_str}_{i}.md"
                if not backup_path.exists():
                    break
                i += 1
        shutil.copy2(profile_path, backup_path)

    # 写入新profile
    with open(profile_path, "w", encoding="utf-8") as f:
        f.write(profile_text)


def load_profile(handle: str) -> str:
    """加载KOL的Profile"""
    handle = handle.lstrip("@")
    kol_dir = get_kol_dir(handle)
    profile_path = kol_dir / "profile.md"

    if not profile_path.exists():
        raise FileNotFoundError(
            f"@{handle} 还没有生成Profile，请先执行蒸馏"
        )

    with open(profile_path, "r", encoding="utf-8") as f:
        return f.read()


def has_profile(handle: str) -> bool:
    """检查KOL是否已有Profile"""
    handle = handle.lstrip("@")
    kol_dir = get_kol_dir(handle)
    return (kol_dir / "profile.md").exists()
