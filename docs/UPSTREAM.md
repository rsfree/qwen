# Qwen 网页端上游契约（视频 t2v / i2v + 文本对话 t2t）

> 本文件是本仓**上游侧的唯一真源**：`app/upstream/qwen/*` 里凡与上游形态相关的判读，
> 都能在这里找到依据与证据等级。
>
> 跨项目姊妹资料（取证过程更全，引用时注明出处）：
> - `reverse-proxy/docs/upstream/qwen-chat-api.md`（鉴权 / 反爬 / 错误码 / 身份池 / RGV587 订正）
> - `reverse-proxy/docs/upstream/qwen-async-task-api.md`（异步任务端点全量）
> - `reverse-proxy/docs/upstream/qwen-quota-api.md`（额度口径）
> - `video-adapter/docs/upstreams/qwen-official-api.md`（视频侧差异核对 D-1…D-14）
>
> 证据等级：✅ **实测**（本生态发出真实请求并读到响应）／📖 **静态取证**（前端 bundle）／
> ⚠️ **未证实**（不得在实现里假设）。

---

## 0. 一句话模型

```
POST /api/v2/chats/new                        → data.id（chat_id；可长期复用）
POST /api/v2/chat/completions?chat_id=<id>    → 视频：data.messages[0].extra.wanx.task_id（同步返回）
                                                文本：SSE 增量流（stream:true，形态见 §4.5）
GET  /api/v2/task/status/<task_id>            → data.task_status / data.content（视频产物 URL）
GET  /api/models                              → 模型清单（免鉴权，✅ 2026-09-24 实测）
```

- **创建是两段式**：`completions` 的 `chat_id` 必须是**已存在**的会话（瞎填 UUID ⇒ `CHAT_NOT_FOUND`）。
- **查询 HTTP 状态码恒为 200**，真实业务码在响应头 `x-actual-status-code` 与 body 的 `success`。

---

## 1. 端点

| Method | 路径 | 用途 | 证据 |
|---|---|---|---|
| `POST` | `/api/v2/auths/signin` | 登录取 token（`Set-Cookie: token=…`） | ✅ |
| `POST` | `/api/v2/chats/new` | 建会话，返回 `data.id` | ✅ |
| `POST` | `/api/v2/chat/completions?chat_id=<id>` | 提交生成（`chat_type` 三处同标；t2t 即文本对话/多模态解析） | ✅ |
| `GET` | `/api/v2/task/status/<task_id>` | 查询任务 | ✅ |
| `GET` | `/api/models` | 模型清单（**免鉴权**：无任何 cookie 即 200） | ✅（2026-09-24 实测） |
| — | 无取消端点 | 脚本/适配层不声明取消能力（未终态 DELETE 由本服务拒绝） | ✅（证据是"不存在"） |
| `POST` | `/api/v2/users/user/entitlement_quota` | 额度查询（只读） | ✅（`times_left` 有滞后，见 §3） |

---

## 2. 鉴权与请求头

### 2.1 最小凭据 = `Cookie: token=<JWT>`

✅ 实测（`qwen-chat-api.md` §10.1，单变量）：**只给一个 `Cookie: token=<JWT>`**、其他 cookie 与
`bx-ua`/`bx-umidtoken` 一律不带，完整跑通生成链路，出图 owner 与 token 账号一致。

✅ **2026-09-22 再证（视频写端点）**：本服务用同一份最小凭据（`Cookie: token=<JWT>`，无 bx-*、
无其他 cookie）真实跑通 **t2v 与 i2v** 两条链路（见 §7.1）—— U-7 由此关闭。
✅ **2026-09-24 三证（t2t 写端点）**：同一头集跑通文本对话与图片解析（§4.5），零 RGV587。

- token 是**无状态 JWT**（实测：`token_len=209`；签发后 27 分钟仍被接受）。
  **载荷只有三个键：`{exp, id, last_password_change}`（没有 `iat`）**，`id` 即账号 id，
  实测 `exp` = **铸后 30 天**（2026-09-22 现铸解析：`2592000` 秒）。
- 🔴 **`exp` 只是上游"自称"，不得当真实寿命用**：服务端可能提前失效 ——
  **用户经验是"可能 7 天就失效"**（未一手实测，见 §9 U-10）。本仓口径（2026-09-22 起）：
  **主动**续期 = `min(exp − 提前量, 铸后 QWEN_TOKEN_TTL)`，提前量 = 生命的 10%（夹在 1 分钟 ~ 6 小时），
  **`QWEN_TOKEN_TTL` 默认 6 天**（= 给"疑似 7 天"预留 1 天；`<=0` 也归一到 6 天，
  **刻意不提供"关掉上限"的开关**）；
  **被动**兜底 = 上游判 401 ⇒ 清缓存 → **立即重铸 → 原请求重试一次**（写端点同样重试：
  401 = 上游未受理，不会重复计费）。两条路都在 `app/upstream/qwen/accounts.py` + `app/service.py::_authed_call`。
- token 由 `POST /api/v2/auths/signin` 铸造：body `{"email", "password": sha256hex(password)}`，
  token **只在 `Set-Cookie`**（body 是账号记录，没有 token）。✅ 2026-09-22 经池复现（4.3s/账号）。
- 🔴 **signin 有 IP 级频率墙**：同出口几秒内连登多个账号 ⇒ `aliyun_waf` 挑战页 ⇒
  **必须走轮换出口**（本仓 `QWEN_SIGNIN_PROXY` / `QWEN_TOKEN_URL`，见 `.env.example`）。
  签发与使用分离是安全的（token 无状态）：**登录走轮换出口、出片走正常出口**。

### 2.2 请求头（照 `biz-api::build_headers` 逐字段对齐）

```
Accept: application/json
Content-Type: application/json
User-Agent: <Chrome macOS>
Origin: https://chat.qwen.ai          Referer: https://chat.qwen.ai/（或 /c/<chat_id>）
source: web
version: 0.2.0                        ← 🔴 写端点硬门槛，见 §2.3
Accept-Language: zh-CN,zh;q=0.9       Connection: keep-alive
Sec-Fetch-Dest: empty   Sec-Fetch-Mode: cors   Sec-Fetch-Site: same-origin
Timezone: <本地时间串，如 Tue Sep 22 2026 04:13:35 GMT+0800>
X-Request-Id: <uuid4>                 X-Accel-Buffering: no
sec-ch-ua / sec-ch-ua-mobile / sec-ch-ua-platform
Cookie: token=<JWT>
```

✅ 2026-09-22 实测：该头集（不含 `bx-*`）连续完成 signin / chats/new / completions / task/status
全部 200，**零 RGV587**。✅ 2026-09-24 在 t2t 写端点复证（文本 + 图片解析）。

### 2.3 `version: 0.2.0` 是写端点的硬门槛

✅ 六格对照实测（`qwen-async-task-api.md` §7.3 / §11）：缺该头 ⇒ HTTP 200 +
`{"success":false,"code":"Bad_Request","details":"…"}`（**文案像"请求体写错了"，极易误诊**）；
补上即正常。`chats/new` **不要求**该头。判据看响应形态，不要读文案。

### 2.4 🔴 RGV587 风控的根因订正（2026-09-19）

`RGV587_ERROR::SM`（x5sec 挑战）曾被认为是"累计写请求门禁"。**订正**：
2026-09-18 下午那批 RGV587 的根因是**探针自身请求头不全**（缺
`Sec-Fetch-*` / `Timezone` / `Connection` / `X-Accel-Buffering`，且 `Accept` 形态不对）——
**与账号、出口 IP、累计写计数无关**；换上 `biz-api::build_headers` 同款头后同一账号同一出口
连续 3 次成功（`qwen-chat-api.md` §10.19）。

