"""Twitter/X 推文抓取模块

使用 httpx 直接请求 Twitter GraphQL API 抓取推文。
只需要浏览器 Cookie 中的 auth_token 和 ct0 即可工作。

获取方式：
  1. 用浏览器登录 x.com
  2. F12 → Application → Cookies → x.com
  3. 复制 auth_token 和 ct0 的值

支持：
- 首次添加KOL时批量拉取历史推文
- 增量抓取（只拉上次之后的新推文）
"""

import json
import asyncio
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from .config import AppConfig, get_kol_dir, ROOT_DIR


# Twitter GraphQL API 端点
GRAPHQL_BASE = "https://x.com/i/api/graphql"

# UserByScreenName 查询端点（用于获取用户ID）
USER_BY_SCREEN_NAME_URL = f"{GRAPHQL_BASE}/qW5u-DAen47o2oBGIw8Nhg/UserByScreenName"

# UserTweets 查询端点（用于获取用户推文）
USER_TWEETS_URL = f"{GRAPHQL_BASE}/E3opETHurmVJflFsUBVuUQ/UserTweets"


def _load_twitter_credentials() -> tuple[str, str]:
    """
    加载 Twitter 凭证（auth_token 和 ct0）
    优先从 twitter_credentials.json 读取（Web设置页面保存的），
    其次从 config.yaml 的 twitter 段读取。
    """
    # 优先读取 Web 页面保存的凭证文件
    creds_path = ROOT_DIR / "twitter_credentials.json"
    if creds_path.exists():
        try:
            with open(creds_path, "r", encoding="utf-8") as f:
                creds = json.load(f)
            auth_token = creds.get("auth_token", "").strip()
            ct0 = creds.get("ct0", "").strip()
            if auth_token and ct0:
                return auth_token, ct0
        except (json.JSONDecodeError, OSError):
            pass

    return "", ""


