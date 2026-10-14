"""OpenAI Responses 门（`POST /v1/responses`）—— 零网络。

语义（2026-09-24 用户指令「qwen response 也适配下吧」+「response 调用工具兜底用方舟 /responses」）：
  · qwen 路径：input/instructions → chat 门同款翻译（t2t + 单图解析）；
    应答 = `object:"response"`；流式 = created → output_text.delta → completed；
  · 回退路径：tools / 工具调用状态输入项 / 不支持分段 / 多图 / data: 图
    ⇒ 整单转方舟 `/responses`（原生 tools + web_search），应答原样透传 + 回退头；
  · 🔴 回退模型名全链路脱敏：应答 `model` 改写为调用方请求值、流式/报错/dry-run 同样清洗。
"""
from __future__ import annotations

import json

import pytest

from app import openai_responses
from app.errors import InvalidParameterError

from .conftest import AUTH_A, CHAT_PATH

RESPONSES_PATH = "/v1/responses"
RESP_BODY = {"model": "qwen3.7-plus", "input": "你好"}


# ---------------------------------------------------------------- 转换层（纯函数）

def test_string_input_and_instructions():
    chat_body, reason = openai_responses.to_chat_body(
        {"instructions": "你是助手", "input": "你好"})
    assert reason is None
    assert chat_body["messages"] == [{"role": "system", "content": "你是助手"},
                                     {"role": "user", "content": "你好"}]
    assert "input" not in chat_body and "instructions" not in chat_body


def test_input_items_with_parts():
    chat_body, reason = openai_responses.to_chat_body({"input": [
        {"role": "user", "content": [
            {"type": "input_image", "image_url": "https://cdn.qwenlm.ai/a.png"},
            {"type": "input_text", "text": "图里是什么"}]},
        {"role": "assistant", "content": [{"type": "output_text", "text": "潜水员"}]},
        {"role": "user", "content": "继续"}]})
    assert reason is None
    roles = [(m["role"], m["content"]) for m in chat_body["messages"]]
    assert roles[0] == ("user", [{"type": "image_url",
                                  "image_url": {"url": "https://cdn.qwenlm.ai/a.png"}},
                                 {"type": "text", "text": "图里是什么"}])
    assert roles[1] == ("assistant", [{"type": "text", "text": "潜水员"}])


def test_tool_state_items_mark_reason():
    chat_body, reason = openai_responses.to_chat_body({"input": [
        {"type": "function_call", "call_id": "c1", "name": "f"},
        {"type": "function_call_output", "call_id": "c1", "output": "{}"},
        {"role": "user", "content": "继续"}]})
    assert reason and "function_call" in reason
    assert [m["role"] for m in chat_body["messages"]] == ["user"], "工具状态项被剔除"


def test_bad_input_raises():
    with pytest.raises(InvalidParameterError):
        openai_responses.to_chat_body({"input": 42})


def test_response_object_shape():
    obj = openai_responses.response_object(
        response_id="resp_x", created=1, model="qwen3.7-plus", text="hi",
        usage={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5})
    assert obj["object"] == "response" and obj["status"] == "completed"
    assert obj["output"][0]["content"][0]["type"] == "output_text"
    assert obj["usage"] == {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}
    assert "degradations" not in obj, "降级说明不下发（2026-10-14 用户口径）"
    minimal = openai_responses.response_object(response_id="r", created=1, model="m", text="")
    assert "usage" not in minimal and "degradations" not in minimal


# ---------------------------------------------------------------- 路由（qwen 路径）

def test_responses_non_stream_route(client_app):
    tc, fake, _, _ = client_app
    resp = tc.post(RESPONSES_PATH, json=RESP_BODY, headers=AUTH_A)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["object"] == "response" and data["status"] == "completed"
    assert data["model"] == "qwen3.7-plus"
    assert data["output"][0]["content"][0]["text"] == "你好，世界"
    assert data["usage"] == {"input_tokens": 2421, "output_tokens": 12, "total_tokens": 2433}
    assert fake.bodies("/api/v2/chat/completions"), "qwen 路径真实触达上游"


def test_responses_stream_route(client_app):
    tc, _, _, _ = client_app
    with tc.stream("POST", RESPONSES_PATH, json={**RESP_BODY, "stream": True},
                   headers=AUTH_A) as resp:
        assert resp.status_code == 200
        raw = "".join(resp.iter_text())
    events = [json.loads(line[len("data:"):]) for line in raw.splitlines()
              if line.startswith("data:")]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "response.created" and kinds[-1] == "response.completed"
    assert "response.output_text.delta" in kinds
    deltas = "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta")
    assert deltas == "你好，世界"
    completed = events[-1]["response"]
    assert completed["status"] == "completed"
    assert completed["output"][0]["content"][0]["text"] == "你好，世界"


def test_responses_image_route(client_app):
    tc, fake, _, _ = client_app
    body = {"model": "qwen3.7-plus", "input": [{"role": "user", "content": [
        {"type": "input_image", "image_url": "https://cdn.qwenlm.ai/a.png"},
        {"type": "input_text", "text": "图里是什么"}]}]}
    resp = tc.post(RESPONSES_PATH, json=body, headers=AUTH_A)
    assert resp.status_code == 200
    files = fake.bodies("/api/v2/chat/completions")[0]["messages"][0]["files"]
    assert files and files[0]["file_class"] == "vision"


