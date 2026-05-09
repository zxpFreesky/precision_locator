"""
utils - 共享工具函数与全局配置

提供日志记录、文件名清理、LLM 输出清洗、中文检测等基础工具函数，
供 precision_locator 包内所有模块共用。

路径约定：
    _SCRIPT_DIR  = precision_locator/  包目录
    _PROJECT_DIR = 项目根目录（.env、debug 日志、templates 均在此）
"""

import os
import re
import logging
from typing import Optional

from dotenv import load_dotenv

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_SCRIPT_DIR)
load_dotenv(os.path.join(_PROJECT_DIR, ".env"))

logger = logging.getLogger("precision-locator")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('[%(name)s] %(message)s'))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

_debug_log_file = None


def get_debug_log():
    """获取调试日志文件句柄（首次调用时创建，写入 _PROJECT_DIR/locator_debug.txt）"""
    global _debug_log_file
    if _debug_log_file is None:
        try:
            _debug_log_file = open(os.path.join(_PROJECT_DIR, "locator_debug.txt"), "w", encoding="utf-8")
        except Exception:
            _debug_log_file = None
    return _debug_log_file


def debug_print(msg):
    """双通道日志：同时输出到 stdout 和 locator_debug.txt 文件"""
    print(msg, flush=True)
    f = get_debug_log()
    if f:
        try:
            f.write(msg + "\n")
            f.flush()
        except Exception:
            pass


def sanitize_filename(name: str, max_len: int = 50) -> str:
    """将任意字符串转换为安全的文件名，去除特殊字符并截断"""
    safe = re.sub(r'[\\/*?:"<>|]', '_', name)
    safe = re.sub(r'\s+', '_', safe)
    return safe[:max_len].strip('_') or "screenshot"


def _extract_first_string(args_str: str) -> Optional[str]:
    """从函数调用字符串中提取第一个引号内的参数值，如 get_by_text("登录") → 登录"""
    m = re.search(r"""['"]([^'"]*)['"]""", args_str)
    return m.group(1) if m else None


def _extract_named_arg(args_str: str, name: str) -> Optional[str]:
    """从函数调用字符串中提取命名参数值，如 get_by_role("tab", name="我的客户") → 我的客户"""
    pattern = rf'{name}\s*=\s*["\']([^"\']*)["\']'
    m = re.search(pattern, args_str)
    return m.group(1) if m else None


def clean_llm_output(raw: str) -> str:
    """清洗 LLM 原始输出：移除 <think/> 标签、Markdown 代码块标记"""
    cleaned = re.sub(r'<think.*?</think >', '', raw, flags=re.DOTALL)
    cleaned = re.sub(r'```[\w]*\n?', '', cleaned)
    cleaned = cleaned.replace('```', '')
    return cleaned.strip()


def extract_all_texts(dom: list) -> set:
    """从 DOM 结构列表中收集所有有效文本字段（text/placeholder/ariaLabel/title/name），用于校验 LLM 输出"""
    texts = set()
    for el in dom:
        for key in ('text', 'placeholder', 'ariaLabel', 'title', 'name'):
            val = el.get(key)
            if val and isinstance(val, str):
                texts.add(val)
    return texts


def contains_chinese(text: str) -> bool:
    """检查文本是否包含中文字符（U+4E00 ~ U+9FFF）"""
    return any('\u4e00' <= c <= '\u9fff' for c in text)


def is_translated(generated_text: str, valid_texts: set, instruction: str) -> bool:
    """
    检测 LLM 是否将中文文本翻译为英文：
    当指令包含中文、生成文本不含中文且不在 DOM 有效文本集合中时，判定为翻译行为
    """
    if not generated_text:
        return False
    instruction_has_chinese = contains_chinese(instruction)
    generated_has_chinese = contains_chinese(generated_text)
    if instruction_has_chinese and not generated_has_chinese:
        if generated_text not in valid_texts:
            debug_print(f"[WARNING] 可能存在翻译：指令包含中文，但生成文本 '{generated_text}' 是英文且不在DOM中")
            return True
    return False
