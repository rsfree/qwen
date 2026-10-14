# qwen-service

chat.qwen.ai 的**双门出口**：视频门（**t2v / i2v**，火山方舟 Seedance 契约）+ chat 门
（**t2t 文本对话 + 单张图片解析**，OpenAI 形态，2026-09-24 新增并当日实测）。多账号池
（登录态最小凭据）+ 完整浏览器指纹请求头 + 惰性轮询 + **轻量排队重试（重启不丢任务）**，
一条 `cgt-…` 任务贯穿创建与查询。调用方**只换 Base URL + Key** 即可接入。

```
POST /api/v3/contents/generations/tasks        → 200 {"id": "cgt-…"}   视频·创建（只回 id）
GET  /api/v3/contents/generations/tasks/{id}   → 200 方舟任务对象        视频·查询（六态）
POST /v1/chat/completions                      → 200 chat.completion    chat 门（t2t + 图片解析；流式/非流式）
POST /v1/responses                             → 200 response           Responses 门（语义同 chat 门）
GET  /v1/models                                → 200 能力清单            OpenAI 形态（注册的上游 chat 模型 + 视频条目）
GET  /healthz · /readyz · /stats                                      运维面（无凭据原文）
```

- **对外契约全文**：**[`docs/INTERFACE.md`](docs/INTERFACE.md)**（冻结；§8 = chat 门，§9 = Responses 门，§10 = 能力回退通道）
- **上游契约**：**[`docs/UPSTREAM.md`](docs/UPSTREAM.md)**（qwen 网页端唯一真源；§4.4/§4.5 = t2t 请求体与响应形态）

---

## 0. 我要做什么 → 看哪个文件

| 我想… | 看这里 |
|---|---|
| 接这个服务（方舟 SDK 客户端） | `docs/INTERFACE.md` |
| 接 chat 门（OpenAI SDK / new-api 等客户端） | `docs/INTERFACE.md` §8 |
| 改 qwen 请求头 / 请求体 / 响应判读 | `app/upstream/qwen/client.py` |
| 改"多账号怎么轮换、冷却、计额度" | `app/upstream/qwen/accounts.py`（chat 门取号不看视频额度） |
| 改登录铸造（signin / 轮换出口） | `app/upstream/qwen/signin.py` |
| 改方舟 ↔ qwen 的翻译与降级口径 | `app/ark.py`（纯函数，测试主战场） |
| **改 OpenAI chat 的翻译与拍平口径** | `app/openai_chat.py`（纯函数）+ `app/models.py`（模型注册） |
| **改"排队 / 重试 / 出队"策略** | `app/service.py`（编排）+ `app/store.py`（`queued` 记录、`attempts`/`next_attempt_at`）+ `app/coordinator.py`（出队与推进） |
| 改任务存取 | `app/store.py`（SQLModel；SQLite 默认 / PostgreSQL 可选） |
| 改对外鉴权与路由 | `app/main.py` |
| 改上游有没有这个能力 / 未证实项 | `docs/UPSTREAM.md` |

---

## 1. 跑起来

```bash
cp .env.example .env
# 填：QWEN_ACCOUNTS（多账号）、QWEN_ACCOUNT_PASSWORD、QWEN_SIGNIN_PROXY（轮换 HTTP 代理出口，必填）
docker compose up -d --build
curl -s localhost:8400/readyz
```

本机直接跑（用仓内 venv）：

```bash
export QWEN_ACCOUNTS='a@x.cn,b@x.cn' QWEN_ACCOUNT_PASSWORD='…'
export QWEN_SIGNIN_PROXY='http://user:pass@pool.example:2086'   # signin 必须走轮换出口（HTTP 代理）
/Users/betterme/.workbuddy/binaries/python/envs/qwen/bin/gunicorn \
  -c gunicorn_conf.py "app.main:create_app()"
```

🔴 末尾那对**括号不能省**：目标是**工厂**而不是模块级 `app` 对象。
写成 `app.main:app` 会得到 `App failed to load.`（`tests/test_wiring.py` 钉住这条）。

### 零成本自检（不提交、不落库）

```bash
# 视频门 dry-run
curl -s localhost:8400/api/v3/contents/generations/tasks \
  -H 'Authorization: Bearer <key>' -H 'X-Avm-Dry-Run: 1' \
  -d '{"model":"qwen/video","content":[{"type":"text","text":"一只猫"}],"ratio":"16:9"}'
# → 返回"将要发出的请求"（上游 URL / 完整头（Cookie 打码）/ body）

# chat 门 dry-run（同样零上游调用）
curl -s localhost:8400/v1/chat/completions \
  -H 'Authorization: Bearer <key>' -H 'X-Avm-Dry-Run: 1' \
  -d '{"model":"qwen3.7-plus","messages":[{"role":"user","content":"你好"}]}'
```

### 测试