def test_responses_requires_key(client_app):
    tc, _, _, _ = client_app
    assert tc.post(RESPONSES_PATH, json=RESP_BODY).status_code == 401


def test_responses_dry_run_zero_calls(client_app):
    tc, fake, _, _ = client_app
    resp = tc.post(RESPONSES_PATH, json=RESP_BODY,
                   headers={**AUTH_A, "X-Avm-Dry-Run": "1"})
    assert resp.status_code == 200
    assert resp.json()["dry_run"] is True
    assert "degradations" not in resp.json()
    assert fake.requests == []


# ---------------------------------------------------------------- 回退路径（脱敏）

def test_responses_tools_fall_back_to_ark(ark_app):
    tc, fake_qwen, fake_ark, _ = ark_app
    body = {"model": "qwen3.7-plus",
            "input": [{"role": "user", "content": "搜一下今天的新闻"}],
            "tools": [{"type": "web_search", "max_keyword": 3}]}
    resp = tc.post(RESPONSES_PATH, json=body, headers=AUTH_A)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["model"] == "qwen3.7-plus", "回退应答 model 改写为调用方请求值（脱敏）"
    assert data["output"][0]["content"][0]["text"] == "（方舟 responses 应答）"
    assert "degradations" not in data, "降级说明不下发（只进服务端日志）"
    assert resp.headers.get("x-qwen-fallback") == "ark"
    assert "doubao-test" not in json.dumps(data), "🔴 应答全文不得出现回退模型名"
    ark_body = json.loads(fake_ark.requests[0].content)
    assert ark_body["tools"] == body["tools"], "tools 原样透传（方舟原生执行）"
    assert ark_body["model"] == "doubao-test"
    assert fake_qwen.calls("/api/v2/chats/new") == [], "qwen 未被打扰"


def test_responses_tool_state_items_fall_back(ark_app):
    tc, _, _, _ = ark_app
    body = {"model": "m", "input": [
        {"type": "function_call", "call_id": "c1", "name": "f"},
        {"type": "function_call_output", "call_id": "c1", "output": "{}"},
        {"role": "user", "content": "继续"}]}
    resp = tc.post(RESPONSES_PATH, json=body, headers=AUTH_A)
    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] == "m"
    assert "doubao-test" not in json.dumps(data)


def test_responses_stream_fall_back_scrubbed(ark_app):
    tc, _, _, _ = ark_app
    with tc.stream("POST", RESPONSES_PATH,
                   json={**RESP_BODY, "stream": True,
                         "tools": [{"type": "web_search"}]},
                   headers=AUTH_A) as resp:
        assert resp.status_code == 200
        assert resp.headers.get("x-qwen-fallback") == "ark"
        raw = "".join(resp.iter_text())
    assert '"response.output_text.delta"' in raw
    assert "（方舟 responses 流式）" in raw
    assert "doubao-test" not in raw, "🔴 流式事件逐条清洗，回退模型名不得外泄"
    assert '"model": "qwen3.7-plus"' in raw


def test_responses_tools_without_fallback_degrade(client_app):
    """未配置回退通道 ⇒ tools 降级（同 chat 门语义），不 400。"""
    tc, _, _, _ = client_app
    resp = tc.post(RESPONSES_PATH,
                   json={**RESP_BODY, "tools": [{"type": "web_search"}]},
                   headers=AUTH_A)
    assert resp.status_code == 200
    assert "degradations" not in resp.json()


def test_responses_tool_state_without_fallback_is_actionable_400(client_app):
    """工具状态输入项 + 未配置回退 ⇒ 400 且指明"配置 ARK_FALLBACK_* 可支持"。"""
    tc, _, _, _ = client_app
    body = {"model": "m", "input": [
        {"type": "function_call", "call_id": "c1", "name": "f"},
        {"role": "user", "content": "继续"}]}
    resp = tc.post(RESPONSES_PATH, json=body, headers=AUTH_A)
    assert resp.status_code == 400
    assert "ARK_FALLBACK" in resp.json()["error"]["message"]


def test_responses_fallback_failure_is_loud_and_sanitized(ark_app):
    tc, _, fake_ark, _ = ark_app
    fake_ark.fail = True
    resp = tc.post(RESPONSES_PATH,
                   json={**RESP_BODY, "tools": [{"type": "web_search"}]},
                   headers=AUTH_A)
    assert resp.status_code == 502
    message = resp.json()["error"]["message"]
    assert "回退通道" in message
    assert "<redacted-model>" in message
    assert "doubao-test" not in message, "🔴 报错报文不得外泄回退模型名"


def test_responses_dry_run_fallback_masks_model(ark_app):
    tc, _, fake_ark, _ = ark_app
    resp = tc.post(RESPONSES_PATH,
                   json={**RESP_BODY, "tools": [{"type": "web_search"}]},
                   headers={**AUTH_A, "X-Avm-Dry-Run": "1"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["channel"] == "ark-fallback"
    assert data["upstream"]["url"].endswith("/responses")
    assert data["upstream"]["body"]["model"] == "<redacted>"
    assert fake_ark.requests == []


def test_chat_path_untouched(client_app):
    """chat 门不受本门影响（回归锚）。"""
    tc, _, _, _ = client_app
    resp = tc.post(CHAT_PATH, json={"model": "qwen3.7-plus",
                                    "messages": [{"role": "user", "content": "你好"}]},
                   headers=AUTH_A)
    assert resp.status_code == 200
    assert resp.json()["object"] == "chat.completion"
