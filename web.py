#!/usr/bin/env python3
"""
KOL Distiller - Web UI

FastAPI 后端 + Jinja2 模板渲染
启动: python web.py [--port 8080] [--host 0.0.0.0]
"""

import os
import sys
import json
import asyncio
import hashlib
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, HTTPException, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware


def render(request: Request, template_name: str, context: dict = None):
    """兼容新旧版本Starlette的模板渲染"""
    ctx = context or {}
    ctx["request"] = request
    return templates.TemplateResponse(request, template_name, ctx)

from src.config import (
    load_config, ensure_dirs, list_kols, get_kol_dir,
    AppConfig, ROOT_DIR, DISCUSSIONS_DIR,
)
from src.scraper import TweetScraper, save_tweets
from src.classifier import (
    TweetClassifier, save_tagged_tweets,
    load_tagged_tweets, count_recent_trade_tweets,
)
from src.distiller import (
    ProfileDistiller, save_profile, load_profile, has_profile,
)
from src.discussion import DiscussionEngine
from src.market_data import MarketDataClient


# === 后台任务管理 ===

class TaskManager:
    """管理后台异步任务"""

    MAX_TASKS = 200  # 最多保留200条任务记录

    def __init__(self):
        self.tasks: dict[str, dict] = {}  # task_id -> {status, progress, result, error}

    def _cleanup_old_tasks(self):
        """清理超出限制的旧任务"""
        if len(self.tasks) < self.MAX_TASKS:
            return
        # 按创建时间排序，删除最旧的已完成任务
        completed = [
            tid for tid, t in self.tasks.items()
            if t["status"] in ("completed", "failed")
        ]
        completed.sort(key=lambda tid: self.tasks[tid]["created_at"])
        # 删除一半旧任务腾出空间
        to_remove = completed[:max(len(completed) // 2, 1)]
        for tid in to_remove:
            del self.tasks[tid]

    def create_task(self, task_type: str, description: str) -> str:
        self._cleanup_old_tasks()
        task_id = secrets.token_hex(8)
        self.tasks[task_id] = {
            "id": task_id,
            "type": task_type,
            "description": description,
            "status": "running",  # running / completed / failed
            "progress": "",
            "result": None,
            "error": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
        }
        return task_id

    def update_progress(self, task_id: str, progress: str):
        if task_id in self.tasks:
            self.tasks[task_id]["progress"] = progress

    def complete_task(self, task_id: str, result: str = None):
        if task_id in self.tasks:
            self.tasks[task_id]["status"] = "completed"
            self.tasks[task_id]["result"] = result
            self.tasks[task_id]["completed_at"] = datetime.now(timezone.utc).isoformat()

    def fail_task(self, task_id: str, error: str):
        if task_id in self.tasks:
            self.tasks[task_id]["status"] = "failed"
            self.tasks[task_id]["error"] = error
            self.tasks[task_id]["completed_at"] = datetime.now(timezone.utc).isoformat()

    def get_task(self, task_id: str) -> Optional[dict]:
        return self.tasks.get(task_id)

    def get_recent_tasks(self, limit: int = 20) -> list[dict]:
        tasks = sorted(
            self.tasks.values(),
            key=lambda t: t["created_at"],
            reverse=True,
        )
        return tasks[:limit]

    def get_notifications(self) -> list[dict]:
        """获取最近完成/失败的任务作为通知"""
        return [
            t for t in self.get_recent_tasks(50)
            if t["status"] in ("completed", "failed")
        ]


task_manager = TaskManager()


# === App 初始化 ===

@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    yield

app = FastAPI(title="KOL Distiller", lifespan=lifespan)

# session secret固定化：从文件读取或首次生成后保存
_session_secret_path = ROOT_DIR / ".session_secret"
if _session_secret_path.exists():
    _session_secret = _session_secret_path.read_text().strip()
else:
    _session_secret = secrets.token_hex(32)
    _session_secret_path.write_text(_session_secret)

app.add_middleware(SessionMiddleware, secret_key=_session_secret)

# 静态文件和模板
STATIC_DIR = ROOT_DIR / "web_static"
TEMPLATES_DIR = ROOT_DIR / "web_templates"
STATIC_DIR.mkdir(exist_ok=True)
TEMPLATES_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# === 认证 ===

def get_config() -> AppConfig:
    return load_config()


def check_auth(request: Request) -> bool:
    """检查是否已登录"""
    return request.session.get("authenticated", False)


def require_auth(request: Request):
    """要求登录，否则重定向"""
    if not check_auth(request):
        raise HTTPException(status_code=302, headers={"Location": "/login"})


# === 页面路由 ===

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = None):
    if check_auth(request):
        return RedirectResponse("/", status_code=302)
    return render(request, "login.html", {
        "error": error,
    })


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    try:
        correct_password = "admin"

        config_path = ROOT_DIR / "config.yaml"
        if config_path.exists():
            import yaml
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    raw = yaml.safe_load(f) or {}
                correct_password = raw.get("web", {}).get("password", "admin")
            except Exception:
                # 配置文件读取失败时使用默认密码
                pass

        if not password.strip():
            return RedirectResponse("/login?error=请输入密码", status_code=302)

        if password == correct_password:
            request.session["authenticated"] = True
            return RedirectResponse("/", status_code=302)
        else:
            return RedirectResponse("/login?error=密码错误，请重试", status_code=302)
    except Exception as e:
        return RedirectResponse(f"/login?error=登录异常，请稍后重试", status_code=302)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)

    kols = list_kols()
    kol_data = []
    for handle in kols:
        kol_dir = get_kol_dir(handle)
        meta_path = kol_dir / "meta.json"
        meta = {}
        if meta_path.exists():
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)

        kol_data.append({
            "handle": handle,
            "has_profile": has_profile(handle),
            "total_tweets": meta.get("total_tweets", 0),
            "last_fetch": meta.get("last_fetch_time", "")[:16] if meta.get("last_fetch_time") else "未抓取",
            "trade_count_7d": count_recent_trade_tweets(handle, days=7),
        })

    notifications = task_manager.get_notifications()[:5]

    return render(request, "dashboard.html", {
        "kols": kol_data,
        "notifications": notifications,
        "kol_count": len(kols),
    })


