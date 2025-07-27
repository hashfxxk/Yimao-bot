# 🐾 一猫 AI 伙伴 Bot

一只温暖、好奇，带有一点点猫咪慵懒和俏皮的 AI 伙伴。基于 NoneBot2 和 Gemini API 构建，拥有强大的多模态对话、工具调用和记忆能力。

---

## ✨ 核心特性

*   **双重人格对话系统**:
    *   **一猫 (默认模式)**: 温暖、亲切的 AI 伙伴，能够使用工具（网络搜索、查天气、看新闻）来帮助你。
    *   **Loki (斜杠模式)**: 狡黠、不羁的恶作剧之神，为创意写作而生，拥有强大的无限制文本生成能力。
*   **多模态理解**:
    *   能够理解和分析图片内容，并结合你的问题进行回答。
*   **持久化多插槽记忆**:
    *   为两种人格分别提供 10 个独立的记忆插槽，所有对话历史都会被永久保存，可以随时切换和回顾。
*   **丰富的功能插件**:
    *   **禁漫下载器**: 支持通过ID下载和随机下载。
    *   **猜病挑战**: 一个有趣的文字解谜 RolePlay 游戏。
    *   **主动聊天**: 在授权的群聊中，能够像真人一样观察并参与到有趣的话题中。
*   **强大的上下文理解**:
    *   能够理解引用回复、合并转发等复杂消息，并围绕其展开对话。

---

## 🛠️ 完整技术栈 (The Full Stack)


*   **QQ 客户端 (QQ Client):** [**LLOneBot**](https://github.com/LLOneBot/LLOneBot)
    *   负责连接 QQ 平台，将真实的 QQ 消息转换为 OneBot V11 标准事件，并反向将机器人的回复发送回 QQ。是机器人与外界沟通的桥梁。

*   **机器人框架 (Bot Framework):** [**NoneBot2**](https://nonebot.dev/)
    *   项目的核心驱动，负责事件处理、插件化管理、指令匹配等所有后端逻辑。本项目的所有功能都作为插件运行在 NoneBot2 之上。

*   **大模型 API 网关 (LLM API Gateway):** [**new-api**](https://github.com/QuantumNous/new-api)
    *   一个强大的中间件，它将标准的 OpenAI API 请求格式无缝转换为 Google Gemini API 格式。这是本项目能够灵活调用 Gemini 的关键，使得代码可以像请求 OpenAI 一样请求 Gemini。

*   **核心大语言模型 (Core LLM):** **Google Gemini**
    *   所有 AI 对话、内容生成和决策的最终驱动力，由 `new-api` 网关进行代理和适配。

---

## 🚀 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git
cd YOUR_REPOSITORY
```

### 2. 安装依赖

确保你的 Python 版本 >= 3.8。建议在虚拟环境中使用。

```bash
pip install -r requirements.txt
```

### 3. 创建配置文件

项目依赖两个配置文件，请在项目根目录手动创建它们。

**a) 创建 `.env` 文件 (用于存放密钥)**

```env
# .env

# 你的 new-api 网关地址
NEWAPI_URL=http://localhost:3000

# 你的 new-api 网关令牌 (如果设置了的话)
NEWAPI_TOKEN=sk-YOUR_OWN_GEMINI_API_KEY

# 你的和风天气API密钥
QWEATHER_API_KEY=YOUR_OWN_QWEATHER_API_KEY

# 主动聊天功能的群聊白名单，用英文逗号(,)隔开
ACTIVE_CHAT_GROUP_IDS=11111,22222,33333
```

**b) 创建 `jm_option.yml` 文件 (用于JMComic下载)**

这是一个最小化的配置示例，你可以根据 [Jmcomic官方文档](https://github.com/hect0x7/Jmcomic-qt) 进行更详细的配置。

```yaml
# jm_option.yml

dir_rule:
  base_dir: ./data/jmcomic
  
# 如果你需要代理才能访问，请取消下面的注释并配置
# network:
#  proxy: http://127.0.0.1:7890
```

### 4. 运行 Bot

确保你的 `LLOneBot` 和 `new-api` 服务已经启动，然后使用 `nb-cli` 启动机器人。

```bash
nb run
```

---

## 📖 功能与指令

*   **@一猫 [问题]**: 与一猫进行普通对话。
*   **@一猫 /[问题]**: 切换到 Loki 人格进行对话。
*   **回复图片 + @一猫 [问题]**: 对图片进行提问。
*   **@一猫 help**: 显示详细的帮助菜单。
*   **/memory** 或 **//memory**: 查看当前模式下的记忆插槽列表。
*   **/memory [编号]** 或 **//memory [编号]**: 切换记忆插槽。
*   **/restart** 或 **//restart**: 清空当前记忆插槽。
*   **/jm [ID]**: 下载指定ID的禁漫本子。
*   **/随机jm**: 随机下载一本禁漫本子。
*   **#[你的话]**: 开始或继续猜病挑战游戏。
*   **#新游戏**: 重置猜病挑战。

---

感谢使用一猫 Bot！喵~ ❤️
