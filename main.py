#!/usr/bin/env python3
"""
KOL Distiller - 主入口

用法:
  python main.py add @handle          添加KOL（抓取历史+分类+蒸馏）
  python main.py discuss BTC          让所有KOL Agent讨论某标的
  python main.py discuss BTC @a @b    指定KOL参与讨论
  python main.py update @handle       手动更新某KOL（增量抓取+分类+重新蒸馏）
  python main.py update-all           更新所有KOL（增量抓取+分类）
  python main.py distill @handle      只重新蒸馏某KOL（不抓取新推文）
  python main.py distill-all          重新蒸馏所有KOL
  python main.py list                 列出所有已添加的KOL
  python main.py status @handle       查看某KOL的状态
  python main.py cron                 执行定时任务（供cron调用）
"""

import sys
import asyncio
from rich.console import Console
from rich.table import Table

from src.config import load_config, ensure_dirs, list_kols, get_kol_dir
from src.scraper import TweetScraper, save_tweets
from src.classifier import TweetClassifier, save_tagged_tweets, count_recent_trade_tweets
from src.distiller import ProfileDistiller, save_profile, has_profile, load_profile
from src.discussion import DiscussionEngine

console = Console()


async def cmd_add(handle: str):
    """添加一个新KOL"""
    handle = handle.lstrip("@")
    config = load_config()
    ensure_dirs()

    console.print(f"\n[bold green]➕ 添加KOL: @{handle}[/bold green]\n")

    # Step 1: 抓取历史推文
    console.print("[bold]Step 1/3: 抓取历史推文[/bold]")
    scraper = TweetScraper(config)
    try:
        tweets = await scraper.fetch_initial(handle)
    except Exception as e:
        console.print(f"[red]❌ 抓取失败: {e}[/red]")
        console.print("[yellow]提示: 请检查Twitter Cookie是否有效（auth_token和ct0）[/yellow]")
        return

    if not tweets:
        console.print("[red]❌ 未获取到任何推文，请检查handle是否正确[/red]")
        return

    save_tweets(handle, tweets)
    console.print(f"  ✅ 保存 {len(tweets)} 条推文\n")

    # Step 2: 分类标注
    console.print("[bold]Step 2/3: AI分类标注[/bold]")
    classifier = TweetClassifier(config)
    tagged = await classifier.classify_tweets(tweets)
    save_tagged_tweets(handle, tagged)

    # 统计分类结果
    categories = {}
    for t in tagged:
        cat = t.get("category", "noise")
        categories[cat] = categories.get(cat, 0) + 1

    for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
        console.print(f"  {cat}: {count}条")
    console.print()

    # Step 3: 蒸馏Profile
    console.print("[bold]Step 3/3: 蒸馏交易画像[/bold]")
    distiller = ProfileDistiller(config)
    profile = await distiller.distill(handle)
    save_profile(handle, profile)

    console.print(f"\n[bold green]✅ @{handle} 添加完成！[/bold green]")
    console.print(f"  Profile 已保存到: kols/{handle}/profile.md")
    console.print(f"  可以使用 [bold]python main.py discuss BTC[/bold] 让他参与讨论\n")


async def cmd_discuss(coin: str, handles: list[str] | None = None):
    """发起讨论"""
    config = load_config()
    ensure_dirs()

    if handles:
        handles = [h.lstrip("@") for h in handles]

    console.print(f"\n[bold cyan]💬 发起讨论: {coin.upper()}[/bold cyan]")

    engine = DiscussionEngine(config)
    result = await engine.discuss(coin, handles)

    console.print("\n" + "=" * 60)
    console.print(result)
    console.print("=" * 60 + "\n")


async def cmd_update(handle: str):
    """更新某个KOL（增量抓取+分类+重新蒸馏）"""
    handle = handle.lstrip("@")
    config = load_config()
    ensure_dirs()

    console.print(f"\n[bold yellow]🔄 更新 @{handle}[/bold yellow]\n")

    # 增量抓取
    console.print("[bold]Step 1/3: 增量抓取推文[/bold]")
    scraper = TweetScraper(config)
    try:
        tweets = await scraper.fetch_incremental(handle)
    except Exception as e:
        console.print(f"[red]❌ 抓取失败: {e}[/red]")
        console.print("[yellow]提示: 请检查Twitter Cookie是否有效[/yellow]")
        return

    if tweets:
        save_tweets(handle, tweets)

        # 分类新推文
        console.print("[bold]Step 2/3: 分类新推文[/bold]")
        classifier = TweetClassifier(config)
        tagged = await classifier.classify_tweets(tweets)
        save_tagged_tweets(handle, tagged)
    else:
        console.print("  没有新推文\n")

    # 重新蒸馏
    console.print("[bold]Step 3/3: 重新蒸馏[/bold]")
    distiller = ProfileDistiller(config)
    profile = await distiller.distill(handle)
    save_profile(handle, profile)

    console.print(f"\n[bold green]✅ @{handle} 更新完成[/bold green]\n")


