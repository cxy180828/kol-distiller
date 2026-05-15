"""Twitter/X 推文抓取模块

使用 twikit 通过 cookie 模拟登录抓取推文。
支持：
- 首次添加KOL时批量拉取历史推文
- 增量抓取（只拉上次之后的新推文）
"""

import json
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from twikit import Client

from .config import AppConfig, get_kol_dir


class TweetScraper:
    """推文抓取器"""

    def __init__(self, config: AppConfig):
        self.config = config
        self.client: Optional[Client] = None

    async def _ensure_client(self):
        """确保客户端已初始化并登录"""
        if self.client is not None:
            return

        self.client = Client("zh-CN")

        # 优先使用cookies.json文件（最可靠的方式）
        cookies_path = Path(__file__).parent.parent / "cookies.json"
        if cookies_path.exists():
            try:
                self.client.load_cookies(str(cookies_path))
                return
            except Exception as e:
                raise RuntimeError(
                    f"cookies.json 加载失败（可能已过期），请到【设置】页面重新登录Twitter。\n"
                    f"（错误: {e}）"
                )

        # 如果有用户名密码，用登录方式
        username = getattr(self.config.twitter, 'username', None) or ""
        password = getattr(self.config.twitter, 'password', None) or ""
        email = getattr(self.config.twitter, 'email', None) or ""

        if username and password:
            try:
                await self.client.login(
                    auth_info_1=username,
                    auth_info_2=email,
                    password=password,
                    cookies_file=str(cookies_path),
                )
                return
            except Exception as e:
                error_msg = str(e).lower()
                if "2fa" in error_msg or "totp" in error_msg or "confirmation" in error_msg:
                    raise RuntimeError(
                        "此账号开启了2FA验证，无法通过命令行自动登录。\n"
                        "请到Web端【设置】页面手动登录（支持输入2FA验证码）。"
                    )
                raise RuntimeError(f"Twitter登录失败: {e}")

        # 兜底：使用auth_token和ct0（不稳定）
        auth_token = self.config.twitter.auth_token
        ct0 = self.config.twitter.ct0
        if auth_token and ct0:
            self.client.set_cookies({
                "auth_token": auth_token,
                "ct0": ct0,
            })
            self.client._token = ct0
            return

        raise RuntimeError(
            "❌ Twitter未登录。请到Web端【设置】页面登录Twitter账号。\n"
            "（需要用户名+密码，支持2FA验证码）"
        )

    async def fetch_user_tweets(
        self,
        handle: str,
        count: int = 500,
        since_id: Optional[str] = None,
    ) -> list[dict]:
        """
        抓取某个用户的推文

        Args:
            handle: 用户handle（不带@）
            count: 最多抓取多少条
            since_id: 只抓取这个ID之后的推文（增量抓取用）

        Returns:
            推文列表，每条包含 id, text, time, metrics
        """
        await self._ensure_client()
        handle = handle.lstrip("@")

        try:
            # 获取用户信息
            user = await self.client.get_user_by_screen_name(handle)
            if user is None:
                raise ValueError(f"找不到用户: @{handle}")

            tweets = []
            cursor = None
            fetched = 0

            while fetched < count:
                # 获取一批推文
                if cursor is None:
                    result = await user.get_tweets("Tweets", count=40)
                else:
                    result = await result.next()

                if not result:
                    break

                for tweet in result:
                    # 如果设了since_id，跳过旧推文
                    if since_id and tweet.id <= since_id:
                        # 推文按时间倒序，遇到旧的说明后面都是旧的
                        return tweets

                    # 跳过转推，只要原创
                    if hasattr(tweet, 'retweeted_tweet') and tweet.retweeted_tweet:
                        continue

                    tweet_data = self._parse_tweet(tweet, handle)
                    tweets.append(tweet_data)
                    fetched += 1

                    if fetched >= count:
                        break

                # 检查是否还有更多
                if not hasattr(result, 'next') or result.next is None:
                    break

                # 防止被限流，间隔一下
                await asyncio.sleep(1)

            return tweets

        except Exception as e:
            error_msg = str(e)

            # 检测Cookie过期/登录失效的常见错误
            cookie_expired_keywords = [
                "KEY_BYTE", "unauthorized", "401", "403",
                "Could not authenticate", "InvalidToken",
                "BadRequest", "session", "expired",
            ]
            is_cookie_error = any(kw.lower() in error_msg.lower() for kw in cookie_expired_keywords)

            if is_cookie_error:
                raise RuntimeError(
                    f"Twitter登录已过期或无效，请到【设置】页面重新登录Twitter。\n"
                    f"（原始错误: {error_msg}）"
                )
            else:
                raise RuntimeError(f"抓取 @{handle} 推文失败: {error_msg}")

    def _parse_tweet(self, tweet, handle: str) -> dict:
        """解析单条推文为标准格式"""
        # 解析时间
        created_at = ""
        if hasattr(tweet, 'created_at') and tweet.created_at:
            created_at = tweet.created_at

        return {
            "id": str(tweet.id),
            "handle": handle,
            "text": tweet.text or "",
            "time": created_at,
            "metrics": {
                "likes": getattr(tweet, 'favorite_count', 0) or 0,
                "retweets": getattr(tweet, 'retweet_count', 0) or 0,
                "replies": getattr(tweet, 'reply_count', 0) or 0,
            },
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    async def fetch_initial(self, handle: str) -> list[dict]:
        """首次添加KOL，拉取历史推文"""
        count = self.config.distill.initial_fetch_count
        print(f"  正在拉取 @{handle} 最近 {count} 条推文...")
        tweets = await self.fetch_user_tweets(handle, count=count)
        print(f"  完成，共获取 {len(tweets)} 条推文")
        return tweets

    async def fetch_incremental(self, handle: str) -> list[dict]:
        """增量抓取：只拉取上次之后的新推文"""
        handle = handle.lstrip("@")
        kol_dir = get_kol_dir(handle)
        meta_path = kol_dir / "meta.json"

        # 读取上次最新的推文ID
        since_id = None
        if meta_path.exists():
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
                since_id = meta.get("latest_tweet_id")

        if since_id:
            print(f"  增量抓取 @{handle}（since_id={since_id}）...")
        else:
            print(f"  首次抓取 @{handle}...")
            return await self.fetch_initial(handle)

        tweets = await self.fetch_user_tweets(
            handle, count=200, since_id=since_id
        )
        print(f"  获取 {len(tweets)} 条新推文")
        return tweets


def save_tweets(handle: str, tweets: list[dict]):
    """
    保存推文到文件（追加写入JSONL）并更新meta，自动去重
    """
    handle = handle.lstrip("@")
    kol_dir = get_kol_dir(handle)
    raw_path = kol_dir / "tweets_raw.jsonl"
    meta_path = kol_dir / "meta.json"

    if not tweets:
        return

    # 加载已有的推文ID用于去重
    existing_ids = set()
    if raw_path.exists():
        with open(raw_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        existing_ids.add(json.loads(line).get("id"))
                    except json.JSONDecodeError:
                        pass

    # 过滤重复
    new_tweets = [t for t in tweets if t.get("id") not in existing_ids]

    if not new_tweets:
        return

    # 追加写入原始推文
    with open(raw_path, "a", encoding="utf-8") as f:
        for tweet in new_tweets:
            f.write(json.dumps(tweet, ensure_ascii=False) + "\n")

    # 更新meta
    meta = {}
    if meta_path.exists():
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)

    # 最新推文ID（tweets已经是倒序的，第一条最新）
    meta["handle"] = handle
    meta["latest_tweet_id"] = tweets[0]["id"]
    meta["last_fetch_time"] = datetime.now(timezone.utc).isoformat()
    meta["total_tweets"] = meta.get("total_tweets", 0) + len(new_tweets)

    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def load_raw_tweets(handle: str) -> list[dict]:
    """加载某个KOL的所有原始推文"""
    handle = handle.lstrip("@")
    kol_dir = get_kol_dir(handle)
    raw_path = kol_dir / "tweets_raw.jsonl"

    if not raw_path.exists():
        return []

    tweets = []
    with open(raw_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                tweets.append(json.loads(line))

    return tweets
