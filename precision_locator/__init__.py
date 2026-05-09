#!/usr/bin/env python3
"""
Precision Locator - 智能精准定位工具包
四级降级策略：Level 0 (OCR) → Level 1 (文本) → Level 2 (截图LLM) → Level 3 (坐标)
"""

from precision_locator.utils import debug_print, sanitize_filename
from precision_locator.dom_extractor import DOMExtractor
from precision_locator.llm_config import get_llm, MODEL_CONFIGS
from precision_locator.screenshot_locator import ScreenshotLocator
from precision_locator.locator_agent import LocatorAgent
from precision_locator.visual_locator import VisualLocator
from precision_locator.safe_locator import safe_build_locator
from precision_locator.local_vision import LocalVisionLocator
from precision_locator.executor import SmartExecutor
from precision_locator import server
from precision_locator.server import ensure_browser, main

__version__ = "5.0.0"
__all__ = [
    "debug_print", "sanitize_filename",
    "DOMExtractor", "get_llm", "MODEL_CONFIGS",
    "ScreenshotLocator", "LocatorAgent", "VisualLocator",
    "safe_build_locator", "LocalVisionLocator", "SmartExecutor",
    "ensure_browser", "main",
]