⇒ 本仓纪律：**见到 RGV587，第一步把请求头与本文件 §2.2 逐字段对齐**，再谈限速/换号。
处置上仍按 429 + `Retry-After` 退避（不自动重试，避免加深标记）。
（⚠️ 2026-09-24 又踩一次同款坑：探针只带 Cookie 打 `chats/new` ⇒ 非 JSON 响应；
换完整头即正常 —— 探针一律复用 `QwenClient.headers()`。）

### 2.5 出口拓扑：哪个请求走哪个 IP（2026-09-22 代码核对 + 实测）

两扇门**刻意解耦**——`token` 是**无状态 JWT**，铸造 IP 与使用 IP 不需要一致（09-22 全链路实测已证）。

| 路径 | 出口 | 跳不跳 |
|---|---|---|
| **使用侧**：`chats/new` / `chat/completions` / `task/status` / 产物下载 | **服务宿主机直连**（`QwenClient` 不配 proxy，`trust_env` 默认 `False`） | **零跳转**：同一次任务全程同一 IP；**所有账号共用这一个 IP**（无"每号一 IP"能力） |
| **铸造侧**：signin 铸 token | `QWEN_SIGNIN_PROXY`（**HTTP(S) 代理**；**真实地址只在 gitignored 的 `.env`**，库内一律占位符） | **会跳**，且刻意如此（直连登录会把出口打进 WAF 墙，`qwen-chat-api.md` §2.6） |
| 兜底：`QWEN_TOKEN_URL` | 外部 token 服务 | 由该服务决定（`accounts.py` 亦显式 `trust_env=False`） |

🔴 **铸造侧只支持 HTTP(S) 代理形态（2026-09-22 用户决策：不做兼容，SOCKS 分支已删除）**。理由是对池的实测：

| 语义 | 实测（池 2086） | 为什么正好合适 |
|---|---|---|
| **每连接换出口 IP** | 三次新连接 = **三个互不相同的出口 IP** | 每次铸造换一个 IP ⇒ 不撞登录 IP 墙 |
| **同连接复用同 IP** | 同一 client 两次请求 = 同一 IP | 一次铸造的「预热 + 登录」**全程一个 IP**（自洽） |

⇒ 删掉自研 SOCKS5 拨号器后少约 130 行 socket/TLS/HTTP 解析，且**顺带修掉一个语义瑕疵**：
旧实现一次铸造开**两条连接**（预热 + 登录），用"每连接换 IP"的池时那两条**可能落在两个 IP**。
现在 `mint_token` **每次调用新建一个 client**（= 新连接 = 新 IP），同一个 client 内完成预热与登录 —— 别把它改成常驻 client。

⚠️ 跨账号共享 signin 节奏（`QWEN_SIGNIN_MIN_INTERVAL=45s`）是**防连登把出口打进墙**，与 IP 轮换策略无关。

🔴 **危险开关 `QWEN_TRUST_ENV`**：置 `1` 会让使用侧也去读宿主环境的 `HTTP(S)_PROXY`/`ALL_PROXY`
（本机/沙箱环境**确有**这些变量）⇒ 使用侧会变成**经环境代理、可能一请求一 IP**，且是**静默**行为改变。
**默认 `False`（不带此变量）是正确姿势**，别开。

🔴 **"不跳"的代价 = 绑死一个出口**：该 IP 若被整域拦（405 / WAF 挑战页）**不会自愈** ——
只能换机或加 SNAT 固定干净出口（image-adapter 在 node-064 上踩过：默认 IP 405、须挑干净 IP + 策略路由）。
因此**部署前先验宿主出口**：`curl -sS -o /dev/null -w "%{http_code}\n" https://chat.qwen.ai/api/models`（200 = 干净）。

> ⚠️ 尺度提醒：7 账号 × 3 次/天**共用一个出口**已被实测证实可用（09-22 两账号两任务同 IP 连续成功）；
> 但账号数/频率继续上量时，"全账号同 IP"是一个相关性风险（上游收紧时会一起收紧）——届时再考虑 per-account 出口（当前**无此能力**）。

### 2.6 鉴权形态 × 视频矩阵（2026-09-22 一手实测）

四形态构造**照图片侧口径逐字段照抄**（`reverse-proxy/qwen/probe/probe_auth_forms_matrix.py` 的
`http_t2i.headers` / `anon_headers` / `ident_pool.build_headers`）；探针 `scripts/probe_auth_forms_video.py`
（两段式，与图片侧判据结构一致）。

| 鉴权形态 | ① `chats/new`（免费段，`chat_type=t2v`） | ② `chat/completions`（写端点，`stream:false`） | 判决 |
|---|---|---|---|
| **`cookie`**：`Cookie: token=<JWT>` | `200` | **`200`** ⇒ 回 `task_id`，出片 | ✅ **能出视频** |
| **`bearer`**：`Authorization: Bearer <JWT>`（**不带任何 cookie**） | `200` | 🔴 **RGV587 风控 + x5sec** | ❌ 写端点被拒 |
| **`guest`**：设备指纹三件套（无 `token`），`chat_mode=guest` | `200` | 🔴 **`400 internal_error`**（单发实测，见 §9.2） | ❌ 会话能建、**生成被拒** |
| **`anon`**：完全未登录（无 Cookie / Authorization / `bx-*`） | 🔴 **`401 Unauthorized`** | —（会话都建不了） | ❌ |

🔴 **本条最可复用的判读：免费段不能当鉴权判据。** `cookie` / `bearer` / `guest` 三种形态在
`chats/new` 上**全回 200**，差异**全部**出现在写端点 —— 图片侧早已记过该结论
（`qwen-chat-api.md` §2.13 第 2 条的单变量对照），本次在**视频侧一手复现**。
⇒ 任何"某凭据形态能用"的结论，必须打在写端点上；只测 `chats/new` 会得出三种形态都行的错判。

**`cookie` 格的三道自证（全绿）**：
1. **归属**：产物 URL 的 `/output/<uuid>/` 段 == JWT 载荷里的账号 id（**一致**；账号 id 本身不入库/不写文档）；
2. **产物**：下载 **5,487,103 bytes**，容器 `mvhd` 时长 **5.042s**（与既有 n≥5 实测一致）；
3. **凭据形态**：仅 `Cookie: token=<JWT>`（无其它 cookie）+ `chat_mode=normal`，全链路零 RGV587。

⚠️ **口径偏差（如实标注）**：图片侧要求「一格一号」（6 格 6 个号）；本次只有**一个账号** ⇒
`cookie` 与 `bearer` 两格落在同一个号上（中间**强制冷却 90s**）。要严格版请给足账号，探针支持 `--forms` 逐格指定。

### 2.7 🔴 2026-09-30 认证域改版：滑块墙 + auth.qwen.ai + bx 三件套（当日一手实测闭环）

**上游变更**（上表 2.6 的结论自此修订）：chat 域的 **signin 与写端点**对**一切纯 HTTP 客户端**
回 aliyun_waf **滑块挑战页**（HTTP 200 + text/html、16KB、25×captcha+slider，无可计算项）——
httpx / curl_cffi(chrome) / 池三个网段出口 / VPS 直连 / 全新账号 ⇒ **全灭**；**与 IP、账号、
TLS 指纹均无关**（各变量单发实测排除）。

**通行证（三层，全部当日实测）**：

1. 🔑 **`bx-ua` / `bx-umidtoken` / `bx-v` 三件套**（浏览器 JS 生成）：带它们 ⇒ 写端点放行；
   **捕获值可跨 IP 重放**（本机浏览器捕获 → VPS 重放 `waf=False`）。查询 GET **不需要**。