async def cmd_update_all():
    """更新所有KOL（增量抓取+分类，不重新蒸馏）"""
    config = load_config()
    ensure_dirs()

    kols = list_kols()
    if not kols:
        console.print("[yellow]没有已添加的KOL[/yellow]")
        return

    console.print(f"\n[bold yellow]🔄 批量更新 {len(kols)} 个KOL[/bold yellow]\n")

    scraper = TweetScraper(config)
    classifier = TweetClassifier(config)

    for handle in kols:
        console.print(f"\n--- @{handle} ---")
        try:
            tweets = await scraper.fetch_incremental(handle)
            if tweets:
                save_tweets(handle, tweets)
                tagged = await classifier.classify_tweets(tweets)
                save_tagged_tweets(handle, tagged)
                console.print(f"  ✅ {len(tweets)}条新推文已处理")
            else:
                console.print(f"  没有新推文")
        except Exception as e:
            console.print(f"  [red]❌ 失败: {e}[/red]")

        # KOL之间间隔，避免限流
        await asyncio.sleep(2)

    console.print(f"\n[bold green]✅ 批量更新完成[/bold green]\n")


async def cmd_distill(handle: str):
    """只重新蒸馏（不抓取）"""
    handle = handle.lstrip("@")
    config = load_config()
    ensure_dirs()

    console.print(f"\n[bold magenta]🧪 重新蒸馏 @{handle}[/bold magenta]\n")

    distiller = ProfileDistiller(config)
    profile = await distiller.distill(handle)
    save_profile(handle, profile)

    console.print(f"\n[bold green]✅ 蒸馏完成[/bold green]\n")


async def cmd_distill_all():
    """重新蒸馏所有KOL"""
    config = load_config()
    ensure_dirs()

    kols = list_kols()
    if not kols:
        console.print("[yellow]没有已添加的KOL[/yellow]")
        return

    console.print(f"\n[bold magenta]🧪 批量蒸馏 {len(kols)} 个KOL[/bold magenta]\n")

    distiller = ProfileDistiller(config)

    for handle in kols:
        console.print(f"\n--- @{handle} ---")
        try:
            profile = await distiller.distill(handle)
            save_profile(handle, profile)
            console.print(f"  ✅ 蒸馏完成")
        except Exception as e:
            console.print(f"  [red]❌ 失败: {e}[/red]")

    console.print(f"\n[bold green]✅ 批量蒸馏完成[/bold green]\n")


async def cmd_list():
    """列出所有KOL"""
    ensure_dirs()
    kols = list_kols()

    if not kols:
        console.print("\n[yellow]还没有添加任何KOL[/yellow]")
        console.print("使用 [bold]python main.py add @handle[/bold] 添加\n")
        return

    table = Table(title="已添加的KOL")
    table.add_column("Handle", style="cyan")
    table.add_column("Profile", style="green")
    table.add_column("总推文数", justify="right")
    table.add_column("最后抓取", style="dim")

    import json
    for handle in kols:
        kol_dir = get_kol_dir(handle)
        meta_path = kol_dir / "meta.json"

        total = "-"
        last_fetch = "-"
        if meta_path.exists():
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                    total = str(meta.get("total_tweets", 0))
                    last_fetch = meta.get("last_fetch_time", "-")[:16]
            except (json.JSONDecodeError, KeyError):
                total = "数据损坏"
                last_fetch = "数据损坏"

        has_p = "✅" if has_profile(handle) else "❌"
        table.add_row(f"@{handle}", has_p, total, last_fetch)

    console.print()
    console.print(table)
    console.print()


async def cmd_status(handle: str):
    """查看某KOL的状态"""
    handle = handle.lstrip("@")
    ensure_dirs()

    kol_dir = get_kol_dir(handle)
    meta_path = kol_dir / "meta.json"

    if not meta_path.exists():
        console.print(f"\n[red]❌ @{handle} 不存在，请先添加[/red]")
        console.print(f"  使用: [bold]python main.py add @{handle}[/bold]\n")
        return

    import json
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except json.JSONDecodeError:
        console.print(f"\n[red]❌ @{handle} 的数据文件损坏，建议删除后重新添加[/red]\n")
        return

    console.print(f"\n[bold]📊 @{handle} 状态[/bold]\n")
    console.print(f"  总推文数: {meta.get('total_tweets', 0)}")
    console.print(f"  最后抓取: {meta.get('last_fetch_time', '无')}")
    console.print(f"  Profile: {'✅ 已生成' if has_profile(handle) else '❌ 未生成'}")

    # 近7天交易观点数
    trade_count = count_recent_trade_tweets(handle, days=7)
    console.print(f"  近7天交易观点: {trade_count}条")

    # 显示profile摘要
    if has_profile(handle):
        profile = load_profile(handle)
        # 只显示前5行
        lines = profile.split("\n")[:5]
        console.print(f"\n  Profile开头:")
        for line in lines:
            console.print(f"    {line}")

    console.print()


