# zcode2api

将 ZCode (zcode.z.ai) Coding Plan 额度转为标准 Anthropic Messages API，支持多账号轮询、
额度用完自动换号、实时用量监控、后台管理 UI 与鉴权，以及阿里云无痕验证自动续期。

## 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env                     # 按需修改
# 无痕验证求解会自动探测并优先使用本机真实 Chrome / Edge（无需配置）；
# Playwright 自带 Chromium 会被阿里云风控识破，仅作无浏览器时的兜底。

python main.py serve                     # 启动网关 + 后台 UI（默认端口 3000）
```

> Windows 用户可直接双击 `start_windows.bat` 一键启动（自动建虚拟环境、装依赖）。

### 本地运行（推荐）与内网穿透

阿里云无痕验证按 IP 信誉判定，**数据中心/云服务器 IP 会被直接拒绝（F001）**，
因此网关应跑在家庭宽带等住宅网络环境（本机 Chrome 求解约 3 秒）。
需要公网访问时用内网穿透把 3000 端口映射出去，以 [cpolar](https://www.cpolar.com) 为例：

```bash
cpolar http 3000        # 得到形如 https://xxxx.r6.cpolar.top 的公网地址
```

客户端 base_url 填该地址即可（也可用 frp / ngrok 等任意穿透工具）。
若必须部署在云上，可配置 `ZCODE_UPSTREAM_PROXY` 指向住宅代理，验证码求解与上游请求将统一走代理。

- 后台管理：`http://localhost:3000/admin`（默认账号 `admin`、密码 `zcode`）
- 对话端点：`http://localhost:3000/v1/messages`（兼容 Anthropic Messages 协议）
- OpenAI 兼容端点：`http://localhost:3000/v1/chat/completions`（流式 / 非流式均支持）
- OpenAI Responses 端点：`http://localhost:3000/v1/responses`（Codex / 新版 SDK 使用）
- 存活探针：`GET /healthz`；就绪探针：`GET /readyz`（可直接用于 Docker / K8s）

## Docker 部署

镜像内包含 Python(网关)与 Playwright 无头 Chromium(无痕验证求解器),开箱即用。

```bash
# 方式一：docker compose（推荐）
docker compose up -d --build
# 账号 / 设置持久化在宿主机 ./data 目录；停止：docker compose down

# 方式二：docker 原生命令
docker build -t zcode2api:latest .
docker run -d --name zcode2api \
  -p 3000:3000 \
  -v "$(pwd)/data:/data" \
  -e ZCODE_ADMIN_USER=admin \
  -e ZCODE_ADMIN_KEY=zcode \
  --restart unless-stopped \
  zcode2api:latest
```

- 数据卷:容器内 `/data`(对应 `ZCODE_DATA_DIR`)存放 `accounts.db`,务必挂载到宿主机以持久化。
- 环境变量同下方「环境变量」表,可在 `docker-compose.yml` 的 `environment` 下覆盖。
- **请勿**将 `.env`、`data/` 打入镜像——已在 `.dockerignore` 中排除。

### 自动构建镜像(GHCR)

`.github/workflows/docker-build.yml` 会在**每次更新**(push 到 `master` 或打 `v*` tag)时
**自动构建并发布镜像到 GHCR(GitHub 容器仓库,`ghcr.io`)**,使用内置 `GITHUB_TOKEN`,
**不使用 Docker Hub**;Pull Request 仅构建验证、不推送。

```bash
# 拉取并运行已发布镜像（tag: latest 或 sha-xxxxxxx）
docker run -d --name zcode2api -p 3000:3000 \
  -v "$(pwd)/data:/data" -e ZCODE_ADMIN_KEY=zcode \
  ghcr.io/yuanhhs/zcode2api:latest
```

> 首次发布后,GHCR 上的包默认可能为私有;如需公开拉取,请到仓库 **Packages → 该包 → Package settings → Change visibility** 设为 Public。

## 后台 UI