2. **认证迁独立域 `auth.qwen.ai`**：浏览器登录根本不走 chat 域 ——
   - `POST https://auth.qwen.ai/api/v2/auths/signin`（`email` + `sha256hex(password)`）⇒
     响应**体** `data.access_token`（**15 分钟**寿命），Set-Cookie 只剩 `refresh_token`
     （30 天、HttpOnly、`Domain=.qwen.ai`；实测**不轮换**）。头集缺一件 ⇒
     `Invalid request header`；`version` 必须是 **`0.3.12`**（auth 域自报版本，
     **不是**写端点的 0.2.0），另需 `x-request-origin`/`source`/`sec-fetch-site: same-site`。
   - `GET https://auth.qwen.ai/api/v2/auths/refresh`（Cookie 带 jar，内含 RT）⇒
     体 `data{access_token, refresh_token}`。**免密码、免预热、不拦滑块、无 IP 墙**
     ⇒ **续期主路**（15 分钟一次）；signin 只在 RT 失效时发生（≈30 天一次）。
     signin 另有 **IP 级频率墙 ≈12 次/6 分钟** ⇒ 仍必须走轮换出口 + 跨账号节流。
3. **pair 门（token×jar 成对）**：写请求必须携带**产出该 token 的那次会话**种下的
   cookie 全集（预热 `GET /auth` 的 WAF 冷启动章 `acw_tc`/`x-ap` + signin 的
   Set-Cookie + `token=`）—— 实测 2026-09-22：token 配别人的 jar ⇒ x5sec。
   "token-only jar" 在生成端点同样吃 x5sec（图片侧 2026-09-18 实测）。

**端到端实证（VPS 直连）**：浏览器登录 → RT → refresh 全自动续期 → `chats/new`
（body: `title/models/chat_mode/chat_type/timestamp(ms)/project_id`）→ t2v 创建（bx 头）
→ `extra.wanx.task_id` → `/api/v2/task/status/<id>` 查询 → **真实出片 6.3MB**
（`var/live/bx-probe-6f6f4644.mp4`，16:9）。挑战页形态另见 §9.

**对服务的影响**：`upstream/qwen/signin.py` 全面改写（auth 域 signin + refresh +
jar 工具）；账号池凭据 = `(token, jar)` 二元组、**jar/RT 进 KV 持久化**（政策修订：
access token 仍不落盘）；客户端 Cookie 改发同源 jar（pair 门）。



---

## 3. 额度

| 项 | 值 | 证据 |
|---|---|---|
| 视频额度 | **3 次/天**（`t2v` 与 `i2v` **共用**同一池） | ✅（normal 免费档） |
| 窗口 | **UTC 日**（本地日会提前 8 小时"误判恢复"） | ✅（`qwen-chat-api.md` §2.12c） |
| 查询接口 | `POST /api/v2/users/user/entitlement_quota`（只读） | ✅ |
| `times_left` | **有滞后/缓存，不是实时计数** ⇒ 只当参考，熔断以本仓账号池的计数为准 | ✅（09-22 复证：成功出片后 5 分钟与 10 分钟两次读 `t2v` 仍为 `3`；`t2i` 同为 `3`） |
| 风控拦截时 | **不扣额度**（实测：被 RGV587 拦后 `t2v.times_left` 仍为 3） | ✅ |
| "提交即扣 vs 成功才扣" | ⚠️ 未证实 | — |
| **t2t（文本对话/解析）** | **不在视频额度池内**（chat 门不计数、不受 3 次/天约束；当日 7 连发真实写请求零频控/零风控） | ✅（09-24 实测；⚠️ 上游对 t2t 的正式频控形态仍未观测到，见 U-14） |

### 3.1 🔴 积分（Credits）—— 视频真正的计量口径（2026-10-02 一手实测）

上游有两道门，**第二道（积分）才是绑定约束**：

| 项 | 值 | 证据 |
|---|---|---|
| 计费 | **25 积分/条** 视频（`Wan2.5-preview.1080P.5s`） | ✅ 账本 `-25` 记录（池账号 `2xx***`/`b3t***`） |
| 每日免费 | **+40 积分/天/账号**（`bonusCredits`，懒授予） | ✅ 全新号首次触碰即 `total=40`；`+40` 与 `-25` **同一秒**（视频请求自身触发授予，无需预热查询） |
| 有效期 | **日清**：未用完部分 `expire` 清零，**不跨天累积** | ✅ `expire -40` / `expire -15` 记录 |
| ⇒ 实际容量 | **1 片/天/账号**（40÷25，余 15 过期）⇒ 3 次/天的额度闸门**用不满** | 推算（账本算术） |
| entitlement 账户 | **懒创建**：任意积分 API 触碰即开通（此前 `NOT_LOGIN` = 尚未创建，不是没资格） | ✅ 7 个全新号批量验证 |
| 账本查询 | `GET https://sg-entitlement.qwen.com/api/entitlement/credits/credits/history?biz_id=ai_qwen&...`，`Authorization: Bearer <access_token>`，只读不扣 | ✅（只有 `history`，`balance/summary/detail/account` 全 404） |

**服务侧口径（2026-10-02 定）**：
`daily_video_cap` 保持 **3**（派单**尝试上限**，不是容量承诺）；账号出不了片
（积分/额度不足）⇒ 归入 `QuotaExhaustedError` ⇒ **换号重试**；全池不可用 ⇒ **429**
（上游网关如 new-api 据此轮下一个渠道）。
⚠️ "积分不足"的**上游文案形态未实测**（余额 15 分的第二单一撞滑块没跑成）⇒ 归类走
关键词匹配（`client.QUOTA_WORDS_UNPROVEN`），命中即换号；命中不了仍按 `UpstreamError` 处理，不瞎猜。

---

## 4. 创建请求体（实测形态）

### 4.1 视频（t2v / i2v）

```jsonc
{
  "stream": false, "version": "2.1", "incremental_output": true,
  "chatId": "<chat_id>", "chat_id": "<chat_id>",
  "parentId": "", "parent_id": null, "chat_mode": "normal",
  "model": "qwen3.7-plus",
  "messages": [{
    "id": null, "fid": "<uuid>", "parentId": null, "childrenIds": ["<uuid>"],
    "role": "user", "content": "<prompt>", "user_action": "chat",
    "files": [ /* 仅 i2v，见 §4.2 */ ],
    "timestamp": <epoch 秒>, "models": ["qwen3.7-plus"], "model": "",
    "chat_type": "i2v",
    "feature_config": {"thinking_enabled": false, "output_schema": "phase",
                       "research_mode": "normal", "auto_thinking": false,
                       "thinking_mode": "Fast", "auto_search": true},
    "extra": {"meta": {"subChatType": "i2v", "size": "16:9"}},
    "sub_chat_type": "i2v", "parent_id": null
  }],
  "timestamp": <epoch 秒>, "size": "16:9"
}
```

### 4.1.1 t2v / i2v 判定：三处必须同时标

| 位置 | t2v | i2v |
|---|---|---|
| `messages[0].chat_type` | `"t2v"` | `"i2v"` |
| `messages[0].sub_chat_type` | `"t2v"` | `"i2v"` |
| `messages[0].extra.meta.subChatType` | `"t2v"` | `"i2v"` |

`size` 出现在**两处**（顶层与 `extra.meta.size`），必须同值。比例枚举（UI 截图确认）：
`1:1` / `3:4` / `4:3` / `16:9` / `9:16`；**上游只吃这 5 个**。

### 4.2 `files[0]`（仅 i2v —— 形状依据 2026-09-22 用户抓包；✅ t2t 图片解析复用同款形状，§4.5）

```jsonc
{"type": "image", "name": "example.png", "file_type": "image/png",
 "showType": "image", "status": "uploaded", "file_class": "vision",
 "url": "<上游认识的图片 URL>"}
```

- 🔴 **上游的 i2v 是"引用"而非"上传"**：抓包里 `url` 指向上游已有资源。
  ✅ **2026-09-22 实测**：用户抓包里的样例图（`qwen-chat.oss-ap-southeast-1.aliyuncs.com/
  resources/i2v/…png`）作首帧，经本服务**真实出片**（§7.1 i2v 行）。
  ✅ **2026-09-24 再证（t2t 图片解析）**：同一形状放进 t2t 的 `files`，上游**真实看图作答**（§4.5）。
