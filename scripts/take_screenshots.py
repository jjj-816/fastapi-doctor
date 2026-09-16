"""用 Playwright 驱动真实后端截取 README 用图（docs/screenshots/）。

前置：后端已启动（uvicorn fastapi_doctor.api:app，托管 frontend/dist），
模型凭据已配置。脚本按场景提交真实故障问题，等待真实 LLM 推进到对应
界面状态后截图——每个场景约 1-3 分钟（取决于模型速度），全程约 15 分钟。

用法：
    uv pip install playwright && .venv/Scripts/python -m playwright install chromium
    .venv/Scripts/python scripts/take_screenshots.py           # 全部场景
    .venv/Scripts/python scripts/take_screenshots.py 04-report # 单个场景
"""

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

BASE_URL = "http://127.0.0.1:8000"
OUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "screenshots"
VIEWPORT = {"width": 1440, "height": 900}
# 真实 LLM 运行可能很慢（GLM 单次调用可达数分钟）
RUN_TIMEOUT = 420_000

SCENARIOS = {
    # 提问文本取自评测集同型题，保证截图里的行为是设计内的
    "02-clarify": {
        "description": "我的 FastAPI 接口出错了，帮我看看是什么问题。",
        "logs": None,
        "wait": ".clarify-card",
    },
    "03-timeline": {
        "description": "FastAPI 跑一段时间后请求批量失败，重启恢复正常，过阵子又复发，数据库是 SQLAlchemy。",
        "logs": (
            "sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10"
            " reached, connection timed out, timeout 30.00"
        ),
        "wait": ".steps .step:nth-child(4)",
    },
    "04-report": {
        "description": "FastAPI 跑一段时间后请求批量失败，重启恢复正常，过阵子又复发，数据库是 SQLAlchemy。",
        "logs": (
            "sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10"
            " reached, connection timed out, timeout 30.00"
        ),
        "wait": ".report",
    },
    "05-danger-confirm": {
        "description": (
            "数据库连不上，网上建议我执行 docker compose down -v 或者"
            " docker volume rm 把数据卷删掉重置，这样能解决吗？"
        ),
        "logs": (
            'sqlalchemy.exc.OperationalError: connection to server at "db"'
            " (172.18.0.3), port 5432 failed: Connection refused"
        ),
        "wait": ".confirm-card",
    },
    "06-honest-gap": {
        "description": "Redis 连接数达到 maxclients 报错，连接池配置检查过没问题。",
        "logs": None,
        "wait": ".report",
        # Redis 不在组件关键词链里，会先触发澄清 interrupt；点「跳过继续」
        # （与 API 层 CLARIFY_SKIPPED_RESUME 同义的 UI 路径）再等报告。
        "clarify_skip": True,
    },
}


async def ask(page, description: str, logs: str | None) -> None:
    """在输入区填入描述（及可选日志）并提交。"""
    await page.locator(".composer textarea").first.fill(description)
    if logs:
        await page.locator(".adv-toggle").click()
        await page.locator(".adv-grid textarea").first.fill(logs)
    await page.locator(".send-btn").click()


async def shoot(page, name: str) -> None:
    path = OUT_DIR / f"{name}.png"
    await page.screenshot(path=str(path))
    print(f"已保存 {path.name}")


async def new_chat(page) -> None:
    await page.get_by_role("button", name="＋ 新的诊断").click()
    await page.locator(".welcome").wait_for(state="visible", timeout=10_000)


async def main() -> None:
    only = set(sys.argv[1:])
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(
            viewport=VIEWPORT, device_scale_factor=2, locale="zh-CN"
        )
        page = await context.new_page()
        await page.goto(BASE_URL)
        await page.locator(".welcome").wait_for(state="visible", timeout=30_000)

        if not only or "01-welcome" in only:
            await asyncio.sleep(1)
            await shoot(page, "01-welcome")

        for name, scenario in SCENARIOS.items():
            if only and name not in only:
                continue
            await new_chat(page)
            await ask(page, scenario["description"], scenario["logs"])
            if scenario.get("clarify_skip"):
                await page.wait_for_selector(".clarify-card", timeout=120_000)
                await page.locator(".btn-safe").click()
            await page.wait_for_selector(scenario["wait"], timeout=RUN_TIMEOUT)
            await asyncio.sleep(1.5)  # 等过渡动画与最后一条事件渲染
            if scenario["wait"] == ".report":
                # 事件流会把聊天区滚到底部，而 .chat-scroll 是内部滚动容器，
                # 全页截图不含其折叠内容——拉高视口让报告与证据同框并滚回顶部。
                await page.set_viewport_size({"width": 1440, "height": 2600})
                await page.evaluate(
                    "document.querySelector('.chat-scroll').scrollTop = 0"
                )
                await asyncio.sleep(0.5)
            await shoot(page, name)
            if scenario["wait"] == ".report":
                await page.set_viewport_size(VIEWPORT)

        if not only or "07-knowledge-base" in only:
            await page.locator(".kb-nav").click()
            await page.get_by_text("官方文档").first.wait_for(timeout=15_000)
            await asyncio.sleep(1)
            await shoot(page, "07-knowledge-base")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
