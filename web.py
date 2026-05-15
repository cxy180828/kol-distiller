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

    def __init__(self):
        self.tasks: dict[str, dict] = {}  # task_id -> {status, progress, result, error}

    def create_task(self, task_type: str, description: str) -> str:
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
app.add_middleware(SessionMiddleware, secret_key=secrets.token_hex(32))

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
        config = load_config()
        # 密码存在config.yaml里的web.password字段
        correct_password = getattr(config, 'web_password', None)
        if correct_password is None:
            # 从yaml原始数据读
            import yaml
            with open(ROOT_DIR / "config.yaml", "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            correct_password = raw.get("web", {}).get("password", "admin")

        if password == correct_password:
            request.session["authenticated"] = True
            return RedirectResponse("/", status_code=302)
        else:
            return RedirectResponse("/login?error=密码错误", status_code=302)
    except Exception as e:
        return RedirectResponse(f"/login?error={e}", status_code=302)


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

    handle = handle.lstrip("@")
    kol_dir = get_kol_dir(handle)
    meta_path = kol_dir / "meta.json"

    if not meta_path.exists():
        raise HTTPException(404, f"@{handle} 不存在")

    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

    profile_text = ""
    if has_profile(handle):
        profile_text = load_profile(handle)

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

    filepath = DISCUSSIONS_DIR / filename
    if not filepath.exists():
        raise HTTPException(404, "讨论记录不存在")

    content = filepath.read_text(encoding="utf-8")

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
    config_text = ""
    if config_path.exists():
        config_text = config_path.read_text(encoding="utf-8")

    return render(request, "settings.html", {
        "config_text": config_text,
    })


# === API 路由 ===

@app.post("/api/kol/add")
async def api_add_kol(request: Request, handle: str = Form(...)):
    if not check_auth(request):
        return JSONResponse({"error": "未登录"}, status_code=401)

    handle = handle.lstrip("@").strip()
    if not handle:
        return JSONResponse({"error": "handle不能为空"}, status_code=400)

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
    handle = handle.lstrip("@")
    kol_dir = get_kol_dir(handle)
    if kol_dir.exists():
        shutil.rmtree(kol_dir)
    return JSONResponse({"message": f"@{handle} 已删除"})


@app.post("/api/discuss")
async def api_discuss(request: Request, coin: str = Form(...), kols: str = Form("")):
    if not check_auth(request):
        return JSONResponse({"error": "未登录"}, status_code=401)

    coin = coin.upper().strip()
    selected_kols = [k.strip() for k in kols.split(",") if k.strip()] or None

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


@app.post("/api/settings/save")
async def api_save_settings(request: Request, config_text: str = Form(...)):
    if not check_auth(request):
        return JSONResponse({"error": "未登录"}, status_code=401)

    import yaml
    # 验证YAML格式
    try:
        yaml.safe_load(config_text)
    except yaml.YAMLError as e:
        return JSONResponse({"error": f"YAML格式错误: {e}"}, status_code=400)

    config_path = ROOT_DIR / "config.yaml"
    # 备份
    backup_path = ROOT_DIR / "config.yaml.bak"
    if config_path.exists():
        import shutil
        shutil.copy2(config_path, backup_path)

    config_path.write_text(config_text, encoding="utf-8")
    return JSONResponse({"message": "配置已保存"})


# === 启动 ===

if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="KOL Distiller Web UI")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=8080, help="端口")
    args = parser.parse_args()

    print(f"\n🚀 KOL Distiller Web UI")
    print(f"   访问: http://{args.host}:{args.port}")
    print(f"   按 Ctrl+C 停止\n")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