- ⚠️ **第三方域名的外链图仍未验证** ⇒ 本服务照发 + 降级告警（`app/media.py::host_warning`）。
- 自有图正解（已端到端验证过出片）：`POST /api/v2/files/getstsToken` → OSS V4 PUT → 得 `file_url`
  （⚠️ 签名仅 **300s**，缓存/排队会静默失效）。本仓 v1 **不实现上传链路**，`data:` URI 明确 400。
- 早期抓包版本里曾有 `isQuote: true` 等字段；2026-09-22 抓包**没有**这些键 ⇒ 本仓按**最小集**发。

### 4.3 不接受的字段（视频）

`duration` / `resolution` / `seed` / `watermark` / `camera_fixed` / `generate_audio` / `frames`
**在上游请求体里不存在**（发了也无效，且可能触发校验拒绝）—— 见 §8。

### 4.4 t2t（文本对话/多模态解析）提交体 —— 逐字对齐 2026-09-24 用户抓包 + 当日实测证词

```jsonc
{
  "stream": true, "version": "2.1", "incremental_output": true,
  "chatId": "<chat_id>", "chat_id": "<chat_id>",     // 🔴 双写都要：缺小写 chat_id ⇒ 400
  "parentId": "", "parent_id": null, "chat_mode": "normal",
  "model": "qwen3.7-plus",
  "messages": [{
    "id": null,                                       // 抓包与实测一致：null（不是 UUID）
    "fid": "<uuid>", "parentId": null, "childrenIds": ["<uuid>"],
    "role": "user", "content": "<prompt>", "user_action": "chat",
    "files": [],                                      // 恒在：纯文本为 []；图片解析放 §4.2 条目
    "timestamp": <epoch 秒>, "models": ["qwen3.7-plus"], "model": "",
    "chat_type": "t2t",
    "feature_config": {"thinking_enabled": true, "output_schema": "phase",
                       "research_mode": "normal", "auto_thinking": true,
                       "thinking_mode": "Thinking", "thinking_format": "summary",
                       "auto_search": true},
    "extra": {"meta": {"subChatType": "t2t"}},
    "sub_chat_type": "t2t", "parent_id": null
  }],
  "timestamp": <epoch 秒>
}
```

与视频体（§4.1）的**实证差异**（各按各的抄，别"顺手统一"）：

| 维度 | 视频（t2v/i2v，09-22 抓包） | 文本（t2t，09-24 抓包+实测） |
|---|---|---|
| `stream` | `false`（同步拿 `wanx.task_id`） | `true`（流式增量） |
| `size` | 顶层 + `extra.meta` 两处 | **完全没有** |
| `feature_config` | thinking 关、`thinking_mode=Fast`、无 `thinking_format` | thinking 开、`thinking_mode=Thinking`、`thinking_format=summary` |

与视频体**相同**的（09-24 实测证词，别猜）：

| 维度 | 证词 |
|---|---|
| 顶层 `chatId` + 小写 `chat_id` **双写** | 🔴 缺小写 `chat_id` ⇒ HTTP 200/actual 400 + `RequestValidationError: Field 'chat_id': Field required`（经本服务真实一发踩中并修正） |
| `messages[0].id` = `null` | 抓包原样 |
| `messages[0].files` 恒在 | 纯文本 `[]`；图片解析放 §4.2 条目 |

### 4.5 t2t 响应形态（✅ 2026-09-24 单发探针实测，U-12 关闭）与多模态矩阵

**SSE 事件流**（`content-type: text/event-stream`，探针原文落档 `var/probe/20260924_030839_text/`）：

```jsonc
data: {"response.created":{"chat_id":"…","parent_id":"…","response_id":"…","response_index":"0"}}
data: {"choices":[{"delta":{"role":"assistant","content":"","phase":"thinking_summary",
       "extra":{"summary_title":{"content":[…]},"summary_thought":{"content":[…]}}}}]}
data: {"choices":[{"delta":{"role":"assistant","content":"","phase":"thinking_summary","status":"finished"}}], …}
data: {"choices":[{"delta":{"role":"assistant","content":"你好","phase":"answer","status":"typing"}}],
       "response_id":"…","usage":{"input_tokens":2421,"output_tokens":95,"characters":0,"total_tokens":2516,…}}
data: {"choices":[{"delta":{"content":"","role":"assistant","status":"finished","phase":"answer"}}],"response_id":"…"}
```

判读（实现见 `client.py::stream_chat` / `extract_stream_*`）：

| 维度 | 实测结论 |
|---|---|
| 正文增量 | `choices[0].delta.content`（`phase:"answer"`）—— 逐段推进 |
| thinking | `phase:"thinking_summary"` 事件的 `content` **恒为空串**（摘要正文在 `extra.summary_title/summary_thought`，v1 不透传）；其结束事件**也带 `status:"finished"`** ⇒ 🔴 结束判据必须 `status=="finished"` **且 `phase=="answer"`**（判宽了会在思考阶段掐断整条流） |
| usage | **真实存在**：`input_tokens` / `output_tokens` / `total_tokens`（+ `input_tokens_details` 等），随 answer 事件出现且 **output 逐事件递增** ⇒ 最后一份即终值（OpenAI 映射：prompt/completion/total） |
| 结束 | `status:"finished"`（`phase:"answer"`）后流自然结束；**没有 `data: [DONE]`**（解析器容忍网关注入的 DONE） |
| 流内错误 | 🔴 **HTTP 200 包错误事件**：`data: {"error":"Internal error!"}`（外链 PDF 实测）与 `data: {"error":{"code":"invalid_input","details":"输入或附件无效。请检查后重试。"}}`（外链音频/视频实测）⇒ 解析器必须识别并响亮失败 |

**多模态（files[]）× 判决矩阵**（每格一发，单发即停，探针 `scripts/probe_chat_parse.py`）：

| 附件 | files 条目 | 判决 |
|---|---|---|
| 图片（**上游域内** URL） | §4.2 形状（`file_class:"vision"`），`chat_type` 保持 `t2t` | ✅ **真实看图作答**（潜水员样例图，描述准确；usage input=1826） |
| 文档（外链 PDF） | 同构推测（`type/file_class:"document"`） | 🔴 `{"error":"Internal error!"}`（HTTP 200 流内） |
| 音频（外链 wav） | 同构推测（`"audio"`） | 🔴 `invalid_input`「输入或附件无效」 |
| 视频（外链 mp4） | 同构推测（`"video"`） | 🔴 `invalid_input`（同上） |

> 文档/音频/视频解析的**正解**是上游 OSS 上传链路（`getstsToken` → OSS PUT → `file_url`）——
> 但"外链 URL + 推测形状被拒"与"必须上传"之间的因果**未拆分**（也可能是形状不对）；
> 要定论需先实现上传链路再对照。登记为 **U-15**。

### 4.6 工具调用（tools）× 判决（✅ 2026-09-24 三发对照实测，U-16）

| 实验 | 请求 | 结果 |
|---|---|---|
| ① tools + 内置搜索开 | OpenAI `tools`（`get_weather`）+ `tool_choice:"auto"` + 抓包原样 feature_config（`auto_search:true`） | `get_weather` **零出现**；模型改用**内置 `web_search`** 自答（真实天气，18.6s 服务端闭环） |
| ② tools + 内置搜索关 | 同上 + `feature_config.auto_search:false` | `function_call` **零出现**；模型正文自述"无法使用指定工具"，改用其他方式作答 |

**判决**：上游 `chat/completions` **不支持 OpenAI 风格的客户端函数调用** —— `tools`/`tool_choice`
被**静默忽略**（不报错、不产生 `tool_calls`、连错误事件都没有）。**别再试"透传 tools 给上游"这条路。**

但上游有**内置服务端工具**（`feature_config.auto_search` 触发的 `web_search` 等；MCP 生态见
`GET /api/models` 的 `info.meta.mcp`：image-generation / code-interpreter / amap / fire-crawl），
**服务端闭环**——模型自己调用、自己执行、自己消费结果，SSE 有结构化事件：