async def cmd_cron():
    """定时任务：增量抓取 + 判断是否需要重新蒸馏"""
    config = load_config()
    ensure_dirs()

    kols = list_kols()
    if not kols:
        return

    print(f"[cron] 开始定时更新，共{len(kols)}个KOL")

    scraper = TweetScraper(config)
    classifier = TweetClassifier(config)
    distiller = ProfileDistiller(config)

    for handle in kols:
        try:
            # 增量抓取
            tweets = await scraper.fetch_incremental(handle)
            if tweets:
                save_tweets(handle, tweets)
                tagged = await classifier.classify_tweets(tweets)
                save_tagged_tweets(handle, tagged)
                print(f"  @{handle}: {len(tweets)}条新推文")

            # 判断是否触发提前蒸馏
            trade_count = count_recent_trade_tweets(handle, days=7)
            threshold = config.schedule.early_distill_threshold

            if trade_count >= threshold:
                print(f"  @{handle}: 交易推文达{trade_count}条，触发蒸馏")
                profile = await distiller.distill(handle)
                save_profile(handle, profile)

        except Exception as e:
            print(f"  @{handle}: 失败 - {e}")

        await asyncio.sleep(2)

    # 检查是否是蒸馏日（每周固定重新蒸馏）
    from datetime import datetime
    today = datetime.now().weekday()  # 0=周一
    if today == config.schedule.distill_day:
        print(f"[cron] 今天是蒸馏日，重新蒸馏所有KOL")
        for handle in kols:
            try:
                profile = await distiller.distill(handle)
                save_profile(handle, profile)
                print(f"  @{handle}: 蒸馏完成")
            except Exception as e:
                print(f"  @{handle}: 蒸馏失败 - {e}")

    print("[cron] 定时任务完成")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1].lower()

    try:
        if cmd == "add":
            if len(sys.argv) < 3:
                console.print("[red]用法: python main.py add @handle[/red]")
                sys.exit(1)
            asyncio.run(cmd_add(sys.argv[2]))

        elif cmd == "discuss":
            if len(sys.argv) < 3:
                console.print("[red]用法: python main.py discuss BTC [@kol1 @kol2 ...][/red]")
                sys.exit(1)
            coin = sys.argv[2]
            handles = sys.argv[3:] if len(sys.argv) > 3 else None
            asyncio.run(cmd_discuss(coin, handles))

        elif cmd == "update":
            if len(sys.argv) < 3:
                console.print("[red]用法: python main.py update @handle[/red]")
                sys.exit(1)
            asyncio.run(cmd_update(sys.argv[2]))

        elif cmd == "update-all":
            asyncio.run(cmd_update_all())

        elif cmd == "distill":
            if len(sys.argv) < 3:
                console.print("[red]用法: python main.py distill @handle[/red]")
                sys.exit(1)
            asyncio.run(cmd_distill(sys.argv[2]))

        elif cmd == "distill-all":
            asyncio.run(cmd_distill_all())

        elif cmd == "list":
            asyncio.run(cmd_list())

        elif cmd == "status":
            if len(sys.argv) < 3:
                console.print("[red]用法: python main.py status @handle[/red]")
                sys.exit(1)
            asyncio.run(cmd_status(sys.argv[2]))

        elif cmd == "cron":
            asyncio.run(cmd_cron())

        else:
            console.print(f"[red]未知命令: {cmd}[/red]")
            print(__doc__)
            sys.exit(1)

    except FileNotFoundError as e:
        console.print(f"\n[red]❌ 文件未找到: {e}[/red]")
        console.print("[yellow]提示: 请确认config.yaml是否存在，或先添加KOL[/yellow]\n")
        sys.exit(1)
    except ValueError as e:
        console.print(f"\n[red]❌ 配置错误: {e}[/red]\n")
        sys.exit(1)
    except RuntimeError as e:
        console.print(f"\n[red]❌ 运行错误: {e}[/red]\n")
        sys.exit(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]已取消操作[/yellow]\n")
        sys.exit(0)
    except Exception as e:
        console.print(f"\n[red]❌ 未预期的错误: {type(e).__name__}: {e}[/red]")
        console.print("[yellow]如果问题持续出现，请检查配置文件和网络连接[/yellow]\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
