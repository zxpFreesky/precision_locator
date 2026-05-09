"""
locator_agent - AI 定位器生成器（Level 1c）

将 DOM 结构和用户指令发送给 LLM，生成 Playwright 定位器表达式。
包含严格的后校验机制：文本/placeholder/label/name 必须在 DOM 中存在，
且检测并拦截 LLM 的中文→英文翻译行为。
"""

import json
import re
from typing import Optional

from langchain_core.messages import SystemMessage, HumanMessage

from precision_locator.utils import (
    debug_print, clean_llm_output, _extract_first_string,
    _extract_named_arg, extract_all_texts, is_translated
)
from precision_locator.llm_config import get_llm


class LocatorAgent:
    """
    基于 LLM 的 Playwright 定位器生成器

    工作流程：
        1. 将 DOM 结构（截断至 30 个元素、5000 字符）和用户指令组合为 Prompt
        2. 调用 LLM 生成定位器表达式
        3. 对输出进行后校验：文本必须在 DOM 中存在、禁止翻译
    """

    SYSTEM_PROMPT = """你是一个 Playwright 定位专家。根据提供的元素结构 JSON 和用户指令，生成一条最稳定的定位器代码。

定位器优先级（严格遵守）：
1. data-testid → page.locator('[data-testid="value"]')
2. role + name → page.get_by_role(role, name="元素的显示文本")
3. placeholder → page.get_by_placeholder("元素的真实placeholder值")
4. 文本 → page.get_by_text("显示文本")

极其重要的规则：
- 绝对禁止将中文元素文本翻译为英文或其他语言！必须完全使用 JSON 中出现的原文，不能有任何翻译或改写。
- 绝对不能编造 dataTestId、id、placeholder 值或文本内容。
- 若找不到匹配的元素，请直接输出 #NULL#，不要强行生成定位器。
- 对于按钮类元素，优先使用 get_by_role("button", name="...")。
- 当页面可能有重复文本时，必须使用 role 进行区分（如 menuitem, tab, radio）。
- 生成的代码支持链式调用 .filter(has_text="...") 和 .first，但不要使用 nth 等复杂索引。
- 代码格式：page.xxx(...).filter(...).first 或 page.xxx(...)
- 无法定位时输出 #NULL#
- 如果指令是"输入"或"填充"操作，优先使用 get_by_placeholder 定位输入框。
"""

    def __init__(self):
        self._llm = None

    @property
    def llm(self):
        if self._llm is None:
            self._llm = get_llm()
        return self._llm

    async def generate_locator(self, dom_structure: list, instruction: str, action_type: str = "click") -> Optional[str]:
        """
        根据页面 DOM 结构和用户指令生成 Playwright 定位器表达式

        Args:
            dom_structure: DOMExtractor 返回的元素列表
            instruction: 用户自然语言指令
            action_type: 操作类型（click/fill/select 等），影响定位策略

        Returns:
            合法的 Playwright 定位器字符串（如 page.get_by_role("button", name="登录")），
            或 None 表示无法生成
        """
        truncated_dom = dom_structure[:30]
        structure_str = json.dumps(truncated_dom, indent=2, ensure_ascii=False)
        if len(structure_str) > 5000:
            structure_str = structure_str[:5000] + "\n... (truncated)"
        user_prompt = f"""
页面元素结构：
{structure_str}

指令：{instruction}
操作类型：{action_type}

请生成定位器。
"""
        messages = [
            SystemMessage(content=self.SYSTEM_PROMPT),
            HumanMessage(content=user_prompt)
        ]
        try:
            response = await self.llm.ainvoke(messages)
            raw = clean_llm_output(response.content)
            debug_print(f"[LocatorAgent] LLM 输出: {raw[:200]}")
        except Exception as e:
            debug_print(f"[LocatorAgent] LLM 调用失败: {e}")
            return None

        valid_texts = extract_all_texts(dom_structure)

        for line in raw.split('\n'):
            line = line.strip()
            if line.startswith('#NULL#'):
                return None
            if line.startswith('page.'):
                if '//' in line:
                    line = line.split('//')[0].strip()
                line = re.sub(r'\.(click|fill|type|press|check|uncheck|select_option|hover|dblclick)\(.*?\)$', '', line)
                line = re.sub(r'\.(click|fill|type|press|check|uncheck|select_option|hover|dblclick)$', '', line)
                line = line.rstrip(';')
                if not (any(method in line for method in ['page.locator', 'page.get_by'])):
                    continue
                if 'get_by_text' in line:
                    text_param = _extract_first_string(line)
                    if text_param:
                        if text_param not in valid_texts:
                            debug_print(f"[LocatorAgent] 文本 '{text_param}' 不存在于 DOM，忽略此定位器")
                            continue
                        if is_translated(text_param, valid_texts, instruction):
                            debug_print(f"[LocatorAgent] 检测到翻译，忽略此定位器")
                            continue
                if 'get_by_role' in line:
                    name_param = _extract_named_arg(line, 'name')
                    if name_param:
                        if name_param not in valid_texts:
                            debug_print(f"[LocatorAgent] name '{name_param}' 不存在于 DOM，忽略此定位器")
                            continue
                        if is_translated(name_param, valid_texts, instruction):
                            debug_print(f"[LocatorAgent] 检测到翻译，忽略此定位器")
                            continue
                if 'get_by_placeholder' in line:
                    placeholder_param = _extract_first_string(line)
                    if placeholder_param and placeholder_param not in valid_texts:
                        debug_print(f"[LocatorAgent] placeholder '{placeholder_param}' 不存在于 DOM，忽略此定位器")
                        continue
                if 'get_by_label' in line:
                    label_param = _extract_first_string(line)
                    if label_param and label_param not in valid_texts:
                        debug_print(f"[LocatorAgent] label '{label_param}' 不存在于 DOM，忽略此定位器")
                        continue
                return line
        return None