```jsonc
data: {"choices":[{"delta":{"role":"assistant","content":"","phase":"web_search","status":"typing",
       "function_call":{"name":"web_search","arguments":"{\"queries\": …"},
       "function_id":"call_244e3bb85b3043eb83e21348","extra":{"display_position":"think"}}}]}
```

判读：`phase:"web_search"` 事件的 `content` 恒空 ⇒ 适配层只提取 content 就**天然不受工具事件污染**；
最终 `answer` 是工具结果的总结（回答含真实天气数据）。探针原文：
`var/probe/20260924_034322_tools/`、`var/probe/20260924_034525_tools_no_search/`。

---

## 5. 创建响应（视频，`stream:false` ⇒ 同步返回 task_id）

```json
{"success": true, "request_id": "…",
 "data": {"chat_id": "…", "parent_id": "…", "message_id": "…",
          "messages": [{"role": "assistant", "content": "",
                        "extra": {"wanx": {"task_id": "e6b0a76d-…"}},
                        "done": false, "size": "16:9"}]}}
```

🔴 **task_id 路径 = `data.messages[0].extra.wanx.task_id`**（✅ 实测；与前端
`msg?.extra?.wanx?.task_id` 读法逐字一致）。缺这个路径 ⇒ 必须响亮失败，**不得猜别的字段**。

`success: false` 时 `data = {"code": "Bad_Request" | "Not_Found" | "Unauthorized" | …, "details": "…"}`。

---

## 6. 查询与状态

### 6.1 HTTP 恒 200，真码在响应头

| 形态 | HTTP | `x-actual-status-code` | body |
|---|---|---|---|
| 成功 | 200 | `200` | `{"success":true,"data":{…}}` |
| 任务不存在 | 200 | `404` | `{"success":false,"data":{"code":"Not_Found","details":"Task not found"}}` |
| 未鉴权 | 200 | `401` | `{"success":false,"data":{"code":"Unauthorized","details":"…"}}` |

### 6.2 成功态 `data`

```json
{"chat_type": "i2v", "task_status": "success", "message": "",
 "content": "https://cdn.qwenlm.ai/output/<uid>/i2v/<chat>/<task_id>.mp4?key=<签名JWT>",
 "remaining_time": "", "sub_chat_type": null, "duration": null}
```

`content` **仅成功时有值**；带签名 `key` 的 URL 直接可下载（✅ 实测多次）。`duration` **恒为 null**
（上游不回传时长，不要用它）。

### 6.3 状态枚举与映射

| 上游 `task_status` | → 本服务六态 | 证据 |
|---|---|---|
| `running` | `running` | ✅（提交后立即 running，**没有 queued**） |
| `success` | `succeeded`（同时给 `content.video_url`） | ✅ |
| 其它任何值 | `failed` | 📖（前端 `else → handleTaskError`） |

🔴 **未知取值一律 `failed`**，禁止默认成"还在跑"（否则上游改枚举时会静默挂死）。
上游没有 `queued` / `expired` / `cancelled` 三态；`expired` 由本服务看门狗按 `TASK_TIMEOUT` 产生。

### 6.4 轮询节奏（📖 前端 bundle + ✅ 实测）

`i2v` 3s / 其它 10s；单次 setTimeout 递归；网络异常重试上限 i2v 5 次 / 其它 10 次；
**上游没有总超时** ⇒ 由本服务 `TASK_TIMEOUT`（默认 900s）兜底。

**出片耗时实测波动大**：约 93s ～ 343s（同一天内：i2v 93s / t2v 343s；09-17 记录为 105s）
⇒ **不要用固定耗时做超时假设**；本服务默认 900s 留足余量。
（t2t 实测 5.8s ～ 22.7s 出全量回复，量级完全不同 —— chat 门是同步链路，不落任务表。）

---

## 7. 产物

| 维度 | 实测值 | 说明 |
|---|---|---|
| 时长 | **5.042s**（n≥7，`mvhd` 全为 `timescale=1000, duration=5042`） | **上游固定，不可指定** |
| 分辨率 / 比例 | 1920×1080（16:9）/ 1440×1440（1:1）/ … | 比例由 `size` 决定；**像素不可指定** |
| 文件大小 | 3.2 – 14.3 MB | — |
| URL 有效期 | ⚠️ 未证实（签名 JWT；`resource_chat_id` 为 null） | 决定是否需要转存 |

### 7.1 真实出片实录（2026-09-22，经本服务全链路）

| 任务 | 形态 | 账号 | 耗时 | 产物 | 时长 |
|---|---|---|---|---|---|
| `cgt-20260922012530-tyoa9` | t2v（16:9） | `2xx***@…` | ≈343s | 5.50 MB | **5.042s** |
| `cgt-20260922013158-ae0tq` | i2v（16:9，上游样例图作首帧） | `yek***@…` | ≈93s | 9.59 MB | **5.042s** |

- 两条链路的 upstream task id 已归档在**本层任务记录**里（不进对外响应，也不写进文档）。
- **账号轮换实证**：两条产物 URL 里的 `resource_user_id` **互不相同**
  ⇒ 两次生成落在两个不同账号，各消耗 1/3 日额度。
- 全链路零 RGV587、零告警；signin → `chats/new` → `completions` → `task/status` 全部 200。

### 7.2 chat 门真实一发实录（2026-09-24，经本服务 + 直连探针）

| 请求 | 结果 |
|---|---|
| t2t 非流式「用一句话介绍你自己」 | ✅ HTTP 200，58 字，22.7s（首次因缺顶层 `chat_id` 被 400，修正后通过 —— 证词进 §4.4） |
| t2t 流式「只回答两个字：你好」 | ✅ SSE 全量提取成功（宽容解析器首选路径命中） |
| 图片解析（上游域内样例图）经服务 | ✅ 准确描述潜水员/沉船场景；usage 1826/678/2504 透传 |
| 文档/音频/视频解析（外链） | 🔴 流内错误事件（见 §4.5 矩阵 → U-15） |

---

## 8. 与目标契约（方舟 Seedance）的差异核对

| # | 方舟契约 | 本上游 | 本层处置 |
|---|---|---|---|
| D-1 | `duration` 2–12…/`-1` 自选 | **不存在该参数，固定 ~5s** | 允许集 = {5}；>5 向下吸附 + 告警；<5 **400**；`-1` 替换成 5 + 告警 |
| D-2 | `ratio` 支持 `adaptive` | 只吃 5 个枚举值 | 🔴 枚举外（含 `adaptive` / 缺失 / 写错）**一律落 1:1 + 告警**（用户 2026-09-17 冻结口径，刻意偏离"缺省 16:9"） |
| D-3 | `resolution` 1080p/720p/4k | 不可指定 | 进 `degradations`；**不回填** |
| D-4/D-5/D-6 | `seed` / `watermark` / `generate_audio` | 不支持 | `degradations`（`seed=-1` 视为"没给"，不报） |
| D-7 | `camera_fixed` / `frames` / `service_tier` / `draft` / `return_last_frame` | 不支持 | `degradations`（`return_last_frame` 另注明"连续拼接链路会断"） |
| D-8 | `content[]` 六类 | 只吃 `text` +（i2v）**一张首帧图** | 见 `app/ark.py` 降维矩阵：last_frame / reference_image / video_url / audio_url / draft_task 一律 **400** |
| D-9 | `DELETE` 取消 | **无取消端点** | 本服务不实现 DELETE（路由不存在；未终态也不会伪造 `cancelled`） |
| D-10 | 创建响应 `{id}` + `cgt-` 前缀 | 上游给裸 UUID | **本地任务 id 由本服务生成**（`cgt-…`），上游 id 只存本层记录 |
| D-11 | 查询无参数（凭证即身份） | **查询带路径参数** | 任务归属校验落在本层（`credential_id` 指纹；不符本地 404） |
| D-12 | 回调 | 上游无 webhook | 本服务 v1 不实现回调（`callback_url` 进 `degradations`） |
| D-13 | `execution_expires_after` | 无 | 由本服务 `TASK_TIMEOUT` 兜 |
| D-14 | `model` = provider/model | **上游没有"视频模型名"**：能力由 `chat_type` 决定 | 视频门：`model` 段只做 provider 校验（必须 `qwen`），**不转发**；chat 门：模型名**转发上游**（`messages[0].models` 与顶层 `model`），清单注册自 `GET /api/models` |