```bash
/Users/betterme/.workbuddy/binaries/python/envs/qwen/bin/python -m pytest   # 165 项，零网络
/Users/betterme/.workbuddy/binaries/python/envs/qwen/bin/ruff check .
/Users/betterme/.workbuddy/binaries/python/envs/qwen/bin/python scripts/env_sync_check.py  # .env ⇄ 模板 一致性
```

> 🔒 **公开仓纪律**：真实基础设施标识**一律不入库**（代理池地址 / 内网 IP / 账号上游 id /
> 上游 task id / 任何凭据样态）—— 池地址只在 gitignored 的 `.env` 里，库内一律占位符
> （`pool.example` / `token-service.example`）。这条有门禁：`tests/test_public_surface.py`
> （主机名白名单 + 私网 IP + 凭据样态，白名单每条都要写理由）。

> **配置两份文件的分工**：`.env.example` = 字段说明的权威来源（42 键，含代码默认与坑，进库）；
> `.env` = 本机/部署机的真实取值（gitignored）。模板侧门禁在 `tests/test_env_contract.py`；
> 生效文件侧只能在有 `.env` 的机器上跑脚本（`.env` 不入库 ⇒ 写成测试就只能是假绿灯）。

---

## 2. 冻结决策（2026-09-22；chat 门 2026-09-24 增补）

1. **范围 = 创建 + 查询两个核心端点**；列表 / DELETE **刻意不实现**（路由不存在，
   不返回空列表之类的假数据）。回调 v1 不实现（`callback_url` 只记服务端日志）。
2. **创建响应逐字 `{"id": …}`**；查询响应 = 白名单 ∧ 有真值：
   不出现 `resolution` / `seed` / `usage` 等（不编"看起来合理"的常量）；
   降级说明**不下发**（只进服务端日志；2026-10-14 用户口径）。
3. **容量语义 = 3 次/天/账号（UTC 日，t2v+i2v 共用）** ⇒ 账号池按 UTC 日计数 + 冷却 +
   提交节奏（`QWEN_SUBMIT_MIN_INTERVAL`）。真实天花板 = 账号数 × 3。
4. **凭据 = 登录态最小凭据**（`Cookie: token=<JWT>`），signin **必须走轮换出口**；
   每账号 token 分格缓存（互不挤掉），任务记录只存 `credential_id` 指纹。
5. **RGV587 的根因是请求头不全**（不是限速/账号/IP）⇒ 请求头照 `biz-api::build_headers`
   逐字段对齐，`version: 0.2.0` 是写端点硬门槛，两者都有实证（`docs/UPSTREAM.md` §2）。
6. **查询不强制 Key（`id` 即凭据）**：`GET /tasks/{id}` 不带 `Authorization` 也放行（可分享结果链接）；
   **带了 Key 才按归属过滤** —— 非属主仍 **404、不发上游**（有变异自证用例钉住），
   无效 Key 仍 401（不静默吞掉配置错误）。创建（POST）**必须**带 Key。
7. **容量不足不再硬 429**：落 `queued` 排队，等窗口自己提交（见 §3）；**只有**队列深度超限
   才回 429 背压。重试**只覆盖"可证明未提交"的失败** —— 建任务是计费动作，"可能已提交"的重试
   等于赌重复计费。
8. **chat 门（2026-09-24）= t2t 文本 + 单张图片解析**：不做 t2i / image_edit / 视频生成
   （视频走方舟门）；文件/音频/视频解析**明确 400**（外链附件上游实测拒绝，U-15）；
   **必须带 Key**；**无排队**（同步链路，容量不足直接 429 背压）；**不消耗视频额度**
   （独立取号通道，但同账号提交节奏照守）；多轮/system 请求**拍平**成单条 prompt（写降级）；
   上游 `usage` 真实透传（缺键不出）；流内 error 事件响亮失败；降级说明只记服务端日志。
9. **能力回退通道（2026-09-24）**：请求点了 qwen 给不了的能力（`tools`、文件/音频/视频分段、
   多图、data: 图片、Responses 工具状态项）⇒ **整单转方舟 chat/responses 应答**
   （函数调用/内置搜索真实生效）。🔴 **回退模型名全链路脱敏**：应答 `model` 回显请求值、
   流式/报错/dry-run 一并清洗（`<redacted-model>`），完整原文只进服务端日志；
   回退事实经 `x-qwen-fallback` 头披露（不再写 degradations）。
   配置 `ARK_FALLBACK_KEY`+`ARK_FALLBACK_MODEL` 才启用；通道故障 ⇒ 502，**不静默降回 qwen**
   （`docs/INTERFACE.md` §10）。

## 3. 排队 / 重试 / 重启不丢（轻量实现）

**没有引入任何独立队列组件**（无 Redis/Celery/独立表）：队列就是任务表里 `status='queued'`
的记录，消费者是既有件 —— 后台协调器（默认开）与调用方的 GET。`queued` 本身是方舟契约里的
合法初始态，所以**对外形态零改动**。⚠️ 只作用于**视频门**；chat 门是同步链路、无排队。

