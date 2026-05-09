"""
visual_locator - DOM 上下文视觉定位器（Level 1b / Level 2c）

将 DOM 中关键元素（input/button/th/img/svg）的结构化上下文与用户指令一起
发送给 LLM，生成 Playwright 定位器。适用于"表头内点击图标"等需要 DOM 上下文
辅助的复杂场景。
"""

import re
from typing import Optional

from langchain_core.messages import HumanMessage

from precision_locator.utils import debug_print, clean_llm_output
from precision_locator.llm_config import get_llm


class VisualLocator:
    """基于 DOM 上下文 + LLM 的视觉定位器，处理需要结构化上下文的复杂定位场景"""

    def __init__(self, llm=None):
        self._llm = llm

    @property
    def llm(self):
        """懒加载 LLM 实例"""
        if self._llm is None:
            self._llm = get_llm()
        return self._llm

    async def analyze_and_locate(self, instruction: str, dom: list) -> Optional[str]:
        """
        根据 DOM 上下文和指令生成定位器

        从 DOM 中筛选 input/textarea/select/button/th/td/img/svg 等关键元素，
        构建结构化 Prompt 发送给 LLM，返回 Playwright 定位器表达式。

        Args:
            instruction: 用户自然语言指令
            dom: DOMExtractor 返回的元素列表

        Returns:
            Playwright 定位器字符串，或 None
        """
        target_tags = {'input', 'textarea', 'select', 'button', 'th', 'td', 'img', 'svg'}
        contextual = [el for el in dom if el.get('tag') in target_tags or el.get('isDialog')]
        context_str = f"指令：{instruction}\n关键候选元素：\n"
        for el in contextual[:50]:
            placeholder = el.get('placeholder', '')
            text = el.get('text', '')[:40]
            role = el.get('role', '')
            tag = el.get('tag', '')
            context_str += f"- tag={tag} placeholder={placeholder} text={text} role={role}\n"
        prompt = f"""{context_str}
规则：
1. 只允许使用 get_by_placeholder("placeholder文字") 或 get_by_role(role, name="文本") 或 page.locator("css") 后接 .filter(has_text="...") 或 .first。
2. 若需要在表头内点击图标，可使用 page.locator("th").filter(has_text="列名").locator("img, svg").first
3. 绝对禁止翻译或编造文本，必须使用上面列出的原文。
4. 只输出定位器代码，不要解释。若无法定位输出 #NULL#
"""
        try:
            resp = await self.llm.ainvoke([HumanMessage(content=prompt)])
            raw = clean_llm_output(resp.content)
            debug_print(f"[VisualLocator] 输出: {raw[:150]}")
            if raw.startswith('#NULL#'):
                return None
            for line in raw.split('\n'):
                line = line.strip().rstrip(';')
                if not line or line.startswith('#'):
                    continue
                if line.startswith('page.'):
                    line = re.sub(r'\.(click|fill|type|press)\(.*?\)$', '', line)
                    return line
                if line.startswith('get_by_'):
                    line = re.sub(r'\.(click|fill|type|press)\(.*?\)$', '', line)
                    return 'page.' + line
                if line.startswith('locator(') or line.startswith('.locator('):
                    line = re.sub(r'\.(click|fill|type|press)\(.*?\)$', '', line)
                    if line.startswith('.'): line = 'page' + line
                    else: line = 'page.' + line
                    return line
            return None
        except Exception as e:
            debug_print(f"[VisualLocator] 失败: {e}")
            return None
