"""能力回退通道 —— chat 门对 qwen 上游不支持的能力，整单转**火山方舟 chat**（Doubao）应答。

触发面（`fallback_reason`，纯函数；2026-09-24 用户决策：「调用 tools 或者其他不支持功能的时候
回退到方舟」）：
  · `tools` / `tool_choice` —— OpenAI 函数调用（qwen 实测静默忽略，UPSTREAM §4.6 / U-16）；
  · 文件 / 音频 / 视频 content 分段（qwen 实测拒绝外链附件，U-15）；
  · 多图、`data:` URI 图片（qwen 门只收单张 http(s) 图）；
  · **函数调用状态消息**（`role:"tool"` / `assistant.tool_calls`）—— 函数调用是多轮闭环，
    即使本跳没带 tools 也必须回退（qwen 无法表达这两种消息形态）。
其余降级（temperature 等采样参数）**不触发**回退 —— qwen 仍能正常应答，不必换通道。

形态（方舟 `/chat/completions` 本就是 OpenAI 兼容）：
  · 请求体**只换 model、原样透传**（messages / tools / stream / 采样参数全部保留；
    多轮对话由方舟原生消化 —— 没有拍平降级）；
  · 🔴 **回退模型名对调用方全链路脱敏**（2026-09-24 用户指令）：
    应答 `model` 改写为调用方请求的模型；流式 chunk 逐条清洗；报错报文清洗
    （完整原文只进服务端日志）；回退事实只由 `x-qwen-fallback: ark` 响应头披露
    （说走了通道，不泄哪个模型；2026-10-14 起不再写 `degradations`）。
  · **模型级 failover**（2026-09-24 用户指令「429 可以用下 doubao-seed-2-1-turbo」→
    升级为**多模型链**「兜底模型支持多模型配置」）：主回退模型被方舟限流（429）⇒
    按 `ARK_FALLBACK_MODELS`（逗号分隔）依次切换重试；全部被限 ⇒ 429 原样转发。
    所有模型名都在脱敏之列。
🔴 回退通道故障 ⇒ 502，**不静默降回 qwen**：调用方点名的能力 qwen 给不了，
   静默降级等于让调用方在不知情下拿次级结果。
凭据纪律：`ARK_FALLBACK_KEY` / 模型名 只来自 env（gitignored），绝不入源码 / 文档 / 测试，
且**任何出站报文**（应答 / 报错 / 流）都过脱敏。
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import httpx

from .config import Settings
from .errors import RateLimitedError, UpstreamError, UpstreamTimeoutError

logger = logging.getLogger("qwen.ark_fallback")


def fallback_reason(body: dict) -> str | None:
    """该请求是否点了 qwen 给不了的能力 → 给出可读理由；不需要回退则 None。

    🔴 函数调用是**多轮闭环**：不只看 `tools` 参数 —— 对话里出现
    `role:"tool"`（工具结果回传）或 `assistant.tool_calls`（上一跳的调用指令）时，
    即使本跳没带 tools 也必须回退（qwen 无法表达这两种消息形态，parse 一律 400）。
    ⚠️ 文件/音频/视频/data: 图附件**不再触发回退**（2026-09-24 起走上传链，
    UPSTREAM §4.7）—— 由 service 层解析并在上传失败时报错。
    """
    if not isinstance(body, dict):
        return None
    if body.get("tools") or body.get("tool_choice"):
        return "函数调用（tools/tool_choice）"
    messages = body.get("messages")
    if not isinstance(messages, list):
        return None
    images = 0
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role == "tool":
            return "函数调用状态消息（role:tool）"
        if role == "assistant" and item.get("tool_calls"):
            return "函数调用状态消息（assistant.tool_calls）"
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                images += 1
    if images > 1:
        return "多图输入"
    return None


# ------------------------------------------------------------------ 脱敏（🔴 出站必经）

def _sanitize(text: str, settings: Settings) -> str:
    """报文脱敏：回退模型链（主+全部备用）与 Key 不外泄。完整原文只进服务端日志。"""
    for model in [settings.ark_fallback_model, *settings.ark_fallback_models]:
        if model:
            text = text.replace(model, "<redacted-model>")
    if settings.ark_fallback_key:
        text = text.replace(settings.ark_fallback_key, "<redacted-key>")
    return text


def _scrub_model(obj: object, requested: str) -> object:
    """深度改写对象里所有 `model` 字段为调用方请求的模型（回退模型名不外泄）。"""
    if isinstance(obj, dict):
        if isinstance(obj.get("model"), str):
            obj["model"] = requested
        for value in obj.values():
            _scrub_model(value, requested)
    elif isinstance(obj, list):
        for value in obj:
            _scrub_model(value, requested)
    return obj


def _requested_model(body: dict) -> str:
    return str(body.get("model") or "").strip() or "<redacted-model>"


def _scrub_line(line: str, settings: Settings, requested: str) -> str:
    """流式单行清洗：`data: {JSON}` 逐条深度改写 model；其余行原文脱敏。"""
    if line.startswith("data:") and "[DONE]" not in line:
        try:
            event = json.loads(line[len("data:"):])
        except ValueError:
            return _sanitize(line, settings)
        if isinstance(event, dict):
            return "data: " + json.dumps(_scrub_model(event, requested), ensure_ascii=False)
    return _sanitize(line, settings)


def _raise_http(resp: httpx.Response, settings: Settings, channel: str) -> None:
    """非流式状态码分类。🔴 完整原文进服务端日志；给调用方的报文过脱敏。

    429（方舟限流，实测 `RequestBurstTooFast`）⇒ 保留 429 + Retry-After 语义
    （有备用模型时已在此前完成 failover，走到这里的 429 = 主备都被限流）。
    """
    if resp.status_code < 400:
        return
    logger.warning("回退通道（%s）HTTP %s：%s", channel, resp.status_code, resp.text[:500])
    if resp.status_code == 429:
        raw = resp.headers.get("retry-after", "")
        try:
            retry_after = float(raw) if raw else None
        except ValueError:
            retry_after = None
        raise RateLimitedError(
            f"回退通道（{channel}）限流（429）：{_sanitize(resp.text[:200], settings)}",
            retry_after=retry_after)
    raise UpstreamError(
        f"回退通道（{channel}）HTTP {resp.status_code}: {_sanitize(resp.text[:200], settings)}")


def _raise_stream_status(resp: httpx.Response, client: httpx.Client,
                         settings: Settings, channel: str) -> None:
    """流式打开后的状态码分类（429 语义保留同上）；出错时负责清理流/连接。"""
    if resp.status_code < 400:
        return
    raw = resp.read()[:500]
    logger.warning("回退通道（%s）HTTP %s：%s", channel, resp.status_code, raw)
    snippet = raw[:200].decode("utf-8", "replace")
    resp.close()
    client.close()
    if resp.status_code == 429:
        retry_raw = resp.headers.get("retry-after", "")
        try:
            retry_after = float(retry_raw) if retry_raw else None
        except ValueError:
            retry_after = None
        raise RateLimitedError(
            f"回退通道（{channel}）限流（429）：{_sanitize(snippet, settings)}",
            retry_after=retry_after)
    raise UpstreamError(
        f"回退通道（{channel}）HTTP {resp.status_code}: {_sanitize(snippet, settings)}")


def _headers(settings: Settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.ark_fallback_key}",
            "Content-Type": "application/json"}


def _open(settings: Settings, *,
          transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(base_url=settings.ark_fallback_base,
                        timeout=settings.ark_fallback_timeout,
                        trust_env=False, transport=transport)


def _candidate_models(settings: Settings) -> list[str]:
    """尝试序列：主回退模型 → 备用模型链（逗号分隔配置，按序 failover）。"""
    return [settings.ark_fallback_model, *settings.ark_fallback_models]


def _post_with_failover(settings: Settings, path: str, payload: dict, *, channel: str,
                        transport: httpx.BaseTransport | None = None) -> httpx.Response:
    """非流式 POST，**主模型 429 ⇒ 备用模型自动重试一次**；最终响应交调用方分类。"""
    candidates = _candidate_models(settings)
    resp: httpx.Response | None = None
    for index, model in enumerate(candidates):
        attempt = {**payload, "model": model}
        with _open(settings, transport=transport) as client:
            try:
                resp = client.post(path, json=attempt, headers=_headers(settings))
            except httpx.TimeoutException as exc:
                raise UpstreamTimeoutError(
                    f"回退通道（{channel}）超时：{_sanitize(str(exc), settings)}") from exc
            except httpx.HTTPError as exc:
                raise UpstreamError(
                    f"回退通道（{channel}）传输失败：{_sanitize(str(exc), settings)}") from exc
        if resp.status_code == 429 and index < len(candidates) - 1:
            logger.warning("回退通道（%s）主模型 429 —— 切换备用模型重试", channel)
            continue
        break
    assert resp is not None
    return resp


def _open_stream(settings: Settings, path: str, payload: dict, *, channel: str,
                 transport: httpx.BaseTransport | None = None,
                 ) -> tuple[httpx.Client, httpx.Response]:
    """流式打开，**主模型 429 ⇒ 备用模型自动重试一次**；返回 (client, resp)。"""
    candidates = _candidate_models(settings)
    client: httpx.Client | None = None
    resp: httpx.Response | None = None
    for index, model in enumerate(candidates):
        client = _open(settings, transport=transport)
        attempt = {**payload, "model": model}
        try:
            request = client.build_request("POST", path, json=attempt,
                                           headers=_headers(settings))
            resp = client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            client.close()
            raise UpstreamTimeoutError(
                f"回退通道（{channel}）超时：{_sanitize(str(exc), settings)}") from exc
        except httpx.HTTPError as exc:
            client.close()
            raise UpstreamError(
                f"回退通道（{channel}）传输失败：{_sanitize(str(exc), settings)}") from exc
        if resp.status_code == 429 and index < len(candidates) - 1:
            logger.warning("回退通道（%s）主模型 429 —— 切换备用模型重试", channel)
            resp.close()
            client.close()
            continue
        break
    assert client is not None and resp is not None
    return client, resp


def chat(settings: Settings, body: dict, *,
         transport: httpx.BaseTransport | None = None) -> dict:
    """非流式回退：方舟应答 → `model` 改写为调用方请求值（回退事实走 `x-qwen-fallback` 头）。"""
    resp = _post_with_failover(settings, "/chat/completions", body,
                               channel="方舟 chat", transport=transport)
    _raise_http(resp, settings, "方舟 chat")
    try:
        data = resp.json()
    except ValueError as exc:
        raise UpstreamError("回退通道（方舟 chat）响应不是 JSON") from exc
    if not isinstance(data, dict):
        raise UpstreamError("回退通道（方舟 chat）响应形态异常（非 JSON 对象）")
    _scrub_model(data, _requested_model(body))
    return data


def chat_stream(settings: Settings, body: dict, *,
                transport: httpx.BaseTransport | None = None) -> Iterator[str]:
    """流式回退：打开方舟 SSE，**逐行清洗**后转发（chunk 内 model 改写为调用方请求值）。

    🔴 请求建立（状态码判读）在**返回前**完成：失败在这里就抛，调用方还能回正经 HTTP 错误；
    进入迭代后的失败只能走流内错误事件。
    """
    client, resp = _open_stream(settings, "/chat/completions", body,
                                channel="方舟 chat", transport=transport)
    if resp.status_code >= 400:
        _raise_stream_status(resp, client, settings, "方舟 chat")
    requested = _requested_model(body)

    def iterate() -> Iterator[str]:
        try:
            for line in resp.iter_lines():
                if line:
                    yield _scrub_line(line, settings, requested)
        finally:
            resp.close()
            client.close()

    return iterate()


# ------------------------------------------------------------------ Responses 通道

def responses_fallback_reason(body: dict) -> str | None:
    """Responses 请求的回退判定（`POST /v1/responses`）。

    覆盖：`tools`/`tool_choice`、工具调用状态输入项（function_call 等）、
    不支持的 content 分段、多图 / data: 图片 —— 判定与 chat 门同源
    （把 Responses 转成 chat 形态后复用 `fallback_reason`）。
    请求本身写错 ⇒ None（交给正式解析去 400，不在这里抢报）。
    """
    if not isinstance(body, dict):
        return None
    if body.get("tools") or body.get("tool_choice"):
        return "函数调用（tools/tool_choice）"
    from .openai_responses import to_chat_body

    try:
        chat_body, state_reason = to_chat_body(body)
    except Exception:  # noqa: BLE001 - 形态错误让正式解析去报，触发判定不抢
        return None
    return state_reason or fallback_reason(chat_body)


def responses(settings: Settings, body: dict, *,
              transport: httpx.BaseTransport | None = None) -> dict:
    """非流式回退（方舟 `/responses`，原生支持 tools/web_search）：model 改写（脱敏）。"""
    resp = _post_with_failover(settings, "/responses", body,
                               channel="方舟 responses", transport=transport)
    _raise_http(resp, settings, "方舟 responses")
    try:
        data = resp.json()
    except ValueError as exc:
        raise UpstreamError("回退通道（方舟 responses）响应不是 JSON") from exc
    if not isinstance(data, dict):
        raise UpstreamError("回退通道（方舟 responses）响应形态异常（非 JSON 对象）")
    _scrub_model(data, _requested_model(body))
    return data


def responses_stream(settings: Settings, body: dict, *,
                     transport: httpx.BaseTransport | None = None) -> Iterator[str]:
    """流式回退（方舟 `/responses` SSE，逐行清洗后原样透传事件形态）。"""
    client, resp = _open_stream(settings, "/responses", {**body, "stream": True},
                                channel="方舟 responses", transport=transport)
    if resp.status_code >= 400:
        _raise_stream_status(resp, client, settings, "方舟 responses")
    requested = _requested_model(body)

    def iterate() -> Iterator[str]:
        try:
            for line in resp.iter_lines():
                if line:
                    yield _scrub_line(line, settings, requested)
        finally:
            resp.close()
            client.close()

    return iterate()