---

## 9. 未证实项（不得在实现里假设）

| # | 项 | 状态 | 说明 |
|---|---|---|---|
| U-1 | 外链图作 i2v 首帧 | 🟡 **部分关闭**（2026-09-22） | **上游域内图（OSS `resources/i2v/…`）✅ 实测出片**；**第三方域名外链仍未验证**（本服务照发 + 告警）。t2t 图片解析同口径（§4.5） |
| U-2 | `task_status` 的失败值形态（`failed`？`failure`？） | ⚠️ 未证实 | 需观测一个真实失败任务 |
| U-3 | `bx-*` 是否任何情况都不必需 | ⚠️ 未证实（当前不发送、连续成功） | 上游收紧时需补。t2t 写端点同样未带 bx-*（09-24 已复证可用） |
| U-4 | 额度耗尽的**真实错误形态**（视频档） | ⚠️ 未证实 | 打满一个账号的 3 次后观察 |
| U-5 | 产物 URL 的**有效期** | ⚠️ 未证实 | 定时 ping 一个产物 URL |
| U-6 | 额度是"提交即扣"还是"成功才扣" | ⚠️ 未证实 | 对照实验：提交后立刻查额度 |
| U-7 | 登录态 token 直接跑**视频**写端点 | ✅ **关闭（2026-09-22）** | `Cookie: token=<JWT>` 最小凭据跑通 t2v + i2v 真实出片（§7.1） |
| U-8 | 同一 `chat_id` 上并发提交多个视频任务 | 🟡 保守规避 | 当前按账号串行 + 提交最小间隔（实践稳定）；并发未验 |
| U-9 | **guest（匿名访客身份）能否提交视频任务** | ✅ **关闭（2026-09-22）：不支持** | 单发实测：免费段 `chats/new`（`chat_mode=guest` + `chat_type=t2v`）→ **200 受理**；真实一发 t2v 提交 → **`x-actual-status-code: 400` + `code=internal_error`**（无 task_id、未扣额度）。既非额度拒绝（会回 `RateLimited`+额度文案）也非凭据问题（会 401/RGV587）⇒ **guest 门接受会话但拒绝视频生成**。过程见 §9.2 |
| U-10 | **token 的"真实"失效时点** | ⚠️ **未证实**（2026-09-22 登记） | JWT 自称 `exp` = 铸后 **30 天**，但**用户经验是"可能 7 天就失效"**（服务端可提前失效）⇒ 本仓按 **6 天**主动续期（预留 1 天）+ 401 当场重铸兜底。要定论需**长跑打点**：同一 token 定时发只读请求，记录首次 401 的时点（≥7 天） |
| U-11 | **`task_id` 随机段的熵够不够**（免 Key 读的安全前提） | ⚠️ **待收口**（2026-09-22 登记） | 2026-09-22 起 `GET /tasks/{id}` **不强制 Key**（`id` 即凭据，同 `../jimeng`）。前提是 id **不可猜**：jimeng 用 128 bit 随机，而本仓沿用方舟格式 `cgt-<UTC 秒>-<5 位随机>` ≈ **29.8 bit**（36^5≈6.05e7/秒窗）⇒ **待办：随机段加长到 ≥16 位**（客户把 id 当不透明串用，加长不破坏契约）。当前部署只绑回环，**暂无暴露面** |
| U-12 | **上游 t2t 的流式响应事件形态** | ✅ **关闭（2026-09-24）** | 单发探针拿全原始 SSE（§4.5）：正文在 `delta.content`（`phase:"answer"`）、thinking 摘要 content 恒空、**结束判据 = `status:"finished"` ∧ `phase:"answer"`**（thinking 的 finished 也带 status —— 判宽即截断，实测踩中）、无 `[DONE]`、流内 error 事件两种形态、usage 真实存在且递增 |
| U-13 | **`chats/new` 的 `chat_type`/`models` 参数对 t2t 会话的影响** | ✅ **关闭（2026-09-24）** | `chats/new(chat_type="t2t", models=[模型])` + `chat_id` 双写提交体，真实一发成功（§7.2）；🔴 **缺小写 `chat_id` ⇒ 400 `Field 'chat_id': Field required`**（唯一踩中的坑，证词进 §4.4） |
| U-14 | **上游对 t2t 的频控/风控形态** | ⚠️ 未证实（2026-09-24 登记） | 当日 7 连发真实写请求（文本×3 + 图片×2 + 音频/视频探针）**零频控、零风控、零额度计数**；正式频控形态仍需长跑观测。chat 门仍守同账号提交间隔（写端点突发纪律） |
| U-15 | **文档/音频/视频解析（files 附件）** | ✅ **关闭（2026-09-24 晚）：上传链打通，三类全通** | 根因确证：**必须走上游 OSS 上传链**（外链 URL 任何形状都被拒）。上传链 = `getstsToken{"file_name","file_size","file_type"}` → 预签名 URL **不可用**（SignatureDoesNotMatch，7 种头组合全败）→ **改用 STS 凭证自签 OSS V1**（HMAC-SHA1，`x-oss-security-token` 头）PUT 成功。files 条目形状：文档 `type/file_class:"file"`、音频 `"audio"`、视频 `"video"`（§4.7）。经服务实测：PDF 答出标题（Attention Is All You Need）、音频听出蜂鸣声、视频描述出森林洞穴场景 |
| U-16 | **OpenAI 风格客户端函数调用（`tools`/`tool_choice`）** | ✅ **关闭（2026-09-24）：不支持** | 三发对照（§4.6）：带 tools 被**静默忽略**（`get_weather` 零痕迹）；关内置搜索后模型自述"无法使用指定工具"（`function_call` 零出现）。**内置服务端工具**（`auto_search` 的 `web_search` 等）不受影响、照常闭环（SSE 有 `phase:"web_search"` + `function_call` 结构化事件，content 恒空）。chat 门对 `tools`/`tool_choice` 忽略 + 降级说明（行为与上游一致，文档登记证据） |
| U-18 | **t2t 流式形态：thinking 只有状态信号、answer 真增量** | ✅ **关闭（2026-09-24 二次实测，phase 计数级）** | 上游 SSE `delta.phase` 两段式：**`thinking_summary`**（typing 4 + finished 1，**content 恒空 0 字符**——纯状态信号，思考文本不下发；⇒ `delta.reasoning_content` **无源可透传**，DeepSeek/Doubao 式思考流不存在）→ **`answer`**（typing 92 + finished 1，502 字——**真·增量流式**；此前"批量化"观察是短答案单 chunk 特例）。非流式 message 同样无 reasoning_content。门不编造 reasoning（不编造纪律）；TTFT ≈ thinking+首 token 时长（实测 15s，max 慢于 plus），与门无关 |
| U-17 | **t2t 最大上下文（实测边界）** | 🟡 **部分关闭（2026-09-24）** | 上游自报 `max_context_length=1,000,000` tokens，但经网页端接口实测：**≈5 万汉字（usage 39,668 prompt tokens）可靠工作**（首尾密钥双命中，无截断）；**≥6.2 万汉字触发 x5sec 风控**（秒拒、90s 冷却重试无效）。🔴 **换出口/轮换代理不可绕过**（两个全新 IP 同样秒拒 —— WAF 请求体大小规则，与 §2.4"与出口无关"结论一致）。比率实测 ≈1.3 字/token（usage 口径），上游每请求固定注入 ~1.4-2.8K tokens。实用建议：单次 prompt ≤5 万汉字；更长走分段或方舟回退通道（Doubao 256K 未实测） |