| 关切 | 行为 |
|---|---|
| 容量不足（全冷却 / 额度尽 / 节奏窗） | 落 `queued` + **立即**返回 `cgt-…`（不再 429） |
| 谁把它送上去 | 协调器每 `COORDINATOR_TICK`（默认 5s）出队；调用方 GET 也顺带推进一格 |
| 重试范围 | 风控（RGV587）/ 额度尽 / 鉴权失效 / 铸造失败 / 会话失效 —— **都可证明未受理** |
| 不重试范围 | 上游 5xx / 超时 / 未知业务码 ⇒ 照实回报错误，任务落 `failed`（绝不赌重复计费） |
| 退避与上限 | 指数退避 `QUEUE_RETRY_BASE × 2^n`（上限 600s）；`SUBMIT_MAX_ATTEMPTS`（5）与 `TASK_TIMEOUT`（900s）双闸门封顶 |
| 背压 | 排队深度 ≥ `QUEUE_MAX_DEPTH`（50）⇒ 429 + `Retry-After`（绝不无限囤积） |
| 重启不丢 | `queued`/`running` 记录在任务库（≥7 天）；**账号额度计数与冷却也在库**（KV 快照）⇒ 不会把已用额度算成 0；上游 token 刻意不落盘（重启重铸，免费） |
| 严格模式 | `SUBMIT_QUEUE_ENABLED=0` 回到"容量不足立即 429"（既有调用方口径不变） |

对应用例：`tests/test_queue.py`（7 项）—— 排队→出队→成功、协调器无人查询也推进、
风控换号重试、**含义不明的失败不重试**、背压 429、**跨进程重启后 queued 任务与额度计数都还在**。

## 4. 真实链路状态

经本服务全链路真实跑通（实录：`docs/UPSTREAM.md` §7.1/§7.2）：

| 步骤 | 结果 |
|---|---|
| signin（经轮换出口） | ✅ `token_len=209` / 4.3s |
| dry-run（两扇门） | ✅ 零成本，头与 body 与抓包同构 |
| **t2v / i2v 真跑**（2026-09-22） | ✅ ≈93~343s，产物均 **5.042s**，账号轮换实证 |
| **chat t2t 非流式 / 流式**（2026-09-24） | ✅ 22.7s / 5.8s，SSE 形态实测关闭（U-12） |
| **图片解析**（上游域内图） | ✅ 经服务全链路 200，usage 真实透传 |
| 文档/音频/视频解析（外链） | 🔴 上游拒绝（`Internal error!` / `invalid_input`，U-15）⇒ 本门明确 400 |

- 冒烟驱动：`scripts/live_smoke.py signin|dryrun|run|chat`（`run` 耗视频额度；`chat` 免费）；
  探针 `scripts/probe_chat_parse.py --kind text|image|document|audio|video --confirm`
  （单发取证，原文落档 `var/probe/`）。

### 诚实边界（未完成，不许当已完成）

- 队列**不做请求去重**：同一 `POST` 重发两次 = 两条独立任务（方舟原生同样不保证幂等）。
- 队列吞吐受协调器**单轮串行推进**限制；多 worker 需要选主/租约去重（未实现）—— 当前部署模型是 `WORKERS=1`。
- **文件/音频/视频解析未实现**：外链附件被上游拒绝（U-15，因果未拆分）；正解是上游 OSS 上传链路
  （`getstsToken` → PUT，签名 300s）—— 实现后再开。
- **t2t 正式频控形态（U-14）、token 真实失效时点（U-10）等长跑项未观测**：见 `docs/UPSTREAM.md` §9。
- 镜像：CI 构建 + **推送前容器冒烟** + 推 GHCR（`ghcr.io/aicatfire/qwen`，版本/`latest`/`sha-` 三 tag）；
  **chat 门尚未部署**（需重新就地构建发版）。
- `rehost`（产物转存）未实现（上游 URL 实测可直下，先透传）。
- 降级说明**不下发**（2026-10-14 用户口径）：响应与上游/方舟原生逐字段一致，严格 SDK 的 unknown-field 报错问题随之消失；可观测性移到服务端日志。

## 5. 相关技能

`seedance-protocol-adapter` · `site-api-to-protocol-adapter` · `multi-account-adapter-pool` ·
`qwen-guest-identity-service`（访客身份，另一个方向）

## 6. 发版（与姊妹仓同一套）

push `main` → 自增 patch（`v0.0.x`）→ 版本号写回 `app/__init__.py` → 构建镜像 →
**容器内冒烟（不过不推）** → 推 GHCR（`ghcr.io/aicatfire/qwen`：版本 / `latest` / `sha-<7位>` 三 tag）
→ 打 annotated tag → 建 Release。人工 tag（minor/major）走 tag 模式（**不改代码**）；
逃生阀：commit message 含 `[skip release]`（仅 push 事件生效，补发用 `workflow_dispatch`）。
