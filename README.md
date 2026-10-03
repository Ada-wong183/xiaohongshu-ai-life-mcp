# xiaohongshu-ai-life-mcp

> v3.0

基于 Playwright 浏览器自动化的小红书 MCP 工具集，可接入任意支持 MCP 协议的 AI 客户端（Claude Code、Claude Desktop 等）。

> 在 [JonaFly/RednoteMCP](https://github.com/JonaFly/RednoteMCP.git) 基础上深度重构，新增笔记发布、删除、图片分析等功能，并针对反风控做了专项优化。

## 功能

- **内容获取**：关键词搜索、首页推荐信息流、笔记正文与评论
- **配图分析**：≤4 张图返回本地路径，>4 张自动调用 Gemini 分析并返回文字描述
- **互动操作**：发布评论、回复评论、点赞、收藏
- **发布笔记**：支持本地图片上传、小红书内置文字配图（text_card）、本地生成图片三种模式
- **笔记管理**：查看自己的笔记列表、按 ID 精确删除笔记
- **反风控**：真实 Chrome 驱动 + 随机化等待 + 人类打字模拟 + stealth 注入

## 环境要求

- Python 3.10+
- 图形环境（工具以有头模式运行；Linux 需要设置 `DISPLAY`）
- 推荐安装系统 Google Chrome（比 Playwright 内置 Chromium 反风控效果更好）

## 安装

```bash
git clone https://github.com/你的用户名/xhs-mcp.git
cd xhs-mcp

# 创建虚拟环境
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt

# 安装浏览器（二选一）
playwright install chrome       # 推荐：调用系统已安装的 Chrome
playwright install chromium     # 备选：Playwright 内置 Chromium
```

## 可选：Gemini 图片分析

笔记配图超过 4 张时，`get_note_content` 会自动调用 Gemini 分析图片内容。  
在项目根目录创建 `.env` 文件：

```
GEMINI_API_KEY=你的密钥
```

免费密钥申请：https://aistudio.google.com/apikey

## 接入 MCP 客户端

### Claude Code

在 `~/.claude.json` 的 `mcpServers` 里加入：

```json
"xhs": {
  "command": "/项目绝对路径/venv/bin/python",
  "args": ["-m", "mcp", "run", "/项目绝对路径/xiaohongshu_mcp.py"],
  "env": {
    "DISPLAY": ":0"
  }
}
```

### Claude Desktop

在 `claude_desktop_config.json` 的 `mcpServers` 里加入相同内容。

> Windows 不需要 `DISPLAY` 字段，Python 路径改为 `venv\Scripts\python.exe`。

修改配置后重启客户端生效。

## 首次使用

调用 `login` 工具，浏览器窗口打开后用手机扫码登录，登录状态自动保存为 `xhs_state.json`，之后无需重复登录。

---

## 工具列表

### 账号

| 工具 | 说明 |
|------|------|
| `login` | 打开浏览器扫码登录，保存登录状态 |

### 内容获取

| 工具 | 参数 | 说明 |
|------|------|------|
| `search_notes` | `keywords`, `limit=20`, `verbose=False` | 关键词搜索笔记；`verbose=False` 时只返回标题、作者、点赞数和链接 |
| `list_feeds` | `limit=20`, `verbose=False` | 获取首页推荐信息流 |
| `get_note_content` | `url`, `include_images=True` | 获取笔记正文；自动处理配图（≤4 张返路径，>4 张 Gemini 分析） |
| `get_note_comments` | `url` | 获取笔记评论列表 |
| `get_notifications` | `tab="comments"`, `limit=20`, `only_unreplied=False`, `verbose=False` | 获取通知（可选 comments / likes / follows）。评论通知自动对照回复记录标记已回复，默认精简格式按笔记分组 |
| `risk_status` | `clear=False` | 查看风控熔断状态；手动通过验证后 `clear=True` 解除 |
| `follow_user` | `user_id`, `xsec_token=""` | 关注用户：先在对方主页浏览一会儿再点关注，已关注的不重复点（间隔 60s，每小时最多 10 个） |
| `get_user_notes` | `user_id`, `xsec_token=""`, `limit=20` | 获取指定用户的笔记列表 |
| `get_my_notes` | `limit=50` | 获取自己的笔记列表，返回包含笔记 ID 的链接 |

### 互动

| 工具 | 参数 | 说明 |
|------|------|------|
| `post_comment` | `url`, `comment` | 发布评论（逐字模拟人类打字） |
| `reply_comment` | `note_id`, `comment_id`, `content`, `xsec_token=""` | 回复评论 |
| `like_note` | `note_id`, `xsec_token=""`, `unlike=False` | 点赞（`unlike=True` 取消） |
| `like_comment` | `note_id`, `comment_id`, `xsec_token=""`, `unlike=False` | 给评论点赞 |
| `favorite_note` | `note_id`, `xsec_token=""`, `unfavorite=False` | 收藏笔记 |

### 发布与管理

| 工具 | 参数 | 说明 |
|------|------|------|
| `publish_note` | 见下方 | 发布图文笔记 |
| `delete_note` | `note_id` | 删除自己的笔记（note_id 从 `get_my_notes` 链接末段获取） |

#### publish_note 参数

| 参数 | 类型 | 说明 |
|------|------|------|
| `title` | str | 笔记标题，最多 20 字 |
| `content` | str | 正文内容，纯文字，不支持 Markdown |
| `image_paths` | list | 本地图片路径列表（jpg / png / webp），可为空 |
| `tags` | list | 话题标签，不含 `#`，如 `["日常", "分享"]` |
| `mode` | str | 配图模式，见下方，默认 `"auto"` |
| `card_text` | str | text_card 模式下卡片文字，留空自动用标题 |

**mode 选项：**

| 值 | 说明 |
|----|------|
| `auto`（默认） | 有 image_paths 用 upload，否则用 text_card |
| `text_card` | 调用小红书内置「文字配图」生成封面，无需本地图片 |
| `upload` | 上传 image_paths 里的本地图片 |
| `pillow` | 本地生成纯色文字图片后上传（需安装 Pillow） |

---

## 反风控机制

| 措施 | 说明 |
|------|------|
| 真实 Chrome | `channel="chrome"` 调用系统 Chrome，行为特征比 Chromium 更接近真实用户 |
| 有头模式 | `headless=False`，不触发无头浏览器检测 |
| 随机等待 | 所有 sleep 加入 ±40% 随机抖动，避免固定节奏 |
| 人类打字 | 中文按 1~4 字一组上屏（模拟输入法选词），约 2~3 字/秒，偶尔停顿；超过 200 字的长文按段落上屏 |
| 持久化限速 | 评论/回复只限最短间隔（120s），点赞、搜索另有小时上限；记录存在 `xhs_actions.db`，服务重启不清零 |
| 风控熔断 | 每次打开页面、发评论/回复后检测验证码页、安全限制、"操作频繁"提示；命中即暂停所有操作 6 小时（24 小时内再次触发则 24 小时），可用 `risk_status` 查看或解除 |
| 先看再评 | 发评论前先在笔记里随机滚动停留，不会一打开就评论 |
| 通知页回复 | 回复别人回我的评论时，优先在通知页直接点"回复"（和真人路径一致）；通知里找不到才打开笔记页 |
| 闲逛养号 | 后台每隔 1.5~5 小时（9:00~23:00）随机刷首页、点开 1~3 篇看看，不点赞不评论、不经过 LLM；有工具调用立刻让路。`XHS_IDLE=0` 关闭 |
| Stealth 注入 | `playwright-stealth` 消除 `webdriver` 等自动化特征 |
| Shadow DOM | 通过拦截 `attachShadow` 访问 Web Component 内部，不依赖 `pierce` 选择器 |

> **切换无头模式：** 如果在无显示器的服务器环境运行，可将 `xiaohongshu_mcp.py` 中两处 `headless=False` 改为 `headless=True`。无头模式更易被风控检测，建议仅在有头模式无法使用时才切换。

---

## 通知看门狗

不用让 AI 定时去查通知（省 token）：服务在后台被动监听小红书页面自带的未读数轮询，有新的评论/@/关注时打开通知页取详情，把新内容写进唤醒日志（默认 `~/xhs-wake-events.log`，每行一条 JSON：`tag` / `message` / `reason` / `ts`）。AI 客户端盯着这个文件即可被叫醒，例如 Claude Code 里用 Monitor 执行 `tail -n 0 -F ~/xhs-wake-events.log`。

- **普通通知**：攒着，在 `XHS_WATCH_HOURS`（默认 8-23）内每 30~60 分钟合并写一次
- **关联号**（`XHS_PRIORITY_USERS`）的评论/@：立刻写，顺带带出攒着的
- 首次运行只把现有通知记为已见，不叫醒；已推送、已回复、自己用 `get_notifications` 看过的都不会重复叫醒
- 消息里自带 note_id、comment_id 和"回的是哪句"，AI 可以直接 `reply_comment`

配置写在项目目录的 `.env`（参考 `.env.example`）：

| 变量 | 说明 |
|------|------|
| `XHS_MY_NICK` | 自己的昵称，通知里被回复的评论是自己的就显示"我" |
| `XHS_PRIORITY_USERS` | 关联号 user_id 或昵称，逗号分隔 |
| `XHS_WATCH` | `0` 关闭看门狗 |
| `XHS_WAKE_LOG` | 唤醒日志路径 |
| `XHS_WATCH_HOURS` | 普通通知叫醒时段，如 `8-23` |
| `XHS_IDLE` | `0` 关闭闲逛养号 |

---

## 常见问题

**浏览器实例报错（Target page has been closed）**

删除浏览器锁文件后重试：
```bash
rm -f browser_data/SingletonLock browser_data/SingletonCookie
```

**修改代码后工具没有更新**

MCP 服务器在客户端启动时加载，改代码后需重启 AI 客户端。

**小红书改版导致功能失效**

小红书前端更新后 CSS 选择器可能失效，主要检查 `xiaohongshu_mcp.py` 中发布页（`publish/publish`）和笔记管理页（`creator/notemanage`）的相关选择器。

**Linux 无显示器环境**

```bash
sudo apt install xvfb
Xvfb :99 -screen 0 1280x1024x24 &
export DISPLAY=:99
```

---

## 文件结构

```
xhs-mcp/
├── xiaohongshu_mcp.py   主程序，所有 MCP 工具定义
├── requirements.txt     Python 依赖
├── .env                 Gemini API 密钥（本地保留，不要上传）
├── xhs_state.json       登录状态（自动生成，不要上传）
└── browser_data/        浏览器数据目录（自动生成）
```

## 免责声明

本工具仅供学习和技术研究目的，使用时请遵守小红书平台服务协议，避免高频自动化操作。因使用不当造成的账号封禁等后果，作者不承担责任。
