"""
executor - 智能执行器（核心调度引擎）

SmartExecutor 是整个 precision_locator 的核心，实现四级降级定位策略：

    Level 0 (零 Token): 本地 OCR + 模板匹配 → 坐标 → 稳定定位器
    Level 1 (低 Token): 文本定位 → testid → DOM 视觉 → AI → 规则引擎 → 直接查询
    Level 2 (中 Token): 截图 + 多模态 LLM → 定位器
    Level 3 (高 Token): 截图 + 多模态 LLM → 坐标

每个 Level 失败后自动降级到下一级，直到定位成功或全部失败。

同时提供坐标 → 稳定定位器转换机制（build_stable_locator_from_coords），
将分辨率依赖的像素坐标反查为基于 DOM 属性的稳定 Playwright 定位器。
"""

import asyncio
import base64
import re
from typing import Any, Dict, List, Optional

from playwright.async_api import Page

from precision_locator.utils import debug_print, sanitize_filename
from precision_locator.dom_extractor import DOMExtractor
from precision_locator.locator_agent import LocatorAgent
from precision_locator.visual_locator import VisualLocator
from precision_locator.screenshot_locator import ScreenshotLocator
from precision_locator.safe_locator import safe_build_locator
from precision_locator.local_vision import LocalVisionLocator


