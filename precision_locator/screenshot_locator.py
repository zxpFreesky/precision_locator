"""
screenshot_locator - 截图视觉定位器（Level 2 / Level 3）

通过多模态 LLM 分析页面截图，提供两种定位模式：
    - analyze():          Level 2，生成 Playwright 定位器表达式
    - analyze_coordinates(): Level 3，返回元素中心像素坐标

使用独立的视觉模型配置（VISION_LLM_PROVIDER / VISION_MODEL），
与文本定位使用的 LLM 模型解耦。
"""

import os
import re
import json
from typing import Any, Dict, Optional

from precision_locator.utils import debug_print, clean_llm_output
from precision_locator.llm_config import get_llm


class ScreenshotLocator:
    """
    截图 + 多模态 LLM 定位器

    支持两种分析模式：
        1. 定位器模式（analyze）：截图 → LLM → Playwright 定位器表达式
        2. 坐标模式（analyze_coordinates）：截图 → LLM → 像素坐标 {x, y, description}

    429 / 余额不足时自动标记模型不可用，避免连续重试浪费请求。
    """

    def __init__(self):
        self.vision_provider = os.getenv("VISION_LLM_PROVIDER", os.getenv("LLM_PROVIDER", "openai"))
        self.vision_model = os.getenv("VISION_MODEL", "")
        if not self.vision_model:
            if self.vision_provider == "openai":
                self.vision_model = "gpt-4o"
            elif self.vision_provider == "zhipu":
                self.vision_model = "glm-4v-plus"
            elif self.vision_provider == "qwen":
                self.vision_model = "qwen-vl-plus"
            else:
                self.vision_model = os.getenv("LLM_MODEL", "gpt-4o")
        self._llm = None

    @property
    def llm(self):
        if self._llm is None:
            try:
                self._llm = get_llm(self.vision_provider, self.vision_model)
                debug_print(f"[ScreenshotLocator] 使用视觉模型: {self.vision_provider}/{self.vision_model}")
            except Exception as e:
                debug_print(f"[ScreenshotLocator] 视觉模型初始化失败: {e}")
                self._llm = None
        return self._llm

    def _parse_llm_response(self, response) -> str:
        """解析 LLM 响应，兼容字符串和结构化内容块（多模态模型可能返回列表）"""
        content = response.content
        if isinstance(content, list):
            texts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get('type') == 'text':
                        texts.append(block.get('text', ''))
                elif isinstance(block, str):
                    texts.append(block)
            content = '\n'.join(texts)
        return clean_llm_output(str(content))

    async def analyze(self, image_base64: str, instruction: str) -> Optional[str]:
        """
        Level 2: 分析截图生成 Playwright 定位器表达式

        Args:
            image_base64: 页面截图的 Base64 编码
            instruction: 用户自然语言指令

        Returns:
            Playwright 定位器字符串，或 None
        """
        if not self.llm:
            debug_print("[ScreenshotLocator] 视觉模型不可用，跳过截图分析")
            return None
        prompt = f"""你是一个 Playwright 定位专家。根据截图和指令，生成一条稳定的定位器代码。

指令：{instruction}

定位器优先级：
1. 如果有 data-testid，使用 page.locator('[data-testid="..."]')
2. 如果有明确的文字标签或角色，使用 page.get_by_role(role, name="...") 或 page.get_by_text("...")
3. 如果是输入框，使用 page.get_by_placeholder("...")
4. 如果元素是按钮，优先使用 get_by_role("button", name="...")

规则：
- 只能输出一行 Playwright 定位器代码（以 page. 开头），可添加 filter、first，不得有多余解释
- 如果无法定位，请输出 #NULL#
- 禁止使用变量或复杂逻辑
- 绝对禁止翻译文本，必须使用截图中显示的原文
"""
        from langchain_core.messages import HumanMessage
        messages = [
            HumanMessage(
                content=[
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_base64}"}}
                ]
            )
        ]
        try:
            response = await self.llm.ainvoke(messages)
            raw = self._parse_llm_response(response)
            debug_print(f"[ScreenshotLocator] 定位器分析输出: {raw[:200]}")
            if '#NULL#' in raw:
                return None
            for line in raw.split('\n'):
                line = line.strip().rstrip(';')
                if not line or line.startswith('#'):
                    continue
                if line.startswith('page.'):
                    line = re.sub(r'\.(click|fill|type|press|check|select_option|hover)\(.*?\)$', '', line)
                    if any(m in line for m in ['page.locator', 'page.get_by']):
                        return line
                elif line.startswith('get_by_'):
                    line = re.sub(r'\.(click|fill|type|press|check|select_option|hover)\(.*?\)$', '', line)
                    return 'page.' + line
            return None
        except Exception as e:
            err_str = str(e)
            if '429' in err_str or '余额' in err_str:
                debug_print(f"[ScreenshotLocator] 视觉模型余额不足(429)，标记为不可用")
                self._llm = None
            else:
                debug_print(f"[ScreenshotLocator] 定位器分析失败: {e}")
            return None

    async def analyze_coordinates(self, image_base64: str, instruction: str) -> Optional[Dict[str, Any]]:
        """
        Level 3: 分析截图返回元素中心像素坐标

        Args:
            image_base64: 页面截图的 Base64 编码
            instruction: 用户自然语言指令

        Returns:
            {"x": int, "y": int, "description": str} 或 None
        """
        if not self.llm:
            debug_print("[ScreenshotLocator] 视觉模型不可用，跳过坐标分析")
            return None
        prompt = f"""你是一个UI自动化测试专家。请分析截图，找到与指令匹配的元素，返回其中心坐标。

指令：{instruction}

请严格按照以下JSON格式返回，不要输出其他任何内容：
{{"found": true, "x": 元素中心x像素坐标, "y": 元素中心y像素坐标, "description": "元素简要描述"}}

如果找不到匹配的元素，返回：
{{"found": false, "reason": "未找到匹配元素"}}

注意：坐标必须是页面截图中的实际像素位置。
"""
        from langchain_core.messages import HumanMessage
        messages = [
            HumanMessage(
                content=[
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_base64}"}}
                ]
            )
        ]
        try:
            response = await self.llm.ainvoke(messages)
            raw = self._parse_llm_response(response)
            debug_print(f"[ScreenshotLocator] 坐标分析输出: {raw[:300]}")
            json_match = re.search(r'\{[^{}]+\}', raw, re.DOTALL)
            if not json_match:
                return None
            result = json.loads(json_match.group())
            if result.get('found') and isinstance(result.get('x'), (int, float)) and isinstance(result.get('y'), (int, float)):
                x, y = int(result['x']), int(result['y'])
                if x > 0 and y > 0:
                    debug_print(f"[ScreenshotLocator] 坐标定位成功: ({x}, {y}) - {result.get('description', '')}")
                    return {"x": x, "y": y, "description": result.get('description', '')}
            debug_print(f"[ScreenshotLocator] 坐标分析未找到有效位置: {result}")
            return None
        except json.JSONDecodeError as e:
            debug_print(f"[ScreenshotLocator] 坐标JSON解析失败: {e}")
            return None
        except Exception as e:
            err_str = str(e)
            if '429' in err_str or '余额' in err_str:
                debug_print(f"[ScreenshotLocator] 视觉模型余额不足(429)，标记为不可用")
                self._llm = None
            else:
                debug_print(f"[ScreenshotLocator] 坐标分析失败: {e}")
            return None
