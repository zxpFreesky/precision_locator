"""
local_vision - 本地视觉定位器（Level 0，零 Token 消耗）

使用 PaddleOCR 进行文字识别 + OpenCV 模板匹配，在本地完成元素定位，
不消耗任何 LLM Token。适用于文字明确的按钮、标签、输入框等场景。

当 PaddleOCR/OpenCV 未安装时自动降级（available=False），不影响其他 Level 运行。

依赖（可选）：
    paddleocr>=2.7.0,<3.0.0
    paddlepaddle>=2.5.0
    opencv-python>=4.8.0
    numpy>=1.24.0
"""

import os
import re
from typing import Any, Dict, Optional

from precision_locator.utils import debug_print

try:
    import cv2
    import numpy as np
    from paddleocr import PaddleOCR
    _LOCAL_VISION_AVAILABLE = True
except ImportError:
    _LOCAL_VISION_AVAILABLE = False

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_SCRIPT_DIR)


class LocalVisionLocator:
    """
    本地视觉定位器（单例模式）

    定位策略：
        1. locate_by_text():   PaddleOCR 文字识别 → bigram/trigram 分词匹配 → 返回中心坐标
        2. locate_by_template(): OpenCV 模板匹配 → 返回匹配区域中心坐标

    排序优先级：最长搜索词匹配 > 最短文本长度 > 最高置信度
    """

    _instance = None
    _ocr = None

    @classmethod
    def get_instance(cls):
        """获取单例实例（首次调用时初始化 PaddleOCR）"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._ocr = None
        self._templates_dir = os.path.join(_PROJECT_DIR, "templates")
        self._available = _LOCAL_VISION_AVAILABLE
        if self._available:
            try:
                self._ocr = PaddleOCR(use_angle_cls=True, lang='ch', show_log=False)
                debug_print("[LocalVision] PaddleOCR 初始化成功")
            except Exception as e:
                debug_print(f"[LocalVision] PaddleOCR 初始化失败: {e}")
                self._available = False

    @property
    def available(self):
        return self._available

    async def locate_by_text(self, screenshot_bytes: bytes, target_text: str) -> Optional[Dict[str, Any]]:
        """
        PaddleOCR 文字识别定位

        将截图送入 PaddleOCR 识别所有文字，通过 bigram/trigram 分词在识别结果中
        搜索与目标文本匹配的元素，返回匹配度最高的元素中心坐标。

        Args:
            screenshot_bytes: 页面截图原始字节
            target_text: 目标文本（用户指令）

        Returns:
            {"x": int, "y": int, "text": str, "confidence": float, "method": "ocr"} 或 None
        """
        if not self._available or not self._ocr:
            return None
        try:
            nparr = np.frombuffer(screenshot_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is None:
                return None
            results = self._ocr.ocr(img, cls=True)
            if not results or not results[0]:
                debug_print(f"[LocalVision] OCR未识别到任何文字 (指令: {target_text})")
                return None
            all_texts = [f"'{line[1][0]}'({line[1][1]:.2f})" for line in results[0] if line[1][1] >= 0.5]
            debug_print(f"[LocalVision] OCR识别到 {len(all_texts)} 项: {', '.join(all_texts[:15])}")
            target_lower = target_text.lower()
            chinese_chars = re.findall(r'[\u4e00-\u9fa5]', target_lower)
            stopwords = {'点击','切换','选择','输入','打开','关闭','这个','那个','请','帮我',
                         '按钮','标签','菜单','表头','筛选','图标','列表','列',
                         '右侧','左侧','上方','下方','第一个','最后一个','里面','外面'}
            search_terms = []
            for n in [4, 3, 2]:
                for i in range(len(chinese_chars) - n + 1):
                    seg = ''.join(chinese_chars[i:i+n])
                    if seg not in stopwords:
                        search_terms.append(seg)
            candidates = []
            for line in results[0]:
                text = line[1][0]
                confidence = line[1][1]
                if confidence < 0.5:
                    continue
                text_lower = text.lower()
                best_term_len = 0
                for term in search_terms:
                    if term in text_lower:
                        best_term_len = max(best_term_len, len(term))
                if best_term_len > 0:
                    box = line[0]
                    x = int((box[0][0] + box[2][0]) / 2)
                    y = int((box[0][1] + box[2][1]) / 2)
                    w = int(box[2][0] - box[0][0])
                    h = int(box[2][1] - box[0][1])
                    candidates.append({
                        "x": x, "y": y, "w": w, "h": h,
                        "text": text, "confidence": confidence,
                        "method": "ocr",
                        "best_term_len": best_term_len,
                        "text_len": len(text)
                    })
            if candidates:
                candidates.sort(key=lambda c: (-c["best_term_len"], c["text_len"], -c["confidence"]))
                best = candidates[0]
                debug_print(f"[LocalVision] OCR命中: text='{best['text']}', conf={best['confidence']:.2f}, pos=({best['x']},{best['y']}), term_len={best['best_term_len']}")
                return best
            debug_print(f"[LocalVision] OCR未匹配 (指令: {target_text}, 搜索词: {search_terms[:6]})")
            return None
        except Exception as e:
            debug_print(f"[LocalVision] OCR定位失败: {e}")
            return None

    async def locate_by_template(self, screenshot_bytes: bytes, template_name: str, confidence: float = 0.8) -> Optional[Dict[str, Any]]:
        """
        OpenCV 模板匹配定位

        在截图中搜索预存的图标模板（templates/ 目录），返回匹配区域的中心坐标。

        Args:
            screenshot_bytes: 页面截图原始字节
            template_name: 模板文件名（如 "filter_icon.png"）
            confidence: 匹配置信度阈值（默认 0.8）

        Returns:
            {"x": int, "y": int, "confidence": float, "method": "template"} 或 None
        """
        if not self._available:
            return None
        try:
            template_path = os.path.join(self._templates_dir, template_name)
            if not os.path.exists(template_path):
                return None
            nparr = np.frombuffer(screenshot_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            template = cv2.imread(template_path, cv2.IMREAD_GRAYSCALE)
            if img is None or template is None:
                return None
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            result = cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED)
            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)
            if max_val >= confidence:
                x = max_loc[0] + template.shape[1] // 2
                y = max_loc[1] + template.shape[0] // 2
                debug_print(f"[LocalVision] 模板匹配命中: template='{template_name}', conf={max_val:.2f}, pos=({x},{y})")
                return {
                    "x": x, "y": y,
                    "w": template.shape[1], "h": template.shape[0],
                    "confidence": max_val,
                    "method": "template"
                }
            return None
        except Exception as e:
            debug_print(f"[LocalVision] 模板匹配失败: {e}")
            return None

    def get_template_name(self, instruction: str) -> Optional[str]:
        """根据指令关键词匹配图标模板文件名（如"筛选" → "filter_icon.png"）"""
        icon_map = {
            '搜索': 'search_icon.png',
            '筛选': 'filter_icon.png',
            '刷新': 'refresh_icon.png',
            '新增': 'add_icon.png',
            '导出': 'export_icon.png',
            '导入': 'import_icon.png',
            '编辑': 'edit_icon.png',
            '删除': 'delete_icon.png',
            '关闭': 'close_icon.png',
        }
        instruction_lower = instruction.lower()
        for keyword, template_name in icon_map.items():
            if keyword in instruction_lower:
                return template_name
        return None