### 4.7 附件上传链（✅ 2026-09-24 晚实测，U-15 关闭）

```
POST /api/v2/files/getstsToken   body: {"file_name", "file_size", "file_type"}
                                 → data{access_key_id, access_key_secret, security_token,
                                        bucketname, endpoint, file_path, file_url(预签名), file_id, region}
PUT  https://{bucket}.{endpoint}/{file_path}   ← OSS V1 签名（Authorization: OSS ak:sig）
```

实测要点（实现 `app/upstream/qwen/upload.py`）：
- 🔴 **`file_type` 必填**：缺失 ⇒ `Bad_Request "Invalid file information!"`；
- 🔴 **预签名 `file_url` 不可用**：直接 PUT 一律 `SignatureDoesNotMatch`（VH/path × 7 种头组合全败，
  含浏览器头）——用响应里的 **STS 凭证自签 OSS V1**（StringToSign = `PUT\n\n{ct}\n{date}\n
  x-oss-security-token:{tok}\n/{bucket}/{path}`，HMAC-SHA1）即 200；
- PUT 的 **Content-Type 必须与 getstsToken 一致**（签名含 content-type）；
- `file_url` 签名仅 **300s** ⇒ 上传后立即用规范 URL（无签名）进 `files[]`，勿缓存；
- files 条目形状（type 与 file_class 同值）：图片 `image/vision`（§4.2）、文档 **`file/file`**、
  音频 `audio/audio`、视频 `video/video` —— 逐类实测。

| 附件 | 实测结果（经本服务） |
|---|---|
| PDF 2.2MB（arxiv attention） | ✅ 模型答出标题 **"Attention Is All You Need"** |
| wav 1.5s（440Hz 蜂鸣） | ✅ "持续的电子蜂鸣声，像警报或提示音" |
| mp4 0.99MB（Big Buck Bunny） | ✅ "阳光明媚的森林中，大树下长满青草的洞穴入口" |

### 9.1 guest 通路的事实边界（2026-09-22 盘点，来源：既有取证，非新实验）

| 事实 | 依据 |
|---|---|
| guest = **设备指纹身份**（cookie 无 `token`；`bx-ua` / `bx-umidtoken` / `ssxmod_itna` 等），由真浏览器铸造 | `qwen-chat-api.md` §2.6 |
| guest 的**图片/文本**写端点已验证可用（4 鉴权 × 5 形态矩阵中 guest **5/5**，含 2.0/3.0-pro/16:9 与 t2t） | 同上 §2.13 |
| guest **读不到额度视图**（`entitlement_quota` → `x-actual-status-code: 401`）；额度墙文案为「今日**生图**额度已用完，登录后可继续生图。」 | 同上 §2.6/§2.7 |
| guest 额度**绑设备身份**（与出口 IP 无关），单身份约 4~5 张/天，且额度数额随模型不同 | 同上 §2.12 |
| **完全未登录（anon）不可用**：3/3 在 `chats/new` 即 401 ⇒ 匿名必须走 guest 身份池 | 同上 §2.13 |
| 视频写端点要求 `token` **cookie**（仅 `Authorization: Bearer` 会落 x5sec 惩罚流）；视频查询端点无凭据 → 401 | `qwen-async-task-api.md` §2.3 / `qwen-chat-api.md` §2.13 |
| 视频额度项 `t2v`（3/天，t2v+i2v 共用）**只出现在登录态**的额度视图里 | `qwen-quota-api.md` §3 |
| 上游 guest 通路属「抓一次包用一阵」形态（`ssxmod_itna` 无生成器），**不宜作无人值守生产凭据** | `qwen-chat-api.md` §2.6 |

**结论（供决策，不是实现依据）**：guest 能到达**同一个**写端点（图片已证），故"能不能发出 t2v 请求"机械上大概率可以；
但 guest 档位的产品语义是**生图**（额度文案与不可读视图都指向此），视频额度大概率**不在 guest 档位** ⇒
预期结果是同类 `RateLimited` 拒绝。**要定论必须实测**，路径见 §9.2。

### 9.2 guest × 视频：判定实验与结果（2026-09-22 已执行，**单发**）

探针：`scripts/probe_guest_video.py`（单发写死在代码里：不重试、不换身份、不打印凭据；须显式 `--confirm` 才真发）。
身份：用 `reverse-proxy/qwen/tools/make_identities.py 1` **现铸一条全新 guest 身份**
（Playwright + 系统 Chrome，7.4s，`bx-ua` 版本 `234!` 与当前 fireye 对齐）—— 用新身份是为了**排除"身份过期"这个混淆项**。

| 步骤 | 请求 | 结果 |
|---|---|---|
| ① 零成本 · UI 取证 | Playwright 载入 `/c/guest` | 🔴 **直接跳转 `https://chat.qwen.ai/auth`**（登录/注册页）；页面无「视频」「图像生成」等任何生成入口 ⇒ 访客 **UI 入口已不存在**（截图：`var/probe/20260922_guest_redirect_to_auth.png`） |
| ② 免费段 · 会话取证 | `POST /api/v2/chats/new`（`chat_mode=guest`、`chat_type=t2v`） | ✅ **200 + `success=true`**，回 `chat_id` ⇒ 身份有效、指纹通过、无 WAF/RGV587；**API 层的 guest 门接受 t2v 会话** |
| ③ **真实一发** | `POST /api/v2/chat/completions?chat_id=…`（`stream:false`） | 🔴 **`x-actual-status-code: 400`**、`success=false`、`code=internal_error`、`details=Internal Error`；**无 task_id** |

**判决**：guest × 视频 = **不支持**。三项证据互不冲突：API 门收会话（②）但拒生成（③），UI 层则干脆把访客入口撤了（①）。

**边界与注意**：
- 🔴 这不是"额度不够"：额度拒绝的形态是 `code=RateLimited` + 「今日…额度已用完」文案（`qwen-chat-api.md` §2.12）；
  也不是"参数写错"：缺 `version` 头的形态是 `Bad_Request`（`qwen-async-task-api.md` §7.3），而本次该头已带。
  `internal_error` 是上游在"这条路走不通"时给的**无信息量错误码**（同类已知用法：`3.0-pro` 传超大 `size` 也回它；
  09-24 外链 PDF 也是它）。
- **未扣额度**：无 task_id、无产物，`t2v` 计数不变；单发即停（遵守 `R-2`：写端点勿连打）。
- ⚠️ **未验证（不得推断）**：**图片侧的 guest 通路今天是否仍可用**。历史实证是 09-18/19（矩阵 5/5、批量出图工具），
  而本次 ① 显示访客 UI 已被重定向到登录页 ⇒ 存在"guest 通路整体收紧"的可能。
  要定论只需**一发 t2i**（会消耗一条 guest 身份 1 张图额度）；在那之前，**不得**把"guest 出图仍可用"当现状。
- 本服务**不受影响**：本服务凭据恒为账号 token（`chat_mode="normal"`），与 guest 门无关（§2.1）。



---