@app.get("/kol/{handle}", response_class=HTMLResponse)
async def kol_detail(request: Request, handle: str):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)

    handle = handle.lstrip("@").strip()
    if not handle:
        raise HTTPException(400, "KOL handle不能为空")

    kol_dir = get_kol_dir(handle)
    meta_path = kol_dir / "meta.json"

    if not meta_path.exists():
        raise HTTPException(404, f"@{handle} 不存在，请先通过「添加KOL」功能添加")

    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except json.JSONDecodeError:
        raise HTTPException(500, f"@{handle} 的元数据文件损坏，请尝试重新添加该KOL")

    profile_text = ""
    if has_profile(handle):
        try:
            profile_text = load_profile(handle)
        except Exception:
            profile_text = "（Profile文件读取失败）"

    # 分类统计
    tagged = load_tagged_tweets(handle)
    categories = {}
    for t in tagged:
        cat = t.get("category", "noise")
        categories[cat] = categories.get(cat, 0) + 1

    return render(request, "kol_detail.html", {
        "handle": handle,
        "meta": meta,
        "profile": profile_text,
        "has_profile": has_profile(handle),
        "categories": categories,
        "total_tagged": len(tagged),
        "trade_count_7d": count_recent_trade_tweets(handle, days=7),
    })


@app.get("/discuss", response_class=HTMLResponse)
async def discuss_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)

    kols = list_kols()
    available_kols = [h for h in kols if has_profile(h)]

    return render(request, "discuss.html", {
        "available_kols": available_kols,
    })


@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)

    discussions = []
    if DISCUSSIONS_DIR.exists():
        for f in sorted(DISCUSSIONS_DIR.iterdir(), reverse=True):
            if f.suffix == ".md":
                discussions.append({
                    "filename": f.name,
                    "date": f.name[:10],
                    "coin": f.name.split("_")[-1].replace(".md", ""),
                    "size": f.stat().st_size,
                })

    return render(request, "history.html", {
        "discussions": discussions[:50],
    })


