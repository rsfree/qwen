"""能力回退通道（chat 门 → 方舟 chat）—— 零网络。

触发面与语义（2026-09-24 用户决策 + INTERFACE §10）：
  · tools/tool_choice、文件/音频/视频分段、多图、data: 图片 ⇒ 整单转方舟；
  · 其余降级（temperature 等）不触发；qwen 正常路径完全不受影响；
  · 🔴 **回退模型名对调用方全链路脱敏**：应答 `model` 改写为调用方请求值、
    流式 chunk 逐条清洗、报错报文清洗（含模型名的上游错误只出 `<redacted-model>`）、
    dry-run 打码；回退事实只由 `x-qwen-fallback: ark` 头披露（2026-10-14 起不下发 degradations）；
  · 通道故障 ⇒ 502 响亮失败，不静默降回 qwen。
"""
from __future__ import annotations

import json

from .conftest import AUTH_A, CHAT_PATH

TOOLS_BODY = {"model": "qwen3.7-plus",
              "messages": [{"role": "user", "content": "北京天气"}],
              "tools": [{"type": "function",
                         "function": {"name": "get_weather", "parameters": {}}}]}
FILE_BODY = {"model": "qwen3.7-plus", "messages": [{"role": "user", "content": [
    {"type": "file", "file": {"url": "https://arxiv.org/pdf/1706.03762"}},
    {"type": "text", "text": "总结"}]}]}


def test_tools_request_falls_back(ark_app):
    tc, fake_qwen, fake_ark, _ = ark_app
    resp = tc.post(CHAT_PATH, json=TOOLS_BODY, headers=AUTH_A)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["model"] == "qwen3.7-plus", "回退应答 model 改写为调用方请求值（脱敏）"
    assert data["choices"][0]["message"]["content"] == "（方舟应答）"
    assert "degradations" not in data, "降级说明不再下发（只进服务端日志）"
    assert resp.headers.get("x-qwen-fallback") == "ark"
    assert "doubao-test" not in json.dumps(data), "🔴 应答全文不得出现回退模型名"
    # 方舟收到：model 已替换、tools 原样保留、鉴权头正确
    ark_body = json.loads(fake_ark.requests[0].content)
    assert ark_body["model"] == "doubao-test"
    assert ark_body["tools"] == TOOLS_BODY["tools"]
    assert fake_ark.requests[0].headers["authorization"] == "Bearer ark-test-key"
    # qwen 完全没被打扰
    assert fake_qwen.calls("/api/v2/chats/new") == []


def test_file_part_falls_back(ark_app):
    """上传链未启用 + 回退启用 ⇒ 文件附件转方舟（历史兼容口径）。"""
    tc, _, _, _ = ark_app
    resp = tc.post(CHAT_PATH, json=FILE_BODY, headers=AUTH_A)
    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] == "qwen3.7-plus"
    assert "doubao-test" not in json.dumps(data)


def test_multi_image_falls_back(ark_app):
    """多图 ⇒ 回退方舟（qwen 单图规则；多图未验证）。"""
    tc, _, _, _ = ark_app
    multi = {"model": "m", "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://cdn.qwenlm.ai/a.png"}},
        {"type": "image_url", "image_url": {"url": "https://cdn.qwenlm.ai/b.png"}},
        {"type": "text", "text": "对比"}]}]}
    resp = tc.post(CHAT_PATH, json=multi, headers=AUTH_A)
    assert resp.status_code == 200
    assert resp.json()["model"] == "m"


