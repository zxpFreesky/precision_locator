"""
dom_extractor - DOM 紧凑采集器

通过注入 JavaScript 脚本到页面，提取所有可交互元素的属性（标签、文本、角色、
placeholder、data-testid、坐标等），返回精简的 JSON 结构供后续定位使用。

核心特性：
    - 强制采集关键交互元素（Radio、Tab、Menu），即使不可见也保留
    - 支持提示词相关性排序（hint 参数），提高后续 AI 定位精度
    - 兼容 Element Plus 等 UI 框架（el-tabs__item、el-radio-button 等）
    - 自动推导隐式 role（button → "button"、a → "link"）
"""

import os
import json
from typing import Dict, List

from playwright.async_api import Page

from precision_locator.utils import debug_print


class DOMExtractor:
    """页面 DOM 紧凑采集器，提取可交互元素的结构化信息"""

    @staticmethod
    async def get_compact_dom(page: Page, hint: str = "", max_elements: int = 400) -> List[Dict]:
        """
        从页面中提取紧凑的 DOM 结构列表

        Args:
            page: Playwright Page 实例
            hint: 提示词（用于相关性排序，通常传入用户指令）
            max_elements: 最大返回元素数量（默认 400）

        Returns:
            按相关性降序排列的元素属性字典列表，每个字典包含 tag/text/role/placeholder 等字段
        """
        safe_hint = json.dumps(hint.lower()) if hint else '""'
        script = f"""
        () => {{
            const MAX = {max_elements};
            const hintLower = {safe_hint} || "";
            const formElements = new Set(['input', 'textarea', 'select']);

            const baseSelectors = [
                'button', 'input', 'textarea', 'select', 'a', 'li',
                '[role="button"]', '[role="link"]', '[role="textbox"]', '[role="menuitem"]',
                '[role="menu"]', '[role="tab"]', '[role="tablist"]', '[role="tabpanel"]',
                '[role="radio"]', '[role="checkbox"]', '[role="combobox"]', '[role="listbox"]',
                '[role="option"]', '[role="switch"]', '[role="treeitem"]', '[role="gridcell"]',
                '[data-testid]', '[data-test-id]', '[aria-label]',
                'th', 'td', 'label', 'img', 'svg'
            ];
            const all = document.querySelectorAll(baseSelectors.join(','));
            const elements = [];

            for (const el of all) {{
                const tag = el.tagName.toLowerCase();
                const rect = el.getBoundingClientRect();
                const classAttr = el.getAttribute('class') || '';
                const placeholder = el.placeholder ? el.placeholder.trim() : '';
                const dataTestId = el.getAttribute('data-testid') || el.getAttribute('data-test-id') || '';
                const role = el.getAttribute('role') || (tag === 'button' ? 'button' : (tag === 'a' ? 'link' : ''));
                const ariaLabel = el.getAttribute('aria-label') || '';

                let isVisibleNow = (
                    rect.width > 0 &&
                    rect.height > 0 &&
                    !el.hasAttribute('inert')
                );
                if (isVisibleNow) {{
                    const style = window.getComputedStyle(el);
                    isVisibleNow = (
                        style.visibility !== 'hidden' &&
                        style.display !== 'none' &&
                        parseFloat(style.opacity) > 0.1
                    );
                }}

                const isRadio = (role === 'radio' || el.type === 'radio' || classAttr.includes('radio'));
                const isTab = (role === 'tab' || /\bel-tabs__item\b/.test(classAttr) || /\btab-item\b/.test(classAttr) || /\bnav-item\b/.test(classAttr) || /\btab-pane\b/.test(classAttr) || /\bel-radio-button\b/.test(classAttr) || (role === 'tablist'));
                const isMenu = (role === 'menuitem' || role === 'menu' || classAttr.includes('menu-item'));
                const isDialog = (role === 'dialog' || classAttr.includes('dialog'));

                const isKeyElement = (
                    formElements.has(tag) ||
                    !!placeholder ||
                    !!dataTestId ||
                    !!role ||
                    !!ariaLabel ||
                    isRadio || isTab || isMenu
                );
                const isHeader = (tag === 'th' || tag === 'td');

                if (!isVisibleNow && !isKeyElement && !isHeader) continue;

                let text = (el.innerText || el.textContent || el.value || '').trim().substring(0, 120);

                if ((!text || text.length === 0) && tag === 'input' && (el.type === 'radio' || el.type === 'checkbox')) {{
                    const id = el.id;
                    if (id) {{
                        const labelEl = document.querySelector(`label[for="${{id}}"]`);
                        if (labelEl) text = (labelEl.innerText || labelEl.textContent || '').trim().substring(0, 120);
                    }}
                    if (!text) {{
                        const parent = el.closest('label, li, div');
                        if (parent) {{
                            const clone = parent.cloneNode(true);
                            clone.querySelectorAll('input').forEach(inp => inp.remove());
                            text = (clone.innerText || clone.textContent || '').trim().substring(0, 120);
                        }}
                    }}
                }}

                if ((!text || text.length === 0) && isTab) {{
                    const span = el.querySelector('span, .el-tab__label, .tab-label, .el-tabs__item');
                    if (span) text = (span.innerText || span.textContent || '').trim().substring(0, 120);
                    if (!text || text.length === 0) {{
                        const parent = el.closest('.el-tabs__item, .nav-item, [role="tab"]');
                        if (parent) text = (parent.innerText || parent.textContent || '').trim().substring(0, 120);
                    }}
                    if (!text || text.length === 0) {{
                        const children = el.querySelectorAll('span');
                        for (let child of children) {{
                            const childText = (child.innerText || child.textContent || '').trim();
                            if (childText && childText.length > 0) {{
                                text = childText.substring(0, 120);
                                break;
                            }}
                        }}
                    }}
                }}

                if ((!text || text.length === 0) && isRadio) {{
                    const innerSpan = el.querySelector('.el-radio-button__inner, span');
                    if (innerSpan) text = (innerSpan.innerText || innerSpan.textContent || '').trim().substring(0, 120);
                }}

                const title = el.title || '';
                const id = el.id || '';
                const type = el.type || '';
                const name = el.getAttribute('name') || '';
                const href = el.href || '';

                let relevance = 0;
                if (dataTestId && hintLower && dataTestId.toLowerCase().includes(hintLower)) relevance = 100;
                if (text && hintLower && text.toLowerCase().includes(hintLower)) relevance = 80;
                if (placeholder && hintLower && placeholder.toLowerCase().includes(hintLower)) relevance = 75;
                if (ariaLabel && hintLower && ariaLabel.toLowerCase().includes(hintLower)) relevance = 70;
                if (formElements.has(tag)) relevance = Math.max(relevance, 50);
                if (isRadio || isTab || isMenu) relevance = Math.max(relevance, 60);

                const info = {{
                    tag, type, text, placeholder, id, className: classAttr, dataTestId,
                    role, ariaLabel, name, href, title,
                    visible: isVisibleNow,
                    relevance,
                    isRadio, isTab, isMenu, isDialog,
                    ariaSelected: el.getAttribute('aria-selected') === 'true',
                    checked: el.checked || false,
                    rect: {{ x: Math.round(rect.x), y: Math.round(rect.y), w: rect.width, h: rect.height }}
                }};

                Object.keys(info).forEach(k => {{ if (info[k] === undefined || info[k] === '') delete info[k]; }});
                if (!isVisibleNow) info.possiblyHidden = true;
                elements.push(info);
            }}

            elements.sort((a,b) => (b.relevance || 0) - (a.relevance || 0));
            return elements.slice(0, MAX);
        }}
        """
        try:
            dom = await page.evaluate(script)
            return dom if isinstance(dom, list) else []
        except Exception as e:
            debug_print(f"[DOMExtractor] JS 执行失败: {e}")
            return []
