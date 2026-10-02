# qwen-service 对外契约（方舟 Seedance 形态 + OpenAI chat 形态）

> 两扇门，互不相通：
> · **视频门** = `POST|GET /api/v3/contents/generations/tasks`，与火山方舟原生契约**逐字段对齐**
>   —— 调用方**只换 Base URL + Key** 即可接入。上游是 chat.qwen.ai 视频链路（t2v / i2v）。
> · **chat 门** = `POST /v1/chat/completions`（OpenAI 形态，**t2t 文本 + 单张图片解析**，
>   2026-09-24 新增并当日实测，见 §8）。上游是同一批账号的对话链路。
> 上游侧契约见 [`UPSTREAM.md`](UPSTREAM.md)。

---

## 0. 端点总表

| # | 方法 | 路径 | 状态 |
|---|---|---|---|
| 1 | `POST` | `/api/v3/contents/generations/tasks` | 🔴 核心（逐字实现） |
| 2 | `GET` | `/api/v3/contents/generations/tasks/{id}` | 🔴 核心（逐字实现） |
| 3 | `GET` | `/api/v3/contents/generations/tasks`（列表） | ⚪ **刻意不实现**（路由不存在 ⇒ 404/405） |
| 4 | `DELETE` | `/api/v3/contents/generations/tasks/{id}` | ⚪ **刻意不实现**（同上；未终态也绝不伪造 `cancelled`） |
| 5 | `GET` | `/v1/models` | 🟢 **OpenAI 形态能力清单**（见 §7；**不校验 Key**） |
| 6 | `POST` | `/v1/chat/completions` | 🔴 **chat 门（OpenAI 形态，t2t + 图片解析）**（见 §8；**必须带 Key**） |
| 7 | `POST` | `/v1/responses` | 🔴 **Responses 门（OpenAI 形态；语义同 §8，能力回退走方舟 `/responses`）**（见 §9；**必须带 Key**） |
| — | `GET` | `/healthz` `/readyz` `/stats` | 运维面（不含任何凭据原文） |

> 🔴 **范围冻结**：不适配的可选端点**不得返回空列表之类的假数据** —— 调用方会把"空列表"
> 误读成"没有任务"。列表与取消要真做时，**先改本文件**再动代码。
>
> ⚠️ 上面第 5 条**不属于方舟任务契约**，与范围冻结不冲突：冻结的是**任务列表**（方舟的任务枚举），
> `/v1/models` 给的是**能力列表**（OpenAI 系的模型发现），两者语义、形状、受众都不同。

---

## 1. 鉴权

- `Authorization: Bearer <API Key>`；**未配置 `API_KEYS` 时鉴权关闭**（仅限内网部署，显式声明）。
- **Key 的三种形态**（2026-10-01 起，"是什么"按形状自动判别）：

  | 形态 | 长相 | 接受判据 |
  |---|---|---|
  | ① 静态 Key | `sk-qwen-…`（运营方登记在 `API_KEYS` 的任意串） | 白名单逐字比对 |
  | ② 账号串 | `<email>\|<password>`（同上，白名单里的不透明字符串） | 白名单逐字比对 |
  | ③ **JWT 门卡** | `eyJ…`（调用方**自己的** qwen access token） | 形状 + `type=access_token` + `exp` 未过期（**不验签**） |

  ③ 的语义是**透传账号门卡**："你有一个活的 qwen 登录态"即可进门；上游凭据仍由账号池托管。
  ⚠️ access token **15 分钟**一换，换新后指纹随之改变 ⇒ 用 JWT 建的任务，**旧 JWT 过期后
  不可再读**（要持久归属请用 ①/②）。`refresh_token` 形态**不收**（只认活会话，不收长期凭据）。

- 🔴 **写端点必须带 Key；读不强制**（2026-09-22 修订，同 `../jimeng` 口径）：

  | 请求 | 行为 |
  |---|---|
  | `POST /tasks`（视频创建） | **必须**带有效 Key（缺失/无效 ⇒ 401） |
  | `POST /v1/chat/completions`（chat 创建） | **必须**带有效 Key（同上；错误体用 **OpenAI 词汇表**，见 §8.4） |
  | `GET /tasks/{id}` **不带** `Authorization` | ✅ **放行** —— `id` 本身就是凭据（调用方可把结果链接直接分享出去） |
  | `GET /tasks/{id}` 带**无效** Key | ❌ 401 —— 不因为"反正放行"就把配置错误静默吞掉（那是最难查的一类问题） |
  | `GET /tasks/{id}` 带**有效但非属主**的 Key | ❌ 404 —— 比 `jimeng` **严一档**，保留"跨 Key 读不到"（ADR-003） |
  | `GET /v1/models` | ✅ 免 Key（能力探测先于填 Key） |
  | `GET /healthz` `/readyz` `/stats` | ✅ 免 Key（运维面；⚠️ 上反代时**必须**挡住 `/stats`，它含账号用量与冷却） |

- 任务与创建 Key **绑定**：任务记录里存 `credential_id = HMAC-SHA256(secret, key)` 指纹
  （永不落明文）。**带上 Key 读别人的任务 ⇒ 本地直接 404，且不发任何上游请求**。
