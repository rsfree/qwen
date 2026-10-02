"""OpenAI chat 契约翻译层 —— `POST /v1/chat/completions`（纯函数，无 IO；同 `ark.py` 的职责边界）。

🔴 范围（2026-09-24 用户冻结 + 当日实测扩口）：chat（t2t）任务，支持**图片输入**（解析）
与**文件/音频/视频解析**（2026-09-24 晚间二扩：用户指令「走他的上传」—— 上传链实测契约
见 UPSTREAM §4.7：getstsToken → OSS V1 PUT → files[] 条目，文档/音频/视频逐类实测通过）。
视频生成走方舟契约门（`/api/v3/contents/generations/tasks`），两扇门互不相通。

附件口径（§8.7）：
  · 单附件规则：图片 / 文件 / 音频 / 视频 **一次一个**（多附件未验证，400）；
  · 图片 http(s) 直链 ⇒ 直接引用（§4.2 形状，实测）；**data: URI 图片 ⇒ 转上传链**（不再 400）；
  · 文件 / 音频 / 视频（任意 http(s)/data: 来源）⇒ **转上传链**（`service` 里下载/解码 →
    getstsToken → OSS PUT → files[] 条目）；服务端未启用上传且未配置回退 ⇒ 400。
上游流式响应形态已实测关闭（UPSTREAM §4.5，U-12）；`usage` 真实透传。

语义决策（docs/INTERFACE.md §8 有登记）：
  · **无状态**：每个请求新建上游会话；多轮/system 请求**拍平**成单条 prompt（写降级说明）；
  · **不编造**：`usage` 只透传上游真值；`degradations` 加性扩展（非空才出现）；
  · `model` 不做本地白名单硬校验（未知模型由上游拒绝），只拦明确的错误指向（`qwen/video`）。
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from . import media
from .errors import AdapterError, InvalidParameterError

#: 本门唯一做的任务类型（上游 `chat_type` 三处同标）。
CHAT_TASK = "t2t"

#: 认得但上游 t2t 没有的 OpenAI 参数 —— 出现即进 `degradations`（不假装支持、不静默丢弃）。
#: 🔴 `reasoning_effort` **不在其中**：它是思考档位入口（`_thinking_gear`），见 THINKING_GEARS。
UNSUPPORTED_PARAMS = (
    "temperature", "top_p", "n", "max_tokens", "max_completion_tokens", "max_output_tokens",
    "presence_penalty", "frequency_penalty", "stop", "seed", "tools", "tool_choice",
    "response_format", "logit_bias", "logprobs", "top_logprobs",
    "modalities", "audio", "stream_options", "service_tier", "user",
)

_ROLE_LABELS = {"user": "User", "assistant": "Assistant"}

#: 文件/音频/视频分段类型 → 附件 kind（2026-09-24 起**转上传链**而非 400——用户指令「走他的上传」）。
_ATTACHMENT_KINDS = {
    "file": "file", "input_file": "file", "file_url": "file",
    "input_audio": "audio", "audio_url": "audio",
    "video_url": "video", "video": "video",
}
#: 各分段类型里取来源 URL/data: 的字段路径（dict 键；字符串形态直接当 URL）。
_ATTACHMENT_FIELDS = {
    "file": ("file", "url"), "input_file": ("file", "url"),
    "file_url": ("file_url", "file_url"),
    "input_audio": ("input_audio", "data"), "audio_url": ("audio_url", "url"),
    "video_url": ("video_url", "url"), "video": ("video", "url"),
}


@dataclass
class ChatRequest:
    """解析后的 chat 请求（响应里 `model` 用调用方**原样写法**回显，同方舟门口径）。"""

    model_requested: str          # 调用方原始写法（可能带 qwen/ 前缀）
    model: str                    # 剥掉 `qwen/` 前缀后转发上游的模型 id
    prompt: str                   # 拍平后的单条 prompt
    stream: bool
    files: list[dict] = field(default_factory=list)   # 直引图片条目（http(s) 图，§4.2 形状）
    #: 待上传附件（kind ∈ file/audio/video/image；source = http(s) URL 或 data: URI）。
    #: 由 service 层走上传链（getstsToken → OSS PUT）换成 files[] 条目。
    attachment: tuple[str, str] | None = None
    #: 思考档位（auto/thinking/fast，对齐前端三档；缺省 auto = 前端默认）。
    thinking_gear: str = "auto"
    degradations: list[str] = field(default_factory=list)


def _image_file_entry(url: str, *, index: int, part: int) -> dict:
    """OpenAI `image_url` 分段 → 上游 `files[]` 条目（形状 = §4.2 抓包逐字，复用 `media`）。"""
    url = media.validate_image_url(str(url or ""), param=f"messages[{index}].content[{part}]")
    return media.image_entry(url)


def _image_url_of(part: dict, *, index: int, part_index: int) -> str:
    raw = part.get("image_url")
    if isinstance(raw, dict):
        return str(raw.get("url") or "")
    if isinstance(raw, str):
        return raw
    raise InvalidParameterError(
        f"messages[{index}].content[{part_index}] 的 image_url 必须是 {{url: …}} 或字符串",
        param=f"messages[{index}].content[{part_index}].image_url")


def _attachment_source(part: dict, *, index: int, part_index: int) -> str:
    kind = str(part.get("type") or "")
    field, key = _ATTACHMENT_FIELDS[kind]
    raw = part.get(field)
    if isinstance(raw, dict):
        source = raw.get(key) or raw.get("url") or raw.get("file_url")
    elif isinstance(raw, str):
        source = raw
    else:
        source = None
    source = str(source or "").strip()
    if not source:
        raise InvalidParameterError(
            f"messages[{index}].content[{part_index}]（{kind}）缺少来源 URL/data:",
            param=f"messages[{index}].content[{part_index}].{field}")
    if key == "data" and not source.startswith("data:"):
        fmt = ""
        inner = part.get(field)
        if isinstance(inner, dict):
            fmt = str(inner.get("format") or "wav")
        source = f"data:audio/{fmt};base64,{source}"
    return source


def _split_content(content: object, *, index: int, role: str
                   ) -> tuple[str, list[dict], tuple[str, str] | None]:
    """OpenAI content → (纯文本, 直引图片条目, 待上传附件)。

    · 图片 http(s) 直链 ⇒ 直引条目（§4.2 形状）；**data: URI ⇒ 待上传附件**（不再 400）；
    · 文件/音频/视频分段 ⇒ 待上传附件（用户指令「走他的上传」—— 上传链实测见 §4.7）；
    · 多附件 ⇒ 400（多附件未验证，不替调用方挑一个）。
    """
    if isinstance(content, str):
        return content, [], None
    if not isinstance(content, list):
        raise InvalidParameterError(
            f"messages[{index}].content 必须是字符串或分段数组",
            param=f"messages[{index}].content")
    pieces: list[str] = []
    files: list[dict] = []
    attachment: tuple[str, str] | None = None
    for part_index, part in enumerate(content):
        if not isinstance(part, dict):
            raise InvalidParameterError(
                f"messages[{index}].content[{part_index}] 必须是对象",
                param=f"messages[{index}].content[{part_index}]")
        kind = str(part.get("type") or "")
        if kind == "text" and isinstance(part.get("text"), str):
            pieces.append(part["text"])
        elif kind == "image_url":
            if role != "user":
                raise InvalidParameterError(
                    f"messages[{index}]（{role}）不能带图片附件 —— 只有 user 消息可带",
                    param=f"messages[{index}].content[{part_index}]")
            url = _image_url_of(part, index=index, part_index=part_index)
            if url.strip().lower().startswith("data:"):
                if attachment is not None:
                    raise InvalidParameterError(
                        "一次最多 1 个附件（收到多个）—— 多附件输入未经验证", param="messages")
                attachment = ("image", url)
            else:
                files.append(_image_file_entry(url, index=index, part=part_index))
        elif kind in _ATTACHMENT_KINDS:
            if role != "user":
                raise InvalidParameterError(
                    f"messages[{index}]（{role}）不能带 {kind} 附件 —— 只有 user 消息可带",
                    param=f"messages[{index}].content[{part_index}]")
            if attachment is not None:
                raise InvalidParameterError(
                    "一次最多 1 个附件（收到多个）—— 多附件输入未经验证", param="messages")
            attachment = (_ATTACHMENT_KINDS[kind],
                          _attachment_source(part, index=index, part_index=part_index))
        else:
            raise InvalidParameterError(
                f"messages[{index}].content[{part_index}] 的分段类型 {kind!r} 不支持"
                "（支持：text / image_url / file / input_audio / video_url 等，见 §8.7）",
                param=f"messages[{index}].content[{part_index}].type")
    return "".join(pieces), files, attachment


def _flatten(messages: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """多轮/system → 单条 prompt（转录拼接）。单条 user 消息**原样直发**（抓包实证路径）。"""
    degradations: list[str] = []
    system = [text for role, text in messages if role == "system"]
    turns = [(role, text) for role, text in messages if role in ("user", "assistant")]
    if not turns:
        raise InvalidParameterError(
            "messages 里没有可回复的用户内容（只有 system 不构成一轮对话）", param="messages")

    parts: list[str] = []
    if system:
        parts.append("【系统指令】\n" + "\n".join(system))
    prior = turns[:-1]
    if prior:
        for role, text in prior:
            parts.append(f"{_ROLE_LABELS[role]}: {text}")
    last_role, last_text = turns[-1]
    if prior or system:
        label = _ROLE_LABELS.get(last_role, "User")
        parts.append(f"{label}: {last_text}")
        degradations.append(
            "多轮/带 system 的请求按「转录拼接」拍平成单条 prompt（上游一次只收一条消息，"
            "会话历史由调用方维护）")
    else:
        parts.append(last_text)
    return "\n\n".join(parts), degradations


def _thinking_gear(body: dict) -> str:
    """思考档位解析（对齐前端"自动/思考/快速"三档，UPSTREAM §4.6 档位表）。

    · `reasoning_effort`："none"/"minimal" ⇒ fast（关思考，首字最快）；"high" ⇒ thinking
      （强制思考）；其余/缺省 ⇒ auto（前端默认）。
    · `enable_thinking`（DashScope 风格）：false ⇒ fast（显式声明优先于 reasoning_effort 缺省）。
    """
    if "enable_thinking" in body:
        et = body.get("enable_thinking")
        truthy = et is True or (isinstance(et, str) and et.strip().lower() in ("1", "true", "yes"))
        if not truthy:
            return "fast"
    effort = str(body.get("reasoning_effort") or "").strip().lower()
    if effort in ("none", "minimal"):
        return "fast"
    if effort == "high":
        return "thinking"
    return "auto"


#: 🔴 2026-10-02 实测缺陷模型名单（逗号分隔 env `QWEN_THINKING_LEAK_MODELS` 覆盖）：
#: 这些模型在 **auto / thinking 档**把思考过程直接写进正文 `content`（不拆
#: `reasoning_content`，也没有 `</analysis>` 标记），实测复现 3/3 含流式。
#: 根因在**模型侧**：上游不发 `thinking_summary` 事件 ⇒ 本服务无从映射（对比同请求
#: `qwen3.7-plus` 流式正确分离）。**fast 档干净**（`thinking_enabled:false` ⇒ 无思考）。
#:
#: 为何是名单而不是硬编码判定：模型注册清单有 TTL（`INTERFACE §8.1` 同源纪律）——
#: 上游修好或换名后，改一行 env 即可，不必改代码发版。
#: 默认值只含**实测确认**的模型；未实测的模型不猜。
DEFAULT_THINKING_LEAK_MODELS: tuple[str, ...] = ("qwen3.8-omni-flash",)


def _is_thinking_leak_model(model: str, leak_models: Sequence[str]) -> bool:
    """模型是否在「思考泄漏」名单里（裸名比较，容忍调用方写 `qwen/` 前缀已剥离的形态）。"""
    return bool(model) and model in tuple(leak_models)


def parse_openai_chat_request(body: dict, *,
                              thinking_leak_models: Sequence[str] | None = None) -> ChatRequest:
    """OpenAI chat 请求 → `ChatRequest`。请求写错一律 400（`InvalidParameterError`）。

    `thinking_leak_models`：思考摘要会漏进正文的模型名单（None ⇒ 用代码默认值）。
    见 `_is_thinking_leak_model`。
    """
    if not isinstance(body, dict):
        raise InvalidParameterError("请求体必须是 JSON 对象")

    model_requested = str(body.get("model") or "").strip()
    if not model_requested:
        raise InvalidParameterError("缺少 model", param="model")
    model = model_requested[5:] if model_requested.lower().startswith("qwen/") else model_requested
    if model == "video":
        raise InvalidParameterError(
            "qwen/video 是视频能力条目 —— 视频任务请走方舟契约门 "
            "/api/v3/contents/generations/tasks；本端点只做 chat 文本任务",
            param="model")
    if not model:
        raise InvalidParameterError("model 不能为空", param="model")

    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise InvalidParameterError("messages 必须是非空数组", param="messages")
    parsed: list[tuple[str, str]] = []
    files: list[dict] = []
    attachment: tuple[str, str] | None = None
    for index, item in enumerate(raw_messages):
        if not isinstance(item, dict):
            raise InvalidParameterError(f"messages[{index}] 必须是对象", param=f"messages[{index}]")
        role = str(item.get("role") or "")
        if role not in ("system", "user", "assistant"):
            raise InvalidParameterError(
                f"messages[{index}].role 只支持 system/user/assistant（收到 {role!r}）",
                param=f"messages[{index}].role")
        text, part_files, part_attachment = _split_content(item.get("content"),
                                                           index=index, role=role)
        if part_files or part_attachment:
            # 附件只认**最后一条 user 消息**（拍平后它就是本轮的提问）；
            # 更早轮次里的附件会在拍平时被丢弃 ⇒ 直接 400，不静默丢输入。
            if index != len(raw_messages) - 1:
                raise InvalidParameterError(
                    f"messages[{index}] 的附件会被拍平丢弃 —— 请把带附件的一轮放在 messages 末尾",
                    param=f"messages[{index}]")
        if part_files:
            files.extend(part_files)
        if part_attachment:
            attachment = part_attachment
        parsed.append((role, text))

    total_attachments = len(files) + (1 if attachment else 0)
    if total_attachments > 1:
        raise InvalidParameterError(
            f"一次最多 1 个附件（收到 {total_attachments} 个）—— 多附件输入未经验证，"
            "不替调用方挑一个（同方舟门口径）",
            param="messages")
    degradations: list[str] = []
    if files:
        warning = media.host_warning(files[0]["url"])
        if warning:
            degradations.append(warning)

    prompt, flatten_degradations = _flatten(parsed)
    degradations.extend(flatten_degradations)

    raw_stream = body.get("stream", False)
    if isinstance(raw_stream, bool):
        stream = raw_stream
    else:
        stream = str(raw_stream).strip().lower() in ("1", "true", "yes")

    for key in UNSUPPORTED_PARAMS:
        if key in body and body[key] not in (None, "", [], {}):
            degradations.append(f"参数 {key} 已忽略（上游 t2t 无此入参）")

    leak_models = (DEFAULT_THINKING_LEAK_MODELS if thinking_leak_models is None
                   else tuple(thinking_leak_models))
    gear = _thinking_gear(body)
    # 🔴 思考泄漏加固（2026-10-02 实测）：泄漏模型在 auto/thinking 档会把思考过程
    # 写进正文 content ⇒ **缺省档（auto）静默降 fast**，让默认路径拿到干净正文。
    # 调用方**显式**声明档位时不静默覆盖（显式意图优先），只写 degradation 告知风险
    # —— 契合本项目「不静默丢/改调用方语义」纪律。
    if _is_thinking_leak_model(model, leak_models) and gear == "auto":
        gear = "fast"
        degradations.append(
            f"模型 {model} 实测会把思考过程混进正文（上游不发独立思考事件）"
            f"⇒ 已自动切到快速档（reasoning_effort=\"none\"）以保证正文干净；"
            f"需要思考请显式传 reasoning_effort:\"high\"（思考内容会并入正文）")
    elif _is_thinking_leak_model(model, leak_models) and gear == "thinking":
        degradations.append(
            f"模型 {model} 实测不分离思考内容：reasoning_effort=\"high\" 的思考过程"
            f"会并入正文 content（无 reasoning_content 字段）——按显式声明照发，请知悉")

    return ChatRequest(model_requested=model_requested, model=model, prompt=prompt,
                       stream=stream, files=files, attachment=attachment,
                       thinking_gear=gear,
                       degradations=degradations)


def new_completion_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


def openai_usage(raw: dict | None) -> dict | None:
    """上游 `usage`（input/output/total_tokens，2026-09-24 实测）→ OpenAI 三键。
    只做**真实值的改名映射**，缺键不编；映射不出任何键 ⇒ None（usage 整键缺席）。"""
    if not isinstance(raw, dict):
        return None
    mapped: dict = {}
    if isinstance(raw.get("input_tokens"), int):
        mapped["prompt_tokens"] = raw["input_tokens"]
    if isinstance(raw.get("output_tokens"), int):
        mapped["completion_tokens"] = raw["output_tokens"]
    if isinstance(raw.get("total_tokens"), int):
        mapped["total_tokens"] = raw["total_tokens"]
    return mapped or None


def completion_object(*, completion_id: str, created: int, model: str, text: str,
                      degradations: list[str], usage: dict | None = None) -> dict:
    """非流式响应（OpenAI `chat.completion`）。`usage` 仅在上游给出真值时出现。"""
    obj: dict = {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
    }
    mapped = openai_usage(usage)
    if mapped:
        obj["usage"] = mapped
    if degradations:
        obj["degradations"] = list(degradations)   # 加性扩展（同方舟门 GET 口径）
    return obj


def chunk_object(*, completion_id: str, created: int, model: str, delta: dict,
                 finish_reason: str | None = None,
                 degradations: list[str] | None = None,
                 usage: dict | None = None) -> dict:
    """流式分片（OpenAI `chat.completion.chunk`）。`degradations` 只随首片出现（非空时）。"""
    obj: dict = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    mapped = openai_usage(usage)
    if mapped:
        obj["usage"] = mapped
    if degradations:
        obj["degradations"] = list(degradations)
    return obj


#: chat 门错误 `type` 词汇（OpenAI 形态）——按 HTTP 状态映射；`/llms.txt` 错误表由此派生。
CHAT_ERROR_TYPE_BY_STATUS = {
    400: "invalid_request_error",
    401: "authentication_error",
    404: "not_found_error",
    429: "rate_limit_error",
    502: "server_error",
    503: "service_unavailable",
    504: "server_error",
}


def openai_error_body(exc: AdapterError, request_id: str) -> dict:
    """本门的错误体用 **OpenAI 词汇表**（方舟门的 `code/type` 词汇不通用，分开给）。"""
    err: dict = {
        "message": f"{exc.message} Request ID: {request_id}",
        "type": CHAT_ERROR_TYPE_BY_STATUS.get(exc.status_code, "server_error"),
        "code": exc.code,
    }
    if exc.param:
        err["param"] = exc.param
    return {"error": err}


__all__ = [
    "CHAT_TASK",
    "UNSUPPORTED_PARAMS",
    "ChatRequest",
    "chunk_object",
    "completion_object",
    "new_completion_id",
    "openai_error_body",
    "openai_usage",
    "parse_openai_chat_request",
]
