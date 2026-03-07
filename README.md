# AstrBot 搜图神器插件 (astrbot_plugin_soutushenqi)

🎨 基于大语言模型（LLM）与视觉大模型（VLM）的智能搜图插件。通过主图源 API 拦截与 Bing 备用图源的动态组合抓取，结合 VLM 的视觉语义筛选，为机器人提供准确度更高的图片搜索与百科配图能力。

## ✨ 核心特性

### 🖼️ 智能搜图与比对
* **🧠 VLM 裁判优选**：摒弃传统“盲盒搜图”。并发抓取多张候选图片拼接成九宫格，交由视觉大模型仔细端详，精准选出最符合用户需求的那一张。
* **🗣️ 深度自然语言集成**：
    * **问答配图**：当用户提问“歼20是什么？”时，大模型在科普的同时，会自动附上高清配图。
    * **意图识别**：对机器人说“帮我找一张布偶猫的图”，大模型能精准识别意图并自动调用工具，无需死记硬背 `/搜图` 指令。
* **⚙️ 独立场景控制**：指令模式、自然语言模式、百科配图模式的 VLM 筛选开关相互独立，可按需配置以平衡搜索质量与响应速度。。

### 🛡️ 容错架构
* **🚧 防盗链穿透**：针对国内主流图床，内置动态 Referer 与多重 Header 伪造机制，极大降低 `HTTP 403/400` 拦截率。
* **🔀 图源兜底 (Fallback)**：
    * **百度隔离**：主动屏蔽防盗链重灾区（百度系图床），提高基础存活率。
    * **Bing 动态缓冲补充 (Smart Batching)**：主图源经过分辨率过滤和感知哈希去重后若未达到指定数量，系统会自动介入 Bing 备用图源，按缺口数量动态计算冗余并循环补齐，避免一次性过量下载。
* **⚡ 内存级极速流转**：下载与拼图彻底解耦。VLM 选定后，直接从内存读取 `bytes` 缓存发送，省去二次下载，响应速度翻倍。

---

## 📦 安装方法

### 前置要求
* AstrBot 4.10+
* Python 3.10+

### 依赖库
插件需要以下依赖（会自动安装）：
* `playwright`：无头浏览器渲染与抓取
* `aiohttp`：高并发异步网络请求
* `Pillow`：图片内存级拼合处理

### 安装步骤
1.  进入 AstrBot 插件目录：
    ```bash
    cd AstrBot/data/plugins/
    ```
2.  克隆本仓库（或从插件市场直接安装）：
    ```bash
    git clone [https://github.com/674537331/astrbot_plugin_soutushenqi](https://github.com/674537331/astrbot_plugin_soutushenqi)
    ```
3.  安装 Playwright 内核（若环境未安装）：
    ```bash
    playwright install chromium
    ```
4.  在 AstrBot 后台重启机器人或重载插件即可。

---

## 🔧 配置说明 (可视化面板)

插件安装后，可在 AstrBot 后台配置面板进行自定义调整：

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `enable_cmd_vlm_selection` | `true` | 【指令搜图】(/搜图) 是否启用大模型淘汰比对 |
| `enable_nl_search_vlm_selection` | `true` | 【自然语言搜图】(如“帮我搜张图”) 是否启用大模型淘汰比对 |
| `enable_explanation_image` | `true` | 【解释附图】是否启用功能（解答明确实体时自动配图） |
| `enable_explanation_vlm_selection` | `false` | 【解释附图】是否启用大模型淘汰比对（默认关闭以提升解答速度） |
| `vlm_provider_id` | （空） | 用于图片比对的 VLM Provider ID。留空则降级为默认Provider |
| `batch_size` | `9` | 单批次抓取及拼图的候选图片数量（建议 4-9，最大限制为 16）。数量越大图片质量越高，但处理时间会变长 |
| `min_resolution` | `500` | 候选图最小宽高阈值，低于该分辨率的图片会在清洗阶段被过滤丢弃（建议 400-800） |

---

## 🎮 使用指南

### 1. 指令模式 (经典)
精准、快速地通过命令触发：
* `/搜图 最终幻想14`
* `/搜图 歼20高清壁纸`

![指令模式示例](https://raw.githubusercontent.com/674537331/astrbot_plugin_soutushenqi/main/assets/demo_cmd.jpg)

### 2. 自然语言模式 (智能)
无需命令，像和朋友聊天一样提出需求：
* *“帮我找一张初音未来的壁纸”*
* *“给我看看长城长什么样”*

![自然语言模式示例](https://raw.githubusercontent.com/674537331/astrbot_plugin_soutushenqi/main/assets/demo_nl.jpg)

### 3. 百科配图模式 (科普)
触发“What is”类问题时，图文并茂：
* **用户**：*“歼35战斗机是什么？介绍一下”*
* **Bot**：*(发送歼35高清图)* + *"歼35是咱家自研的新型隐身战斗机，也就是空军刚官宣不久的五代机，具备卓越的隐身性能......"*

![百科配图模式示例](https://raw.githubusercontent.com/674537331/astrbot_plugin_soutushenqi/main/assets/demo_wiki.jpg)

---

## 📁 项目结构

```text
astrbot_plugin_soutushenqi/
├── main.py             # 插件主入口（总调度中心与 Bing 混合补充策略）
├── metadata.yaml       # 插件元数据
├── _conf_schema.json   # 可视化配置 Schema
├── requirements.txt    # 依赖列表
├── README.md           # 说明文档
├── scraper.py          # 数据抓取模块（Playwright 抓取 + Bing 兜底提取）
├── composer.py         # 图像处理模块（防盗链穿透 + 并发下载 + 内存拼图）
└── vlm.py              # 模型交互模块（Prompt 构建与 JSON 解析兜底）
```

---

## 🤝 致谢

* [@RC-CHN](https://github.com/RC-CHN) -  `astrbot_plugin_pic_search` 架构灵感
* Gemini3.1 Pro完成代码构建


## 📄 许可证

本项目采用 **AGPL-3.0 License** 开源协议 - 详见 [LICENSE](LICENSE) 文件。

---

如果您发现这个插件对您有所帮助，请给一个 ⭐ **Star** 以示支持！