- ⚠️ 既然 GET 免 Key，`task_id` 的**不可猜性**就成了安全前提：当前格式为
  `cgt-<UTC 秒>-<5 位随机>`（≈29.8 bit 熵）。**待办**：把随机段加长到 ≥16 位再对外大面积开放
  （见 `docs/UPSTREAM.md` 的登记；本机部署当前只绑回环，暂无暴露面）。

---

## 2. 创建任务（视频门）

```
POST /api/v3/contents/generations/tasks
Content-Type: application/json
Authorization: Bearer <key>
```

### 2.1 请求字段

| 字段 | 类型 | 必填 | 本服务行为 |
|---|---|---|---|
| `model` | string | ✅ | `qwen/<任意名>` 或裸名；**provider 段必须为 `qwen`**，否则 400。模型名不转发上游（上游无视频模型维度，见 UPSTREAM §8 D-14） |
| `content` | object[] | ✅ | 见 §2.2 |
| `ratio` | string | ❌ | 枚举：`1:1` / `3:4` / `4:3` / `16:9` / `9:16`；**枚举外（含 `adaptive`、缺失、写错）一律落 `1:1` + 降级说明**（冻结口径 D-2） |
| `duration` | integer | ❌ | 上游固定 ~5s：`5` 静默；`>5` 向下吸附 + 说明；`-1` 替换成 5 + 说明；**`<5` 直接 400** |
| `callback_url` | string | ❌ | v1 未实现回调 ⇒ 进 `degradations`（请轮询查询接口） |
| `resolution` / `seed` / `watermark` / `camera_fixed` / `generate_audio` / `return_last_frame` / `frames` / `service_tier` / `draft` / `priority` / `safety_identifier` / `tools` / `omni_reference_task_type` / `output_format` / `execution_expires_after` | — | ❌ | **认得但做不到** ⇒ 进 `degradations`（不假装支持、不静默丢弃；`seed: -1` 视为"没给"） |
| `extra_body` | object | ❌ | 未建模字段口袋：**每个键**都会进 `degradations` |

### 2.2 `content[]` 降维矩阵（方舟超集 → qwen 子集）

| 入参 | 处置 |
|---|---|
| `text`（可多个） | 按顺序换行拼接为单条 prompt |
| `image_url` + `role:"first_frame"`（或**无 role**） | ⇒ i2v 首帧；**恰好一张**才合法 |
| `image_url` + `role:"last_frame"` | **400**（上游没有尾帧能力，也不悄悄当首帧） |
| `image_url` + `role:"reference_image"` | **400**（上游不支持参考图） |
| 两张及以上 `image_url` | **400**（不替调用方挑一张） |
| `video_url` / `audio_url` / `draft_task` | **400**（上游无此能力） |
| 认不出的 `type` | **400** |

**图片 URL 形态**：只收 `http(s)` 绝对地址；`data:` URI ⇒ **400**（v1 无上传链路）。
非上游域名（非 `cdn.qwenlm.ai` / `qwen-chat.oss-*`）⇒ **照发 + 降级告警**（外链作首帧未完全验证）。

### 2.3 响应（逐字对齐原生）

```json
{ "id": "cgt-20260922043000-ab12c" }
```

- **只回 `id`**，不含 status（必须轮询或查询）。
- `id` 由本服务生成（`cgt-YYYYMMDDHHMMSS-xxxxx`）；上游 UUID 只存本层记录与观测面。
- 容量不足时**不再回 429**：任务落 `queued` 并**立即**返回 id（`queued` 是方舟契约里的合法初始态），
  详见 §3.3。只有**排队深度**超 `QUEUE_MAX_DEPTH` 才回 429 背压。

---

## 3. 查询任务（视频门）

```
GET /api/v3/contents/generations/tasks/{id}
Authorization: Bearer <key>
```

### 3.1 响应字段（**键集最小且只给真值**）

| 键 | 出现条件 | 说明 |
|---|---|---|
| `id` | 恒有 | 本服务任务 id |
| `model` | 恒有 | 创建时请求的 `model` 原样回显 |
| `status` | 恒有 | 六态枚举：`queued` / `running` / `succeeded` / `failed` / `expired` / `cancelled`（当前实现只会出现前五个之一，`cancelled` 无触发入口） |
| `error` | 恒有 | 成功时显式 `null`（方舟规定）；失败时 `{code, message}` |
| `created_at` / `updated_at` | 恒有 | **epoch 秒**（整数） |
| `content.video_url` | 仅 `succeeded` | 调用方拿到即可下载（上游 CDN 签名直链，实测可直下） |
| `duration` | 仅 `succeeded` | **5**（上游固定 ~5.042s，实测 n≥5；不是请求值） |
| `ratio` | 有值时 | 实际生效比例（决定输出画幅） |
| `degradations` | 仅非空时 | 🔴 **加性扩展**（原生没有这个键）：本层的降级/吸附/忽略说明清单 |

🔴 **不编造**：`resolution` / `seed` / `usage` / `frames` / `framespersecond` / `draft` 等
**一概不出现**（上游不回传/不支持，编一个"看起来合理"的常量比留空更糟）。
`usage` 尤其：视频链路上游没有 token 口径，视频额度是"3 次/天"计数 —— 给 0 等于声称"消耗 0 token"。
（⚠️ chat 链路**有**真实 usage，见 §8.3 —— 两扇门口径不同，别互相串。）