class SmartExecutor:
    """
    智能执行器：协调四级降级策略完成元素定位与操作

    聚合所有定位器模块（LocalVision / DOMExtractor / LocatorAgent /
    VisualLocator / ScreenshotLocator），通过 _try_locate_and_act 统一调度。
    """

    def __init__(self, page: Page):
        self.page = page
        self.extractor = DOMExtractor()
        self.locator_agent = LocatorAgent()
        self.visual_locator = VisualLocator()
        self.screenshot_locator = ScreenshotLocator()
        self.local_vision = LocalVisionLocator.get_instance()

    async def _wait_page_stable(self):
        """等待页面达到稳定状态（networkidle + complete），超时不阻塞"""
        try:
            await self.page.wait_for_load_state('networkidle', timeout=5000)
        except Exception:
            pass
        try:
            await self.page.wait_for_function(
                "() => document.readyState === 'complete'",
                timeout=3000
            )
        except Exception:
            await asyncio.sleep(0.5)

    async def _try_locate_and_act(self, instruction: str, action: str, value: str = None) -> Dict[str, Any]:
        """
        核心定位与执行方法（四级降级策略）

        Args:
            instruction: 用户自然语言指令
            action: 操作类型（click/fill/select/hover/check）
            value: 填充值（仅 fill/select 操作需要）

        Returns:
            {"success": bool, "locator": str, "method": str, ...}
            失败时包含 "error" 字段
        """
        max_attempts = 2
        last_error = None
        screenshot_tried = False

        for attempt in range(max_attempts):
            await self._wait_page_stable()

            dom = await self.extractor.get_compact_dom(self.page, hint=instruction)
            if not dom and attempt == 0:
                debug_print("[SmartExecutor] 首次采集 DOM 为空，等待后重试...")
                await asyncio.sleep(3)
                dom = await self.extractor.get_compact_dom(self.page, hint=instruction)

            current_url = self.page.url
            if not dom:
                if attempt == 0:
                    debug_print(f"[SmartExecutor] DOM为空，跳过Level 1/2，直接尝试Level 3")
                    break
                return {"success": False, "error": f"页面 {current_url} 没有可用的交互元素"}

            locator_expr = None
            visual_used = False

            if attempt == 0:
                if self.local_vision.available:
                    level0_result = None
                    try:
                        try:
                            await self.page.evaluate("document.activeElement?.blur()")
                        except Exception:
                            pass
                        await asyncio.sleep(0.2)
                        screenshot_bytes = await self.page.screenshot()
                        level0_result = await self.local_vision.locate_by_text(screenshot_bytes, instruction)
                        if not level0_result:
                            template_name = self.local_vision.get_template_name(instruction)
                            if template_name:
                                level0_result = await self.local_vision.locate_by_template(screenshot_bytes, template_name)
                    except Exception as e:
                        debug_print(f"[SmartExecutor] Level 0 本地视觉异常: {e}")

                    if level0_result:
                        x, y = level0_result['x'], level0_result['y']
                        debug_print(f"[SmartExecutor] Level 0 OCR命中: ({x}, {y}), method={level0_result.get('method')}, text='{level0_result.get('text', '')}'")

                        stable_locator = await self.build_stable_locator_from_coords(x, y, instruction)
                        if stable_locator:
                            debug_print(f"[SmartExecutor] Level 0 → 稳定定位器: {stable_locator}")
                            try:
                                loc = safe_build_locator(self.page, stable_locator)
                                if loc:
                                    if action == "click":
                                        await loc.click(timeout=5000)
                                    elif action == "fill" and value is not None:
                                        await loc.fill(value, timeout=5000)
                                    elif action == "hover":
                                        await loc.hover(timeout=5000)
                                    else:
                                        await loc.click(timeout=5000)
                                    return {"success": True, "method": "level0_stable",
                                            "locator": stable_locator, "visual_used": True}
                            except Exception as e:
                                debug_print(f"[SmartExecutor] Level 0 稳定定位器执行失败: {e}，进入 Level 1")
                        else:
                            debug_print(f"[SmartExecutor] Level 0 无法生成稳定定位器，降级到坐标点击")
                            try:
                                if action == "click":
                                    await self.page.mouse.click(x, y)
                                elif action == "fill" and value is not None:
                                    await self.page.mouse.click(x, y)
                                    await asyncio.sleep(0.3)
                                    await self.page.keyboard.type(value, delay=30)
                                elif action == "hover":
                                    await self.page.mouse.move(x, y)
                                else:
                                    await self.page.mouse.click(x, y)
                                return {"success": True, "method": "local_vision_coordinate",
                                        "x": x, "y": y,
                                        "detail": level0_result, "visual_used": True}
                            except Exception as e:
                                debug_print(f"[SmartExecutor] Level 0 坐标点击也失败: {e}，进入 Level 1")
                    else:
                        debug_print(f"[SmartExecutor] Level 0 OCR未命中，尝试DOM补充查询")

                        dom_result = await self._dom_supplement_locate(instruction, action)
                        if dom_result:
                            debug_print(f"[SmartExecutor] Level 0 DOM补充命中: {dom_result}")
                            locator_expr = dom_result
                            visual_used = True

                locator_expr = self._match_by_testid(dom, instruction)

                if not locator_expr and any(kw in instruction for kw in ['表头', '筛选', '图标', 'filter', 'dialog', '弹窗']):
                    locator_expr = await self.visual_locator.analyze_and_locate(instruction, dom)
                    if locator_expr:
                        visual_used = True

                if not locator_expr:
                    locator_expr = await self.locator_agent.generate_locator(dom, instruction, action)

                if not locator_expr:
                    locator_expr = self._fallback_rule(dom, instruction, action)

                if not locator_expr:
                    locator_expr = await self._direct_text_query(dom, instruction, action)

                if not locator_expr:
                    debug_print(f"[SmartExecutor] Level 1 文本定位失败，准备 Level 2 截图视觉定位 (指令: {instruction})")
                    continue

            else:
                if not screenshot_tried:
                    screenshot_tried = True
                    debug_print(f"[SmartExecutor] Level 2: 截图视觉定位... (指令: {instruction})")
                    try:
                        screenshot_bytes = await self.page.screenshot()
                        img_base64 = base64.b64encode(screenshot_bytes).decode('utf-8')
                        locator_expr = await self.screenshot_locator.analyze(img_base64, instruction)
                        if locator_expr:
                            visual_used = True
                            debug_print(f"[SmartExecutor] Level 2 截图视觉定位成功: {locator_expr}")
                        else:
                            debug_print("[SmartExecutor] Level 2 截图视觉定位器生成失败")
                    except Exception as e:
                        debug_print(f"[SmartExecutor] Level 2 截图失败: {e}")

                if not locator_expr:
                    locator_expr = await self._direct_text_query(dom, instruction, action)
                    if locator_expr:
                        debug_print(f"[SmartExecutor] Level 2 direct query 成功: {locator_expr}")

                if not locator_expr:
                    locator_expr = await self.visual_locator.analyze_and_locate(instruction, dom)
                    if locator_expr:
                        visual_used = True

                if not locator_expr:
                    debug_print(f"[SmartExecutor] Level 2 也失败，准备 Level 3 坐标定位 (指令: {instruction})")
                    continue

            debug_print(f"[SmartExecutor] 尝试定位器: {locator_expr}")
            try:
                locator = safe_build_locator(self.page, locator_expr)
                if action == "click":
                    await locator.click(timeout=5000)
                elif action == "fill" and value is not None:
                    await locator.fill(value, timeout=5000)
                elif action == "select" and value is not None:
                    await locator.select_option(value, timeout=5000)
                elif action == "hover":
                    await locator.hover(timeout=5000)
                elif action == "check":
                    await locator.check(timeout=5000)
                else:
                    return {"success": False, "error": f"不支持的操作: {action}"}
                return {"success": True, "locator": locator_expr, "visual_used": visual_used}
            except Exception as e:
                error_msg = str(e)
                if 'strict mode violation' in error_msg:
                    aka_matches = re.findall(r'aka (get_by_\w+\([^)]+\)|locator\([^)]+\))', error_msg)
                    recommended = None
                    for candidate in aka_matches:
                        if 'get_by_role' in candidate or 'get_by_placeholder' in candidate:
                            recommended = candidate
                            break
                    if not recommended and aka_matches:
                        recommended = aka_matches[0]
                    if recommended:
                        if not recommended.startswith('page.'): recommended = 'page.' + recommended
                        debug_print(f"[SmartExecutor] 尝试使用推荐定位器: {recommended}")
                        try:
                            locator = safe_build_locator(self.page, recommended)
                            if action == "click":
                                await locator.click(timeout=5000)
                            elif action == "fill" and value is not None:
                                await locator.fill(value, timeout=5000)
                            elif action == "select" and value is not None:
                                await locator.select_option(value, timeout=5000)
                            elif action == "hover":
                                await locator.hover(timeout=5000)
                            elif action == "check":
                                await locator.check(timeout=5000)
                            return {"success": True, "locator": recommended, "note": "通过 strict mode 自动修正"}
                        except Exception as retry_e:
                            debug_print(f"[SmartExecutor] 推荐定位器也失败: {retry_e}")
                last_error = f"操作失败: {error_msg}"
                debug_print(f"[SmartExecutor] 第{attempt+1}次失败: {last_error}")
                if 'intercepts pointer events' in error_msg and action == 'click':
                    debug_print(f"[SmartExecutor] 元素被遮挡，尝试 force click")
                    try:
                        await locator.click(force=True, timeout=3000)
                        return {"success": True, "locator": locator_expr, "note": "force click", "visual_used": visual_used}
                    except Exception as force_e:
                        debug_print(f"[SmartExecutor] force click 也失败: {force_e}")

        debug_print(f"[SmartExecutor] Level 3: 尝试坐标定位... (指令: {instruction})")
        try:
            screenshot_bytes = await self.page.screenshot()
            img_base64 = base64.b64encode(screenshot_bytes).decode('utf-8')

            debug_path = f"debug_screenshot_{sanitize_filename(instruction)}.png"
            try:
                with open(debug_path, 'wb') as f:
                    f.write(screenshot_bytes)
                debug_print(f"[SmartExecutor] 调试截图已保存: {debug_path}")
            except Exception:
                pass

            coords = await self.screenshot_locator.analyze_coordinates(img_base64, instruction)
            if coords:
                x, y = coords['x'], coords['y']
                debug_print(f"[SmartExecutor] Level 3 坐标定位命中: ({x}, {y})")

                stable_locator = await self.build_stable_locator_from_coords(x, y, instruction)
                if stable_locator:
                    debug_print(f"[SmartExecutor] Level 3 → 稳定定位器: {stable_locator}")
                    try:
                        loc = safe_build_locator(self.page, stable_locator)
                        if action == "click":
                            await loc.click(timeout=5000)
                        elif action == "fill" and value is not None:
                            await loc.fill(value, timeout=5000)
                        elif action == "hover":
                            await loc.hover(timeout=5000)
                        return {"success": True, "method": "level3_stable", "locator": stable_locator, "visual_used": True}
                    except Exception as stable_e:
                        debug_print(f"[SmartExecutor] Level 3 稳定定位器执行失败: {stable_e}")

                try:
                    if action == "click":
                        await self.page.mouse.click(x, y)
                    elif action == "fill" and value is not None:
                        await self.page.mouse.click(x, y)
                        await asyncio.sleep(0.3)
                        await self.page.keyboard.type(value, delay=30)
                    elif action == "hover":
                        await self.page.mouse.move(x, y)
                    else:
                        await self.page.mouse.click(x, y)
                    return {"success": True, "method": "coordinate_click", "x": x, "y": y,
                            "description": coords.get('description', ''), "visual_used": True}
                except Exception as coord_e:
                    debug_print(f"[SmartExecutor] Level 3 坐标点击执行失败: {coord_e}")
                    last_error = f"坐标点击失败: {coord_e}"
            else:
                debug_print("[SmartExecutor] Level 3 坐标分析也未找到目标元素")
                last_error = "所有定位策略均失败（文本定位 + 截图视觉 + 坐标定位）"
        except Exception as level3_e:
            debug_print(f"[SmartExecutor] Level 3 执行异常: {level3_e}")
            last_error = f"Level 3 异常: {level3_e}"

        return {"success": False, "error": f"{last_error} (页面: {current_url})"}

    async def _dom_supplement_locate(self, instruction: str, action: str) -> Optional[str]:
        """
        DOM 补充定位：当 OCR 无法识别（如白色文字在透明背景上）时，
        直接查询 DOM 的 placeholder/role/text 属性生成定位器

        使用 bigram/trigram 分词从指令中提取搜索词，匹配 DOM 元素。
        """
        if not instruction:
            return None
        chinese_chars = re.findall(r'[\u4e00-\u9fa5]', instruction.lower())
        if not chinese_chars:
            return None
        stopwords = {'点击','切换','选择','输入','打开','关闭','这个','那个','请','帮我',
                     '按钮','标签','菜单','表头','筛选','图标','列表','列',
                     '右侧','左侧','上方','下方','第一个','最后一个','里面','外面',
                     '弹出','框中','标签页','子菜单','展开'}
        search_terms = []
        for n in [4, 3, 2]:
            for i in range(len(chinese_chars) - n + 1):
                seg = ''.join(chinese_chars[i:i+n])
                if seg not in stopwords:
                    search_terms.append(seg)
        if not search_terms:
            return None
        try:
            if action in ('fill', 'type'):
                placeholders = await self.page.evaluate("""() => {
                    const inputs = document.querySelectorAll('input:not([type="hidden"]), textarea');
                    return Array.from(inputs).map(el => ({
                        tag: el.tagName,
                        type: el.type || '',
                        placeholder: el.placeholder || '',
                        name: el.name || '',
                        visible: el.offsetParent !== null
                    })).filter(el => el.placeholder && el.visible);
                }""")
                for inp in placeholders:
                    ph = inp['placeholder']
                    if any(term in ph.lower() for term in search_terms):
                        escaped = ph.replace('"', '\\"')
                        debug_print(f"[DomSupplement] placeholder匹配: {ph}")
                        return f'page.get_by_placeholder("{escaped}")'

            elements = await self.page.evaluate("""(searchTerms) => {
                const all = document.querySelectorAll('button, a, [role="button"], [role="tab"], [role="menuitem"], [role="radio"], .el-radio-button, .el-tabs__item, label');
                return Array.from(all).map(el => ({
                    tag: el.tagName,
                    text: (el.innerText || '').trim().substring(0, 50),
                    role: el.getAttribute('role') || '',
                    visible: el.offsetParent !== null && el.getBoundingClientRect().width > 0
                })).filter(el => el.visible && el.text);
            }""", search_terms)
            for el in elements:
                text = el['text'].lower()
                if '\n' in text:
                    continue
                best_len = 0
                for term in search_terms:
                    if term in text:
                        best_len = max(best_len, len(term))
                if best_len >= 2 and len(el['text']) <= 20:
                    escaped = el['text'].replace('"', '\\"')
                    role = el['role']
                    tag = el['tag'].lower()
                    if role:
                        return f'page.get_by_role("{role}", name="{escaped}")'
                    elif tag == 'button':
                        return f'page.get_by_role("button", name="{escaped}")'
                    else:
                        return f'page.get_by_text("{escaped}")'
            return None
        except Exception as e:
            debug_print(f"[DomSupplement] 查询失败: {e}")
            return None

    def _match_by_testid(self, dom: list, instruction: str) -> Optional[str]:
        """Level 1a: 从指令中提取关键词，匹配 DOM 元素的 data-testid 属性"""
        if not instruction: return None
        english_words = re.findall(r'[a-zA-Z0-9]+', instruction.lower())
        chinese_chars = re.findall(r'[\u4e00-\u9fa5]', instruction.lower())
        stopwords = {'点击','切换','选择','输入','打开','关闭','这个','那个','请','帮我',
                     '按钮','标签','菜单','表头','筛选','图标','列表','列',
                     '右侧','左侧','上方','下方','第一个','最后一个','里面','外面'}
        words = [w for w in english_words if len(w) >= 2]
        for n in [4, 3, 2]:
            for i in range(len(chinese_chars) - n + 1):
                seg = ''.join(chinese_chars[i:i+n])
                if seg not in stopwords:
                    words.append(seg)
        if not words: return None
        for el in dom:
            tid = el.get('dataTestId')
            if tid and any(w in tid.lower() for w in words):
                return f"page.locator('[data-testid=\"{tid}\"]')"
        return None

    def _fallback_rule(self, dom: list, instruction: str, action: str) -> Optional[str]:
        """
        Level 1d: 规则引擎 Fallback

        基于硬编码规则匹配常见 UI 模式：
            - 筛选图标 → th.filter().locator("img, svg")
            - 筛选/查询/确定按钮 → get_by_role("button")
            - 登录/注册等常用按钮 → get_by_role("button")
            - Tab/标签页 → get_by_role("tab") 或 locator(".el-radio-button").filter()
            - 通用匹配 → testid > radio > tab > menu > placeholder > role > text
        """
        instruction_lower = instruction.lower()
        english_words = re.findall(r'[a-zA-Z0-9]+', instruction_lower)
        chinese_chars = re.findall(r'[\u4e00-\u9fa5]', instruction_lower)
        stopwords = {'点击','切换','选择','输入','打开','关闭','这个','那个','请','帮我',
                     '按钮','标签','菜单','表头','筛选','图标','列表','列',
                     '右侧','左侧','上方','下方','第一个','最后一个','里面','外面'}
        chinese_keywords = []
        if chinese_chars:
            for w in english_words:
                if len(w) >= 2 and w not in stopwords:
                    chinese_keywords.append(w)
            for n in [4, 3, 2]:
                for i in range(len(chinese_chars) - n + 1):
                    seg = ''.join(chinese_chars[i:i+n])
                    if seg not in stopwords:
                        chinese_keywords.append(seg)
        seen = set()
        keywords = []
        for w in chinese_keywords:
            if w not in seen:
                seen.add(w)
                keywords.append(w)
        keywords.sort(key=len, reverse=True)
        debug_print(f"[FallbackRule] 指令: {instruction}, 关键词: {keywords[:15]}")
        if not keywords: return None

        if ('筛选' in instruction_lower or 'filter' in instruction_lower) and ('图标' in instruction_lower or 'icon' in instruction_lower):
            for el in dom:
                original_text = el.get('text') or ''
                if el.get('tag') in ('th','td') and any(kw in original_text.lower() for kw in keywords):
                    escaped_text = original_text.replace('"','\\"')
                    return f'page.locator("th").filter(has_text="{escaped_text}").locator("img, svg, i").first'
            for kw in keywords:
                return f'page.get_by_text("{kw}").locator("xpath=ancestor::th").locator("img, svg, i").first'

        if '筛选' in instruction_lower or '查询' in instruction_lower or '确定' in instruction_lower:
            for el in dom:
                if el.get('tag') == 'button' or el.get('role') == 'button':
                    text = el.get('text') or ''
                    if any(kw in text for kw in ['筛选','查询','确定','搜索','查找']):
                        escaped_text = text.replace('"','\\"')
                        return f'page.get_by_role("button", name="{escaped_text}")'

        button_keywords = {'登录','注册','提交','保存','确认','取消'}
        if any(kw in instruction_lower for kw in button_keywords):
            for el in dom:
                if el.get('tag') == 'button' or el.get('type') == 'submit':
                    text = el.get('text') or ''
                    if text: return f'page.get_by_role("button", name="{text}")'

        if 'tab' in instruction_lower or '标签' in instruction_lower or '选项卡' in instruction_lower:
            tab_elements = []
            for el in dom:
                class_name = (el.get('className') or '').lower()
                el_role = el.get('role') or ''
                is_tab = (el_role == 'tab' or
                          bool(re.search(r'\bel-tabs__item\b', class_name)) or
                          bool(re.search(r'\bel-radio-button\b', class_name)) or
                          bool(re.search(r'\btab-item\b', class_name)) or
                          bool(re.search(r'\bnav-item\b', class_name)) or
                          bool(re.search(r'\btab-pane\b', class_name)))
                if is_tab:
                    tab_elements.append(el)
                    original_text = el.get('text') or ''
                    debug_print(f"[FallbackRule] 找到tab/radio元素: text='{original_text[:50]}', role={el_role}, class={class_name[:60]}")
                    if original_text and any(kw in original_text for kw in keywords):
                        if 'is-active' in class_name:
                            debug_print(f"[FallbackRule] 跳过已激活的tab: {original_text[:30]}")
                            continue
                        escaped_text = original_text.replace('"','\\"').replace('\n',' ')
                        debug_print(f"[FallbackRule] tab匹配成功: {escaped_text}")
                        if el_role == 'tab' or 'tabs__item' in class_name:
                            return f'page.get_by_role("tab", name="{escaped_text}")'
                        elif 'radio-button' in class_name:
                            return f'page.locator(".el-radio-button").filter(has_text="{escaped_text}")'
            debug_print(f"[FallbackRule] DOM中找到 {len(tab_elements)} 个tab/radio元素，无文本匹配")
            short_keywords = sorted([kw for kw in keywords if len(kw) >= 2], key=len)
            for kw in short_keywords[:3]:
                debug_print(f"[FallbackRule] 尝试关键词定位tab: {kw}")
                return f'page.get_by_role("tab", name="{kw}")'

        for el in dom:
            original_text = el.get('text') or ''
            if '\n' in original_text:
                continue
            text = original_text.lower()
            original_placeholder = el.get('placeholder') or ''
            placeholder = original_placeholder.lower()
            role = el.get('role') or ''
            is_radio = el.get('isRadio')
            is_tab = el.get('isTab')
            is_menu = el.get('isMenu')
            testid = el.get('dataTestId')
            if not any(kw in text or kw in placeholder for kw in keywords): continue
            if is_menu and len(original_text) > 15:
                debug_print(f"[FallbackRule] 跳过过长菜单文本: {original_text[:40]}")
                continue
            if testid: return f"page.locator('[data-testid=\"{testid}\"]')"
            escaped_text = original_text.replace('"','\\"') if original_text else ""
            if is_radio: return f'page.get_by_role("radio", name="{escaped_text}")'
            if is_tab: return f'page.get_by_role("tab", name="{escaped_text}")'
            if is_menu: return f'page.get_by_role("menuitem", name="{escaped_text}")'
            if original_placeholder and action == 'fill':
                escaped_ph = original_placeholder.replace('"','\\"')
                return f'page.get_by_placeholder("{escaped_ph}")'
            if role: return f'page.get_by_role("{role}", name="{escaped_text}")'
            if original_text: return f'page.get_by_text("{escaped_text}")'
        return None

    async def _direct_text_query(self, dom: list, instruction: str, action: str) -> Optional[str]:
        """
        Level 1e / Level 2b: 绕过 DOM 提取，直接用 Playwright 在页面中搜索文本

        遍历匹配选择器（tab/input/button/a 等）的前 30 个元素，
        逐个检查 inner_text 是否包含搜索词，生成定位器。
        """
        chinese_chars = re.findall(r'[\u4e00-\u9fa5]', instruction)
        english_words = re.findall(r'[a-zA-Z0-9]+', instruction)
        stopwords = {'点击','切换','选择','输入','打开','关闭','这个','那个','请','帮我',
                     '按钮','标签','菜单','表头','筛选','图标','列表','列',
                     '右侧','左侧','上方','下方','第一个','最后一个','里面','外面'}
        all_text_in_dom = set()
        for el in dom:
            for key in ('text', 'placeholder', 'ariaLabel', 'title'):
                val = el.get(key)
                if val and isinstance(val, str):
                    all_text_in_dom.add(val)

        segments = []
        for w in english_words:
            if len(w) >= 2 and w not in stopwords:
                segments.append(w)
        for n in [4, 3, 2]:
            for i in range(len(chinese_chars) - n + 1):
                seg = ''.join(chinese_chars[i:i+n])
                if seg not in stopwords:
                    segments.append(seg)

        seen = set()
        unique_segments = []
        for s in segments:
            if s not in seen:
                seen.add(s)
                unique_segments.append(s)
        unique_segments.sort(key=len, reverse=True)

        is_tab_instruction = any(kw in instruction.lower() for kw in ['tab', '标签', '选项卡'])
        query_selectors = []
        if is_tab_instruction:
            query_selectors.append('[role="tab"]')
            query_selectors.append('.el-tabs__item')
            query_selectors.append('.el-radio-button')
            query_selectors.append('[role="radio"]')

        if action == 'fill':
            query_selectors.append('input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"])')
            query_selectors.append('textarea')

        if not query_selectors:
            query_selectors.append('button')
            query_selectors.append('a')
            query_selectors.append('[role="button"]')
            query_selectors.append('[role="menuitem"]')
            query_selectors.append('label')

        if '筛选' in instruction.lower() and action == 'click':
            for btn_text in ['筛选', '查询', '确定', '搜索', '查找', '重置']:
                try:
                    btn_locator = self.page.get_by_role("button", name=btn_text)
                    if await btn_locator.count() > 0:
                        debug_print(f"[DirectQuery] 找到弹窗按钮: {btn_text}")
                        return f'page.get_by_role("button", name="{btn_text}")'
                except Exception:
                    pass

        for seg in unique_segments[:8]:
            if len(seg) < 2:
                continue
            for sel in query_selectors:
                try:
                    locator = self.page.locator(sel)
                    count = await locator.count()
                    for idx in range(min(count, 30)):
                        try:
                            el = locator.nth(idx)
                            el_text = (await el.inner_text()).strip()
                            if not el_text or seg not in el_text:
                                continue
                            if len(el_text) > len(seg) * 4:
                                children = await el.locator('> *').count()
                                if children > 1:
                                    continue
                            escaped = el_text.replace('"', '\\"').replace('\n', ' ')
                            el_class = (await el.get_attribute('class')) or ''
                            el_role = (await el.get_attribute('role')) or ''
                            debug_print(f"[DirectQuery] 找到: text='{escaped[:60]}', role={el_role}, class={el_class[:50]}', seg='{seg}'")
                            if 'radio-button' in el_class:
                                return f'page.locator(".el-radio-button").filter(has_text="{escaped}").last'
                            if 'tabs__item' in el_class:
                                return f'page.get_by_role("tab", name="{escaped}")'
                            if el_role:
                                return f'page.get_by_role("{el_role}", name="{escaped}")'
                            return f'page.locator("{sel}").filter(has_text="{seg}")'
                        except Exception:
                            pass
                except Exception:
                    pass

        return None

    async def get_element_by_coordinates(self, x: int, y: int):
        """
        通过 document.elementFromPoint 将像素坐标反查为 DOM ElementHandle

        自动向子元素细化：如果子元素占据父元素 50% 以上面积，则选择子元素。

        Args:
            x: 像素 X 坐标
            y: 像素 Y 坐标

        Returns:
            Playwright ElementHandle，或 None
        """
        try:
            element_handle = await self.page.evaluate_handle(
                """({x, y}) => {
                    let el = document.elementFromPoint(x, y);
                    for (let i = 0; i < 5 && el; i++) {
                        const child = el.querySelector('*');
                        if (!child) break;
                        const childRect = child.getBoundingClientRect();
                        const elRect = el.getBoundingClientRect();
                        if (childRect.width < elRect.width * 0.5) break;
                        el = child;
                    }
                    return el;
                }""",
                {"x": x, "y": y}
            )
            element = element_handle.as_element()
            if element is not None:
                return element
            return None
        except Exception as e:
            debug_print(f"[SmartExecutor] 坐标反查DOM失败: {e}")
            return None

    async def _find_icon_near_element(self, element, x: int, y: int) -> Optional[str]:
        """
        在指定元素附近查找图标（img/svg/filter-icon）

        向上遍历 5 层父元素，若找到 th/td/header，则搜索其内部的 img/svg。
        同时通过 elementsFromPoint 检查坐标附近的图标元素。

        Args:
            element: 目标 ElementHandle
            x: 像素 X 坐标
            y: 像素 Y 坐标

        Returns:
            Playwright 定位器字符串，或 None
        """
        try:
            result = await self.page.evaluate("""({el, x, y}) => {
                let parent = el;
                for (let i = 0; i < 5; i++) {
                    if (!parent) break;
                    const tag = parent.tagName.toLowerCase();
                    if (tag === 'th' || tag === 'td' || tag === 'header' || parent.getAttribute('role') === 'columnheader') {
                        const icon = parent.querySelector('img, svg, .el-table__column-filter-trigger, [class*="filter"], [class*="icon"]');
                        if (icon) {
                            const rect = icon.getBoundingClientRect();
                            return {
                                found: true,
                                parentTag: tag,
                                parentText: (parent.innerText || '').trim().substring(0, 30),
                                iconTag: icon.tagName.toLowerCase(),
                                iconClass: icon.getAttribute('class') || ''
                            };
                        }
                    }
                    parent = parent.parentElement;
                }
                const nearby = document.elementsFromPoint(x, y);
                for (const el of nearby) {
                    const tag = el.tagName.toLowerCase();
                    if (tag === 'img' || tag === 'svg' || (el.getAttribute('class') || '').includes('icon') || (el.getAttribute('class') || '').includes('filter')) {
                        const rect = el.getBoundingClientRect();
                        return {
                            found: true,
                            parentTag: '',
                            parentText: '',
                            iconTag: tag,
                            iconClass: el.getAttribute('class') || ''
                        };
                    }
                }
                return {found: false};
            }""", {"el": element, "x": x, "y": y})

            if result and result.get('found'):
                parent_text = result.get('parentText', '').strip()
                if parent_text:
                    escaped = parent_text.split('\n')[0].strip().replace('"', '\\"')
                    debug_print(f"[SmartExecutor] 坐标({x},{y})→图标定位: th内文字='{escaped}', icon={result.get('iconTag')}")
                    return f'page.locator("th").filter(has_text="{escaped}").locator("img, svg").first'
                debug_print(f"[SmartExecutor] 坐标({x},{y})→附近图标: {result.get('iconTag')}, class={result.get('iconClass')}")
                return None
            return None
        except Exception as e:
            debug_print(f"[SmartExecutor] _find_icon_near_element失败: {e}")
            return None

    async def build_stable_locator_from_coords(self, x: int, y: int, instruction: str = "") -> Optional[str]:
        """
        坐标 → 稳定定位器转换

        通过 document.elementFromPoint 反查 DOM 元素，提取其属性
        （data-testid > placeholder > role+name > text > class），
        生成不依赖分辨率的稳定 Playwright 定位器。

        对于图标定位请求（指令含"图标"/"icon"），额外调用 _find_icon_near_element。

        Args:
            x: 像素 X 坐标
            y: 像素 Y 坐标
            instruction: 用户指令（用于判断是否为图标请求）

        Returns:
            Playwright 定位器字符串，或 None
        """
        is_icon_request = any(kw in instruction for kw in ['图标', 'icon', 'Icon', 'ICON', 'filter icon', 'svg'])
        if is_icon_request:
            debug_print(f"[SmartExecutor] 检测到图标定位请求: {instruction[:50]}")

        element = await self.get_element_by_coordinates(x, y)
        if not element:
            return None
        try:
            if is_icon_request:
                icon_locator = await self._find_icon_near_element(element, x, y)
                if icon_locator:
                    return icon_locator

            attrs = await element.evaluate("""el => {
                const result = {
                    tag: el.tagName.toLowerCase(),
                    role: el.getAttribute('role') || '',
                    testid: el.getAttribute('data-testid') || el.getAttribute('data-test-id') || '',
                    placeholder: el.placeholder || el.getAttribute('placeholder') || '',
                    ariaLabel: el.getAttribute('aria-label') || '',
                    className: el.getAttribute('class') || '',
                    type: el.getAttribute('type') || '',
                    name: ''
                };
                if (result.tag === 'button') result.role = result.role || 'button';
                if (result.tag === 'a') result.role = result.role || 'link';
                if (result.tag === 'input' && result.type === 'radio') result.role = result.role || 'radio';
                if (result.tag === 'input' && result.type === 'checkbox') result.role = result.role || 'checkbox';
                const innerText = (el.innerText || '').trim().substring(0, 100);
                const textContent = (el.textContent || '').trim().substring(0, 100);
                result.name = result.ariaLabel || innerText || textContent;
                return result;
            }""")

            if attrs.get('testid'):
                testid = attrs['testid']
                debug_print(f"[SmartExecutor] 坐标({x},{y})→data-testid: {testid}")
                return f'page.locator("[data-testid=\\"{testid}\\"]")'

            if attrs.get('placeholder'):
                ph = attrs['placeholder'].replace('"', '\\"')
                debug_print(f"[SmartExecutor] 坐标({x},{y})→placeholder: {ph}")
                return f'page.get_by_placeholder("{ph}")'

            role = attrs.get('role', '')
            name = attrs.get('name', '').split('\\n')[0].strip()
            tag = attrs.get('tag', '')

            if role and name and len(name) <= 50:
                escaped_name = name.replace('"', '\\"')
                debug_print(f"[SmartExecutor] 坐标({x},{y})→role={role}, name={escaped_name}")
                return f'page.get_by_role("{role}", name="{escaped_name}")'

            if name and len(name) <= 50:
                escaped_name = name.replace('"', '\\"')
                debug_print(f"[SmartExecutor] 坐标({x},{y})→text: {escaped_name}")
                return f'page.get_by_text("{escaped_name}")'

            class_attr = attrs.get('className', '')
            if class_attr:
                first_class = class_attr.split()[0]
                if first_class and re.match(r'^[a-zA-Z][\w-]*$', first_class):
                    debug_print(f"[SmartExecutor] 坐标({x},{y})→class: {first_class}")
                    return f'page.locator(".{first_class}").first'

            debug_print(f"[SmartExecutor] 坐标({x},{y})→无法生成稳定定位器, tag={tag}")
            return None
        except Exception as e:
            debug_print(f"[SmartExecutor] build_stable_locator失败: {e}")
            return None

    async def smart_click(self, instruction: str) -> Dict[str, Any]:
        return await self._try_locate_and_act(instruction, "click")

    async def smart_fill(self, instruction: str, value: str) -> Dict[str, Any]:
        return await self._try_locate_and_act(instruction, "fill", value=value)

    async def smart_select(self, instruction: str, value: str) -> Dict[str, Any]:
        return await self._try_locate_and_act(instruction, "select", value=value)

    async def smart_hover(self, instruction: str) -> Dict[str, Any]:
        return await self._try_locate_and_act(instruction, "hover")

    async def smart_check(self, instruction: str) -> Dict[str, Any]:
        return await self._try_locate_and_act(instruction, "check")

    async def get_page_structure(self, hint: str = "") -> List[Dict]:
        await self._wait_page_stable()
        return await self.extractor.get_compact_dom(self.page, hint=hint)
