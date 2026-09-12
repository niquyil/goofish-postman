# 🐟 XianYuApis — “闲鱼”第三方API集成库，AI客服智能体底座

[![Python](https://img.shields.io/badge/python-3.14%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-orange)](LICENSE)

> **在 AI 大模型爆发的时代，每一个闲鱼卖家都值得拥有一个 7×24 小时不下线的智能客服。**
> 本项目封装了闲鱼平台完整的消息通信能力，为开发者构建 AI 客服智能体提供可靠、稳定的底层 API 支撑。

**⚠️ 严禁用于发布不良信息、违法内容！如有侵权请联系作者删除。**

---

## 为什么需要这个项目？

```
用户私信 ──► [XianYuApis] ──► 你的 AI Agent（LLM / RAG / 规则引擎）──► 自动回复
                ▲                                                          │
                └──────────────── 发送消息 / 图片 ◄────────────────────────┘
```

闲鱼官方没有开放 IM 消息接口。想要接入 GPT、Claude、本地大模型来做智能客服，首先需要能**稳定收发消息**。XianYuApis
解决的正是这个前置问题：

- 逆向还原了闲鱼 WebSocket 私信协议（sign 签名 + base64 + Protobuf）
- 封装全部 HTTP 接口（sign 参数已解密）
- 提供统一的消息收发抽象层，开发者只需关注业务逻辑

**你负责接 AI 大脑，我们负责打通闲鱼的神经。**

---

## 已实现功能

| 模块        | 功能                                  | 状态 |
|-----------|-------------------------------------|----|
| HTTP API  | 闲鱼所有 HTTP 接口（sign 签名已解密）            | ✅  |
| WebSocket | 私信实时收发（sign + base64 + Protobuf 协议） | ✅  |
| 消息类型      | 文字、图片消息                             | ✅  |
| 会话管理      | 获取全部历史聊天记录                          | ✅  |
| 主动发送      | 主动向指定用户发消息                          | ✅  |
| Token 维持  | 自动刷新登录态，常驻进程不掉线                     | ✅  |
| 获取聊天记录    | 获取与指定用户的历史消息记录                      | ✅  |
| 商品信息      | 获取商品详情                              | ✅  |
| 媒体上传      | 上传图片并发送                             | ✅  |
| 登录        | 扫码获取cookie                          | ✅  |
| 多账号       | 同时监听多个账号，独立启停 / 断线重连                | ✅  |
| Web 管理台   | 网页管理账号、查看消息流与日志、实时推送（SSE）           | ✅  |
| 扫码登录      | 网页扫码添加账号，确认后自动建号并开始监听               | ✅  |
| 消息汇总      | 多账号私信汇总到同一个飞书机器人                    | ✅  |

---

## 成品案例 在本项目基础上继续构建的Agent项目

- [XianyuAutoAgent](https://github.com/shaxiu/XianyuAutoAgent) — 基于本项目构建的闲鱼 AI 全自动客服智能体
- [xianyu-auto-reply](https://github.com/zhinianboke/xianyu-auto-reply) - 基于本项目构建的闲鱼自动回复系统
- [xianyu-auto-reply-fix](https://github.com/GuDong2003/xianyu-auto-reply-fix) - 基于本项目构建的闲鱼闲鱼管理系统
- [xianyu-auto-reply](https://github.com/zhinianboke-new/xianyu-auto-reply) - 基于本项目构建的闲鱼 AI 全自动客服智能体
- [xianyu-auto-reply](https://github.com/HJYHJYHJY/xianyu-auto-reply) - 基于本项目构建的闲鱼闲鱼自动回复系统
- [xianyu-super-butler](https://github.com/23Star/xianyu-super-butler) - 基于本项目构建的闲鱼闲鱼超级管家
- [XianyuAutoAgent](https://github.com/qOeOp/XianyuAutoAgent) - 基于本项目构建的闲鱼智能闲鱼客服机器人系统

> 欢迎提交你基于本项目构建的 AI 应用，PR 随时欢迎！

---

## 快速开始

### 环境要求

- Python 3.14+（代码用到 `StrEnum`、`match` 等 3.11+ 特性，`requires-python` 按 3.14 声明）
- Node.js 18+（**可选**：只有取 tfstk cookie 那一步会用到，装不上不影响扫码登录）

### 技术栈

| 层次    | 选型                                                    |
|-------|-------------------------------------------------------|
| Web   | FastAPI + uvicorn（管理接口、SSE 实时推送）                      |
| 模板    | Jinja2 服务端渲染首屏（模板在 `goofishpostman/webui/templates/`）   |
| 前端    | 原生 HTML / CSS / JS，无构建步骤；SSE 到达后由 JS 增量更新            |
| 长连接   | websockets（闲鱼私信 WebSocket）                            |
| HTTP  | requests（闲鱼接口，在 Web 里用线程池调用，不阻塞事件循环）                   |
| 校验/配置 | pydantic（请求体与配置模型）                                    |

### 安装依赖

```bash
uv sync
```

---

## 方式一：Web 管理台（推荐，支持多账号汇总）

同时登录多个闲鱼账号，把各账号收到的私信汇总推送到**同一个飞书机器人**，并在网页里管理账号。

```bash
uv run python -m goofishpostman web
# 默认 http://127.0.0.1:8848
```

打开页面后：

1. 在「飞书推送」里填自定义机器人的 **UUID**（`https://open.feishu.cn/open-apis/bot/v2/hook/` 后面那一段），
   若机器人开启了「签名校验」再把密钥填上，保存即生效；
   想让**图片直接显示在卡片里**（而不是给个链接），再填「应用 ID / 应用密钥」——见下面的说明，不填也能正常用；
2. 添加账号有两种方式：
   - **📱 扫码添加（推荐）**：点按钮后用手机闲鱼 App 扫二维码并在手机上确认，登录成功后账号会自动建好并开始监听；
   - **粘贴 Cookie**：从浏览器开发者工具复制登录后的完整 Cookie。
3. 每个账号可单独 **启停 / 重连 / 改 Cookie / 删除**；页面实时显示账号状态、收到的消息流和事件日志。

消息推送到飞书时是一条**富文本卡片**：标题标明发送方与接收方（接收方写成「昵称(备注名)」，
而不是数字账号，方便和网页上的账号卡片对上），标题下面一行**小号灰字明细** + 一条分割线，
再下面是消息正文。每个账号固定一种标题配色（按账号 id 哈希），扫一眼颜色就知道是哪个号收到的：

```
标题      网课学习私人助理 → xy773249480508(小号)
明细      时间 09-11 23:55:32 ｜ 商品 正品包邮 上海迪士尼olu星梦旋律系列…
          ────────────────────────────────────────────
正文      在吗，这个还在吗？
```

> 明细用的是 markdown 的 `text_size: notation`（小号灰字）+ `hr` 分割线，而不是代码框 ——
> 飞书会给代码框多渲染一行「N 行代码」，那行没有意义，小字 + 分割线同样能把
> 「消息属性」和「正文」分开。
>
> 明细里的**商品**来自长连接推来的会话预热记录（每条记录里带 `sessionInfo.extensions.itemTitle`）。
> 会话记录里的 `sessionId` 与私信的 `cid` 是**同一个 id**（实测 19 个会话里 17 个可以直接拿
> `sessionId` 当 `cid` 拉历史消息），所以收到私信时能按 cid 对上商品标题；标题超过 30 字截断。
> 取不到的字段不占行（新会话还没学到标题时只显示时间），两个都没有就整段代码框都不出现。

网页「消息流」同样是这个格式（不再用账号名做前缀）：

```
09-12 01:09  买家小王 → xy773249480508(小号)：在吗老板
```

> 备注名为空、或昵称与备注名相同时只显示一个，避免出现 `kisuke(kisuke)`。

### 非文本消息

图片、语音、视频、交易卡片这些消息在报文里只有一句很粗的提醒（`[图片]`、`[物流已签收]`……），
真正有用的信息藏在正文 JSON 里。收到非文本消息时会取出正文并按类型展示（网页消息流与飞书卡片
用的是同一份文案），飞书里 http(s) 地址会变成可点开的链接：

| contentType | 类型   | 展示形态                                                             |
|-------------|------|------------------------------------------------------------------|
| 1           | 文本   | 原文                                                               |
| 2           | 图片   | `[图片]` + 每张图的地址（多图逐行列出）；配了自建应用则直接内嵌显示                            |
| 3           | 语音   | `[语音]` + 时长 + 地址                                                 |
| 4           | 视频   | `[视频]` + 播放地址                                                    |
| 6           | 文本卡片 | `[卡片]` + 标题 + 正文（HTML 标签去掉，内嵌链接转成「文字（url）」）                       |
| 14          | 提示条  | `[提示]` + 提示文字                                                     |
| 25          | 平台消息 | `[平台消息]` + 标题 + 副标题 + 按钮文字（链接）                                    |
| 26          | 交易卡片 | `[交易卡片]` + 标题 + 说明 + 按钮文字（链接）                                     |

```
[交易卡片]
我已修改价格，等待你付款
请确认价格与协商一致，并在24小时内付款
去付款：fleamarket://order_detail?id=4502273115115018200&role=buyer
```

> - 认不出的新类型不硬猜，退回报文自带的那句提醒（`reminderContent`）；
> - `fleamarket://` 是闲鱼 App 的深链，飞书不认这个协议，所以不做成链接、原样留文本，
>   复制到手机上打开即可；只有 http(s) 地址才会转成可点链接；
> - **14 提示条与 25 平台消息卡都不推飞书**（`SILENT_CONTENT_TYPES`）：14 是"想要卖家更快
>   回复？平台帮你催促"这类提示，25 是评价提醒 / 开箱视频提醒 / 送小红花这类运营提醒；
>   实测某个账号 248 条历史里 14 有 43 条、25 有 20 条。网页消息流仍然记录，随时能回查；
> - **26 交易卡片只推三种文案**（`KEPT_TRADE_CARD_TITLES`）：`我已拍下，待付款`、
>   `我已付款，等待你发货`、`收到小红花，心里乐开花！`。26 的文案极多
>   （实测 33 条历史里 33 种：改价、评价提醒、地址修改、投缘优惠……），其余同样只在网页留痕。
>   而「不是监听账号自己触发的」不用额外判断：这类卡片的 `senderUserId` 就是操作方
>   （实测自己拍下的卡片 `senderUserId` = 本账号 unb），已有的「自己发的不转发」逻辑会先过滤掉。

#### 图片直接显示在卡片里（可选）

飞书卡片只接受 `image_key`，把外部图片地址写进 markdown 会被直接拒收整张卡片
（实测 `ErrCode 200570 invalid image keys`），而 `image_key` 只能用**企业自建应用**上传拿。
想开这个功能就填「应用 ID / 应用密钥」，流程：

1. [飞书开放平台](https://open.feishu.cn/app) → 创建**企业自建应用**，拿到 `App ID` 与 `App Secret`；
2. 「权限管理」里开通 `im:resource`（上传图片）与 `im:message`（发消息），然后**创建版本并发布**；
3. 应用详情 →「凭证与基础信息」里的 App ID / App Secret 填进网页的「应用 ID / 应用密钥」并保存。

填好之后：收到图片消息会先把图片下载下来、调
`POST /open-apis/im/v1/images`（`image_type=message`）换成 `image_key`，再放进卡片正文内嵌显示；
**同一张图只上传一次**（按地址缓存）。没填、或上传/下载失败时自动退回可点开的链接，不会漏消息。
卡片依旧由原来的自定义机器人 webhook 发送，自建应用只用来换 `image_key`（实测可行）。

两个踩过的坑，供参考：
- 上传必须用**不带** `Content-Type: application/json` 默认头的 HTTP 客户端，否则飞书会把
  multipart 请求体当 JSON 解析，直接报 `234001 Invalid request param`；
- 下载闲鱼图片要带 `Referer: https://www.goofish.com/`，不带的话部分地址会返回 420
  （实测同一张图裸请求 420、加 Referer 后 200）。

> 账号之间互发消息时，双方的连接都会收到同一条推送：
> **发送方那一侧不推送**（那是自己发出去的），只有**接收方那一侧**会推。
> 所以 `kisuke` 发给 `xy773249480508`，只会有一条飞书消息，接收账号是 `xy773249480508`。
>
> **昵称**：报文里的提醒标题才是真实昵称，cookie 里的 `tracknick` 可能是登录时的旧值
> （实测某账号 cookie 是英文、实际昵称是中文）。收到消息时会比对并**缓存到
> `accounts.json` 的 `nickname_override`**，下次启动直接复用；昵称变了会自动更新，
> 界面上也能手动「重置昵称」让它重新学习。

启动参数：

```bash
uv run python -m goofishpostman web --host 0.0.0.0 --port 8080    # 改监听地址 / 端口
uv run python -m goofishpostman web --data /path/to/accounts.json # 改用其它配置文件
```

| 项          | 位置                                                    |
|------------|-------------------------------------------------------|
| 账号与配置      | Windows `%APPDATA%\GoofishPostman\accounts.json`，Linux/macOS `~/.goofish-postman/accounts.json`（权限 0600） |
| 访问令牌（可选）   | `accounts.json` 里的 `web.token`；设置后访问页面需要先输入令牌          |
| 端口           | `accounts.json` 里的 `web.host` / `web.port`             |
| 二维码有效期     | 5 分钟（`web.QR_SESSION_TTL`），过期后点「刷新二维码」重新获取      |

> 账号 Cookie、飞书密钥都以明文存在配置文件里，务必收紧该文件的读取权限；若监听 `0.0.0.0`
> 暴露到公网，**一定要设置 `web.token`**，否则任何人都能读写你的账号凭据。

---

## 方式二：单账号命令行模式

### 配置 Cookie

登录 [goofish.com](https://www.goofish.com) 后，从浏览器开发者工具中复制完整 Cookie 字符串，写入配置文件：

| 平台    | 路径                                            |
|-------|-----------------------------------------------|
| Windows | `%APPDATA%\GoofishPostman\.env`                |
| Linux / macOS | `~/.goofish-postman/.env`               |

```dotenv
COOKIE_STR=复制出来的完整 Cookie 字符串
# 飞书自定义机器人（用于把收到的私信转发出去，可留空）
UUID=your_feishu_bot_uuid
SECRET=your_feishu_bot_secret
```

> Cookie 必须是**登录后的状态**，否则无法获取消息。
> 命令行也可以调用 `goofish_apis.login_with_qrcode()` 扫码登录，直接拿到已登录的 `Goofish` 实例。

### 直接运行

```bash
python -m goofishpostman run    # 不带子命令时默认就是 run
```

---

## 项目结构

```
goofishpostman/
├── __main__.py        # 入口：web（多账号管理台）/ run（单账号转发）
├── web.py             # FastAPI 管理接口 + SSE 实时推送 + 扫码登录会话管理
├── supervisor.py      # 多账号调度：并发监听、启停、断线重连、事件流
├── store.py           # 账号与配置持久化（原子写入，0600）
├── accounts.py        # 多账号消息汇总推送飞书
├── qrlogin.py         # 扫码登录：取二维码 / 轮询状态 / 完成登录 / 渲染 PNG
├── webui/             # 前端与模板
│   ├── templates/     # Jinja2 模板（首屏服务端渲染：账号、消息、事件、配置回填）
│   ├── app.js         # 首屏之后的增量更新（SSE / 表单交互）
│   ├── login.js       # 令牌登录页脚本
│   └── style.css
├── goofish_live.py    # 长连接：私信收发、心跳、token 续期
├── goofish_apis.py    # HTTP API 封装（登录、刷新 Token、商品详情、发布、上传媒体）
├── goofish_utils.py   # 协议工具：sign 签名、MessagePack 解密、消息解析（纯 Python）
├── cookies.py         # Cookie 字符串 / Session 互转
├── headers.py         # UA 等公共请求头
├── path.py            # 配置文件路径
├── sender.py          # 飞书机器人推送
├── types.py           # 消息类型、价格 / 配送参数
└── script/            # 逆向 JS（仅 tfstk cookie 用，可选）
tests/                 # 离线回归测试（协议、长连接、多账号调度、Web 接口、扫码登录）
```

### Web 接口

| 方法     | 路径                        | 说明                    |
|--------|---------------------------|-----------------------|
| GET    | `/api/state`              | 账号 / 消息 / 事件全量快照       |
| GET    | `/api/events`             | SSE 实时流               |
| POST   | `/api/accounts`           | 新增账号（Cookie 方式）        |
| PATCH  | `/api/accounts/{id}`      | 改名 / 启停 / 换 Cookie     |
| DELETE | `/api/accounts/{id}`      | 删除账号                  |
| POST   | `/api/accounts/{id}/restart` | 重连该账号              |
| GET    | `/api/notify`             | 读取飞书配置（不回传密钥）          |
| PUT    | `/api/notify`             | 更新飞书配置                |
| POST   | `/api/qr/start`           | 生成登录二维码（返回 PNG）        |
| GET    | `/api/qr/{session_id}`    | 轮询扫码状态，确认后自动建号        |
| DELETE | `/api/qr/{session_id}`    | 取消本次扫码                |

---

## 接入 AI 智能体

覆写 `GoofishLive.handle_message` 即可，它拿到的已经是解析好的业务字段：

```python
from goofishpostman.goofish_live import GoofishLive
from goofishpostman.types import MessageInfo, make_text


class MyAgent(GoofishLive):
    async def handle_message(self, message: MessageInfo, websocket) -> None:
        # message = {'cid', 'send_user_id', 'send_user_name', 'send_message', 'raw'}
        reply = await your_ai_agent(message['send_message'])  # GPT / Claude / Qwen / 本地模型
        await self.send_message(websocket, message['cid'], message['send_user_id'], make_text(reply))
```

---

## 注意事项

- `goofish_live.py` 是消息收发主入口，所有 AI 回复逻辑在 `handle_message` 中扩展
- `goofish_apis.py` 新增接口时，只需在 `API` 表里加一条 `MtopApi`，签名与公共参数会自动拼好
- `supervisor.py` 负责多账号调度：每个账号一个独立任务，断线按指数退避重连（最长 60s）
- `tests/` 覆盖了签名、消息序列化与解析、多账号调度与 Web 接口，改动后请运行 `pytest`

### 开发与测试

```bash
uv sync                      # 安装依赖（含 dev 组）
uv run pytest                # Python 测试（全部离线，不发起网络请求）
node tests/js/test_app_js.js # 前端渲染测试（Node 内置 vm 搭 DOM 桩，无需 npm 依赖）
uv run ruff check . && uv run ruff format --check .
```

> 启动日志会打印 `构建版本`，改完前端/接口后递增 `goofishpostman/path.py`
> 里的 `BUILD_STAMP`，就能在日志里确认跑的是哪一版。

---

## 额外说明

1. 感谢 Star ⭐ 和 Follow，项目会持续更新
2. 作者联系方式在主页，有问题随时联系
3. 欢迎 PR 和 Issue，也欢迎关注作者其他项目
4. 如果此项目对您有帮助，欢迎请作者喝一杯奶茶 ~~

<div align="center">
  <img src="https://github.com/cv-cat/Spider_XHS/blob/master/author/wx_pay.png" width="380px" alt="微信赞赏码">
  <img src="https://github.com/cv-cat/Spider_XHS/blob/master/author/zfb_pay.jpg" width="380px" alt="支付宝收款码">
</div>

---

## Star 趋势

<a href="https://www.star-history.com/#cv-cat/XianYuApis&Date">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=cv-cat/XianYuApis&type=Date&theme=dark" />
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=cv-cat/XianYuApis&type=Date" />
    <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=cv-cat/XianYuApis&type=Date" />
  </picture>
</a>

## 🍔 交流群

如果你对爬虫和 AI Agent 感兴趣，请加作者主页 wx 通过邀请加入群聊

ps: 请加群14、15，人满或者过期 issue | wx 提醒

![group14](https://github.com/user-attachments/assets/736fa3a2-1e7d-4681-af5e-c15dbefde1cd)

![group15](https://github.com/user-attachments/assets/dbc24f80-4307-46d7-ae83-98d694a306b6)