| 页面 | 说明 |
|------|------|
| `/admin/login` | 后台登录（Bearer 密钥鉴权，凭证加密存于浏览器 localStorage）|
| `/admin/accounts` | 账号池：新增/导入/导出、启用禁用、**实时额度与状态监控**（每 5 秒刷新）|
| `/admin/settings` | 后台密码、网关 API Key |
| `/admin/proxy` | 代理设置：一键导入本机代理、手动配置、订阅解析、出口 IP 测试 |

账号池页实时展示每个账号的状态（正常 / 额度用完 / 限流 / 异常 / 禁用）、各模型剩余额度、
调用与失败次数。请求按顺序轮询分发，**某账号额度用完会自动切换到下一个账号**，并在 UI 中即时反映。

## 多账号轮询与换号

- 在「账号池」粘贴 Coding Plan JWT（3 段点分）或 API Key，每行一个即可加入轮询。
- 网关每次请求选择下一个「可用」账号（跳过用完 / 限流 / 异常 / 禁用）。
- 命中额度用完信号（余额为 0、上游 402、错误体含 quota/余额 等）→ 标记 `exhausted` 并换下一个账号。
- 上游 429 → 标记 `cooling` 冷却一段时间后自动恢复；401/403（非验证码）→ 标记 `invalid`。
- 后台任务按 `ZCODE_QUOTA_REFRESH_INTERVAL` 周期刷新各账号额度；也可在 UI 手动刷新。

额度刷新会同时读取旧版 `billing/*` 接口和当前 Coding Plan 的订阅/配额接口，兼容
`balances`、`limits`、`available_units` 等返回结构。新增或编辑 JWT 后会立即刷新一次；
若账号尚未购买/领取 Coding Plan，会显示“未激活套餐”，这表示上游没有授予额度，不能由
网关本地伪造激活；该账号会暂时跳过轮询，套餐生效后下次刷新自动恢复。

## OAuth 授权登录（Z.AI）

后台「账号池 → 新增 → 授权登录」提供两种方式，CLI `python main.py login zai` 为手动方式：

**一键登录（推荐，网关在本机运行时）**

1. 点击「开始一键登录」，浏览器打开 Z.AI 官方授权页（`chat.z.ai/api/oauth/authorize`）；
2. 登录并授权后浏览器自动跳回 `http://127.0.0.1:{端口}/oauth/callback`（Z.AI 已注册的回环回调地址），
   网关自动捕获授权码并完成兑换，**全程无需复制粘贴**；
3. 后台页面每 2 秒轮询进度，导入成功后自动关闭弹窗，账号以邮箱前缀命名并附带用户信息。

**手动粘贴（网关不在本机 / 回调被拦截时）**

1. 切到「手动粘贴」，打开授权链接完成登录，浏览器跳回 `zcode.z.ai/login?code=...`（页面可能报错，可忽略）；
2. 复制地址栏完整网址（或 `code` 参数值）粘贴回后台输入框，网关兑换
   编程套餐 JWT（并尝试兑换 API Key 作为回退凭证）自动入池。

