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

    async def chat_with_model(
        self,
        messages: list[dict],
        model: str,
        temperature: Optional[float] = None,
        max_tokens: int = 4096,
        max_retries: int = 3,
    ) -> str:
        """
        使用指定模型发送聊天请求（用于预筛等场景）

        Args:
            messages: 消息列表
            model: 指定使用的模型名称
            temperature: 温度参数
            max_tokens: 最大输出token数
            max_retries: 最大重试次数

        Returns:
            模型回复的文本内容
        """
        # 临时替换模型名
        original_model = self.config.model
        self.config.model = model
        try:
            result = await self.chat(messages, temperature, max_tokens, max_retries)
        finally:
            self.config.model = original_model
        return result

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
                            last_error = f"API限流（状态码429），请稍后再试"
                            continue
                        elif resp.status_code == 401:
                            raise RuntimeError(
                                f"LLM API认证失败（状态码401）：API Key无效或已过期。\n"
                                f"请到【设置】中检查 llm.api_key 是否正确。"
                            )
                        elif resp.status_code == 403:
                            raise RuntimeError(
                                f"LLM API权限不足（状态码403）：当前API Key无权访问该模型。\n"
                                f"请确认模型名称 '{self.config.model}' 正确且有访问权限。"
                            )
                        elif resp.status_code >= 500:
                            last_error = f"LLM服务端错误（状态码{resp.status_code}），服务可能暂时不可用"
                            if attempt < max_retries - 1:
                                import asyncio
                                print(f"  ⚠️ LLM服务端错误，重试({attempt+1}/{max_retries})...")
                                await asyncio.sleep(3)
                                continue
                        else:
                            raise RuntimeError(
                                f"LLM调用失败（状态码{resp.status_code}）: {error_text[:200]}"
                            )

                    data = resp.json()
                    choices = data.get("choices")
                    if not choices:
                        raise RuntimeError(
                            "LLM返回数据异常：响应中没有choices字段。\n"
                            "请检查API地址和模型名称是否正确。"
                        )
                    return choices[0]["message"]["content"]

            except httpx.ConnectError:
                last_error = (
                    f"无法连接到LLM服务（{self.base_url}）。\n"
                    f"请检查：1) API地址是否正确 2) 网络是否通畅 3) 服务是否在线"
                )
                if attempt < max_retries - 1:
                    import asyncio
                    print(f"  ⚠️ 无法连接LLM服务，重试({attempt+1}/{max_retries})...")
                    await asyncio.sleep(2)
                    continue
            except httpx.TimeoutException:
                last_error = "LLM请求超时（120秒），可能是网络慢或模型响应时间过长"
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
            try:
                raw2 = await self.chat(retry_messages, temperature=0.0, max_tokens=max_tokens)
            except Exception as e:
                raise RuntimeError(
                    f"LLM返回的内容不是有效的JSON格式，重试也失败了。\n"
                    f"原始回复前100字: {raw[:100]}...\n"
                    f"重试错误: {e}"
                )

            content2 = raw2.strip()
            if content2.startswith("```"):
                lines = content2.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                content2 = "\n".join(lines)

            try:
                return json.loads(content2)
            except json.JSONDecodeError as e:
                raise RuntimeError(
                    f"LLM两次返回的内容都不是有效JSON，可能是模型不支持结构化输出。\n"
                    f"错误详情: {e}\n"
                    f"第二次回复前100字: {content2[:100]}..."
                )
