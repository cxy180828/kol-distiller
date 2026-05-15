"""LLM 客户端模块

通用 OpenAI 兼容格式调用，支持任意中转站。
"""

import json
import httpx
from typing import Optional

from .config import LLMConfig


class LLMClient:
    """通用LLM客户端（OpenAI兼容格式）"""

    def __init__(self, config: LLMConfig):
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.api_key}",
        }

    async def chat(
        self,
        messages: list[dict],
        temperature: Optional[float] = None,
        max_tokens: int = 4096,
        max_retries: int = 3,
    ) -> str:
        """
        发送聊天请求（带重试）

        Args:
            messages: [{"role": "system/user/assistant", "content": "..."}]
            temperature: 温度参数，None则用默认
            max_tokens: 最大输出token数
            max_retries: 最大重试次数

        Returns:
            模型回复的文本内容
        """
        if temperature is None:
            temperature = 0.7

        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        last_error = None
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    resp = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers=self.headers,
                        json=payload,
                    )

                    if resp.status_code != 200:
                        error_text = resp.text
                        # 如果是限流，等一会重试
                        if resp.status_code == 429:
                            import asyncio
                            wait_time = (attempt + 1) * 5
                            print(f"  ⚠️ API限流，等待{wait_time}秒后重试...")
                            await asyncio.sleep(wait_time)
                            last_error = f"API限流 [{resp.status_code}]"
                            continue
                        raise RuntimeError(
                            f"LLM调用失败 [{resp.status_code}]: {error_text}"
                        )

                    data = resp.json()
                    return data["choices"][0]["message"]["content"]

            except httpx.TimeoutException:
                last_error = "请求超时"
                if attempt < max_retries - 1:
                    import asyncio
                    print(f"  ⚠️ LLM请求超时，重试({attempt+1}/{max_retries})...")
                    await asyncio.sleep(2)
                    continue
            except RuntimeError:
                raise  # 非超时的RuntimeError直接抛出
            except Exception as e:
                last_error = str(e)
                if attempt < max_retries - 1:
                    import asyncio
                    print(f"  ⚠️ LLM调用异常: {e}，重试({attempt+1}/{max_retries})...")
                    await asyncio.sleep(2)
                    continue

        raise RuntimeError(f"LLM调用失败（重试{max_retries}次后仍失败）: {last_error}")

    async def chat_json(
        self,
        messages: list[dict],
        temperature: Optional[float] = None,
        max_tokens: int = 4096,
    ) -> dict | list:
        """
        发送聊天请求并解析JSON响应

        会自动重试一次如果JSON解析失败。
        """
        raw = await self.chat(messages, temperature, max_tokens)

        # 尝试解析JSON（处理markdown代码块包裹的情况）
        content = raw.strip()
        if content.startswith("```"):
            # 去掉 ```json 和 ```
            lines = content.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            content = "\n".join(lines)

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            # 重试：让模型修正格式
            retry_messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": "你的回复不是合法JSON，请只输出纯JSON，不要包含任何其他文字或markdown标记。"},
            ]
            raw2 = await self.chat(retry_messages, temperature=0.0, max_tokens=max_tokens)
            content2 = raw2.strip()
            if content2.startswith("```"):
                lines = content2.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                content2 = "\n".join(lines)

            return json.loads(content2)