**备选：手动提取 JWT**。若上述流程不可用，可在浏览器登录 [zcode.z.ai](https://zcode.z.ai) 后，
按 F12 打开控制台执行 `copy(localStorage.getItem('zcodejwttoken'))`（JWT 已进剪贴板），
再通过「粘贴凭证」入池。

## 代理设置（一键导入本机代理）

后台新增「代理设置」页，代理生效范围为：上游对话、验证码求解浏览器、额度查询、OAuth 兑换。

- **一键导入**：自动读取系统代理（Windows 注册表 / macOS scutil）并探测本机 Clash 等内核的
  监听端口（7897 / 7890 等），点击即启用，保存后即时生效、无需重启；
- **手动配置**：支持 `http://`、`https://`、`socks5://`，可带账号密码；
- **订阅解析**：粘贴机场订阅链接可查看节点协议分布与使用建议。注意订阅中的
  hysteria2 / vmess / trojan / ss 等节点无法被程序直连，须经本机 Clash 内核的
  混合端口（如 `http://127.0.0.1:7897`）转接 —— 与 OmniRoute 的「本地内核端点」方案一致；
- **出口 IP 测试**：一键验证代理是否生效，展示代理出口 IP 与直连 IP 对比；
- 优先级：后台保存的代理 > 环境变量 `ZCODE_UPSTREAM_PROXY` > 直连。

## 鉴权

- **后台鉴权**：所有 `/admin/api/*` 需 `X-Admin-User: <后台账号>` +
  `Authorization: Bearer <后台密码>`；默认 `admin / zcode`。如将后台账号设为空，
  可兼容旧版仅密码模式。
- **网关鉴权（可选）**：在「设置」配置「网关 API Key」后，`/v1/messages`、`/v1/chat/completions`、`/v1/responses` 须携带
  `Authorization: Bearer <key>` 或 `x-api-key: <key>`；留空则不校验。
- 管理设置接口不会回传后台密钥或网关 Key，只返回“已设置”和掩码提示；设置页中留空表示保持原值。

## OpenAI / Codex 接入

Chat Completions:

```bash
curl http://localhost:3000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <网关 API Key，如未设置可省略>" \
  -d '{"model":"GLM-5.3","messages":[{"role":"user","content":"你好"}],"stream":false}'
```

流式请求支持 OpenAI 标准的 `stream_options: {"include_usage": true}`，末尾会返回
携带 `prompt_tokens / completion_tokens / total_tokens` 的用量块（`choices` 为空数组），
Cherry Studio / LobeChat / NextChat 等客户端可直接显示 token 消耗；
`/v1/models` 同时返回 OpenAI 与 Anthropic 两种字段格式，两类客户端均可直接拉取模型列表。

Responses API（Codex 推荐）：

```toml
model = "GLM-5.3"
model_provider = "zcode2api"

[model_providers.zcode2api]
name = "zcode2api"
base_url = "http://127.0.0.1:3000/v1"
env_key = "ZCODE2API_KEY"
wire_api = "responses"
```

```bash
set ZCODE2API_KEY=<网关 API Key；未设置则任意占位>
set NO_PROXY=127.0.0.1,localhost
codex exec "用一句话回答 OK"
```

Responses 兼容层会把 `input` / `instructions` / `max_output_tokens` / function tools
转为上游 Anthropic Messages，再合成标准 `response.*` SSE 事件，避免新版客户端因
只支持 `/v1/responses` 而报格式错误。

## Railway / Zeabur / Ubuntu 部署

- Railway：仓库根目录已有 `railway.toml`，使用 Dockerfile 构建，`/healthz` 做健康检查；
  平台注入的 `PORT` 会自动被读取（`ZCODE_PORT → PORT → 3000`），无需也不要硬编码端口。
- Zeabur：仓库根目录已有 `zbpack.json`，强制使用根目录 `Dockerfile`；Zeabur Git Service
  同样读取 `$PORT`，不要在代码里写死端口。
- Ubuntu：`docker compose up -d --build`，数据在 `./data`；升级前备份
  `data/accounts.db`，升级后 `docker compose logs -f zcode2api` 看启动与健康检查。

> **云上部署必读（验证码风控）**：无痕验证按出口 IP 环境判定，Railway / Zeabur 等
> 云服务器的数据中心 IP 几乎必然被风控拒绝（`verifyCode=F001`），导致所有 JWT 账号
> 对话请求 503。**必须配置住宅代理**：设置环境变量 `ZCODE_UPSTREAM_PROXY`
> （http/socks5），或部署后在后台「代理设置」页保存——验证码求解与上游请求会统一
> 走该代理。本地家庭宽带部署通常无需代理。

- 网关只向上游转发有限的客户端元数据 header，不会转发 Cookie、客户端 Authorization 或连接控制头。

## 无痕验证（无头 Chromium）

Coding Plan（JWT）模式调用 `zcode.z.ai` 上游时需要阿里云无痕验证参数
（请求头 `X-Aliyun-Captcha-Verify-Param`）。网关用 **Playwright + 真实 Chrome**（无头模式）
运行阿里云官方无痕 SDK 来求得该参数。

- 求解逻辑在 `app/captcha.py`：启动无头 Chrome（伪装普通 Chrome UA、关闭自动化特征），
  在 `zcode.z.ai` 同源页面中执行 `startTracelessVerification`，捕获成功回调输出 `verifyParam`。
- **必须使用真实 Chrome 二进制**：Playwright 自带 Chromium 与 Node+jsdom 模拟环境都会被
  阿里云风控识破（返回 `verifyCode=F001` 环境风险拒绝）。Docker 镜像已内置 Google Chrome 并
  默认 `ZCODE_CAPTCHA_BROWSER_CHANNEL=chrome`；本机运行可用 `chrome` / `msedge` 复用系统浏览器。
- 内置结果缓存（默认 45s）、并发去重与失败重试；验证码配置从上游 `client/configs` 拉取。
- `verifyParam` 实为 `base64(JSON{certifyId, sceneId, isSign, securityToken})`，由阿里云服务端签发。
- 仅 Coding Plan（JWT）账号需要；API Key 账号走 `api.z.ai` 回退端点，无需验证码。

## 命令行

```bash
python main.py serve [--port 3000]                 # 启动服务
python main.py login zai [--no-browser]            # OAuth 登录 Z.AI 并自动入池
python main.py add-account zai <name> <jwt|key>    # 添加轮询账号
python main.py accounts [zai|bigmodel]             # 查看账号列表
python main.py remove-account <provider> <id|name> # 删除账号
python main.py quota                               # 查看各账号实时额度
python main.py status                              # 查看配置概览
python main.py set-admin-key <key>                 # 设置后台密码
python main.py export [file] / import <file>       # 导出 / 导入账号
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ZCODE_PORT` | 3000 | 服务端口 |
| `ZCODE_HOST` | 0.0.0.0 | 监听地址 |
| `ZCODE_ADMIN_USER` | admin | 后台账号初始值（之后以 DB 为准；留空可仅密码登录）|
| `ZCODE_ADMIN_KEY` | zcode | 后台密码初始值（之后以 DB 为准）|
| `ZCODE_DATA_DIR` | ./data | 数据目录（SQLite 存放处）|
| `ZCODE_QUOTA_REFRESH_INTERVAL` | 60 | 后台刷新额度间隔（秒），0 关闭 |
| `ZCODE_QUOTA_TIMEOUT` | 20 | 单次额度接口请求超时（秒） |
| `ZCODE_COOLING_SECONDS` | 300 | 限流冷却时长（秒）|
| `ZCODE_CAPTCHA_TIMEOUT` | 40 | 单次验证码求解超时（秒）|
| `ZCODE_CAPTCHA_RETRIES` | 4 | 验证码求解失败重试次数 |
| `ZCODE_APP_VERSION` | 3.7.7 | 伪装的上游客户端版本号（请求头与配置接口）|
| `ZCODE_SUBSCRIPTION_URL` | `https://api.z.ai/api/biz/subscription/list` | Coding Plan 订阅状态接口 |
| `ZCODE_QUOTA_LIMIT_URL` | `https://api.z.ai/api/monitor/usage/quota/limit` | Coding Plan 配额接口 |
| `ZCODE_UPSTREAM_PROXY` | 空 | 上游代理（http/socks5）。后台「代理设置」页保存的代理优先生效；验证码求解与上游请求统一走它 |
| `ZCODE_CAPTCHA_STALE_GRACE` | 300000 | 验证码参数过期宽限期 (ms)，期间返回旧值并后台刷新 |
| `ZCODE_CAPTCHA_FAIL_TTL` | 60000 | 求解失败熔断时长 (ms) |
| `ZCODE_THINKING_BUDGET` | 8192 | GLM-5.3 强制思考：客户端未开启时自动注入的思考预算 tokens |
| `ZCODE_REASONING_EFFORT` | max | GLM-5.3 思考深度（low / high / max）|
| `ZCODE_MODELS` | 内置模型清单 | `/v1/models` 对外公布的模型，逗号分隔，可按套餐覆盖 |
| `ZCODE_MAX_REQUEST_BYTES` | 8388608 | 单次网关 JSON 请求体上限（字节） |
| `ZCODE_OAUTH_REDIRECT_URI` | https://zcode.z.ai/login | OAuth 回跳地址。Z.AI 按 client_id 校验白名单，该公开 client_id 仅注册了 `/login`，请勿改动 |
| `CAPTCHA_CACHE_TTL` | 45000 | 验证码缓存时长 (ms) |
| `ZAI_UPSTREAM_URL` / `ZAI_FALLBACK_URL` / `BIGMODEL_UPSTREAM_URL` | — | 上游端点 |

时间、重试次数、端口和请求体上限等数值配置会在启动时限制到安全范围；超出范围时使用边界值。

## 项目结构

```
├── app/
│   ├── main.py            # FastAPI 应用工厂 + 生命周期
│   ├── settings.py        # 环境变量 / 配置
│   ├── models.py          # Account 数据模型与状态
│   ├── store.py           # SQLite 持久化 + 轮询游标（data/accounts.db）
│   ├── agent.py           # 上游请求构建
│   ├── captcha.py         # 无痕验证求解（Playwright 无头 Chromium）
│   ├── quota.py           # 额度查询 + 后台用量监控
│   ├── oauth.py           # Z.AI OAuth 登录流程
│   ├── auth_admin.py      # 后台 / 网关鉴权
│   ├── logs.py            # 彩色终端日志
│   ├── routes/            # gateway / admin_api / pages
│   └── statics/           # app.css, auth.js, toast.js, header.js, admin/*.html
├── main.py                # 命令行入口（serve / login / accounts / quota ...）
├── data/                  # 运行时生成：accounts.db (SQLite)
├── Dockerfile             # 镜像（Python + Google Chrome）
├── docker-compose.yml     # 一键部署
├── railway.toml           # Railway Dockerfile 部署与健康检查
├── zbpack.json            # Zeabur 指定 Dockerfile
├── .dockerignore
├── .github/workflows/     # docker-build.yml（仅构建验证，不推送 Docker Hub）
├── docs/ARCHITECTURE.md   # 架构概览
├── requirements.txt
└── .env.example
```

## 技术栈

- Python 3.13 · FastAPI · Uvicorn · httpx
- SQLite（账号 / 设置持久化，WAL 模式）
- Playwright + 无头 Chromium（求解阿里云无痕验证 → verifyParam）

## 文档

- [架构概览 docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 系统架构图、请求流程、账号状态机、无痕验证流程与已知限制。

## 开发与测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

测试覆盖协议归一化、OpenAI 工具调用转换、凭证 header 隔离、SQLite 导入去重、
管理密钥脱敏和健康探针；不需要真实 ZCode 账号或浏览器即可运行。

## 致谢

- UI 设计参考:[chenyme/grok2api](https://github.com/chenyme/grok2api)
- 社区:[linux.do](https://linux.do)

## 许可证

本项目采用 [AGPL-3.0](LICENSE) 许可证。

## 重要免责声明

本仓库仅供学习、研究、个人实验和内部验证使用，不提供任何形式的商业授权、适用性保证或结果保证。

作者及仓库维护者不对因使用、修改、分发、部署或依赖本项目而产生的任何直接或间接损失、账号封禁、数据丢失、法律风险或第三方索赔负责。

请勿将本项目用于违反服务条款、协议、法律法规或平台规则的场景。商业使用前请自行确认 LICENSE、相关协议以及你是否获得了作者的书面许可。
