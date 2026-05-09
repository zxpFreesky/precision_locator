"""
safe_locator - Playwright 定位器安全构建器

将 LLM 生成的定位器表达式字符串（如 page.get_by_role("button", name="登录")）
安全解析并构建为 Playwright Locator 对象，支持链式调用。

支持的方法：
    - locator(selector)
    - get_by_role(role, name="...")
    - get_by_placeholder("...")
    - get_by_text("...")
    - get_by_title("...")
    - get_by_label("...")
    - filter(has_text="...")
    - .first / .last
"""

import re
from typing import Optional

from playwright.async_api import Page

from precision_locator.utils import _extract_first_string, _extract_named_arg


def safe_build_locator(page: Page, locator_expr: str):
    """
    将定位器表达式字符串安全构建为 Playwright Locator 对象

    解析流程：
        1. 去除尾部操作方法（click/fill 等）
        2. 验证表达式以 "page." 开头
        3. 逐段解析链式调用（.get_by_role / .locator / .filter 等）
        4. 返回最终的 Locator 实例

    Args:
        page: Playwright Page 实例
        locator_expr: 定位器表达式字符串（如 page.get_by_role("button", name="登录")）

    Returns:
        Playwright Locator 对象

    Raises:
        ValueError: 表达式不以 "page." 开头时抛出
    """
    expr = locator_expr.strip()
    expr = re.sub(r'\.(click|fill|type|press|check|uncheck|select_option|hover|dblclick)\(.*?\)$', '', expr)
    expr = re.sub(r'\.(click|fill|type|press|check|uncheck|select_option|hover|dblclick)$', '', expr)

    if not expr.startswith('page.'):
        raise ValueError(f"定位器必须以 page. 开头，实际: {expr}")

    current = page
    pos = 4
    pattern = re.compile(r'\.([a-z_]+)(?:\(([^)]*(?:"[^"]*"[^)]*)*)\))?')
    while pos < len(expr):
        m = pattern.match(expr, pos)
        if not m:
            if expr.startswith('.first', pos):
                current = current.first
                pos += 6
                continue
            if expr.startswith('.last', pos):
                current = current.last
                pos += 5
                continue
            break
        method = m.group(1)
        args_str = m.group(2) or ''
        full_len = len(m.group(0))

        if method == 'locator':
            selector = _extract_first_string(args_str)
            if selector: current = current.locator(selector)
        elif method == 'get_by_role':
            role = _extract_first_string(args_str)
            name = _extract_named_arg(args_str, 'name')
            if role:
                if name: current = current.get_by_role(role, name=name)
                else: current = current.get_by_role(role)
        elif method == 'get_by_placeholder':
            placeholder = _extract_first_string(args_str)
            if placeholder: current = current.get_by_placeholder(placeholder)
        elif method == 'get_by_text':
            text = _extract_first_string(args_str)
            if text: current = current.get_by_text(text)
        elif method == 'get_by_title':
            title = _extract_first_string(args_str)
            if title: current = current.get_by_title(title)
        elif method == 'get_by_label':
            label = _extract_first_string(args_str)
            if label: current = current.get_by_label(label)
        elif method == 'filter':
            has_text = _extract_named_arg(args_str, 'has_text')
            if has_text: current = current.filter(has_text=has_text)
        elif method == 'first':
            current = current.first
        elif method == 'last':
            current = current.last
        else:
            break
        pos += full_len
    return current