## 10. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-09-22 | 首次成文：整合 `reverse-proxy` / `video-adapter` 既有取证 + 用户当日抓包（i2v 完整头版）；确立"token 最小凭据 + 完整头 + 三处同标 + `wanx.task_id` 路径"四条实现依据 |
| 2026-09-22 | **真实链路首测（经本服务）**：signin ✅（token_len=209）/ t2v ✅ / i2v ✅，两条产物下载核验（均 5.042s）；U-7 关闭、U-1 部分关闭；补 §6.4 耗时波动观测与 §7.1 出片实录 |
| 2026-09-22 | 登记 **U-9（guest × 视频）** 并补 §9.1 事实边界 / §9.2 分级判定实验：guest 在图片面已证可用、视频面**零证据**；澄清"本服务无 guest 通路"（凭据恒为账号 token、`chat_mode` 固定 `normal`） |
| 2026-09-22 | **U-9 单发实测关闭**：现铸全新 guest 身份 → `chats/new(guest,t2v)` 200 / 真实一发 t2v 提交 **400 `internal_error`**（未扣额度）⇒ **guest 不支持视频**；同时观测到 `/c/guest` **已重定向 `/auth`**（访客 UI 入口消失）。新增探针 `scripts/probe_guest_video.py`（单发/不重试/不打印凭据）。⚠️ 登记新未决：**图片侧 guest 通路今日是否仍可用**（需一发 t2i 才能定论） |
| 2026-09-22 | 补 **§2.5 出口拓扑**（代码核对）：使用侧**零代理、单出口、零跳转**（所有账号共用宿主机 IP）；铸造侧走 2088 **每连接换 IP**，且一次铸造开两条连接（预热/登录**可能换 IP**，要稳定换 2089）；🔴 记 `QWEN_TRUST_ENV=1` 的危险（会静默把使用侧变成经环境代理）；部署前须验宿主出口（405 判据） |
| 2026-09-22 | 补 **§2.6 鉴权形态 × 视频矩阵**（一手实测，4×2 格）：`cookie` ✅ 出片（含归属/产物/凭据三道自证）、`bearer` 🔴 RGV587、`guest` 🔴 `internal_error`、`anon` 🔴 `401`；**结论：不登录（含访客身份）都出不了视频**。同时固化"**免费段不能当鉴权判据**"（三形态在 `chats/new` 全 200，差异只在写端点）。新增探针 `scripts/probe_auth_forms_video.py`（两段式、单号冷却、命中风控即停账号写）。§3 的 `times_left` 行补 09-22 复证 |
| 2026-09-22 | **token 续期两层落地 + 铸造出口改 HTTP**（用户决策）：① 主动续期 `min(exp − 提前量, 铸后 QWEN_TOKEN_TTL)`，**默认 6 天**（给"疑似 7 天失效"预留 1 天；`<=0` 归一 6 天，**不提供关掉上限的开关**）；② 被动兜底 = 401 ⇒ 清缓存 → 立即重铸 → **原请求重试一次**（写端点也重试：未受理、不重复计费）。`mint_token` **删除 SOCKS 分支**（不做兼容，少 ~130 行自研 socket/TLS/HTTP 解析），改用 `QWEN_SIGNIN_PROXY`（HTTP 代理）；实测池语义：**每连接换 IP + 同连接复用同 IP** ⇒ 每次铸造换 IP、一次铸造全程一个 IP（顺带修掉旧实现"一次铸造两条连接可能换 IP"的瑕疵）。新增 `tests/test_signin.py` + 续期用例 ⇒ 当轮全量 **115 项**。**登记 U-10**（token 真实失效时点，未证实） |
| 2026-09-22 | **env 三向一致落地**（用户：「同步 env」）：`.env.example` 重建为**全量登记**（37 键，含每个键的代码默认与坑），修正两处**已过期**注释（TTL 还写着"默认 1 天 / 0=不设上限"、出口还写着"两种都收"），补登 3 个此前完全没登记的键（`POLL_INTERVAL` / `WORKERS` / `GUNICORN_TIMEOUT`）；`.env` 按模板结构重建（8 显式 + 29 注释态，600 保留）并写下**机器可核的刻意差异清单**。新增门禁：模板侧 `tests/test_env_contract.py`（6 项，含"注释态值必须等于代码默认"与"差异登记不得过期"），生效文件侧 `scripts/env_sync_check.py`（`.env` 不入库 ⇒ 不能写成会跳过的测试）。全量 **128 项** |
| 2026-09-24 | **chat（t2t）门落地**（用户：「chat 任务 适配 openai」「注册v1/models」「只做chat任务」）：① 登记 **§4.4 t2t 提交体**（当日用户抓包逐字：`stream:true`、无 `size`、thinking 开含 `thinking_format`）；② 实测 **`GET /api/models` 免鉴权**（无任何 cookie 即 200）并注册进 `/v1/models`（TTL 缓存 + 失败回退）；③ **零真实生成请求**（适配纪律），chat 门以 dry-run + 假上游全链路测试覆盖。⚠️ 新增未证实：U-12 / U-13 / U-14。env +1 键（`MODELS_CACHE_TTL`，模板 38 键） |
| 2026-09-24 | **chat 门真实一发 + 多模态取证扩口**（用户：「测试 另外还有图片解析 文件解析 视频解析 音频解析」）：① **t2t 非流式 + 流式经服务真实成功**（22.7s / 5.8s），**U-12 ✅ 关闭**（§4.5 原始 SSE 形态：正文 `delta.content`、thinking 摘要 content 恒空、**结束判据必须 `status ∧ phase:"answer"`**——thinking 的 finished 也带 status，判宽即截断（实测踩中）、无 `[DONE]`、流内 error 事件两种形态、**usage 真实存在**（input/output/total_tokens 随 answer 事件递增）⇒ chat 门改为真实透传）；② **U-13 ✅ 关闭**：`chats/new(t2t, 模型)` 可用，🔴 **顶层小写 `chat_id` 必填**（缺 ⇒ `RequestValidationError: Field 'chat_id': Field required`）——据实订正 §4.4（初版误读抓包，被一发 400 当场打回）；③ **图片解析 ✅**（files §4.2 形状进 t2t，上游域内图真实看图作答，经服务全链路 200）；④ 文件/音频/视频（外链）🔴 全拒（`Internal error!` / `invalid_input`）⇒ 登记 **U-15**，chat 门对三类**明确 400** + 证据；⑤ 新增探针 `scripts/probe_chat_parse.py`（text/image/document/audio/video 五格单发取证，原文落档 `var/probe/`）。当日 7 连发零频控（U-14 观察）。测试 165 项全绿 |
| 2026-09-24 | **工具调用判决**（用户：「是否支持工具调用」）：三发对照实测（① tools+内置搜索开 / ② tools+关搜索）⇒ 上游**不支持** OpenAI 风格函数调用（`tools`/`tool_choice` 静默忽略，`get_weather` 零痕迹、`function_call` 零出现，**U-16 ✅ 关闭**，§4.6）；**内置服务端工具**（`auto_search` 的 `web_search`）照常服务端闭环，SSE 有 `phase:"web_search"` + `function_call`/`function_id` 结构化事件（content 恒空 ⇒ 适配层天然不提取）。chat 门 `tools` 行为维持"忽略 + 降级说明"（与上游一致）。探针 +`tools`/`tools_no_search` 对照格 |
| 2026-09-24 | **附件上传链落地**（用户：「走他的上传哇」）：`app/upstream/qwen/upload.py` 实现上传链（getstsToken → **OSS V1 签名 PUT**——预签名 URL 7 种头组合全败，改自签即通）；chat 门 file/audio/video 分段 + data: 图片/音视频 ⇒ **服务端代下载/解码 → 上传 → files[] 条目 → qwen 原生解析**（SSRF 防护 + 20MB 上限，`QWEN_UPLOAD_ENABLED`/`QWEN_UPLOAD_MAX_BYTES`，模板 45 键）。经服务实测 PDF/音频/视频全通（U-15 ✅ 关闭）。新增 12 项测试 |
| 2026-09-24 | **t2t 最大上下文实测（用户：「qwen最大上下文长度是多少帮我测试下」「加代理可以绕过吗」）**：校准比率 ≈1.3 字/token（usage 口径），上游每请求固定注入 ~1.4-2.8K tokens；**密钥首尾双命中法**阶梯探测 ⇒ **≈5 万汉字（usage 39,668 tokens）可靠工作、≥6.2 万汉字触发 x5sec 风控**（秒拒、90s 冷却重试无效）；🔴 **轮换代理两个全新出口 IP 同样秒拒 ⇒ WAF 请求体大小规则、换出口不可绕过**（与 §2.4 结论一致）。**登记 U-17**；`/v1/models` chat 条目 notes 加实测 caveat。实用建议：单次 prompt ≤5 万汉字，更长走分段或方舟回退通道 |