> ⚠️ **严格 SDK 客户端注意**：官方 Java/Go SDK 对 unknown field 是**报错**。若你的客户端
> 不接受 `degradations` 扩展，请在网关上做响应字段过滤（或告知本服务关闭该键 —— 待实现开关）。

### 3.2 状态语义

| status | 语义 | 终态 |
|---|---|---|
| `queued` | **已受理但还没递交给上游**（账号全忙/冷却/额度用尽 ⇒ 排队等窗口；详见 §3.3） | 否 |
| `running` | 上游生成中（实测 t2v/i2v ≈105s 出片） | 否 |
| `succeeded` | 成功（`content.video_url` 可用） | 是 |
| `failed` | 失败/上游任务不存在/零产物/排队超时或超次数（`error.message` 会写明"未提交、未消耗额度"） | 是 |
| `expired` | 超过 `TASK_TIMEOUT`（默认 900s）仍未终态 | 是 |

**查询是"惰性回查"**：调用方每 GET 一次，本服务推进该任务一格（`queued` ⇒ 尝试提交；
`running` ⇒ 回查上游一次）；终态后不再打扰上游。
（后台协调器 `COORDINATOR_ENABLED=1`，**默认开**，无人查询时也照常推进。）

### 3.3 排队 / 重试 / 重启耐久（轻量实现）

**没有引入任何独立队列组件** —— 队列就是任务表本身（`status='queued'` 的记录），
消费者是既有件：后台协调器 + 调用方的 GET。

| 关切 | 本服务行为 |
|---|---|
| 容量不足 | 落 `queued`，**立即**返回 id（不再硬 429）；由协调器 / 后续 GET 出队提交 |
| 排队深度 | ≥ `QUEUE_MAX_DEPTH`（默认 50）⇒ **429 + `Retry-After`**（背压保留，绝不无限囤积） |
| 重试范围 | **只重试"可证明上游未受理"的失败**：风控（RGV587）、额度耗尽、鉴权失效、凭据铸造失败、会话失效 |
| 不重试范围 | 含义不明的失败（上游 5xx / 超时 / 未知业务码）—— 建任务是**计费动作**，"可能已提交"的重试等于赌重复计费 ⇒ 照实回报错误，任务落 `failed` |
| 退避 | 指数退避（`QUEUE_RETRY_BASE × 2^n`，上限 600s），并受 `SUBMIT_MAX_ATTEMPTS`（默认 5）与 `TASK_TIMEOUT` 双闸门封顶；`failed` 的 `error.message` 会显式声明"未提交、未消耗额度" |
| 重启不丢 | `queued` / `running` 记录都在任务库（≥`TASK_RETENTION_DAYS`=7 天）；**账号额度计数与冷却也在 KV 里** ⇒ 新进程起来接着推进，不会把已用额度算成 0 |
| 重启重新铸造 | 上游 token **刻意不落盘**（重启重新 signin，免费；避免凭据进持久层） |
| **token 过期续期** | **主动**：按 JWT 自带的 `exp`（自称 30 天）**并压一个保守上限**（`QWEN_TOKEN_TTL`，默认 **6 天** = 给"实际可能 7 天失效"预留 1 天）提前重铸；**被动**：上游判 401 ⇒ 清缓存 → **立即重铸 → 原请求重试一次**（写端点也重试：401 = 未受理，不会重复计费）。两次仍 401 ⇒ 判定凭据/账号问题（503 语义 + 账号冷却） |
| 请求去重 | ⚠️ **不提供**：同一 `POST` 重发两次 = 两条独立任务（方舟原生同样不保证幂等）。需要去重请在调用方做 |

> 关闭队列回到严格模式：`SUBMIT_QUEUE_ENABLED=0`（容量不足 ⇒ 立即 429，与既有调用方行为一致）。

---

## 4. 错误（视频门，方舟形状）

```json
{"error": {"code": "InvalidParameter", "message": "… Request ID: <rid>", "type": "BadRequest",
           "param": "content[0].role"}}
```

| HTTP | code | 何时 | 调用方动作 |
|---|---|---|---|
| 400 | `InvalidParameter` | 请求写错（含 content 角色/图片数量/duration<5/模型 provider 错） | 改请求 |
| 401 | `AuthenticationError` | Key 缺失/无效 | 检查 Key |
| 404 | `InvalidEndpointOrModel.NotFound` | 任务不存在**或不属于该 Key** | 检查 id / Key |
| 429 | `RateLimitExceeded` | **排队深度超限**（`QUEUE_MAX_DEPTH`）或关闭队列时的容量不足 | 按 `Retry-After` 退避 |
| 429 | `ServerOverloaded` | 上游 x5sec 风控（RGV587）；队列开启时通常不再直通（会排队换号重试，除非超次数） | **退避，勿连打**（重试会加深标记） |
| 429 | `QuotaExceeded` | 账号额度耗尽（3 次/天/账号，UTC 日重置）；队列开启时通常转成排队 | 等跨日或换渠道 |
| 502 | `InternalServiceError` | 上游 5xx / 非 JSON / WAF 页（**不自动重试**，防重复计费） | 退避重试 |
| 503 | `CredentialUnavailable` | 本服务**凭据铸造/续期失败**（部署问题，非调用方错） | 联系运维（检查 `QWEN_SIGNIN_PROXY` / `QWEN_TOKEN_URL`） |
| 504 | `InternalServiceError` | 上游超时 | 退避重试 |