def test_data_image_falls_back_when_upload_disabled(ark_app):
    """data: 图片本应走上传链；上传未启用但回退已配置 ⇒ 优雅转方舟（方舟视觉支持 base64）。"""
    tc, _, _, _ = ark_app
    datauri = {"model": "m", "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        {"type": "text", "text": "看图"}]}]}
    resp = tc.post(CHAT_PATH, json=datauri, headers=AUTH_A)
    assert resp.status_code == 200
    assert resp.headers.get("x-qwen-fallback") == "ark"


def test_plain_text_stays_on_qwen(ark_app):
    tc, fake_qwen, fake_ark, _ = ark_app
    resp = tc.post(CHAT_PATH, json={"model": "qwen3.7-plus",
                                    "messages": [{"role": "user", "content": "你好"}]},
                   headers=AUTH_A)
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "你好，世界"
    assert "x-qwen-fallback" not in resp.headers
    assert fake_ark.requests == [], "普通文本不得走回退"


def test_unsupported_sampling_params_do_not_fall_back(ark_app):
    tc, fake_qwen, fake_ark, _ = ark_app
    resp = tc.post(CHAT_PATH, json={"model": "qwen3.7-plus", "temperature": 0.5,
                                    "messages": [{"role": "user", "content": "你好"}]},
                   headers=AUTH_A)
    assert resp.status_code == 200
    assert fake_ark.requests == [], "采样参数降级不触发回退"
    assert "degradations" not in resp.json(), "降级说明不再下发（只进服务端日志）"


def test_fallback_stream_passthrough_scrubbed(ark_app):
    tc, _, _, _ = ark_app
    with tc.stream("POST", CHAT_PATH, json={**TOOLS_BODY, "stream": True},
                   headers=AUTH_A) as resp:
        assert resp.status_code == 200
        assert resp.headers.get("x-qwen-fallback") == "ark"
        raw = "".join(resp.iter_text())
    assert '"object": "chat.completion.chunk"' in raw
    assert "（方舟流式）" in raw
    assert raw.endswith("data: [DONE]\n\n")
    assert "doubao-test" not in raw, "🔴 流式 chunk 逐条清洗，回退模型名不得外泄"
    assert '"model": "qwen3.7-plus"' in raw, "chunk 内 model 已改写为调用方请求值"


def test_fallback_failure_is_loud_and_sanitized(ark_app):
    """通道故障 ⇒ 502；上游报文里即使含模型名，调用方看到的也是 <redacted-model>。"""
    tc, _, fake_ark, _ = ark_app
    fake_ark.fail = True
    resp = tc.post(CHAT_PATH, json=TOOLS_BODY, headers=AUTH_A)
    assert resp.status_code == 502
    message = resp.json()["error"]["message"]
    assert "回退通道" in message
    assert "<redacted-model>" in message, "上游报文中的模型名必须被清洗"
    assert "doubao-test" not in message, "🔴 报错报文不得外泄回退模型名"


def test_fallback_429_keeps_rate_limit_semantics(ark_app):
    """方舟限流（RequestBurstTooFast）且**备用链全部被限** ⇒ 429 + rate_limit_error 语义。"""
    tc, _, fake_ark, _ = ark_app
    fake_ark.rate_limited_for = {"doubao-test", "doubao-test-turbo"}
    resp = tc.post(CHAT_PATH, json=TOOLS_BODY, headers=AUTH_A)
    assert resp.status_code == 429, f"429 语义应保留（不是 502）: {resp.status_code}"
    err = resp.json()["error"]
    assert err["type"] == "rate_limit_error"
    assert "RequestBurstTooFast" in err["message"]
    assert "Retry-After" in resp.headers
    assert len(fake_ark.requests) == 2, "主备两跳都被限流（failover 链完整尝试）"
    assert "doubao" not in err["message"], "🔴 报错报文不得外泄回退模型名"


def test_fallback_multi_model_failover_on_429(ark_app):
    """🔴 多模型兜底链：主模型 429 ⇒ 按配置顺序自动切换备用模型重试。"""
    tc, fake_qwen, fake_ark, settings = ark_app
    fake_ark.rate_limited_for = {"doubao-test"}          # 只限主模型
    resp = tc.post(CHAT_PATH, json=TOOLS_BODY, headers=AUTH_A)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["model"] == "qwen3.7-plus", "failover 应答同样回显调用方请求模型"
    assert data["choices"][0]["message"]["content"] == "（方舟应答）"
    assert "doubao-test" not in json.dumps(data) and "doubao-test-turbo" not in json.dumps(data), \
        "🔴 备用模型名同样脱敏"
    # 方舟收到两次请求：第一次主模型（429），第二次备用模型
    assert len(fake_ark.requests) == 2
    models = [json.loads(r.content)["model"] for r in fake_ark.requests]
    assert models == ["doubao-test", "doubao-test-turbo"], "按配置顺序 failover"
    assert fake_qwen.calls("/api/v2/chats/new") == []


def test_tool_state_messages_trigger_fallback_without_tools_key(ark_app):
    """🔴 函数调用是多轮闭环：role:tool / assistant.tool_calls 消息即使本跳没带 tools
    也必须回退（qwen 无法表达这两种形态）。"""
    tc, _, _, _ = ark_app
    history = [
        {"role": "user", "content": "北京天气？"},
        {"role": "assistant", "tool_calls": [{"id": "call_x", "type": "function",
                                              "function": {"name": "get_weather",
                                                           "arguments": "{\"city\":\"北京\"}"}}]},
        {"role": "tool", "tool_call_id": "call_x", "content": "{\"weather\":\"晴\"}"},
    ]
    for label, body in (("role:tool 消息（无 tools）", {"model": "qwen3.7-plus", "messages": history}),
                        ("assistant.tool_calls", {"model": "qwen3.7-plus",
                                                  "messages": history[:2] + [{"role": "user", "content": "继续"}]})):
        resp = tc.post(CHAT_PATH, json=body, headers=AUTH_A)
        assert resp.status_code == 200, f"{label}: {resp.text[:200]}"
        data = resp.json()
        assert data["model"] == "qwen3.7-plus", label
        assert "doubao-test" not in json.dumps(data), label


def test_tool_state_without_fallback_is_actionable_400(client_app):
    """工具状态消息 + 未配置回退 ⇒ 400 且指明"配置 ARK_FALLBACK_* 可支持"。"""
    tc, _, _, _ = client_app
    body = {"model": "m", "messages": [
        {"role": "user", "content": "北京天气？"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "type": "function",
                                                             "function": {"name": "f",
                                                                          "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c", "content": "{}"},
        {"role": "user", "content": "继续"}]}
    resp = tc.post(CHAT_PATH, json=body, headers=AUTH_A)
    assert resp.status_code == 400
    assert "ARK_FALLBACK" in resp.json()["error"]["message"]


def test_fallback_disabled_keeps_old_behavior(client_app):
    """未配置回退且未启用上传 ⇒ tools 照旧降级、文件分段 400（指明 QWEN_UPLOAD_ENABLED）。"""
    tc, fake, _, _ = client_app
    resp = tc.post(CHAT_PATH, json=TOOLS_BODY, headers=AUTH_A)
    assert resp.status_code == 200
    data = resp.json()
    assert "x-qwen-fallback" not in resp.headers
    assert "degradations" not in data, "降级说明不再下发（只进服务端日志）"
    resp = tc.post(CHAT_PATH, json=FILE_BODY, headers=AUTH_A)
    assert resp.status_code == 400
    assert "QWEN_UPLOAD_ENABLED" in resp.json()["error"]["message"]


def test_fallback_dry_run_masks_model(ark_app):
    tc, _, fake_ark, _ = ark_app
    resp = tc.post(CHAT_PATH, json=TOOLS_BODY, headers={**AUTH_A, "X-Avm-Dry-Run": "1"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["channel"] == "ark-fallback"
    assert data["upstream"]["body"]["model"] == "<redacted>"
    assert data["upstream"]["headers"]["Authorization"] == "Bearer <redacted>"
    assert fake_ark.requests == [], "dry_run 不得触方舟"
