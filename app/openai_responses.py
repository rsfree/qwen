"""OpenAI Responses 契约翻译层 —— `POST /v1/responses`（纯函数，无 IO）。

与 `openai_chat.py` 的关系：本模块把 Responses 请求**转换成 chat completions 形态**后，
复用 chat 门的全部翻译 / 触发 / 执行逻辑（`parse_openai_chat_request` / `service.chat_stream` /
`fallback.fallback_reason`）——一套 qwen 语义，两扇 OpenAI 门。

Responses 独有的形态：
  · `input`：字符串 或 条目数组（`{"role", "content"}`，content 分段用 `input_text` /
    `input_image` / `input_file` …）；`instructions` ⇒ system；
  · **工具调用状态输入项**（`function_call` / `function_call_output` / `reasoning` /
    `web_search_call` / `item_reference` 等）：qwen 无法表达 ⇒ 触发回退（未配置回退通道则 400）；
  · 响应对象：`{"object":"response","status":"completed","output":[{"type":"message",
    "content":[{"type":"output_text",…}]}],"usage":{input_tokens,…}}`；
  · 流式事件：`response.created` → `response.output_text.delta`（正文增量）→ `response.completed`。

不编造：`usage` 只透传上游真值（Responses 的键名 input/output/total_tokens 与上游一致，
直接改名映射同 chat 门）；降级说明只进服务端日志、**不下发**（2026-10-14 用户口径）。
"""
from __future__ import annotations

import uuid

from .errors import InvalidParameterError

#: Responses `input` 里的**工具调用状态项**类型 —— qwen 无法表达 ⇒ 回退理由。
_TOOL_STATE_TYPES = ("function_call", "function_call_output", "reasoning",
                     "web_search_call", "file_search_call", "item_reference",
                     "computer_call", "computer_call_output", "mcp_call",
                     "mcp_list_tools", "image_generation_call", "code_interpreter_call")

#: Responses content 分段 → chat completions 分段的映射（其余形态原样透传，
#: 由 chat 门的同款触发/拒绝逻辑处理）。
_PART_MAP = {"input_text": "text", "input_image": "image_url"}


def to_chat_body(body: dict) -> tuple[dict, str | None]:
    """Responses 请求 → chat completions 形态（供 chat 门翻译器与回退判定复用）。

    返回 `(chat_body, reason_or_None)`：`reason` 非空 = 出现了 qwen 无法表达的
    **工具调用状态输入项**（只可能在回退通道里服务；`chat_body` 已把它们剔除，
    因此该值仅在"回退通道关闭"分支用于给出可行动的 400）。
    """
    if not isinstance(body, dict):
        raise InvalidParameterError("请求体必须是 JSON 对象")
    messages: list[dict] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    inp = body.get("input")
    reason: str | None = None
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for index, item in enumerate(inp):
            if not isinstance(item, dict):
                raise InvalidParameterError(f"input[{index}] 必须是对象",
                                            param=f"input[{index}]")
            itype = str(item.get("type") or "message")
            if itype in _TOOL_STATE_TYPES:
                reason = reason or (f"工具调用状态输入项（input[{index}].type={itype}）")
                continue
            role = item.get("role")
            if role not in ("user", "assistant", "system", "developer"):
                raise InvalidParameterError(
                    f"input[{index}] 缺少可执行的 role（收到 {role!r}，type={itype!r}）",
                    param=f"input[{index}].role")
            if role == "developer":
                role = "system"
            content = item.get("content")
            if isinstance(content, list):
                content = [_normalize_part(part, index=index, part_index=part_index)
                           for part_index, part in enumerate(content)]
            messages.append({"role": role, "content": content})
    else:
        raise InvalidParameterError("input 必须是字符串或条目数组", param="input")
    chat_body = {k: v for k, v in body.items() if k not in ("input", "instructions")}
    chat_body["messages"] = messages
    return chat_body, reason


def _normalize_part(part: object, *, index: int, part_index: int) -> object:
    """Responses content 分段 → chat completions 分段（`input_text`/`input_image` 改名，
    其余原样透传交给 chat 门的触发/拒绝逻辑）。"""
    if not isinstance(part, dict):
        raise InvalidParameterError(
            f"input[{index}].content[{part_index}] 必须是对象",
            param=f"input[{index}].content[{part_index}]")
    kind = str(part.get("type") or "")
    if kind in _PART_MAP:
        mapped = dict(part)
        mapped["type"] = _PART_MAP[kind]
        if kind == "input_image" and isinstance(mapped.get("image_url"), str):
            mapped["image_url"] = {"url": mapped["image_url"]}
        return mapped
    if kind == "output_text":
        # assistant 历史里偶见 output_text 形态 —— 等价 text
        return {"type": "text", "text": part.get("text")}
    return part


def new_response_id() -> str:
    return "resp_" + uuid.uuid4().hex


def new_message_id() -> str:
    return "msg_" + uuid.uuid4().hex


def _usage(raw: dict | None) -> dict | None:
    """上游 usage → Responses 键名（input/output/total_tokens，与上游同名，只做白名单）。"""
    if not isinstance(raw, dict):
        return None
    out = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        if isinstance(raw.get(key), int):
            out[key] = raw[key]
    return out or None


def response_object(*, response_id: str, created: int, model: str, text: str,
                    usage: dict | None = None, status: str = "completed",
                    output: list | None = None) -> dict:
    """Responses 对象（`object: "response"`）。`output` 缺省 = 单条 assistant message。"""
    if output is None:
        output = [{
            "type": "message",
            "id": new_message_id(),
            "role": "assistant",
            "status": "completed" if status == "completed" else "in_progress",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }]
    obj: dict = {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": status,
        "model": model,
        "output": output,
    }
    mapped = _usage(usage)
    if mapped:
        obj["usage"] = mapped
    return obj


def created_event(response: dict) -> dict:
    return {"type": "response.created", "response": response}


def delta_event(item_id: str, delta: str) -> dict:
    return {"type": "response.output_text.delta", "item_id": item_id,
            "output_index": 0, "content_index": 0, "delta": delta}


def reasoning_delta_event(item_id: str, delta: str) -> dict:
    """思考摘要增量（Responses 形态）：`response.reasoning_summary_text.delta`。

    数据源 = 上游 `thinking_summary.extra` 的分步标题/要点（真实数据透传，
    2026-09-24 实测；Qwen 门经 chat 流翻译，与 openai_chat 的 reasoning_content 同源）。
    """
    return {"type": "response.reasoning_summary_text.delta", "item_id": item_id,
            "output_index": 0, "summary_index": 0, "delta": delta}


def completed_event(response: dict) -> dict:
    return {"type": "response.completed", "response": response}


__all__ = [
    "completed_event",
    "created_event",
    "delta_event",
    "new_message_id",
    "new_response_id",
    "response_object",
    "to_chat_body",
]