- 失败响应带 `Retry-After` 头（当本层能给出建议等待时）；所有响应带 `x-request-id` 头
  （与 `message` 末尾的 `Request ID:` 同值，便于对账）。
- 🔴 **凭据铸造失败 = 503**（部署状态），不是 400（参数错误）—— 混淆会让调用方去改请求体。
- ⚠️ chat 门的错误**不用**这张表 —— 它是 OpenAI 形态，见 §8.4。

---

## 5. 调试面：dry-run

```
POST /api/v3/contents/generations/tasks
X-Avm-Dry-Run: 1
```

跑完**完整翻译**后直接返回"将要发出的请求"（含上游 URL / 完整头（`Cookie` 打码）/ body），
**零上游调用、零任务落库**。chat 门（§8）支持同一个头。

```json
{"dry_run": true,
 "upstream": {"method": "POST",
              "url": "https://chat.qwen.ai/api/v2/chat/completions?chat_id=<chat_id>",
              "headers": {..., "Cookie": "token=<redacted>"},
              "body": {...}},
 "degradations": ["…"]}
```

用途：部署前/改配置后**零成本**自检翻译与请求形状；也是排障第一手段。

---

## 7. `GET /v1/models`（OpenAI 形态能力清单）

```
GET /v1/models          # 无需 Authorization（能力探测要在填 Key 之前就能用）
```

清单 = **两部分**（2026-09-24 起按用户指令注册上游模型）：

1. **注册的上游 chat 模型** —— 拉自上游 `GET /api/models`（**免鉴权**，✅ 2026-09-24 实测：
   无任何 cookie 即 200）。`TTL` 缓存（`MODELS_CACHE_TTL`，默认 300s），拉取失败**回退上一份
   好清单**（负缓存顺延，绝不因上游抖动让本端点 5xx）。只注册**真正能跑 chat（t2t）**的条目：
   `info.meta.chat_type` 不含 `t2t` 或 `info.is_active: false` 的**不注册**（列出来就是制造假能力）。
2. 视频能力条目 `qwen/video`（原样保留 —— 视频方舟门不变，见 §2/§3）。

```json
{"object": "list",
 "data": [{"id": "qwen3.7-plus", "object": "model", "created": 1732711466, "owned_by": "qwen",
           "title": "Qwen3.7-Plus", "media": "text", "task": "chat",
           "accepts_image": true, "requires_prompt": true, "thinking": true,
           "vision": true, "context_length": 1000000, "verified": false, "notes": "…"},
          {"id": "qwen/video", "object": "model", "created": 0, "owned_by": "qwen",
           "title": "Qwen 视频生成（文生视频 / 单首帧图生视频）", "media": "video",
           "accepts_image": true, "requires_image": false, "requires_prompt": true,
           "max_input_images": 1, "duration_s": 5,
           "ratios": ["1:1", "3:4", "4:3", "16:9", "9:16"],
           "verified": true, "notes": "…"}]}
```

- **OpenAI 原生四键**（`id` / `object` / `created` / `owned_by`）恒在；其余为**加性扩展**
  （对 unknown field 报错的严格客户端只取前四键即可）。
- `created`：**视频条目恒为 0**（本服务无从得知，不编时间戳）；**chat 条目用上游自带的
  `info.created_at`**（有真实来源，不算编造），缺省回 0。
- `verified`：视频条目 `true`（2026-09-22 端到端实测出片，UPSTREAM §7.1）；chat 条目**已实测**
  （2026-09-24 文本 + 图片解析真实一发成功）⇒ 本门真实支持的输入面看 `notes`；
  `vision`/`document`/`video`/`audio` 是**上游模型自报的输入能力**（照值透传，不代表本门全支持）。
- 🔴 **只列真正支持的**：刻意缺席的能力见 `app/models.py::DELIBERATE_ABSENCES`，**不出现在清单里**。
- `model` 传参：chat 门收**裸名或 `qwen/` 前缀**（§8.1）；视频门收 `qwen/video`。
- 用途：OpenAI 系客户端 / 网关（new-api 等）做**能力探测**。

---

## 8. `POST /v1/chat/completions`（OpenAI 形态 chat 门）

> 2026-09-24 新增、**当日真实一发实测通过**（文本 + 图片解析）。🔴 范围：chat（t2t）任务 +
> **单张图片解析**；不做 t2i / image_edit / 视频生成（视频走方舟门 §2/§3）。
> 文件/音频/视频解析**明确 400**（外链附件上游实测拒绝，U-15 —— 见 §8.7）。
> 上游请求体**逐字对齐当日用户抓包 + 实测证词**（UPSTREAM §4.4）；
> 流式响应形态已实测关闭（UPSTREAM §4.5，U-12 ✅）。

### 8.1 请求

```
POST /v1/chat/completions
Content-Type: application/json
Authorization: Bearer <key>        # 必须带（写端点）
```

