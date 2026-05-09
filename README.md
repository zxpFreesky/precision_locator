# Precision Locator

基于 Playwright 的智能元素定位 MCP 服务，支持四级降级策略，实现自然语言指令到精准元素操作的自动转换。

## 架构概览

```
用户指令 → SmartExecutor → 四级降级策略 → Playwright 操作
                             │
                             ├─ Level 0: 本地 OCR + 模板匹配（零 Token）
                             ├─ Level 1: 文本定位（testid → AI → 规则引擎）
                             ├─ Level 2: 截图 + 多模态 LLM 分析
                             └─ Level 3: LLM 坐标定位（终极兜底）
```

### 核心特性

- **四级降级策略**：从零消耗的本地 OCR 逐步降级到 LLM 坐标定位，确保高成功率
- **坐标→稳定定位器转换**：将像素坐标反查为基于 DOM 属性的稳定 Playwright 定位器，消除分辨率依赖
- **多模型支持**：OpenAI / 智谱 / MiniMax / 通义千问 / DeepSeek / Moonshot
- **翻译检测**：自动拦截 LLM 将中文元素文本翻译为英文的行为
- **图标定位**：支持表头内筛选图标、操作按钮等非文字元素的精准定位
- **MCP 协议**：通过 Model Context Protocol 与 AI 客户端无缝集成

## 项目结构

```
precision_locator/
├── __init__.py              # 包入口，统一导出
├── utils.py                 # 工具函数（日志、清洗、中文检测）
├── dom_extractor.py         # DOM 紧凑采集器
├── llm_config.py            # 多模型 LLM 配置
├── locator_agent.py         # AI 定位器生成（Level 1c）
├── visual_locator.py        # DOM 上下文视觉定位（Level 1b）
├── screenshot_locator.py    # 截图视觉定位（Level 2/3）
├── safe_locator.py          # Playwright 定位器安全构建
├── local_vision.py          # 本地 OCR + 模板匹配（Level 0）
├── executor.py              # 智能执行器（核心调度引擎）
└── server.py                # MCP Server 入口
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
playwright install chromium
```

> PaddleOCR 依赖较大（~1.5GB），如不需要 Level 0 本地视觉定位，可跳过相关包。

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入你的 API Key
```

最小配置示例：

```env
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-your-key-here
```

### 3. 启动 MCP Server

```bash
python -m precision_locator.server
```

### 4. 作为 Python 包使用

```python
import asyncio
from precision_locator.server import ensure_browser, _page, _executor

async def main():
    await ensure_browser()
    page = _page
    executor = _executor

    await page.goto("https://example.com")
    result = await executor.smart_click("登录按钮")
    print(result)

asyncio.run(main())
```

## MCP 工具列表

| 工具 | 说明 |
|------|------|
| `navigate` | 导航到指定 URL |
| `smart_click` | 智能点击页面元素 |
| `smart_fill` | 智能填充输入框 |
| `smart_select` | 智能选择下拉选项 |
| `smart_hover` | 智能悬停页面元素 |
| `smart_check` | 智能勾选复选框 |
| `get_page_structure` | 获取页面精简 DOM 结构 |
| `screenshot` | 截取当前页面截图 |
| `close_browser` | 关闭浏览器释放资源 |

## 支持的 LLM 供应商

| 供应商 | LLM_PROVIDER | 默认模型 | 视觉模型 |
|--------|-------------|---------|---------|
| OpenAI | `openai` | gpt-4o | gpt-4o |
| 智谱 AI | `zhipu` | glm-4-plus | glm-4v-plus |
| MiniMax | `minimax` | MiniMax-M2.7-Highspeed | - |
| 通义千问 | `qwen` | qwen-plus | qwen-vl-plus |
| DeepSeek | `deepseek` | deepseek-chat | - |
| Moonshot | `moonshot` | moonshot-v1-8k | - |

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LLM_PROVIDER` | LLM 供应商 | `openai` |
| `LLM_MODEL` | 模型名称 | 各供应商默认值 |
| `LLM_TEMPERATURE` | 采样温度 | `0.0` |
| `VISION_LLM_PROVIDER` | 视觉模型供应商 | 跟随 `LLM_PROVIDER` |
| `VISION_MODEL` | 视觉模型名称 | 各供应商默认值 |
| `HEADLESS` | 无头模式 | `false` |
| `VIEWPORT` | 视口大小 | 浏览器默认 |
| `LOCALE` | 浏览器语言 | 系统默认 |

## 技术细节

### Level 0 - 本地视觉定位

使用 PaddleOCR 进行文字识别，通过 bigram/trigram 分词在 OCR 结果中搜索匹配文本。
当文字不可见（如白色文字在透明背景上）时，自动降级到 DOM 属性查询补充。

排序优先级：最长搜索词匹配 > 最短文本长度 > 最高置信度

### Level 1 - 文本定位

五个子策略按顺序尝试：
1. **data-testid 匹配**：从指令提取关键词匹配元素的 testid
2. **DOM 视觉定位**：复杂场景（表头/图标/弹窗）优先用 LLM + DOM 上下文
3. **AI 定位器生成**：LLM 根据 DOM 结构生成定位器，后校验文本存在性
4. **规则引擎 Fallback**：硬编码规则匹配常见 UI 模式
5. **直接文本查询**：Playwright 原生 API 在页面中搜索

### 坐标→稳定定位器

通过 `document.elementFromPoint` 反查 DOM 元素，按优先级提取属性生成定位器：
`data-testid > placeholder > role+name > text > class`

## License

[MIT](LICENSE)
