"""FastAPI 应用工厂 —— 路由 / 错误信封 / 鉴权 / 健康检查 一处收拢。

对外路由（两扇门，互不相通）：
    POST /api/v3/contents/generations/tasks       视频创建（方舟契约，只回 {"id": …}）
    GET  /api/v3/contents/generations/tasks/{id}  视频查询（方舟任务对象形状；顺带推进任务）
    POST /v1/chat/completions                     **chat 门（OpenAI 形态，t2t + 图片解析）**——2026-09-24 新增
    GET  /v1/models                               能力清单（OpenAI 形态 = 注册的上游 chat 模型 + 视频条目）
    GET  /healthz /readyz /stats                  运维面（不含任何凭据原文）

范围冻结（方舟门）：列表与取消刻意不实现 ⇒ 路由不存在、不返回假数据。
chat 门（OpenAI）：**只做 t2t 文本 + 单张图片解析**；创建写端点 ⇒ **必须带 Key**；
本门的错误体用 OpenAI 词汇表（与方舟门的 Ark 错误码同形不同义，见 `openai_chat.openai_error_body`）。
**能力回退通道**（`app/ark_fallback.py`，2026-09-24）：请求点了 qwen 给不了的能力
（tools / 文件·音频·视频分段 / 多图 / data: 图片）⇒ 整单转方舟 chat 应答（须配置 ARK_FALLBACK_*）。

调试面：`X-Avm-Dry-Run: 1` 请求头（两扇门都支持）⇒ 跑完整翻译后返回"将要发出的请求"，
**零上游调用、零落库**。
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

from . import __version__, ark_fallback, llms_txt, models, openai_chat, openai_responses
from .config import Settings
from .coordinator import Coordinator
from .errors import (
    AdapterError,
    AuthenticationError,
    InvalidParameterError,
    UpstreamError,
)
from .service import QwenVideoService
from .store import TaskStore
from .upstream.qwen.accounts import AccountPool, jwt_payload
from .upstream.qwen.client import QwenClient

logger = logging.getLogger("qwen.main")

#: 账号池状态的 KV 键（额度计数 + 冷却；**不含 token**）
POOL_STATE_KEY = "state:pool"


def fingerprint(secret: str, key: str) -> str:
    """API Key → 指纹（HMAC-SHA256，永不落明文；裸 sha256 对低熵 Key 不够）。"""
    return "hmac-sha256:" + hmac.new(secret.encode(), key.encode(), hashlib.sha256).hexdigest()


def bind_pool_state(store: TaskStore, pool: AccountPool) -> None:
    """账号池的**耐久化**：启动时恢复、变更即落 KV ⇒ 重启不丢额度计数与冷却。

    token 刻意**不进持久层**（重启重新铸造，免费；避免凭据落盘）。
    """
    raw = store.kv_get(POOL_STATE_KEY)
    if raw:
        try:
            pool.restore(json.loads(raw))
        except ValueError:
            logger.warning("账号池状态恢复失败（忽略，按空状态起）")
    pool.on_change = lambda: store.kv_set(
        POOL_STATE_KEY, json.dumps(pool.snapshot(), ensure_ascii=False))


def create_app(settings: Settings | None = None, *, store: TaskStore | None = None,
               pool: AccountPool | None = None, client: QwenClient | None = None,
               service: QwenVideoService | None = None,
               ark_transport: httpx.BaseTransport | None = None,
               attachment_resolver=None) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    store = store or TaskStore(settings.task_db)
    pool = pool or AccountPool(settings)
    client = client or QwenClient(settings)
    service = service or QwenVideoService(settings, store, pool, client,
                                          attachment_resolver=attachment_resolver)
    registry = models.ChatModelRegistry(client.list_upstream_models,
                                        ttl=settings.models_cache_ttl)
    secret = settings.resolved_key_secret()
    bind_pool_state(store, pool)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        background: asyncio.Task | None = None
        if settings.coordinator_enabled:
            background = asyncio.create_task(
                Coordinator(service, store, settings).run(), name="qwen-coordinator")
        try:
            yield
        finally:
            if background is not None:
                background.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await background
            client.close()

    app = FastAPI(title="qwen-service", version=__version__, lifespan=lifespan)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response

    @app.exception_handler(AdapterError)
    async def adapter_error_handler(request: Request, exc: AdapterError):
        request_id = getattr(request.state, "request_id", "")
        headers: dict[str, str] = {}
        if exc.retry_after:
            headers["Retry-After"] = str(int(max(1.0, exc.retry_after)))
        return JSONResponse(status_code=exc.status_code,
                            content=exc.to_body(request_id), headers=headers)

    def _key_accepted(key: str) -> bool:
        """三种形态的调用方凭据（2026-10-01 用户口径）：

          ① **静态 Key**：`API_KEYS` 白名单逐字比对（`sk-qwen-…` 等）；
          ② **账号串** `<email>|<password>`：同上（它就是白名单里的一条不透明字符串）；
          ③ **JWT 门卡**：调用方拿自己的 qwen access token 当 Key —— 形状 + `type`
             = `access_token` + `exp` 未过期即认（**不验签**：签名校验只有上游能做，
             这里只做"是不是一份活的 access token"的形状闸门）。透传账号语义：
             门卡证明"你有一个活的 qwen 登录态"，上游凭据仍由账号池托管。

        JWT 门卡的归属注意：access token 15 分钟一换，换新后指纹随之改变 ⇒
        用 JWT 建的任务，**旧 JWT 过期后不可再读**（要持久归属请用 ①/②）。
        """
        if any(secrets.compare_digest(key, known) for known in settings.api_keys):
            return True
        payload = jwt_payload(key)
        if payload is None:
            return False
        try:
            if str(payload.get("type") or "") != "access_token":
                return False
            return float(payload.get("exp") or 0) > time.time()
        except (TypeError, ValueError):
            return False

    def credential_id_of(request: Request) -> str:
        if not settings.api_keys:
            # 未配置 API_KEYS ⇒ 鉴权关闭（仅限内网；接入文档已显式声明）
            return fingerprint(secret, "open")
        authorization = request.headers.get("authorization", "")
        if not authorization.lower().startswith("bearer "):
            raise AuthenticationError("缺少 Authorization: Bearer <API Key>")
        key = authorization[7:].strip()
        if not _key_accepted(key):
            raise AuthenticationError("API Key 无效")
        return fingerprint(secret, key)

    def credential_id_optional(request: Request) -> str | None:
        """**可选的**调用方 Key —— 只给「按 id 即凭据」的**读单条任务**用。

        三种情况分得很清（刻意不合并，与 `../jimeng` 同口径）：

        · **完全没带** `Authorization` ⇒ 返回 `None`，**放行**。
          理由：`task_id` 只在受理时返回给带 Key 的调用方，调用方可以把结果链接直接分享出去；
        · **带了但无效**（不在白名单）⇒ **照旧 401** —— 不能因为"反正放行"就把错的 Key
          蒙过去，那会让调用方的配置错误被静默吞掉（最难查的一类问题）；
        · **带了且有效、但不是该任务的属主** ⇒ 返回该指纹，由 `service.get` 判 404。
          ⚠️ 这里比 `jimeng` **严一档**：jimeng 对"非属主"与"没带"同一待遇（都放行），
          本服务保留**跨 Key 读 ⇒ 404**（ADR-003 口径）—— 不冲突：调用方要么不带 Key，
          要么用原 Key 读；而"拿着甲 Key 去探乙 Key 的任务"仍然读不到。
        """
        authorization = request.headers.get("authorization", "").strip()
        if not authorization:
            return None
        return credential_id_of(request)

    # ------------------------------------------------------------------ 方舟门（视频）

    @app.post("/api/v3/contents/generations/tasks")
    async def create_generation_task(request: Request):
        credential_id = credential_id_of(request)
        try:
            body = await request.json()
        except ValueError as exc:
            raise InvalidParameterError("请求体不是合法 JSON") from exc
        if not isinstance(body, dict):
            raise InvalidParameterError("请求体必须是 JSON 对象")
        dry_run = request.headers.get("x-avm-dry-run", "").strip().lower() in ("1", "true", "yes")
        result = await asyncio.to_thread(service.create, body, credential_id, dry_run=dry_run)
        return JSONResponse(status_code=200, content=result)

    @app.get("/api/v3/contents/generations/tasks/{task_id}")
    async def get_generation_task(task_id: str, request: Request):
        """查询任务 —— **不强制 Key：`task_id` 本身就是凭据**（方舟语义，同 `../jimeng`）。

        带了 Key 才按归属过滤（不匹配 ⇒ 404，防跨 Key 探测）；完全没带 ⇒ 直接按 id 读。
        """
        credential_id = credential_id_optional(request)
        result = await asyncio.to_thread(service.get, task_id, credential_id)
        return JSONResponse(status_code=200, content=result)

    # ------------------------------------------------------------------ chat 门（OpenAI）

    def _sse_dump(obj: dict) -> str:
        return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

    async def _chat_streaming_response(req: openai_chat.ChatRequest, request_id: str,
                                       meta: dict) -> StreamingResponse:
        """OpenAI SSE 翻译：上游增量文本 → `chat.completion.chunk` 事件流。

        🔴 预检窗口 3s：先等上游首增量 3s——来了 ⇒ 失败（拒绝/风控/零输出）仍回正经
        HTTP 错误；没来 ⇒ 先发头 + role/心跳 chunk（思考期不干等），首个增量改在流内
        等待（届时失败只能按流内 error 事件收尾——上游客已接受，此路径罕见）。
        `usage`（上游真值，经 meta 回传）随末片出现（加性，OpenAI 惯例 include_usage）。
        """
        completion_id = openai_chat.new_completion_id()
        created = int(time.time())
        gen = service.chat_stream(req, meta=meta)
        first_task = asyncio.ensure_future(asyncio.to_thread(next, gen, (None, None)))
        done_set, _ = await asyncio.wait({first_task}, timeout=3.0)
        # 🔴 用 asyncio.wait（不取消任务）而非 wait_for（超时会 cancel —— 线程结果将
        # 无法回收，流内 await 时抛 CancelledError ⇒ 流静默断掉，2026-09-24 实测踩坑）
        pending_first = not done_set
        first = first_task.result() if done_set else None

        async def sse():
            try:
                yield _sse_dump(openai_chat.chunk_object(
                    completion_id=completion_id, created=created,
                    model=req.model_requested,
                    delta={"role": "assistant", "content": ""},
                    degradations=req.degradations))
                if req.thinking_gear != "fast":
                    # 🔴 思考心跳（用户方案「输出空格变相加速」）：上游 thinking 阶段
                    # 不产出任何可见文本（U-18），客户端会干等 ~6-15s+；先发一个空格
                    # 增量让"首字"即刻到达（连接活性 + 打字态解锁）。
                    # 仅流式；非流式正文不加前导空格。fast 档本身无思考等待，不发。
                    yield _sse_dump(openai_chat.chunk_object(
                        completion_id=completion_id, created=created,
                        model=req.model_requested, delta={"content": " "}))
                ping_interval = getattr(settings, "ping_interval", 15.0)
                if pending_first:
                    # 🔴 预检超时（first=None）：必须**先回收结果再解包**——解包 None 会
                    # TypeError（非 AdapterError → 流静默断，2026-09-24 实测踩坑）。
                    # 等待期也发 ping（心跳空格已先行）。
                    while True:
                        done_set, _ = await asyncio.wait({first_task}, timeout=ping_interval)
                        if done_set:
                            break
                        yield ": ping\n\n"
                    kind, pending = first_task.result()
                else:
                    kind, pending = first
                if pending is None:
                    raise UpstreamError("chat 流未产生任何增量（上游零输出按失败，不报成功）")
                while kind is not None:
                    if kind == "reasoning":
                        # 🔴 思考摘要透传（官网 UI 同款数据 → DeepSeek 风格 reasoning_content）
                        yield _sse_dump(openai_chat.chunk_object(
                            completion_id=completion_id, created=created,
                            model=req.model_requested,
                            delta={"reasoning_content": pending}))
                    else:
                        yield _sse_dump(openai_chat.chunk_object(
                            completion_id=completion_id, created=created,
                            model=req.model_requested, delta={"content": pending}))
                    # 🔴 周期 ping（用户 92 报障的根治）：上游 thinking/生成静默可达 50s+，
                    # 客户端↔nginx 之间全程无字节的静默流会被中间层 idle 掐断（curl 92
                    # HTTP/2 stream not closed cleanly）。每 ping_interval 发一条 SSE
                    # 注释（`: ping`）——注释行对 OpenAI 解析器不可见、不污染正文，
                    # 但让任何中间层都不再看到"静默流"。
                    task = asyncio.ensure_future(asyncio.to_thread(next, gen, (None, None)))
                    while True:
                        done_set, _ = await asyncio.wait({task}, timeout=ping_interval)
                        if done_set:
                            break
                        yield ": ping\n\n"
                    kind, pending = task.result()
                yield _sse_dump(openai_chat.chunk_object(
                    completion_id=completion_id, created=created,
                    model=req.model_requested, delta={}, finish_reason="stop",
                    usage=meta.get("usage")))
                yield "data: [DONE]\n\n"
            except AdapterError as exc:
                logger.warning("chat 流中途失败：%s", exc)
                yield _sse_dump(openai_chat.openai_error_body(exc, request_id))
                yield "data: [DONE]\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # ---------------------------------------------------------- chat 门 · 能力回退通道

    def _ark_dry_run_view(body: dict, reason: str, *, path: str = "/chat/completions") -> dict:
        return {
            "dry_run": True,
            "channel": "ark-fallback",
            "reason": reason,
            "upstream": {
                "method": "POST",
                "url": f"{settings.ark_fallback_base}{path}",
                "headers": {"Authorization": "Bearer <redacted>",
                            "Content-Type": "application/json"},
                # 🔴 回退模型名脱敏：dry-run 也不外泄（见 ark_fallback._sanitize）
                "body": {**body, "model": "<redacted>"},
            },
        }

    async def _fallback_response(body: dict, reason: str, request_id: str,
                                 dry_run: bool):
        """能力回退：整单转方舟 chat（详见 `app/ark_fallback.py` 与 INTERFACE §8.8）。

        回退应答带 `x-qwen-fallback: ark` 响应头（流式/非流式都带）；
        通道故障 ⇒ AdapterError ⇒ OpenAI 词汇错误体（**不静默降回 qwen**）。
        """
        if dry_run:
            return JSONResponse(status_code=200, content=_ark_dry_run_view(body, reason))
        if body.get("stream"):
            gen = await asyncio.to_thread(ark_fallback.chat_stream, settings, body,
                                          transport=ark_transport)

            async def sse():
                try:
                    while True:
                        line = await asyncio.to_thread(next, gen, None)
                        if line is None:
                            break
                        yield line + "\n\n"
                except AdapterError as exc:
                    logger.warning("回退流中途失败：%s", exc)
                    yield _sse_dump(openai_chat.openai_error_body(exc, request_id))
                    yield "data: [DONE]\n\n"

            return StreamingResponse(sse(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache",
                                              "X-Accel-Buffering": "no",
                                              "x-qwen-fallback": "ark"})
        data = await asyncio.to_thread(ark_fallback.chat, settings, body, reason,
                                       transport=ark_transport)
        resp = JSONResponse(status_code=200, content=data)
        resp.headers["x-qwen-fallback"] = "ark"
        return resp

    @app.post("/v1/chat/completions")
    async def create_chat_completion(request: Request):
        """OpenAI 形态 chat 门（t2t 文本 + 图片解析；能力不足时回退方舟，见 INTERFACE §8.8）。

        · **必须带 Key**（写端点，同方舟门创建口径；缺失/无效 ⇒ 401）；
        · 本门错误体用 **OpenAI 词汇表**（方舟门的 Ark 错误码在这里不通用）；
        · `stream: true` ⇒ 真流式（上游 SSE 增量 → `chat.completion.chunk`）；
          `stream: false` ⇒ 服务端聚合后一次性回 `chat.completion`；
        · 图片输入 = `image_url` 分段（单张，上游实测）；文件/音频/视频分段 ⇒
          **配置了回退通道则整单转方舟，否则 400**（U-15 实证）；
        · `usage` 只在上游给出真值时透传；`degradations` 非空时作为加性扩展出现。
        """
        request_id = getattr(request.state, "request_id", "")
        try:
            credential_id_of(request)
            try:
                body = await request.json()
            except ValueError as exc:
                raise InvalidParameterError("请求体不是合法 JSON") from exc
            dry_run = request.headers.get("x-avm-dry-run", "").strip().lower() in ("1", "true", "yes")
            if settings.ark_fallback_enabled:
                reason = ark_fallback.fallback_reason(body)
                if reason:
                    return await _fallback_response(body, reason, request_id, dry_run)
            else:
                # 回退未启用：函数调用状态消息（role:tool / assistant.tool_calls）qwen
                # 完全无法表达 ⇒ 给可行动的 400（tools 参数本身仍走"忽略+降级"老口径）
                state_reason = ark_fallback.fallback_reason(body)
                if state_reason and state_reason.startswith("函数调用状态消息"):
                    raise InvalidParameterError(
                        f"qwen 无法表达{state_reason}；如需支持请在服务端配置回退通道"
                        "（ARK_FALLBACK_KEY + ARK_FALLBACK_MODEL）",
                        param="messages")
            req = openai_chat.parse_openai_chat_request(
                body, thinking_leak_models=settings.thinking_leak_models)

            if req.attachment and not settings.upload_enabled:
                # 附件上传链未启用：有回退通道则转方舟（历史兼容），否则 400 指明配置项
                if settings.ark_fallback_enabled:
                    reason = f"{req.attachment[0]} 解析（上传未启用，走回退通道）"
                    return await _fallback_response(body, reason, request_id, dry_run)
                raise InvalidParameterError(
                    "附件（文件/音频/视频/data: 图片）需要服务端启用上传链"
                    "（QWEN_UPLOAD_ENABLED=1），或配置方舟回退通道（ARK_FALLBACK_*）",
                    param="messages")
            if dry_run:
                return JSONResponse(status_code=200,
                                    content=await asyncio.to_thread(service.chat_dry_run, req))
            meta: dict = {}
            if req.stream:
                return await _chat_streaming_response(req, request_id, meta)
            completion_id = openai_chat.new_completion_id()
            created = int(time.time())

            def run() -> str:
                return "".join(t for kind, t in service.chat_stream(req, meta=meta)
                               if kind == "answer")

            text = await asyncio.to_thread(run)
            return JSONResponse(status_code=200, content=openai_chat.completion_object(
                completion_id=completion_id, created=created, model=req.model_requested,
                text=text, degradations=req.degradations, usage=meta.get("usage")))
        except AdapterError as exc:
            headers = ({"Retry-After": str(int(max(1.0, exc.retry_after)))}
                       if exc.retry_after else {})
            return JSONResponse(status_code=exc.status_code,
                                content=openai_chat.openai_error_body(exc, request_id),
                                headers=headers)

    @app.post("/v1/responses")
    async def create_response(request: Request):
        """OpenAI Responses 形态门（同 chat 门语义：t2t + 图片解析；能力不足回退方舟 `/responses`）。

        · `input`（字符串/条目数组）+ `instructions` ⇒ 复用 chat 门的翻译与执行；
        · `tools` / 工具调用状态输入项 / 不支持分段 ⇒ **配置了回退通道则整单转方舟
          `/responses`**（原生支持 tools 与 web_search 等内置工具；应答原样透传 +
          `x-qwen-fallback: ark` 头），未配置 ⇒ 400/降级（语义同 chat 门）；
        · qwen 路径应答 = `object:"response"`（`output[0].content[0].output_text`），
          流式 = `response.created` → `response.output_text.delta` → `response.completed`。
        """
        request_id = getattr(request.state, "request_id", "")
        try:
            credential_id_of(request)
            try:
                body = await request.json()
            except ValueError as exc:
                raise InvalidParameterError("请求体不是合法 JSON") from exc
            dry_run = request.headers.get("x-avm-dry-run", "").strip().lower() in ("1", "true", "yes")
            if settings.ark_fallback_enabled:
                reason = ark_fallback.responses_fallback_reason(body)
                if reason:
                    if dry_run:
                        return JSONResponse(status_code=200, content=_ark_dry_run_view(
                            body, reason, path="/responses"))
                    if body.get("stream"):
                        gen = await asyncio.to_thread(ark_fallback.responses_stream,
                                                      settings, body,
                                                      transport=ark_transport)

                        async def ark_sse():
                            try:
                                while True:
                                    line = await asyncio.to_thread(next, gen, None)
                                    if line is None:
                                        break
                                    yield line + "\n\n"
                            except AdapterError as exc:
                                logger.warning("回退流（responses）中途失败：%s", exc)
                                yield _sse_dump(openai_chat.openai_error_body(exc, request_id))
                                yield "data: [DONE]\n\n"

                        return StreamingResponse(ark_sse(), media_type="text/event-stream",
                                                 headers={"Cache-Control": "no-cache",
                                                          "X-Accel-Buffering": "no",
                                                          "x-qwen-fallback": "ark"})
                    data = await asyncio.to_thread(ark_fallback.responses, settings, body,
                                                   reason, transport=ark_transport)
                    resp = JSONResponse(status_code=200, content=data)
                    resp.headers["x-qwen-fallback"] = "ark"
                    return resp
            chat_body, state_reason = openai_responses.to_chat_body(body)
            if state_reason:
                raise InvalidParameterError(
                    f"qwen 无法表达{state_reason}；如需支持请在服务端配置回退通道"
                    "（ARK_FALLBACK_KEY + ARK_FALLBACK_MODEL）",
                    param="input")
            req = openai_chat.parse_openai_chat_request(
                chat_body, thinking_leak_models=settings.thinking_leak_models)

            meta: dict = {}
            completion_id = openai_responses.new_response_id()
            created = int(time.time())
            degradations = req.degradations

            if dry_run:
                return JSONResponse(status_code=200, content={
                    "dry_run": True,
                    "channel": "qwen",
                    "upstream": (await asyncio.to_thread(service.chat_dry_run, req))["upstream"],
                    "degradations": degradations,
                })

            def _response(text: str) -> dict:
                return openai_responses.response_object(
                    response_id=completion_id, created=created,
                    model=req.model_requested, text=text,
                    degradations=degradations, usage=meta.get("usage"))

            if req.stream:
                gen = service.chat_stream(req, meta=meta)
                first = await asyncio.to_thread(next, gen, (None, None))
                if first[1] is None:
                    raise UpstreamError("chat 流未产生任何增量（上游零输出按失败，不报成功）")
                item_id = openai_responses.new_message_id()

                async def sse():
                    collected: list[str] = []
                    try:
                        yield _sse_dump(openai_responses.created_event(
                            openai_responses.response_object(
                                response_id=completion_id, created=created,
                                model=req.model_requested, text="",
                                degradations=degradations, status="in_progress",
                                output=[{"type": "message", "id": item_id,
                                         "role": "assistant", "status": "in_progress",
                                         "content": []}])))
                        kind, pending = first
                        while kind is not None:
                            if kind == "reasoning":
                                yield _sse_dump(openai_responses.reasoning_delta_event(item_id, pending))
                            else:
                                collected.append(pending)
                                yield _sse_dump(openai_responses.delta_event(item_id, pending))
                            kind, pending = await asyncio.to_thread(next, gen, (None, None))
                        yield _sse_dump(openai_responses.completed_event(
                            _response("".join(collected))))
                    except AdapterError as exc:
                        logger.warning("responses 流中途失败：%s", exc)
                        yield _sse_dump(openai_chat.openai_error_body(exc, request_id))
                        yield "data: [DONE]\n\n"

                return StreamingResponse(sse(), media_type="text/event-stream",
                                         headers={"Cache-Control": "no-cache",
                                                  "X-Accel-Buffering": "no"})

            def run() -> str:
                return "".join(t for kind, t in service.chat_stream(req, meta=meta)
                               if kind == "answer")

            text = await asyncio.to_thread(run)
            return JSONResponse(status_code=200, content=_response(text))
        except AdapterError as exc:
            headers = ({"Retry-After": str(int(max(1.0, exc.retry_after)))}
                       if exc.retry_after else {})
            return JSONResponse(status_code=exc.status_code,
                                content=openai_chat.openai_error_body(exc, request_id),
                                headers=headers)

    # ------------------------------------------------------------------ 能力清单 / 运维面

    @app.get("/v1/models")
    async def list_models() -> dict:
        """本服务的能力清单（**OpenAI 形态**）= 注册的上游 chat 模型 + 视频能力条目。

        chat 模型**注册自上游** `GET /api/models`（免鉴权、TTL 缓存、失败回退上一份好清单，
        见 `app/models.py::ChatModelRegistry`）；只注册真正能跑 chat（t2t）的条目。
        **不校验 Key**：清单不涉密，而 OpenAI 系客户端 / 网关（new-api 等）常在填 Key 之前
        先探一次能力；这里返回 401 会让"探测失败"被误读成"服务不可用"。
        """
        chat_entries = await asyncio.to_thread(registry.entries)
        return {"object": "list", "data": [*chat_entries, *models.catalog()]}

    @app.get("/", include_in_schema=False)
    async def index():
        """落地页（fleet 约定：`/` 给人看，`/llms.txt` 给 LLM 读；都免鉴权）。"""
        chat_ids = [e["id"] for e in await asyncio.to_thread(registry.entries)]
        return HTMLResponse(llms_txt.render_landing(settings, chat_ids))

    @app.get("/llms.txt", include_in_schema=False)
    async def llms_txt_route():
        """LLM 可读契约（fleet 约定：内容派生自注册表/异常类/配置，不手抄）。"""
        chat_ids = [e["id"] for e in await asyncio.to_thread(registry.entries)]
        text = llms_txt.render_llms_txt(settings, chat_ids)
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "version": __version__}

    @app.get("/readyz")
    async def readyz():
        store_ok = True
        try:
            store.count()
        except Exception:  # noqa: BLE001 - 探活不许炸
            store_ok = False
        return {
            "ready": bool(settings.ready and store_ok),
            "accounts": len(settings.accounts),
            "store": "ok" if store_ok else "error",
            "upstream": settings.base_url,
            "coordinator": settings.coordinator_enabled,
        }

    @app.get("/stats")
    async def stats():
        return {
            "accounts": pool.stats(),
            "tasks": {"total": store.count(), "active": store.count_active(),
                      "queued": store.count_queued()},
            "models_registry": registry.stats(),
        }

    return app