| 字段 | 必填 | 本服务行为 |
|---|---|---|
| `model` | ✅ | 上游模型裸名（`qwen3.7-plus`…）或 `qwen/` 前缀形态；**原样回显**在响应 `model` 里。不做本地白名单硬校验（注册清单有 TTL，硬校验会用过期清单拒绝合法新模型）——未知模型由上游拒绝。`qwen/video` ⇒ **400**（视频请走方舟门） |
| `messages` | ✅ | `system` / `user` / `assistant`；`content` 收字符串或分段数组（`text` + `image_url`，见 §8.7）。**文件/音频/视频分段 ⇒ 400**（U-15 实证） |
| `stream` | ❌ | `true` ⇒ 真流式（上游 SSE 增量 → `chat.completion.chunk`）；`false`/缺省 ⇒ 服务端聚合后一次性回 `chat.completion` |
| `temperature` / `top_p` / `max_tokens` / `tools` / `tool_choice` / `response_format` / `seed` / `reasoning_effort` 等 | ❌ | **认得但上游没有** ⇒ 进 `degradations`（不假装支持、不静默丢弃）。⚠️ `tools`/`tool_choice`：上游**不支持 OpenAI 风格函数调用**（实测静默忽略——UPSTREAM §4.6 / U-16），带 tools 的请求**不会产生 `tool_calls`**；模型可能改用**内置工具**（如 `auto_search` 的 web_search）作答 |

### 8.2 多轮与 system（拍平口径）✅ 2026-09-24 三轮真实验证

上游一次只收**一条**用户消息 ⇒ 多轮以**转录拍平**形态支持（不是上游原生会话）：

- **单条 user 消息、无 system** ⇒ 内容**原样直发**（抓包实证路径，零降级）；
- 有 system / 多轮历史 ⇒ 拍平成单条 prompt：system 作 `【系统指令】` 前缀，
  之前各轮作 `User:` / `Assistant:` 转录，最后一条 user 消息收尾；**写降级说明**
  （"会话历史由调用方维护"—— OpenAI 语义本就无状态，与上游语义自洽）；
- ✅ **实测**：三轮真实对话（记忆问答 → 追问验证 → 组合追问，含流式）模型全程正确使用
  历史上下文——拍平形态对标准 OpenAI 客户端（每次带全量历史）**功能等价**；
- 图片附件只认**最后一条 user 消息**；更早轮次里的图片 ⇒ 400（拍平会丢，不静默丢输入）；
- 每个请求**新建上游会话**（`chats/new` 免费段）：无状态、不缓存会话、账号轮换不受影响
  （U-13 ✅：`chats/new` 传 `chat_type="t2t"` + 指定模型，实测可用）。

### 8.2.1 思考档位（`reasoning_effort` / `enable_thinking`，2026-09-24 前端抓包三档）

| 档位 | 触发 | 上游 `feature_config`（抓包逐字） | 首字体感 |
|---|---|---|---|
| **fast（快速）** | `reasoning_effort:"none"/"minimal"` 或 `enable_thinking:false` | `thinking_enabled:false, thinking_mode:"Fast"` | 最快（无思考等待） |
| **auto（自动，缺省）** | 不传 / `reasoning_effort:"medium"/"low"/"auto"` | `thinking_mode:"Auto", auto_thinking:true` | 模型自主决定是否思考 |
| **thinking（思考）** | `reasoning_effort:"high"` | `thinking_mode:"Thinking", auto_thinking:false` | 强制思考（慢） |

- 🔴 **思考摘要透传**（2026-09-24 二次实测）：`thinking_summary` 事件的 `delta.extra` 里有
  `summary_title` / `summary_thought`（**分步标题+要点**，数组逐步累加 —— 官网 UI 同款数据）。
  本门对新增条目做 diff，转成 **`delta.reasoning_content`**（流式）/
  `response.reasoning_summary_text.delta`（Responses 流式）增量下发。思考全文仍不下发（上游只给摘要）。
- 非流式应答的 `message` 无 reasoning 字段（上游不给）——要思考摘要请用流式。
- 🔴 **思考泄漏模型自动降档**（2026-10-02 实测）：部分模型（默认名单 `qwen3.8-omni-flash`，
  env `QWEN_THINKING_LEAK_MODELS` 覆盖）在 **auto / thinking 档把思考过程直接写进正文 `content`**
  —— 不拆 `reasoning_content`、也没有 `</analysis>` 标记，调用方直接渲染会把思考当正文
  （实测复现 3/3，含流式；同请求 `qwen3.7-plus` 流式**正确分离** ⇒ 模型侧行为，非本服务翻译 bug）。
  本门处置：
  - **缺省（auto）命中名单 ⇒ 自动切 fast**（`thinking_enabled:false` ⇒ 无思考、正文干净），
    并在 `degradations` 写明降级理由（**不静默改语义**：降档事实对调用方可见）；
  - 调用方**显式**声明 `reasoning_effort:"high"` ⇒ **照发不覆盖**（显式意图优先），
    但同样写 `degradations` 告知"思考会并入正文、无 reasoning_content"；
  - 名单可换也可关：改 env 即可，不必发版；**未设 env ⇒ 加固默认生效**，显式设为空串 ⇒ 关闭加固。
- 🔴 **思考心跳**：思考期上游静默，流式应答在正文前先发一个**空格增量**变相解锁"首字"
  （用户方案）——流式正文会带一个前导空格（非流式不带；fast 档无心跳）。拼正文请 `strip` 或跳过首空格。

### 8.3 响应

非流式（`stream:false`）：

