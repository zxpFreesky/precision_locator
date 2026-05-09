"""
server - MCP Server 入口

Precision Locator 的 MCP (Model Context Protocol) 服务端入口，
注册 9 个工具（navigate/smart_click/smart_fill/smart_select/smart_hover/
smart_check/get_page_structure/screenshot/close_browser），
通过 stdio 与 MCP 客户端通信。

环境变量：
    HEADLESS  - 是否无头模式运行浏览器（默认 false）
    VIEWPORT  - 视口大小（如 "1920x1080"）
    LOCALE    - 浏览器语言（如 "zh-CN"）

使用方式：
    python -m precision_locator.server
    或直接: python precision_locator/server.py
"""

import asyncio
import json
import os
from typing import Optional

from mcp.server import Server
from mcp.server.models import InitializationOptions
import mcp.server.stdio
import mcp.types as types
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from precision_locator.utils import debug_print, sanitize_filename
from precision_locator.executor import SmartExecutor

app = Server("precision-locator-agent")

_browser_instance: Optional[Browser] = None
_browser_context: Optional[BrowserContext] = None
_page: Optional[Page] = None
_executor: Optional[SmartExecutor] = None
_pw_instance = None
init_lock = asyncio.Lock()


async def ensure_browser():
    """
    确保浏览器实例已启动（幂等，带异步锁保护）

    首次调用时启动 Chromium 浏览器、创建上下文和页面、初始化 SmartExecutor。
    后续调用直接返回，不会重复创建。
    """
    global _browser_instance, _browser_context, _page, _executor, _pw_instance
    async with init_lock:
        if _browser_instance is None:
            debug_print("[INFO] 启动浏览器...")
            _pw_instance = await async_playwright().start()
            _browser_instance = await _pw_instance.chromium.launch(
                headless=os.getenv("HEADLESS", "false").lower() == "true"
            )
            viewport_str = os.getenv("VIEWPORT", "")
            context_kwargs = {}
            if viewport_str:
                try:
                    w, h = viewport_str.split('x')
                    context_kwargs["viewport"] = {"width": int(w), "height": int(h)}
                except ValueError:
                    pass
            locale = os.getenv("LOCALE", "")
            if locale:
                context_kwargs["locale"] = locale
            _browser_context = await _browser_instance.new_context(**context_kwargs)
            _page = await _browser_context.new_page()
            _executor = SmartExecutor(_page)
            debug_print("[INFO] 浏览器已就绪")


@app.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """注册所有 MCP 工具定义"""
    return [
        types.Tool(name="navigate", description="导航到指定的 URL",
                   inputSchema={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}),
        types.Tool(name="smart_click", description="智能点击页面元素（支持截图视觉分析）",
                   inputSchema={"type": "object", "properties": {"instruction": {"type": "string"}}, "required": ["instruction"]}),
        types.Tool(name="smart_fill", description="智能填充输入框",
                   inputSchema={"type": "object", "properties": {"instruction": {"type": "string"}, "value": {"type": "string"}}, "required": ["instruction", "value"]}),
        types.Tool(name="smart_select", description="智能选择下拉选项",
                   inputSchema={"type": "object", "properties": {"instruction": {"type": "string"}, "value": {"type": "string"}}, "required": ["instruction", "value"]}),
        types.Tool(name="smart_hover", description="智能悬停页面元素",
                   inputSchema={"type": "object", "properties": {"instruction": {"type": "string"}}, "required": ["instruction"]}),
        types.Tool(name="smart_check", description="智能勾选复选框",
                   inputSchema={"type": "object", "properties": {"instruction": {"type": "string"}}, "required": ["instruction"]}),
        types.Tool(name="get_page_structure", description="获取当前页面的精简 DOM 结构",
                   inputSchema={"type": "object", "properties": {"hint": {"type": "string"}}}),
        types.Tool(name="screenshot", description="截取当前页面截图",
                   inputSchema={"type": "object", "properties": {"filename": {"type": "string"}}}),
        types.Tool(name="close_browser", description="关闭浏览器并释放资源",
                   inputSchema={"type": "object", "properties": {}}),
    ]


@app.call_tool()
async def handle_call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    """MCP 工具调用分发器：根据工具名路由到对应的处理逻辑"""
    global _browser_instance, _browser_context, _page, _executor, _pw_instance
    try:
        if name == "close_browser":
            if _browser_context: await _browser_context.close()
            if _browser_instance: await _browser_instance.close()
            if _pw_instance: await _pw_instance.stop()
            _browser_instance = _browser_context = _page = _executor = _pw_instance = None
            return [types.TextContent(type="text", text="浏览器已关闭")]
        if name in ("navigate", "smart_click", "smart_fill", "smart_select", "smart_hover", "smart_check", "get_page_structure", "screenshot"):
            await ensure_browser()
        if name == "navigate":
            url = arguments.get("url")
            await _page.goto(url, wait_until="networkidle")
            return [types.TextContent(type="text", text=f"成功导航到 {url}")]
        elif name == "smart_click":
            result = await _executor.smart_click(arguments.get("instruction"))
            return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
        elif name == "smart_fill":
            result = await _executor.smart_fill(arguments.get("instruction"), arguments.get("value"))
            return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
        elif name == "smart_select":
            result = await _executor.smart_select(arguments.get("instruction"), arguments.get("value"))
            return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
        elif name == "smart_hover":
            result = await _executor.smart_hover(arguments.get("instruction"))
            return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
        elif name == "smart_check":
            result = await _executor.smart_check(arguments.get("instruction"))
            return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
        elif name == "get_page_structure":
            structure = await _executor.get_page_structure(arguments.get("hint", ""))
            return [types.TextContent(type="text", text=json.dumps(structure, indent=2, ensure_ascii=False))]
        elif name == "screenshot":
            filename = arguments.get("filename", "screenshot.png")
            if not filename.endswith(".png"): filename += ".png"
            path = os.path.join(os.getcwd(), sanitize_filename(filename))
            await _page.screenshot(path=path)
            return [types.TextContent(type="text", text=f"截图已保存到 {path}")]
        else:
            return [types.TextContent(type="text", text=f"未知工具: {name}")]
    except Exception as e:
        return [types.TextContent(type="text", text=json.dumps({"success": False, "error": str(e)}, ensure_ascii=False))]


async def main():
    """MCP Server 主入口：通过 stdio 启动服务并监听客户端请求"""
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream, write_stream,
            InitializationOptions(
                server_name="precision-locator-agent",
                server_version="5.0.0",
                capabilities=types.ServerCapabilities(tools=types.ToolsCapability()),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