class TweetScraper:
    """推文抓取器 - 使用 httpx 直接请求 Twitter GraphQL API"""

    def __init__(self, config: AppConfig):
        self.config = config
        self._client: Optional[httpx.AsyncClient] = None
        self._auth_token: str = ""
        self._ct0: str = ""

    async def _ensure_client(self):
        """确保 HTTP 客户端已初始化并配置好认证信息"""
        if self._client is not None:
            return

        # 加载凭证
        auth_token, ct0 = _load_twitter_credentials()

        # 如果凭证文件没有，从 config 读取
        if not auth_token or not ct0:
            auth_token = getattr(self.config.twitter, 'auth_token', '') or ''
            ct0 = getattr(self.config.twitter, 'ct0', '') or ''

        if not auth_token or not ct0:
            raise RuntimeError(
                "Twitter 凭证未配置。请到 Web 端【设置】页面填写 auth_token 和 ct0。\n"
                "获取方式：浏览器登录 x.com → F12 → Application → Cookies → 复制 auth_token 和 ct0"
            )

        self._auth_token = auth_token
        self._ct0 = ct0

        # 构建请求头，模拟浏览器访问
        headers = {
            "authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
            "x-csrf-token": ct0,
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "zh-cn",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "content-type": "application/json",
            "accept": "*/*",
            "referer": "https://x.com/",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

        cookies = {
            "auth_token": auth_token,
            "ct0": ct0,
        }

        self._client = httpx.AsyncClient(
            headers=headers,
            cookies=cookies,
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )

    async def _close_client(self):
        """关闭 HTTP 客户端"""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _get_user_id(self, handle: str) -> str:
        """通过 screen_name 获取用户的 rest_id"""
        variables = {
            "screen_name": handle,
            "withSafetyModeUserFields": True,
        }
        features = {
            "hidden_profile_subscriptions_enabled": True,
            "rweb_tipjar_consumption_enabled": True,
            "responsive_web_graphql_exclude_directive_enabled": True,
            "verified_phone_label_enabled": False,
            "subscriptions_verification_info_is_identity_verified_enabled": True,
            "subscriptions_verification_info_verified_since_enabled": True,
            "highlights_tweets_tab_ui_enabled": True,
            "responsive_web_twitter_article_notes_tab_enabled": True,
            "subscriptions_feature_can_gift_premium": True,
            "creator_subscriptions_tweet_preview_api_enabled": True,
            "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
            "responsive_web_graphql_timeline_navigation_enabled": True,
        }
        field_toggles = {
            "withAuxiliaryUserLabels": False,
        }

        params = {
            "variables": json.dumps(variables, separators=(',', ':')),
            "features": json.dumps(features, separators=(',', ':')),
            "fieldToggles": json.dumps(field_toggles, separators=(',', ':')),
        }

        resp = await self._client.get(USER_BY_SCREEN_NAME_URL, params=params)
        self._check_response(resp, f"获取用户 @{handle} 信息")

        data = resp.json()
        try:
            user_result = data["data"]["user"]["result"]
            return user_result["rest_id"]
        except (KeyError, TypeError):
            raise RuntimeError(f"找不到用户 @{handle}，请检查用户名是否正确")

    async def _fetch_user_tweets_page(
        self, user_id: str, cursor: Optional[str] = None, count: int = 40
    ) -> tuple[list[dict], Optional[str]]:
        """
        获取一页用户推文

        Returns:
            (推文列表, 下一页cursor 或 None)
        """
        variables = {
            "userId": user_id,
            "count": count,
            "includePromotedContent": False,
            "withQuickPromoteEligibilityTweetFields": True,
            "withVoice": True,
            "withV2Timeline": True,
        }
        if cursor:
            variables["cursor"] = cursor

        features = {
            "rweb_tipjar_consumption_enabled": True,
            "responsive_web_graphql_exclude_directive_enabled": True,
            "verified_phone_label_enabled": False,
            "creator_subscriptions_tweet_preview_api_enabled": True,
            "responsive_web_graphql_timeline_navigation_enabled": True,
            "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
            "communities_web_enable_tweet_community_results_fetch": True,
            "c9s_tweet_anatomy_moderator_badge_enabled": True,
            "articles_preview_enabled": True,
            "responsive_web_edit_tweet_api_enabled": True,
            "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
            "view_counts_everywhere_api_enabled": True,
            "longform_notetweets_consumption_enabled": True,
            "responsive_web_twitter_article_tweet_consumption_enabled": True,
            "tweet_awards_web_tipping_enabled": False,
            "creator_subscriptions_quote_tweet_preview_enabled": False,
            "freedom_of_speech_not_reach_fetch_enabled": True,
            "standardized_nudges_misinfo": True,
            "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
            "rweb_video_timestamps_enabled": True,
            "longform_notetweets_rich_text_read_enabled": True,
            "longform_notetweets_inline_media_enabled": True,
            "responsive_web_enhance_cards_enabled": False,
        }
        field_toggles = {
            "withArticlePlainText": False,
        }

        params = {
            "variables": json.dumps(variables, separators=(',', ':')),
            "features": json.dumps(features, separators=(',', ':')),
            "fieldToggles": json.dumps(field_toggles, separators=(',', ':')),
        }

        resp = await self._client.get(USER_TWEETS_URL, params=params)
        self._check_response(resp, "获取推文")

        data = resp.json()
        tweets = []
        next_cursor = None

        try:
            timeline = data["data"]["user"]["result"]["timeline_v2"]["timeline"]
            instructions = timeline["instructions"]
        except (KeyError, TypeError):
            return tweets, None

        for instruction in instructions:
            # 寻找 TimelineAddEntries 类型的指令
            if instruction.get("type") not in ("TimelineAddEntries", "TimelineAddToModule"):
                continue

            entries = instruction.get("entries", [])
            for entry in entries:
                entry_id = entry.get("entryId", "")

                # 处理游标
                if "cursor-bottom" in entry_id:
                    try:
                        next_cursor = entry["content"]["value"]
                    except (KeyError, TypeError):
                        pass
                    continue

                if "cursor-top" in entry_id:
                    continue

                # 处理推文
                if entry_id.startswith("tweet-"):
                    tweet_data = self._extract_tweet_from_entry(entry)
                    if tweet_data:
                        tweets.append(tweet_data)

                # 处理会话模块（某些推文在 conversationThread 中）
                elif entry_id.startswith("profile-conversation-"):
                    items = entry.get("content", {}).get("items", [])
                    for item in items:
                        tweet_data = self._extract_tweet_from_item(item)
                        if tweet_data:
                            tweets.append(tweet_data)

        return tweets, next_cursor

    def _extract_tweet_from_entry(self, entry: dict) -> Optional[dict]:
        """从 entry 中提取推文数据"""
        try:
            content = entry.get("content", {})
            item_content = content.get("itemContent", {})
            return self._parse_tweet_result(item_content)
        except (KeyError, TypeError):
            return None

    def _extract_tweet_from_item(self, item: dict) -> Optional[dict]:
        """从 item 中提取推文数据"""
        try:
            item_content = item.get("item", {}).get("itemContent", {})
            return self._parse_tweet_result(item_content)
        except (KeyError, TypeError):
            return None

    def _parse_tweet_result(self, item_content: dict) -> Optional[dict]:
        """解析推文结果为标准格式"""
        tweet_results = item_content.get("tweet_results", {})
        result = tweet_results.get("result", {})

        # 处理 TweetWithVisibilityResults 的情况
        if result.get("__typename") == "TweetWithVisibilityResults":
            result = result.get("tweet", {})

        if result.get("__typename") != "Tweet":
            return None

        # 提取核心信息
        legacy = result.get("legacy", {})
        core = result.get("core", {})

        # 跳过转推
        if legacy.get("retweeted_status_result"):
            return None

        tweet_id = legacy.get("id_str", "")
        full_text = legacy.get("full_text", "")
        created_at = legacy.get("created_at", "")

        # 提取用户 handle
        handle = ""
        try:
            user_legacy = core["user_results"]["result"]["legacy"]
            handle = user_legacy.get("screen_name", "")
        except (KeyError, TypeError):
            pass

        # 提取互动数据
        likes = legacy.get("favorite_count", 0) or 0
        retweets = legacy.get("retweet_count", 0) or 0
        replies = legacy.get("reply_count", 0) or 0

        # 转换时间格式
        time_str = ""
        if created_at:
            try:
                dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
                time_str = dt.isoformat()
            except (ValueError, TypeError):
                time_str = created_at

        return {
            "id": tweet_id,
            "handle": handle,
            "text": full_text,
            "time": time_str,
            "metrics": {
                "likes": likes,
                "retweets": retweets,
                "replies": replies,
            },
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    def _check_response(self, resp: httpx.Response, context: str = ""):
        """检查响应状态码，处理常见错误"""
        if resp.status_code == 200:
            return

        if resp.status_code == 401:
            raise RuntimeError(
                "Twitter 认证失败（401）：auth_token 或 ct0 无效/已过期。\n"
                "请到【设置】页面重新填写有效的 auth_token 和 ct0。"
            )
        elif resp.status_code == 403:
            raise RuntimeError(
                "Twitter 访问被拒绝（403）：可能是 ct0 无效或账号被限制。\n"
                "请到浏览器重新获取 auth_token 和 ct0 后更新。"
            )
        elif resp.status_code == 429:
            raise RuntimeError(
                "Twitter 请求过于频繁（429）：已触发限流。\n"
                "请稍等 5-10 分钟后重试。如频繁出现，可适当增大抓取间隔。"
            )
        else:
            body_preview = resp.text[:200] if resp.text else ""
            raise RuntimeError(
                f"{context}失败（HTTP {resp.status_code}）：{body_preview}"
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
            # 获取用户ID
            user_id = await self._get_user_id(handle)

            tweets = []
            cursor = None
            fetched = 0
            empty_pages = 0  # 连续空页计数

            while fetched < count:
                page_tweets, next_cursor = await self._fetch_user_tweets_page(
                    user_id, cursor=cursor, count=min(40, count - fetched)
                )

                if not page_tweets:
                    empty_pages += 1
                    if empty_pages >= 2:
                        break
                else:
                    empty_pages = 0

                for tweet in page_tweets:
                    # 如果设了 since_id，跳过旧推文（用整数比较确保准确）
                    if since_id and int(tweet["id"]) <= int(since_id):
                        return tweets

                    # 确保 handle 正确
                    tweet["handle"] = handle
                    tweets.append(tweet)
                    fetched += 1

                    if fetched >= count:
                        break

                # 没有更多页了
                if not next_cursor:
                    break

                cursor = next_cursor

                # 随机延迟 2-5 秒，避免限流
                delay = random.uniform(2.0, 5.0)
                await asyncio.sleep(delay)

            return tweets

        except RuntimeError:
            raise
        except Exception as e:
            error_msg = str(e)
            raise RuntimeError(f"抓取 @{handle} 推文失败: {error_msg}")
        finally:
            await self._close_client()

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


async def verify_credentials(auth_token: str, ct0: str) -> dict:
    """
    验证 auth_token 和 ct0 是否有效
    使用 Twitter GraphQL Viewer 查询获取当前登录用户信息

    Returns:
        {"valid": True/False, "message": "...", "screen_name": "..."}
    """
    headers = {
        "authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
        "x-csrf-token": ct0,
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "zh-cn",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "content-type": "application/json",
        "accept": "*/*",
        "referer": "https://x.com/",
    }
    cookies = {
        "auth_token": auth_token,
        "ct0": ct0,
    }

    try:
        async with httpx.AsyncClient(
            headers=headers,
            cookies=cookies,
            timeout=15.0,
            follow_redirects=True,
        ) as client:
            # 使用 GraphQL Viewer 查询验证身份（获取当前登录用户信息）
            variables = {}
            features = {
                "hidden_profile_subscriptions_enabled": True,
                "rweb_tipjar_consumption_enabled": True,
                "responsive_web_graphql_exclude_directive_enabled": True,
                "verified_phone_label_enabled": False,
                "subscriptions_verification_info_is_identity_verified_enabled": True,
                "subscriptions_verification_info_verified_since_enabled": True,
                "highlights_tweets_tab_ui_enabled": True,
                "responsive_web_twitter_article_notes_tab_enabled": True,
                "subscriptions_feature_can_gift_premium": True,
                "creator_subscriptions_tweet_preview_api_enabled": True,
                "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
                "responsive_web_graphql_timeline_navigation_enabled": True,
            }

            params = {
                "variables": json.dumps(variables, separators=(',', ':')),
                "features": json.dumps(features, separators=(',', ':')),
            }

            # Viewer 查询端点
            resp = await client.get(
                f"{GRAPHQL_BASE}/LimHVMF2eqg_dgGVO5JQDA/Viewer",
                params=params,
            )

            if resp.status_code == 200:
                data = resp.json()
                try:
                    viewer = data["data"]["viewer"]
                    user_results = viewer.get("user_results", {})
                    result = user_results.get("result", {})
                    legacy = result.get("legacy", {})
                    screen_name = legacy.get("screen_name", "")

                    if screen_name:
                        return {
                            "valid": True,
                            "message": f"验证成功，当前登录账号: @{screen_name}",
                            "screen_name": screen_name,
                        }
                    else:
                        # viewer 存在但没有 user_results，可能是受限账号
                        return {
                            "valid": True,
                            "message": "验证成功（账号信息受限，但凭证有效）",
                            "screen_name": "unknown",
                        }
                except (KeyError, TypeError):
                    # 如果解析失败但 200，说明凭证有效只是返回格式变了
                    return {
                        "valid": True,
                        "message": "验证成功（凭证有效）",
                        "screen_name": "unknown",
                    }
            elif resp.status_code == 401:
                return {"valid": False, "message": "auth_token 或 ct0 无效/已过期"}
            elif resp.status_code == 403:
                return {"valid": False, "message": "访问被拒绝，ct0 可能不匹配或已过期"}
            else:
                return {"valid": False, "message": f"验证失败（HTTP {resp.status_code}）"}
    except httpx.TimeoutException:
        return {"valid": False, "message": "连接超时，请检查网络（需要能访问 x.com）"}
    except Exception as e:
        return {"valid": False, "message": f"验证出错: {e}"}


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
    parse_errors = 0
    with open(raw_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if line:
                try:
                    tweets.append(json.loads(line))
                except json.JSONDecodeError:
                    parse_errors += 1

    if parse_errors > 0:
        print(f"  @{handle} 的推文文件中有 {parse_errors} 行数据损坏（已跳过）")

    return tweets