```json
{"id": "chatcmpl-…", "object": "chat.completion", "created": 1758…, "model": "qwen3.7-plus",
 "choices": [{"index": 0, "message": {"role": "assistant", "content": "…"}, "finish_reason": "stop"}],
 "usage": {"prompt_tokens": 1826, "completion_tokens": 678, "total_tokens": 2504}}
```

流式（`stream:true`）：`text/event-stream`，`chat.completion.chunk` 事件（首片带 role、
末片 `finish_reason:"stop"` + usage、`data: [DONE]` 结束）。

- `usage`：**上游真实回传**（`input/output/total_tokens`，2026-09-24 实测随 answer 事件递增）
  ⇒ 只做改名映射透传（取流中最后一份 = 终值）；上游没给 ⇒ 整键缺席，不编造。
- `degradations`：**加性扩展**（非空才出现；流式时只随首片）。严格 SDK 若对 unknown field
  报错，请在网关上做响应字段过滤。
- 上游零输出 / SSE 形态不认识 / **流内 error 事件** ⇒ **502 响亮失败**（含上游错误原文），
  绝不静默回空内容。

### 8.4 错误（本门用 OpenAI 词汇表）

```json
{"error": {"message": "… Request ID: <rid>", "type": "invalid_request_error", "code": "InvalidParameter"}}
```

| HTTP | type | 何时 |
|---|---|---|
| 400 | `invalid_request_error` | 请求写错（messages 形态 / 文件·音频·视频分段 / model 指向视频等） |
| 401 | `authentication_error` | Key 缺失/无效 |
| 429 | `rate_limit_error` | 无可用账号（冷却/提交节奏未到；chat **无排队**，直接背压） |
| 502 / 504 | `server_error` | 上游拒绝 / 超时 / 流式零提取 / 流内 error 事件 |
| 503 | `service_unavailable` | 凭据铸造/续期失败（部署问题） |

### 8.5 容量、额度与账号

- **chat 不消耗视频额度**：取号走独立通道（不看 3 次/天的视频计数），但**仍受**
  冷却与同账号提交节奏（`QWEN_SUBMIT_MIN_INTERVAL`）约束 —— 写端点的突发纪律对 t2t 同样适用。
- **无排队**：chat 是同步链路（无任务表可落），容量不足直接 429 背压，调用方退避重试。
- 失败回报沿用视频门的账号冷却分类；额度语义错误按短冷处理（不把账号冷到 UTC 日界——那是视频语义）。
- token 续期两层（主动 TTL + 401 当场重铸重试一次）与视频门共用同一套。

### 8.6 dry-run

`X-Avm-Dry-Run: 1` ⇒ 跑完翻译返回"将要发出的上游请求"（t2t 体含 `files`，`Cookie` 打码），
零上游调用。部署前自检翻译与请求形状的第一手段。

### 8.7 多模态输入矩阵（2026-09-24 单发探针实测）

| OpenAI content 分段 | 上游 files[] | 判决 |
|---|---|---|
| `{"type":"text"}` | —（files 恒在、纯文本为 `[]`） | ✅ 文本对话 |
| `{"type":"image_url","image_url":{"url":"https://…"}}` | `type:"image"` + `file_class:"vision"`（§4.2 形状） | ✅ **图片解析**（上游域内图实测出答案；第三方域名照发 + 降级告警） |
| `data:` URI | — | ❌ **400**（无上传链路，同视频门口径） |
| 多张图 | — | ❌ **400**（多图输入未验证，不替调用方挑一张） |
| `{"type":"file"}` / `input_file`（文档） | — | ❌ **400**：上游对外链 PDF 实测 `Internal error!`（U-15） |
| `{"type":"input_audio"}` / `audio_url` | — | ❌ **400**：上游对外链音频实测 `invalid_input`（U-15） |
| `{"type":"video_url"}` / `video` | — | ❌ **400**：上游对外链视频实测 `invalid_input`（U-15） |

> 文件/音频/视频解析的**正解**是上游 OSS 上传链路（`POST /api/v2/files/getstsToken` →
> OSS V4 PUT → `file_url`，签名仅 300s）—— 实现了它才能做，v1 刻意不做（范围收敛）。
> 比"照发然后上游报无信息量错误"更诚实的做法是**本门明确 400 + 给出证据**。
> ⚠️ 配置了**能力回退通道**（§10）后，本表这些 400 场景会整单转方舟应答。

---

## 9. `POST /v1/responses`（OpenAI Responses 形态门）

> 2026-09-24 新增。语义与 §8 chat 门**完全同源**（同一套 qwen 翻译/执行/回退），
> 只是请求/应答采用 OpenAI **Responses** 形态。**必须带 Key**；错误体 OpenAI 词汇表。

### 9.1 请求

| 字段 | 必填 | 本服务行为 |
|---|---|---|
| `model` | ✅ | 同 §8.1（裸名或 `qwen/` 前缀；`qwen/video` ⇒ 400） |
| `input` | ✅ | 字符串，或条目数组 `{"role", "content"}`（content 分段 `input_text` / `input_image` 等，映射到 §8.7 矩阵） |
| `instructions` | ❌ | ⇒ system（参与 §8.2 拍平） |
| `stream` | ❌ | `true` ⇒ `response.created` → `response.output_text.delta` → `response.completed` 事件流；缺省 ⇒ 一次性 `object:"response"` |
| `tools` / `tool_choice` | ❌ | qwen 不支持 ⇒ **回退方舟 `/responses`**（原生 tools + web_search；§10）；未配置回退 ⇒ 忽略 + 降级（同 chat 门） |
| 其余采样参数 | ❌ | 忽略 + 降级（同 chat 门） |

