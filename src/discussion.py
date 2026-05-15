"""讨论引擎模块

让多个KOL Agent对一个标的进行独立分析，然后汇总。
每个Agent基于自己的Profile（蒸馏出的交易人格）来给出视角。
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from .config import AppConfig, DISCUSSIONS_DIR, list_kols
from .llm_client import LLMClient
from .market_data import MarketDataClient
from .distiller import load_profile


AGENT_SYSTEM_PROMPT = """你现在扮演一位加密货币KOL交易员。你的交易风格和思维方式完全基于以下画像：

{profile}

---

重要规则：
1. 你必须完全以这位KOL的视角和风格来分析，包括说话方式
2. 给出具体的、可操作的观点（做多/做空/观望），不要模棱两可
3. 必须给出你的理由，且理由要符合这位KOL的分析框架
4. 如果当前市场状况不在你的舒适区，也要诚实说明
5. 用中文回复，保持这位KOL的语言风格"""


AGENT_ANALYSIS_PROMPT = """请基于当前市场数据，以你的交易风格和分析框架，对 {coin} 给出你的分析和操作思路。

{market_context}

请按以下结构回答（保持你的个人风格）：

1. **当前看法**：对{coin}目前走势的判断（一句话结论）
2. **方向倾向**：做多 / 做空 / 观望（明确选一个）
3. **核心逻辑**：为什么这么判断（2-3个关键理由）
4. **关键价位**：你关注的支撑位和阻力位
5. **操作建议**：如果要操作，怎么做（入场/止损/目标）
6. **风险提示**：什么情况下你会改变看法"""


SUMMARY_SYSTEM_PROMPT = """你是一位中立的投研主持人。你的任务是汇总多位KOL交易员对某个标的的分析，帮助用户快速理解各方观点。

要求：
1. 客观呈现各方观点，不偏袒任何一方
2. 突出共识和分歧
3. 不要给出你自己的建议
4. 用中文输出"""


SUMMARY_PROMPT = """以下是{count}位KOL对 {coin} 的分析，请做一个汇总：

{all_analyses}

请按以下结构汇总：

## 📊 {coin} 多方观点汇总

### 共识
（多数人认同的点）

### 分歧
（各方不同的判断）

### 方向统计
- 看多: X人
- 看空: X人  
- 观望: X人

### 关键价位汇聚
（各人提到的重要支撑/阻力位汇总）

### 综合风险提示
（各人提到的主要风险因素）"""


class DiscussionEngine:
    """多Agent讨论引擎"""

    def __init__(self, config: AppConfig):
        self.config = config
        self.llm = LLMClient(config.llm)
        self.market = MarketDataClient(config.market_data)

    async def discuss(
        self,
        coin: str,
        kol_handles: list[str] | None = None,
    ) -> str:
        """
        让多个KOL Agent讨论一个标的

        Args:
            coin: 币种名称（如BTC、ETH）
            kol_handles: 指定参与讨论的KOL列表，None则使用所有已蒸馏的KOL

        Returns:
            完整的讨论记录（Markdown格式）
        """
        coin = coin.upper().strip()

        # 确定参与讨论的KOL
        if kol_handles is None:
            kol_handles = list_kols()

        if not kol_handles:
            return "❌ 没有可用的KOL Agent。请先添加并蒸馏KOL。"

        # 过滤掉没有profile的KOL
        available = []
        for handle in kol_handles:
            handle = handle.lstrip("@")
            try:
                load_profile(handle)
                available.append(handle)
            except FileNotFoundError:
                print(f"  ⚠️ @{handle} 没有Profile，跳过")

        if not available:
            return "❌ 所有指定的KOL都没有生成Profile，请先执行蒸馏。"

        # 获取市场数据
        print(f"\n📈 获取 {coin} 实时行情...")
        market_context = await self.market.get_market_context(coin)

        # 各Agent独立分析
        print(f"\n🤖 {len(available)}位KOL Agent开始分析 {coin}...\n")
        analyses = {}
        for handle in available:
            print(f"  💭 @{handle} 正在分析...")
            analysis = await self._agent_analyze(handle, coin, market_context)
            analyses[handle] = analysis
            print(f"  ✅ @{handle} 完成")

        # 汇总
        print(f"\n📋 生成汇总...")
        summary = await self._summarize(coin, analyses)

        # 拼接完整讨论记录
        full_discussion = self._format_discussion(coin, market_context, analyses, summary)

        # 保存讨论记录
        self._save_discussion(coin, full_discussion)

        return full_discussion

    async def _agent_analyze(
        self, handle: str, coin: str, market_context: str
    ) -> str:
        """单个Agent进行分析"""
        profile = load_profile(handle)

        system_prompt = AGENT_SYSTEM_PROMPT.format(profile=profile)
        user_prompt = AGENT_ANALYSIS_PROMPT.format(
            coin=coin, market_context=market_context
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        analysis = await self.llm.chat(
            messages,
            temperature=self.config.llm.temperature_discuss,
            max_tokens=2048,
        )

        return analysis

    async def _summarize(self, coin: str, analyses: dict[str, str]) -> str:
        """汇总所有Agent的分析"""
        all_analyses_text = ""
        for handle, analysis in analyses.items():
            all_analyses_text += f"### @{handle} 的观点：\n{analysis}\n\n---\n\n"

        messages = [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": SUMMARY_PROMPT.format(
                count=len(analyses),
                coin=coin,
                all_analyses=all_analyses_text,
            )},
        ]

        summary = await self.llm.chat(
            messages,
            temperature=0.3,
            max_tokens=2048,
        )

        return summary

    def _format_discussion(
        self,
        coin: str,
        market_context: str,
        analyses: dict[str, str],
        summary: str,
    ) -> str:
        """格式化完整讨论记录"""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        doc = f"""# {coin} 多KOL讨论记录

*时间: {now}*
*参与Agent: {', '.join(f'@{h}' for h in analyses.keys())}*

---

{market_context}

---

## 各KOL分析

"""
        for handle, analysis in analyses.items():
            doc += f"### 🧠 @{handle}\n\n{analysis}\n\n---\n\n"

        doc += f"""## 汇总

{summary}

---
*由 KOL Distiller 自动生成*
"""
        return doc

    def _save_discussion(self, coin: str, content: str):
        """保存讨论记录到文件"""
        DISCUSSIONS_DIR.mkdir(exist_ok=True)
        date_str = datetime.now().strftime("%Y-%m-%d_%H%M")
        filename = f"{date_str}_{coin}.md"
        filepath = DISCUSSIONS_DIR / filename

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        print(f"\n💾 讨论记录已保存: discussions/{filename}")