@app.get("/history/{filename}", response_class=HTMLResponse)
async def history_detail(request: Request, filename: str):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)

    # 防止目录遍历攻击
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "文件名无效")

    filepath = DISCUSSIONS_DIR / filename
    if not filepath.exists():
        raise HTTPException(404, "讨论记录不存在，可能已被删除")

    try:
        content = filepath.read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"读取讨论记录失败: {e}")

    return render(request, "history_detail.html", {
        "filename": filename,
        "content": content,
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)

    import yaml
    config_path = ROOT_DIR / "config.yaml"
    config_data = {}
    config_error = None

    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raw = {}
            config_error = f"配置文件格式错误，请检查YAML语法: {e}"
        except Exception as e:
            raw = {}
            config_error = f"读取配置文件失败: {e}"

        # 扁平化配置供模板使用
        llm = raw.get("llm", {}) if isinstance(raw, dict) else {}
        twitter = raw.get("twitter", {}) if isinstance(raw, dict) else {}
        market = raw.get("market_data", {}) if isinstance(raw, dict) else {}
        schedule = raw.get("schedule", {}) if isinstance(raw, dict) else {}
        distill = raw.get("distill", {}) if isinstance(raw, dict) else {}
        web = raw.get("web", {}) if isinstance(raw, dict) else {}

        config_data = {
            "llm_base_url": llm.get("base_url", ""),
            "llm_api_key": llm.get("api_key", ""),
            "llm_model": llm.get("model", ""),
            "llm_temperature_classify": llm.get("temperature_classify", 0.1),
            "llm_temperature_distill": llm.get("temperature_distill", 0.3),
            "llm_temperature_discuss": llm.get("temperature_discuss", 0.7),
            "twitter_username": twitter.get("username", ""),
            "twitter_password": twitter.get("password", ""),
            "twitter_email": twitter.get("email", ""),
            "market_data_source": market.get("source", "binance"),
            "market_data_base_url": market.get("base_url", "https://api.binance.com"),
            "schedule_fetch_interval_hours": schedule.get("fetch_interval_hours", 6),
            "schedule_distill_day": schedule.get("distill_day", 6),
            "schedule_early_distill_threshold": schedule.get("early_distill_threshold", 15),
            "distill_lookback_days": distill.get("lookback_days", 30),
            "distill_initial_fetch_count": distill.get("initial_fetch_count", 500),
            "distill_batch_size": distill.get("batch_size", 10),
            "web_password": web.get("password", ""),
            "web_port": web.get("port", 8088),
        }

    return render(request, "settings.html", {
        "config": config_data,
        "config_error": config_error,
    })


# === API 路由 ===

@app.post("/api/kol/add")
async def api_add_kol(request: Request, handle: str = Form(...)):
    if not check_auth(request):
        return JSONResponse({"error": "未登录"}, status_code=401)

    handle = handle.lstrip("@").strip()
    if not handle:
        return JSONResponse({"error": "handle不能为空"}, status_code=400)

    # 基本格式校验
    if len(handle) > 50:
        return JSONResponse({"error": "handle过长，请输入正确的Twitter用户名"}, status_code=400)
    if " " in handle:
        return JSONResponse({"error": "handle不能包含空格"}, status_code=400)

    # 检查是否已存在
    existing_kols = list_kols()
    if handle in existing_kols:
        return JSONResponse({"error": f"@{handle} 已存在，如需更新请使用「更新」按钮"}, status_code=400)

    # 创建后台任务
    task_id = task_manager.create_task("add_kol", f"添加 @{handle}")

    async def run_add():
        try:
            config = load_config()
            ensure_dirs()

            # Step 1: 抓取
            task_manager.update_progress(task_id, "正在抓取推文...")
            scraper = TweetScraper(config)
            tweets = await scraper.fetch_initial(handle)
            if not tweets:
                task_manager.fail_task(task_id, "未获取到推文，请检查handle")
                return

            save_tweets(handle, tweets)
            task_manager.update_progress(task_id, f"抓取完成({len(tweets)}条)，正在分类...")

            # Step 2: 分类
            classifier = TweetClassifier(config)
            tagged = await classifier.classify_tweets(tweets)
            save_tagged_tweets(handle, tagged)
            task_manager.update_progress(task_id, "分类完成，正在蒸馏...")

            # Step 3: 蒸馏
            distiller = ProfileDistiller(config)
            profile = await distiller.distill(handle)
            save_profile(handle, profile)

            task_manager.complete_task(task_id, f"@{handle} 添加成功（{len(tweets)}条推文）")
        except Exception as e:
            task_manager.fail_task(task_id, str(e))

    asyncio.create_task(run_add())
    return JSONResponse({"task_id": task_id, "message": f"正在后台添加 @{handle}"})


@app.post("/api/kol/{handle}/update")
async def api_update_kol(request: Request, handle: str):
    if not check_auth(request):
        return JSONResponse({"error": "未登录"}, status_code=401)

    handle = handle.lstrip("@")
    task_id = task_manager.create_task("update_kol", f"更新 @{handle}")

    async def run_update():
        try:
            config = load_config()

            task_manager.update_progress(task_id, "正在抓取新推文...")
            scraper = TweetScraper(config)
            tweets = await scraper.fetch_incremental(handle)

            if tweets:
                save_tweets(handle, tweets)
                task_manager.update_progress(task_id, f"抓取{len(tweets)}条，正在分类...")
                classifier = TweetClassifier(config)
                tagged = await classifier.classify_tweets(tweets)
                save_tagged_tweets(handle, tagged)

            task_manager.update_progress(task_id, "正在重新蒸馏...")
            distiller = ProfileDistiller(config)
            profile = await distiller.distill(handle)
            save_profile(handle, profile)

            msg = f"@{handle} 更新完成"
            if tweets:
                msg += f"（新增{len(tweets)}条推文）"
            task_manager.complete_task(task_id, msg)
        except Exception as e:
            task_manager.fail_task(task_id, str(e))

    asyncio.create_task(run_update())
    return JSONResponse({"task_id": task_id})


@app.post("/api/kol/{handle}/distill")
async def api_distill_kol(request: Request, handle: str):
    if not check_auth(request):
        return JSONResponse({"error": "未登录"}, status_code=401)

    handle = handle.lstrip("@")
    task_id = task_manager.create_task("distill_kol", f"重新蒸馏 @{handle}")

    async def run_distill():
        try:
            config = load_config()
            distiller = ProfileDistiller(config)
            profile = await distiller.distill(handle)
            save_profile(handle, profile)
            task_manager.complete_task(task_id, f"@{handle} 蒸馏完成")
        except Exception as e:
            task_manager.fail_task(task_id, str(e))

    asyncio.create_task(run_distill())
    return JSONResponse({"task_id": task_id})


@app.post("/api/kol/{handle}/delete")
async def api_delete_kol(request: Request, handle: str):
    if not check_auth(request):
        return JSONResponse({"error": "未登录"}, status_code=401)

    import shutil
    handle = handle.lstrip("@").strip()

    if not handle:
        return JSONResponse({"error": "handle不能为空"}, status_code=400)

    kol_dir = get_kol_dir(handle)
    if not kol_dir.exists() or not (kol_dir / "meta.json").exists():
        return JSONResponse({"error": f"@{handle} 不存在，无法删除"}, status_code=404)

    try:
        shutil.rmtree(kol_dir)
    except PermissionError:
        return JSONResponse({"error": f"删除 @{handle} 失败：权限不足，请检查文件权限"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": f"删除 @{handle} 失败: {e}"}, status_code=500)

    return JSONResponse({"message": f"@{handle} 已删除"})


@app.post("/api/discuss")
async def api_discuss(request: Request, coin: str = Form(...), kols: str = Form("")):
    if not check_auth(request):
        return JSONResponse({"error": "未登录"}, status_code=401)

    coin = coin.upper().strip()
    if not coin:
        return JSONResponse({"error": "请输入要讨论的币种（如BTC、ETH）"}, status_code=400)

    if len(coin) > 20:
        return JSONResponse({"error": "币种名称过长，请输入正确的币种代码"}, status_code=400)

    selected_kols = [k.strip() for k in kols.split(",") if k.strip()] or None

    # 检查是否有可用的KOL
    all_kols = list_kols()
    available_kols = [h for h in all_kols if has_profile(h)]
    if not available_kols:
        return JSONResponse({"error": "没有已蒸馏的KOL可以参与讨论，请先添加并蒸馏至少一个KOL"}, status_code=400)

    task_id = task_manager.create_task("discuss", f"讨论 {coin}")

    async def run_discuss():
        try:
            config = load_config()
            engine = DiscussionEngine(config)
            task_manager.update_progress(task_id, "正在获取行情数据...")
            result = await engine.discuss(coin, selected_kols)
            task_manager.complete_task(task_id, result)
        except Exception as e:
            task_manager.fail_task(task_id, str(e))

    asyncio.create_task(run_discuss())
    return JSONResponse({"task_id": task_id})


@app.get("/api/task/{task_id}")
async def api_task_status(request: Request, task_id: str):
    if not check_auth(request):
        return JSONResponse({"error": "未登录"}, status_code=401)

    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return JSONResponse(task)


@app.get("/api/notifications")
async def api_notifications(request: Request):
    if not check_auth(request):
        return JSONResponse({"error": "未登录"}, status_code=401)

    return JSONResponse(task_manager.get_recent_tasks(10))


# Twitter登录状态管理
_twitter_login_state = {"client": None, "status": "idle", "error": None}


@app.post("/api/twitter/login")
async def api_twitter_login(request: Request):
    """第一步：发起Twitter登录（输入用户名密码）"""
    if not check_auth(request):
        return JSONResponse({"error": "未登录"}, status_code=401)

    form = await request.form()
    username = form.get("username", "").strip()
    password = form.get("password", "").strip()
    email = form.get("email", "").strip()

    if not username or not password:
        return JSONResponse({"error": "用户名和密码不能为空"}, status_code=400)

    from twikit import Client

    try:
        client = Client("zh-CN")
        cookies_path = str(ROOT_DIR / "cookies.json")

        # 尝试登录，可能需要2FA
        try:
            await client.login(
                auth_info_1=username,
                auth_info_2=email,
                password=password,
                cookies_file=cookies_path,
            )
            # 登录成功，不需要2FA
            _twitter_login_state["client"] = None
            _twitter_login_state["status"] = "idle"
            return JSONResponse({"status": "success", "message": "✅ 登录成功，cookies.json 已生成"})
        except Exception as e:
            error_msg = str(e).lower()
            # 检测是否需要2FA
            if "confirmation" in error_msg or "challenge" in error_msg or "2fa" in error_msg or "totp" in error_msg or "verification" in error_msg:
                _twitter_login_state["client"] = client
                _twitter_login_state["status"] = "need_2fa"
                return JSONResponse({"status": "need_2fa", "message": "请输入2FA验证码"})
            elif "key_byte" in error_msg:
                return JSONResponse({
                    "error": (
                        "Twitter登录验证失败（KEY_BYTE错误）。\n"
                        "Twitter更新了前端加密，用户名密码登录暂时不可用。\n"
                        "请先在VPS执行: pip install --upgrade twikit\n"
                        "如仍报错，请用浏览器Cookie方式：在config.yaml中填写auth_token和ct0"
                    )
                }, status_code=400)
            else:
                return JSONResponse({"error": f"登录失败: {e}"}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"初始化失败: {e}"}, status_code=500)


@app.post("/api/twitter/verify_2fa")
async def api_twitter_verify_2fa(request: Request):
    """第二步：输入2FA验证码完成登录"""
    if not check_auth(request):
        return JSONResponse({"error": "未登录"}, status_code=401)

    form = await request.form()
    code = form.get("code", "").strip()

    if not code:
        return JSONResponse({"error": "验证码不能为空"}, status_code=400)

    client = _twitter_login_state.get("client")
    if client is None or _twitter_login_state["status"] != "need_2fa":
        return JSONResponse({"error": "请先发起登录"}, status_code=400)

    try:
        cookies_path = str(ROOT_DIR / "cookies.json")
        # twikit的2FA验证
        await client.totp_login(code, cookies_file=cookies_path)
        _twitter_login_state["client"] = None
        _twitter_login_state["status"] = "idle"
        return JSONResponse({"status": "success", "message": "✅ 2FA验证成功，cookies.json 已生成"})
    except Exception as e:
        _twitter_login_state["client"] = None
        _twitter_login_state["status"] = "idle"
        return JSONResponse({"error": f"2FA验证失败: {e}"}, status_code=400)


@app.get("/api/twitter/status")
async def api_twitter_status(request: Request):
    """检查Twitter登录状态"""
    if not check_auth(request):
        return JSONResponse({"error": "未登录"}, status_code=401)

    cookies_path = ROOT_DIR / "cookies.json"
    if cookies_path.exists():
        import os
        mtime = os.path.getmtime(cookies_path)
        from datetime import datetime
        login_time = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        return JSONResponse({
            "logged_in": True,
            "login_time": login_time,
            "cookies_file": str(cookies_path),
        })
    else:
        return JSONResponse({
            "logged_in": False,
            "message": "未登录Twitter，请先登录",
        })


@app.post("/api/settings/save")
async def api_save_settings(request: Request):
    if not check_auth(request):
        return JSONResponse({"error": "未登录"}, status_code=401)

    import yaml

    form = await request.form()

    # 安全转换数值类型的辅助函数
    def safe_float(value, default, field_name):
        try:
            return float(value) if value else default
        except (ValueError, TypeError):
            raise ValueError(f"字段 {field_name} 必须是数字，当前值 '{value}' 无效")

    def safe_int(value, default, field_name):
        try:
            return int(value) if value else default
        except (ValueError, TypeError):
            raise ValueError(f"字段 {field_name} 必须是整数，当前值 '{value}' 无效")

    try:
        # 从表单字段构建配置结构
        config_data = {
            "llm": {
                "base_url": form.get("llm_base_url", ""),
                "api_key": form.get("llm_api_key", ""),
                "model": form.get("llm_model", ""),
                "temperature_classify": safe_float(form.get("llm_temperature_classify"), 0.1, "分类温度"),
                "temperature_distill": safe_float(form.get("llm_temperature_distill"), 0.3, "蒸馏温度"),
                "temperature_discuss": safe_float(form.get("llm_temperature_discuss"), 0.7, "讨论温度"),
            },
            "twitter": {
                "username": form.get("twitter_username", ""),
                "password": form.get("twitter_password", ""),
                "email": form.get("twitter_email", ""),
            },
            "market_data": {
                "source": form.get("market_data_source", "binance"),
                "base_url": form.get("market_data_base_url", "https://api.binance.com"),
            },
            "schedule": {
                "fetch_interval_hours": safe_int(form.get("schedule_fetch_interval_hours"), 6, "抓取间隔"),
                "distill_day": safe_int(form.get("schedule_distill_day"), 6, "蒸馏日"),
                "early_distill_threshold": safe_int(form.get("schedule_early_distill_threshold"), 15, "提前蒸馏阈值"),
            },
            "distill": {
                "lookback_days": safe_int(form.get("distill_lookback_days"), 30, "回看天数"),
                "initial_fetch_count": safe_int(form.get("distill_initial_fetch_count"), 500, "首次拉取数量"),
                "batch_size": safe_int(form.get("distill_batch_size"), 10, "批量大小"),
            },
            "web": {
                "password": form.get("web_password", "admin"),
                "port": safe_int(form.get("web_port"), 8088, "端口"),
            },
        }

        # 数值范围校验
        if config_data["schedule"]["fetch_interval_hours"] < 1:
            return JSONResponse({"error": "抓取间隔至少为1小时"}, status_code=400)
        if not (0 <= config_data["schedule"]["distill_day"] <= 6):
            return JSONResponse({"error": "蒸馏日必须是0-6（0=周一，6=周日）"}, status_code=400)
        if config_data["distill"]["lookback_days"] < 1:
            return JSONResponse({"error": "回看天数至少为1天"}, status_code=400)
        if config_data["distill"]["batch_size"] < 1:
            return JSONResponse({"error": "批量大小至少为1"}, status_code=400)
        if not (1 <= config_data["web"]["port"] <= 65535):
            return JSONResponse({"error": "端口号必须在1-65535之间"}, status_code=400)

        for temp_key in ["temperature_classify", "temperature_distill", "temperature_discuss"]:
            val = config_data["llm"][temp_key]
            if not (0.0 <= val <= 2.0):
                return JSONResponse({"error": f"温度参数必须在0-2之间，当前{temp_key}={val}"}, status_code=400)

    except ValueError as e:
        return JSONResponse({"error": f"配置参数格式错误: {e}"}, status_code=400)

    config_path = ROOT_DIR / "config.yaml"
    # 备份旧配置
    backup_path = ROOT_DIR / "config.yaml.bak"
    if config_path.exists():
        import shutil
        shutil.copy2(config_path, backup_path)

    # 写入新配置
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    except Exception as e:
        # 如果写入失败，尝试恢复备份
        if backup_path.exists():
            import shutil
            shutil.copy2(backup_path, config_path)
        return JSONResponse({"error": f"配置写入失败: {e}，已恢复旧配置"}, status_code=500)

    return JSONResponse({"message": "配置已保存"})


# === 启动 ===

if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="KOL Distiller Web UI")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=8088, help="端口")
    args = parser.parse_args()

    print(f"\n🚀 KOL Distiller Web UI")
    print(f"   访问: http://{args.host}:{args.port}")
    print(f"   按 Ctrl+C 停止\n")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