### 9.2 应答

```json
{"id": "resp_…", "object": "response", "created_at": 1758…, "status": "completed",
 "model": "qwen3.7-plus",
 "output": [{"type": "message", "id": "msg_…", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": "…", "annotations": []}]}],
 "usage": {"input_tokens": 2421, "output_tokens": 12, "total_tokens": 2433}}
```

- `usage`：上游真值透传（Responses 键名与上游一致）；`degradations` 加性扩展（非空才出现）。
- 回退应答 `model` 改写为调用方请求的模型（🔴 **回退模型名不外泄**，见 §10.2）；
  回退事实经 `x-qwen-fallback: ark` 头披露。

### 9.3 工具调用状态输入项

`input` 里出现 `function_call` / `function_call_output` / `reasoning` / `web_search_call` 等
**工具调用状态项**时，qwen 无法表达 ⇒ 配置了回退通道则整单转方舟（多轮工具对话在方舟原生成立）；
未配置 ⇒ **400** 且指明"配置 ARK_FALLBACK_* 可支持"（比静默丢输入诚实）。

---

## 10. 能力回退通道（两扇 OpenAI 门共用；2026-09-24）

> 用户决策：「调用 tools 或者其他不支持功能的时候回退到方舟」。
> 实现 `app/ark_fallback.py`；**配置了才启用**（`ARK_FALLBACK_KEY` + `ARK_FALLBACK_MODEL`，
> 只配一个拒绝启动），未配置 ⇒ 一切维持 §8.7 / §9 的降级与 400 行为。

### 10.1 触发面

| 触发（qwen 给不了的能力） | chat 门原行为 | responses 门原行为 | 回退后 |
|---|---|---|---|
| `tools` / `tool_choice`（函数调用） | 忽略 + 降级（U-16） | 忽略 + 降级 | **方舟原生执行**（实测返回真 `tool_calls`） |
| 文件/音频/视频 content 分段（U-15） | 400 | 400 | 方舟应答（对文件/音视频分段的实际支持以其模型能力为准，原样透传） |
| 多图 / `data:` URI 图片 | 400 | 400 | 方舟应答（视觉模型支持多图/base64） |
| 函数调用状态消息（`role:"tool"` / `assistant.tool_calls`） | 400 | — | 方舟原生多轮工具对话（🔴 函数调用是多轮闭环，本跳没带 tools 也必须回退） |
| Responses 工具调用状态输入项 | —（无此门） | 400 | 方舟原生多轮工具对话 |

其余降级（temperature 等采样参数）**不触发**回退 —— qwen 仍能正常应答。

### 10.2 行为约定

- **请求**：只换 `model`、原样透传（messages/input/tools/stream 全保留；多轮由方舟原生消化）；
  chat 门回退打 `POST {ARK_FALLBACK_BASE}/chat/completions`，responses 门打 `…/responses`。
- 🔴 **回退模型名对调用方全链路脱敏**（2026-09-24 用户指令）：
  · 应答 `model` 改写为调用方请求的模型（含嵌套对象，如 Responses 的 `response.model`）；
  · 流式 chunk/事件**逐条清洗**（每条 JSON 里的 model 一并改写）；
  · **报错报文清洗**：上游错误原文里即使含模型名，调用方看到的也是 `<redacted-model>`；
  · dry-run 预演同样打码；
  · 完整原文**只进服务端日志**（排障用，不出站）。
- **回退事实仍披露**：`degradations` 回退说明（不含模型名）+ `x-qwen-fallback: ark` 响应头
  ——说走了通道，不泄哪个模型。
- **dry-run**：`X-Avm-Dry-Run: 1` ⇒ 返回 `{"channel":"ark-fallback", "reason":…, "upstream":…}`
  （Authorization 与 model 均打码，零调用）。
- 🔴 **回退通道故障 ⇒ 502 响亮失败，不静默降回 qwen** —— 调用方点名的能力 qwen 给不了，
  静默降级等于让调用方在不知情下拿次级结果。
- 🔴 **实测证据（2026-09-24，真实 Key）**：chat 门带 `get_weather` → 方舟返回标准
  `tool_calls`（`get_weather({"city":"北京"})`）；responses 门带 `web_search` → 方舟
  `web_search_call: completed` + 真实天气正文。

### 10.3 配置

| env | 说明 |
|---|---|
| `ARK_FALLBACK_KEY` | 方舟 API Key（**凭据，只写 .env**；与 MODEL 同时配置才启用） |
| `ARK_FALLBACK_MODEL` | 回退主模型 id（如 doubao 系列） |
| `ARK_FALLBACK_MODELS` | **备用模型链**（逗号分隔，按序 failover）：主模型被方舟限流（429）时依次切换；全部被限 ⇒ 429 原样转发。所有链上模型名对调用方脱敏 |
| `ARK_FALLBACK_BASE` | 默认 `https://ark.cn-beijing.volces.com/api/v3`（北京 region） |
| `ARK_FALLBACK_TIMEOUT` | 回退请求超时（秒，默认 120） |

---

## 6. 部署与环境变量（关键项）

> 本节是**关键项速查**；**完整键表（42 个，含每个键的代码默认与坑）见 `.env.example`** ——
> 它是字段说明的权威来源（新参数先加那里，再同步到 `.env`）。两份文件的一致性有门禁：
> 模板侧 `tests/test_env_contract.py`（CI 可跑），生效文件侧 `scripts/env_sync_check.py`（`.env` 不入库，故只能是脚本）。

| env | 默认 | 说明 |
|---|---|---|
| `QWEN_BASE_URL` | `https://chat.qwen.ai` | 上游 base |
| `QWEN_CHAT_MODEL` | `qwen3.7-plus` | 会话聊天模型（视频门的兜底模型，非"视频模型"） |
| `QWEN_ACCOUNTS` / `QWEN_ACCOUNT_PASSWORD` / `QWEN_ACCOUNTS_FILE` | — | 账号池（多账号轮换；视频额度 3 次/天/账号） |
| `QWEN_ACCOUNT_COOKIES[_FILE]` | 空 | 可选：每账号附加 cookie（整份 jar 或指纹 cookie）；默认只发 `token` 最小凭据 |
| `QWEN_TRUST_ENV` | `0` | 🔴 **别开**：置 1 会让使用侧读取宿主环境代理变量 ⇒ 出口变成"经代理、可能一请求一 IP"（静默行为改变）。详见 `UPSTREAM.md` §2.5 |
| `QWEN_SIGNIN_PROXY` | 空 | **轮换 HTTP(S) 代理出口**（signin 必须走它，直连会把出口打进 WAF 墙）。实测池语义：每连接换 IP + 同连接复用同 IP ⇒ 每次铸造换 IP、一次铸造全程一个 IP。**只收 `http(s)://`**（SOCKS 分支已删） |
| `QWEN_TOKEN_URL` | 空 | 或改用外部 token 服务（`GET /token?account=`，同 image-adapter） |
| `QWEN_TOKEN_TTL` | `518400` | token 缓存**上限**（秒）：主动续期取 `min(JWT exp − 提前量, 铸后本值)`。**默认 6 天 = 给"实际可能 7 天失效"预留 1 天**；`<=0` 也归一到 6 天（不提供"关掉上限"的开关）。⚠️ `exp` 是上游**自称**（实测 30 天），真正兜底是 401 当场重铸重试 |
| `QWEN_DAILY_VIDEO_CAP` | `3` | 每账号每日**视频**额度（**UTC 日**窗口）；chat 不消耗该额度 |
| `QWEN_SUBMIT_MIN_INTERVAL` | `15` | 同账号提交最小间隔（防写请求突发；chat 与视频共用该节奏） |
| `QWEN_SIGNIN_MIN_INTERVAL` | `45` | 跨账号共享的 signin 节奏 |
| `MODELS_CACHE_TTL` | `300` | `/v1/models` 的上游模型清单缓存时长（秒）；失败回退上一份好清单 |
| `ARK_FALLBACK_KEY` | 空 | 能力回退通道方舟 Key（**凭据只写 .env**；与 MODEL 同时配置才启用，见 §10） |
| `ARK_FALLBACK_MODEL` | 空 | 回退模型 id；**只配一个拒绝启动** |
| `ARK_FALLBACK_BASE` | 方舟北京 region | 回退通道端点 |
| `ARK_FALLBACK_TIMEOUT` | `120` | 回退请求超时（秒） |
| `SUBMIT_QUEUE_ENABLED` | `1` | 轻量排队重试总开关（`0` = 严格模式：容量不足立即 429）；**只作用于视频门**（chat 无排队） |
| `QUEUE_MAX_DEPTH` | `50` | 排队深度上限（超限 ⇒ 429 背压） |
| `SUBMIT_MAX_ATTEMPTS` | `5` | 单任务最大提交尝试次数（超限 ⇒ `failed`，明示未消耗额度） |
| `QUEUE_RETRY_BASE` | `30` | 排队重试退避基数（秒，指数退避，上限 600s） |
| `API_KEYS` | 空 | 对外 Key（逗号分隔）；空 = 关闭鉴权（仅内网） |
| `TASK_DB` | `sqlite:///<DATA_DIR>/qwen.db` | 任务库（多实例请换 PostgreSQL） |
| `TASK_TIMEOUT` | `900` | 未终态任务的超时阈值（→ `expired`；排队中的任务同样受此闸门） |
| `TASK_RETENTION_DAYS` | `7` | 记录保留期（契约要求 ≥7 天） |
| `COORDINATOR_ENABLED` | `1` | 后台协调器（出队排队任务 + 主动回查 + 过期清理） |
| `COORDINATOR_TICK` | `5` | 协调器轮询间隔（秒） |
| `WORKERS` / `PORT` | `1` / `8400` | 单 worker 是架构约束（节奏是进程内状态） |

**运维面**：`/healthz`（存活）、`/readyz`（账号数/store/coordinator）、`/stats`
（账号池统计：**邮箱半脱敏、无 token 原文**，含冷却与今日用量；`tasks.queued` 给出排队深度；
`models_registry` 给出模型清单缓存状态）。
