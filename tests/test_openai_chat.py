"""OpenAI chat 门（`POST /v1/chat/completions`）+ `/v1/models` 注册 —— 零网络。

范围（2026-09-24）：t2t 文本 + **单张图片解析**（上游实测，UPSTREAM §4.5）；
文件/音频/视频解析明确 400（外链附件上游拒绝，U-15）。
🔴 chat 不消耗视频额度（day_used 必须恒 0）；上游 usage 真实透传（不编造、缺键不出）。
"""
from __future__ import annotations

import json
import time

import pytest

from app import media, openai_chat
from app.errors import QuotaExhaustedError
from app.models import ChatModelRegistry
from app.store import TaskStore
from app.upstream.qwen.accounts import AccountPool
from app.upstream.qwen.client import CHAT_FEATURE_CONFIG, QwenClient, extract_stream_text

from .conftest import AUTH_A, CHAT_PATH, DEFAULT_MODELS, SSE_ERROR_EVENT, _model_item

CHAT_BODY = {"model": "qwen3.7-plus", "messages": [{"role": "user", "content": "你好"}]}
UPSTREAM_IMAGE = "https://cdn.qwenlm.ai/output/u/pic.png"


# ---------------------------------------------------------------- 翻译层（纯函数）

def test_single_turn_verbatim():
    req = openai_chat.parse_openai_chat_request(dict(CHAT_BODY))
    assert req.prompt == "你好"
    assert req.model == "qwen3.7-plus"
    assert req.files == []
    assert req.degradations == []
    assert req.stream is False


def test_provider_prefix_stripped_and_echoed():
    req = openai_chat.parse_openai_chat_request({"model": "qwen/qwen3.8-max", "messages": [
        {"role": "user", "content": "hi"}]})
    assert req.model == "qwen3.8-max"
    assert req.model_requested == "qwen/qwen3.8-max"


def test_multi_turn_flattens_with_degradation():
    req = openai_chat.parse_openai_chat_request({
        "model": "m",
        "messages": [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "叫什么"},
            {"role": "assistant", "content": "美女"},
            {"role": "user", "content": "你好"},
        ]})
    assert req.prompt.startswith("【系统指令】")
    assert "User: 叫什么" in req.prompt
    assert "Assistant: 美女" in req.prompt
    assert req.prompt.endswith("User: 你好")
    assert req.degradations and "拍平" in req.degradations[0]


def test_image_part_becomes_file_entry():
    req = openai_chat.parse_openai_chat_request({
        "model": "m",
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": UPSTREAM_IMAGE}},
            {"type": "text", "text": "这张图里是什么？"},
        ]}]})
    assert req.prompt == "这张图里是什么？"
    assert req.files == [media.image_entry(UPSTREAM_IMAGE)]
    assert req.files[0]["file_class"] == "vision"
    assert req.degradations == [], "上游域内图 = 已实测路径，不告警"


def test_third_party_image_warns():
    req = openai_chat.parse_openai_chat_request({
        "model": "m",
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
            {"type": "text", "text": "图里是什么"},
        ]}]})
    assert req.degradations and "example.com" in req.degradations[0]


def test_image_rules_multiplicity_and_data_uri_upload():
    from app.errors import InvalidParameterError

    def body(urls):
        return {"model": "m", "messages": [{"role": "user", "content": [
            *[{"type": "image_url", "image_url": {"url": u}} for u in urls],
            {"type": "text", "text": "看图"}]}]}

    with pytest.raises(InvalidParameterError):   # 多图：多附件未验证
        openai_chat.parse_openai_chat_request(body([UPSTREAM_IMAGE,
                                                    "https://cdn.qwenlm.ai/b.png"]))
    # data: URI 图片 ⇒ 待上传附件（走上传链，不再 400）
    req = openai_chat.parse_openai_chat_request(body(["data:image/png;base64,AAAA"]))
    assert req.attachment == ("image", "data:image/png;base64,AAAA")
    assert req.files == []


def test_image_only_on_last_user_message():
    from app.errors import InvalidParameterError

    part = [{"type": "image_url", "image_url": {"url": UPSTREAM_IMAGE}},
            {"type": "text", "text": "看"}]

    # 更早轮次：拍平会丢 ⇒ 400 不静默丢输入
    with pytest.raises(InvalidParameterError):
        openai_chat.parse_openai_chat_request({"model": "m", "messages": [
            {"role": "user", "content": part}, {"role": "user", "content": "再来"}]})
    # assistant 消息不能带图
    with pytest.raises(InvalidParameterError):
        openai_chat.parse_openai_chat_request({"model": "m", "messages": [
            {"role": "assistant", "content": part}]})


def test_document_audio_video_parts_become_attachments():
    """文件/音频/视频分段 ⇒ 待上传附件（2026-09-24 起走上传链，不再 400——用户指令「走他的上传」）。"""
    cases = (
        ({"type": "file", "file": {"url": "https://arxiv.org/pdf/1706.03762"}},
         ("file", "https://arxiv.org/pdf/1706.03762")),
        ({"type": "input_file", "file": {"url": "https://arxiv.org/pdf/1706.03762"}},
         ("file", "https://arxiv.org/pdf/1706.03762")),
        ({"type": "file_url", "file_url": {"file_url": "https://arxiv.org/pdf/1706.03762"}},
         ("file", "https://arxiv.org/pdf/1706.03762")),
        ({"type": "input_audio", "input_audio": {"data": "QUFB", "format": "wav"}},
         ("audio", "data:audio/wav;base64,QUFB")),
        ({"type": "audio_url", "audio_url": {"url": "https://example.com/a.wav"}},
         ("audio", "https://example.com/a.wav")),
        ({"type": "video_url", "video_url": {"url": "https://example.com/v.mp4"}},
         ("video", "https://example.com/v.mp4")),
    )
    for part, expected in cases:
        req = openai_chat.parse_openai_chat_request({
            "model": "m",
            "messages": [{"role": "user", "content": [part, {"type": "text", "text": "x"}]}]})
        assert req.attachment == expected, part


def test_attachment_rules_single_and_last_message():
    from app.errors import InvalidParameterError

    def body(content, last=True):
        msg = {"role": "user", "content": content}
        messages = [msg, {"role": "user", "content": "hi"}] if not last else [msg]
        return {"model": "m", "messages": messages}

    part = [{"type": "file", "file": {"url": "https://arxiv.org/pdf/x.pdf"}},
            {"type": "text", "text": "看"}]
    with pytest.raises(InvalidParameterError):   # 更早轮次：拍平会丢 ⇒ 400 不静默丢输入
        openai_chat.parse_openai_chat_request(body(part, last=False))
    with pytest.raises(InvalidParameterError):   # 图片 + 文件 = 2 个附件
        openai_chat.parse_openai_chat_request({"model": "m", "messages": [
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": UPSTREAM_IMAGE}},
                {"type": "file", "file": {"url": "https://arxiv.org/pdf/x.pdf"}},
                {"type": "text", "text": "x"}]}]})
    with pytest.raises(InvalidParameterError):   # 缺来源 URL
        openai_chat.parse_openai_chat_request({"model": "m", "messages": [
            {"role": "user", "content": [{"type": "file", "file": {}}]}]})


def test_rejects_bad_requests():
    from app.errors import InvalidParameterError

    with pytest.raises(InvalidParameterError):
        openai_chat.parse_openai_chat_request({"model": "m", "messages": []})
    with pytest.raises(InvalidParameterError):
        openai_chat.parse_openai_chat_request({"messages": [{"role": "user", "content": "x"}]})
    with pytest.raises(InvalidParameterError):
        openai_chat.parse_openai_chat_request({"model": "m", "messages": [
            {"role": "tool", "content": "x"}]})
    with pytest.raises(InvalidParameterError):   # 视频模型指向视频门
        openai_chat.parse_openai_chat_request({"model": "qwen/video", "messages": [
            {"role": "user", "content": "x"}]})


def test_unsupported_params_degrade():
    req = openai_chat.parse_openai_chat_request(
        {**CHAT_BODY, "temperature": 0.5, "tools": [{"type": "function"}]})
    joined = "；".join(req.degradations)
    assert "temperature" in joined and "tools" in joined


def test_submit_body_matches_capture_shape(piped):
    """t2t 提交体逐字对齐 2026-09-24 抓包 + 真实一发证词（U-13 实测）：
    chatId/chat_id 双写、无 size、thinking 开、三处同标、id=null、files 恒在。"""
    service, _, _, client, _ = piped
    body = client.build_chat_submit_body("chat-1", model="qwen3.7-plus", prompt="你好")
    assert set(body) == {"stream", "version", "incremental_output", "chatId", "chat_id",
                         "parentId", "parent_id", "chat_mode", "model", "messages", "timestamp"}
    assert body["chatId"] == body["chat_id"] == "chat-1"
    assert body["stream"] is True
    assert "size" not in body
    message = body["messages"][0]
    assert message["chat_type"] == "t2t"
    assert message["sub_chat_type"] == "t2t"
    assert message["extra"]["meta"]["subChatType"] == "t2t"
    assert "size" not in message["extra"]["meta"]
    assert message["feature_config"] == CHAT_FEATURE_CONFIG
    assert message["feature_config"]["thinking_enabled"] is True
    assert message["id"] is None, "真实一发证词：message id 为 null（同视频体）"
    assert message["files"] == [], "files 恒在（纯文本为空数组）"
    assert message["content"] == "你好"


def test_extract_stream_text_paths():
    assert extract_stream_text({"choices": [{"delta": {"content": "a"}}]}) == "a"
    assert extract_stream_text({"choices": [{"message": {"content": "b"}}]}) == "b"
    assert extract_stream_text({"data": {"choices": [{"delta": {"content": "c"}}]}}) == "c"
    assert extract_stream_text({"choices": [{"delta": {}}]}) == ""
    assert extract_stream_text("noise") == ""
    # thinking 摘要事件 content 恒空 ⇒ 不提取；旁路字段不提取
    assert extract_stream_text({"choices": [{"delta": {"content": "",
                                                       "phase": "thinking_summary"}}]}) == ""
    assert extract_stream_text({"choices": [{"delta": {"reasoning_content": "思考"}}]}) == ""


# ---------------------------------------------------------------- 路由（非流式）

def test_chat_non_stream_route(client_app):
    test_client, fake, _, _ = client_app
    resp = test_client.post(CHAT_PATH, json=CHAT_BODY, headers=AUTH_A)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["object"] == "chat.completion"
    assert data["model"] == "qwen3.7-plus"
    assert data["choices"][0]["message"]["content"] == "你好，世界"
    assert data["choices"][0]["finish_reason"] == "stop"
    # usage = 上游真值改名映射（UPSTREAM §4.5 实测形态），取最后一份
    assert data["usage"] == {"prompt_tokens": 2421, "completion_tokens": 12,
                             "total_tokens": 2433}

    # 上游收到：先建会话（t2t + 指定模型），再流式提交
    new_chats = fake.bodies("/api/v2/chats/new")
    assert len(new_chats) == 1
    assert new_chats[0]["chat_type"] == "t2t"
    assert new_chats[0]["models"] == ["qwen3.7-plus"]
    submits = fake.bodies("/api/v2/chat/completions")
    assert len(submits) == 1
    assert submits[0]["messages"][0]["chat_type"] == "t2t"
    assert submits[0]["stream"] is True

    # 🔴 chat 不消耗视频额度：day_used 必须仍为 0
    stats = test_client.get("/stats").json()
    assert all(a["day_used"] == 0 for a in stats["accounts"]["accounts"])


def test_chat_image_route_passes_files(client_app):
    """图片解析：image_url 分段 → 上游 files[0]（§4.2 形状，2026-09-24 实测可用）。"""
    test_client, fake, _, _ = client_app
    body = {"model": "qwen3.7-plus", "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": UPSTREAM_IMAGE}},
        {"type": "text", "text": "这张图片里是什么？"}]}]}
    resp = test_client.post(CHAT_PATH, json=body, headers=AUTH_A)
    assert resp.status_code == 200, resp.text
    submits = fake.bodies("/api/v2/chat/completions")
    files = submits[0]["messages"][0]["files"]
    assert len(files) == 1
    assert files[0]["type"] == "image" and files[0]["file_class"] == "vision"
    assert files[0]["url"] == UPSTREAM_IMAGE
    assert files[0]["status"] == "uploaded"


def test_chat_does_not_touch_video_quota_when_video_capped(piped):
    """视频额度打满 ⇒ 视频门排队/429，但 chat 门照常可用（取号不看 day_used）。"""
    service, store, pool, _, _ = piped
    for _ in range(3):
        pool.report_submitted("a@x.cn")
        pool.report_submitted("b@x.cn")
    req = openai_chat.parse_openai_chat_request(dict(CHAT_BODY))
    assert "".join(t for k, t in service.chat_stream(req) if k == "answer") == "你好，世界"


def test_chat_quota_error_cools_short(piped, monkeypatch):
    """chat 链路的额度语义错误按 refused（60s）短冷 —— 不把账号冷到 UTC 日界（那是视频语义）。

    注入点选 `client.new_chat`（在 chat_stream 的 try 块**内**）——模拟上游对 chat 写端点
    回了额度语义错误。取号阶段的失败（无号可冷却）本来就不该触发回报。
    """
    service, store, pool, client, fake = piped

    def boom(token, **kwargs):
        raise QuotaExhaustedError("上游额度已用尽")

    monkeypatch.setattr(client, "new_chat", boom)
    req = openai_chat.parse_openai_chat_request(dict(CHAT_BODY))
    with pytest.raises(QuotaExhaustedError):
        "".join(t for k, t in service.chat_stream(req) if k == "answer")
    state = pool.get_state("a@x.cn")
    assert state.cooldown_reason == "refused"
    assert state.cooldown_until - time.time() < 120, "必须是短冷，不是冷到 UTC 日界"


def test_chat_requires_key(client_app):
    test_client, _, _, _ = client_app
    resp = test_client.post(CHAT_PATH, json=CHAT_BODY)
    assert resp.status_code == 401
    err = resp.json()["error"]
    assert err["type"] == "authentication_error"
    assert "Request ID:" in err["message"]
    resp = test_client.post(CHAT_PATH, json=CHAT_BODY, headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_chat_dry_run_zero_calls(client_app):
    test_client, fake, _, _ = client_app
    resp = test_client.post(CHAT_PATH, json=CHAT_BODY,
                            headers={**AUTH_A, "X-Avm-Dry-Run": "1"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["dry_run"] is True
    assert data["upstream"]["body"]["messages"][0]["chat_type"] == "t2t"
    assert data["upstream"]["headers"]["Cookie"] == "token=<redacted>"
    assert fake.requests == [], "dry_run 不得触上游"


def test_chat_zero_extraction_is_loud(client_app):
    """零提取 ⇒ 502 响亮失败（绝不静默回空内容）。"""
    test_client, fake, _, _ = client_app
    fake.chat_sse_body = 'data: {"choices":[{"delta":{"content":"","phase":"answer"}}]}\n\n'
    resp = test_client.post(CHAT_PATH, json=CHAT_BODY, headers=AUTH_A)
    assert resp.status_code == 502
    err = resp.json()["error"]
    assert err["type"] == "server_error"
    assert "未提取到任何文本" in err["message"]


def test_chat_stream_error_event_is_loud(client_app):
    """流内错误事件（HTTP 200 包错误）⇒ 502 响亮失败，带上游错误内容。"""
    test_client, fake, _, _ = client_app
    fake.chat_sse_body = SSE_ERROR_EVENT
    resp = test_client.post(CHAT_PATH, json=CHAT_BODY, headers=AUTH_A)
    assert resp.status_code == 502
    assert "invalid_input" in resp.json()["error"]["message"]


def test_chat_stream_route(client_app):
    test_client, _, _, _ = client_app
    with test_client.stream("POST", CHAT_PATH, json={**CHAT_BODY, "stream": True},
                            headers=AUTH_A) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        raw = "".join(resp.iter_text())
    events = [line[len("data:"):].strip()
              for line in raw.splitlines() if line.startswith("data:")]
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    assert chunks[0]["object"] == "chat.completion.chunk"
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    texts = [c["choices"][0]["delta"].get("content", "") for c in chunks[1:-1]]
    # 🔴 thinking 心跳：思考档（默认 auto）在正文前先发一个空格增量（变相加速首字）
    assert texts[0] == " "
    assert "".join(texts[1:]) == "你好，世界"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"] == {"prompt_tokens": 2421, "completion_tokens": 12,
                                   "total_tokens": 2433}
    assert all(c["model"] == "qwen3.7-plus" for c in chunks)


def test_stream_ping_comments_during_silence(settings, fake_upstream):
    """🔴 curl 92 根治：上游静默超过 ping_interval ⇒ 流里出现 SSE 注释 `: ping`
    （对 OpenAI 解析器不可见，但让中间层永不静默）。"""
    import dataclasses

    from fastapi.testclient import TestClient

    from app.main import create_app

    s = dataclasses.replace(settings, ping_interval=0.2)
    fake_upstream.chat_sse_delay = 0.6   # 每行 SSE 间隔 0.6s ⇒ 静默窗口触发 ping
    store = TaskStore(s.task_db)
    pool = AccountPool(s, mint=lambda account: f"tok-{account.email}")
    client = QwenClient(s, transport=fake_upstream.transport())
    app = create_app(s, store=store, pool=pool, client=client)
    with TestClient(app) as tc:
        with tc.stream("POST", CHAT_PATH, json={**CHAT_BODY, "stream": True},
                       headers=AUTH_A) as resp:
            raw = "".join(resp.iter_text())
    assert ": ping" in raw, "静默期应出现 SSE 注释心跳"
    assert "你好，世界" in raw.replace(" ", "") or "你好" in raw, "正文不受 ping 影响"


SSE_THINKING_EXTRA = (
    'data: {"response.created":{"chat_id":"chat-1"}}\n\n'
    'data: {"choices":[{"delta":{"role":"assistant","content":"","phase":"thinking_summary",'
    '"status":"typing","extra":{"summary_title":{"content":["分析问题"]},'
    '"summary_thought":{"content":["第一步要点"]}}}}]}\n\n'
    'data: {"choices":[{"delta":{"role":"assistant","content":"","phase":"thinking_summary",'
    '"status":"typing","extra":{"summary_title":{"content":["分析问题","补充标题"]},'
    '"summary_thought":{"content":["第一步要点","第二步要点"]}}}}]}\n\n'
    'data: {"choices":[{"delta":{"content":"答案","phase":"answer","role":"assistant",'
    '"status":"typing"}}]}\n\n'
    'data: {"choices":[{"delta":{"content":"","phase":"answer","role":"assistant",'
    '"status":"finished"}}]}\n\n'
)


def test_reasoning_content_passthrough(client_app):
    """🔴 思考摘要透传：thinking_summary.extra 的分步增量 → delta.reasoning_content
    （真实上游数据，官网 UI 同款；diff 只发新增条目）。"""
    test_client, fake, _, _ = client_app
    fake.chat_sse_body = SSE_THINKING_EXTRA
    with test_client.stream("POST", CHAT_PATH, json={**CHAT_BODY, "stream": True},
                            headers=AUTH_A) as resp:
        raw = "".join(resp.iter_text())
    events = [json.loads(ln[len("data:"):]) for ln in raw.splitlines()
              if ln.startswith("data:") and "[DONE]" not in ln]
    reasoning = [e["choices"][0]["delta"].get("reasoning_content") for e in events
                 if e.get("choices") and "reasoning_content" in e["choices"][0]["delta"]]
    assert reasoning == ["【分析问题】\n第一步要点", "【补充标题】\n第二步要点"], reasoning
    content = "".join(e["choices"][0]["delta"].get("content") or ""
                      for e in events if e.get("choices"))
    assert "答案" in content


def test_stream_slow_upstream_precheck_timeout_path(settings, fake_upstream):
    """🔴 预检窗口（3s）超时路径：上游慢（>3s）⇒ 流仍完整（心跳+ping+正文+DONE）。

    此用例钉住「解包 None」回归：预检超时分支曾先解包 first=None ⇒ TypeError ⇒
    流静默断（无 error 无 DONE），只有回环实测才暴露。
    """
    import dataclasses

    from fastapi.testclient import TestClient

    from app.main import create_app

    s = dataclasses.replace(settings, ping_interval=0.5)
    fake_upstream.chat_sse_delay = 1.0   # 每行 1s ⇒ 首增量 >3s 预检窗口
    store = TaskStore(s.task_db)
    pool = AccountPool(s, mint=lambda account: f"tok-{account.email}")
    client = QwenClient(s, transport=fake_upstream.transport())
    app = create_app(s, store=store, pool=pool, client=client)
    with TestClient(app) as tc:
        with tc.stream("POST", CHAT_PATH, json={**CHAT_BODY, "stream": True},
                       headers=AUTH_A) as resp:
            raw = "".join(resp.iter_text())
    events = [json.loads(ln[len("data:"):]) for ln in raw.splitlines()
              if ln.startswith("data:") and "[DONE]" not in ln]
    content = "".join(e["choices"][0]["delta"].get("content") or ""
                      for e in events if e.get("choices"))
    assert "你好，世界" in content, "慢上游流必须完整送达"
    assert ": ping" in raw
    assert raw.rstrip().endswith("data: [DONE]")


def test_thinking_gear_fast_skips_heartbeat_and_uses_fast_config(client_app):
    """reasoning_effort=none ⇒ 快速档：无思考心跳 + 上游收到 thinking_enabled false。"""
    test_client, fake, _, _ = client_app
    with test_client.stream("POST", CHAT_PATH,
                            json={**CHAT_BODY, "stream": True, "reasoning_effort": "none"},
                            headers=AUTH_A) as resp:
        raw = "".join(resp.iter_text())
    events = [json.loads(line[len("data:"):]) for line in raw.splitlines()
              if line.startswith("data:") and line != "data: [DONE]"]
    texts = [e["choices"][0]["delta"].get("content", "") for e in events
             if e.get("choices")]
    assert " " not in texts[:1], "快速档不发心跳空格"
    assert "".join(texts).strip() == "你好，世界"
    fc = fake.bodies("/api/v2/chat/completions")[0]["messages"][0]["feature_config"]
    assert fc["thinking_enabled"] is False and fc["thinking_mode"] == "Fast"


def test_thinking_gear_mapping_and_high_forces_thinking():
    """reasoning_effort/enable_thinking → 档位映射（none/minimal=fast、high=thinking、缺省=auto）。"""
    def gear(body):
        return openai_chat.parse_openai_chat_request(body).thinking_gear
    assert gear({"model": "m", "messages": [{"role": "user", "content": "x"}]}) == "auto"
    assert gear({"model": "m", "reasoning_effort": "none",
                 "messages": [{"role": "user", "content": "x"}]}) == "fast"
    assert gear({"model": "m", "reasoning_effort": "minimal",
                 "messages": [{"role": "user", "content": "x"}]}) == "fast"
    assert gear({"model": "m", "reasoning_effort": "high",
                 "messages": [{"role": "user", "content": "x"}]}) == "thinking"
    assert gear({"model": "m", "reasoning_effort": "medium",
                 "messages": [{"role": "user", "content": "x"}]}) == "auto"
    assert gear({"model": "m", "enable_thinking": False,
                 "messages": [{"role": "user", "content": "x"}]}) == "fast"
    assert gear({"model": "m", "enable_thinking": True,
                 "messages": [{"role": "user", "content": "x"}]}) == "auto"
    # enable_thinking=false 优先于 reasoning_effort 缺省；reasoning_effort=high 时 false 仍赢
    assert gear({"model": "m", "enable_thinking": False, "reasoning_effort": "high",
                 "messages": [{"role": "user", "content": "x"}]}) == "fast"


def test_thinking_gear_passes_to_upstream_body(ark_app):
    """reasoning_effort=high ⇒ 上游收到思考档（Thinking + auto_thinking false）。"""
    tc, _, fake_ark, _ = ark_app
    body = {"model": "qwen3.7-plus", "reasoning_effort": "high",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "f", "description": "d",
                                                        "parameters": {}}}]}
    resp = tc.post(CHAT_PATH, json=body, headers=AUTH_A)
    assert resp.status_code == 200
    # 走了回退，但提交体在回退前构造？—— 回退不经过 qwen 提交 ⇒ 用非回退路径验证：
    # 直接构造无 tools 请求时才走 qwen；这里改用 dry-run 校验
    body.pop("tools")
    resp = tc.post(CHAT_PATH, json={**body, "X-Avm-Dry-Run": "1"} | {},
                   headers={**AUTH_A, "X-Avm-Dry-Run": "1"})
    assert resp.status_code == 200
    fc = resp.json()["upstream"]["body"]["messages"][0]["feature_config"]
    assert fc["thinking_mode"] == "Thinking" and fc["auto_thinking"] is False


def test_chat_stream_first_chunk_carries_degradations(client_app):
    test_client, _, _, _ = client_app
    body = {**CHAT_BODY, "stream": True, "temperature": 0.7}
    with test_client.stream("POST", CHAT_PATH, json=body, headers=AUTH_A) as resp:
        raw = "".join(resp.iter_text())
    events = [json.loads(line[len("data:"):]) for line in raw.splitlines()
              if line.startswith("data:") and line != "data: [DONE]"]
    assert "temperature" in "；".join(events[0]["degradations"])
    assert all("degradations" not in e for e in events[1:]), "加性扩展只随首片出现"


# ---------------------------------------------------------------- /v1/models 注册

def test_models_lists_upstream_plus_video(client_app):
    test_client, _, _, _ = client_app
    data = test_client.get("/v1/models").json()
    ids = [item["id"] for item in data["data"]]
    assert ids == ["qwen3.7-plus", "qwen3.8-max", "qwen3.8-omni-flash", "qwen/video"]
    chat_entry = data["data"][0]
    assert chat_entry["media"] == "text"
    assert chat_entry["verified"] is False
    assert chat_entry["created"] == 1732711466
    assert chat_entry["context_length"] == 1000000
    assert chat_entry["thinking"] is True
    # 能力透传：vision（图片解析本门已实测支持）；document/video/audio 照上游宣告透传
    assert chat_entry["vision"] is True
    assert chat_entry["accepts_image"] is True
    assert "document" not in chat_entry  # DEFAULT_MODELS 样本只宣告 vision/thinking
    video = data["data"][-1]
    assert video["media"] == "video" and video["verified"] is True


def test_models_capability_flags_passthrough(client_app):
    test_client, fake, _, _ = client_app
    fake.models_payload = [
        _model_item("multimodal", "Multi", ["t2t"]),
    ]
    fake.models_payload[0]["info"]["meta"]["capabilities"] = {
        "vision": True, "document": True, "video": True, "audio": True, "thinking": False}
    data = test_client.get("/v1/models").json()
    entry = next(m for m in data["data"] if m["id"] == "multimodal")
    assert entry["vision"] is True and entry["document"] is True
    assert entry["video"] is True and entry["audio"] is True
    assert entry["thinking"] is False
    assert entry["accepts_image"] is True


def test_models_filter_drops_non_chat_and_inactive(client_app):
    test_client, fake, _, _ = client_app
    fake.models_payload = [
        _model_item("kept", "Kept", ["t2t"]),
        _model_item("image-only", "ImageOnly", ["t2i", "image_edit"]),
        _model_item("offline", "Offline", ["t2t"], active=False),
    ]
    data = test_client.get("/v1/models").json()
    ids = {item["id"] for item in data["data"]}
    assert "kept" in ids
    assert "image-only" not in ids and "offline" not in ids


def test_models_no_auth_required(client_app):
    test_client, _, _, _ = client_app
    assert test_client.get("/v1/models").status_code == 200


def test_registry_falls_back_to_last_good():
    """上游抖动 ⇒ 回退上一份好清单；负缓存顺延，绝不 5xx。"""
    state = {"fail": False, "calls": 0, "tick": 0.0}

    def fetch():
        state["calls"] += 1
        if state["fail"]:
            raise RuntimeError("upstream down")
        return list(DEFAULT_MODELS)

    def clock():
        return state["tick"]

    registry = ChatModelRegistry(fetch, ttl=100.0, clock=clock)
    assert [e["id"] for e in registry.entries()] == [m["id"] for m in DEFAULT_MODELS]
    assert state["calls"] == 1
    assert registry.entries() and state["calls"] == 1, "TTL 内不再拉取"

    state["fail"] = True
    state["tick"] = 200.0        # 过期，触发重拉 → 失败 → 回退
    entries = registry.entries()
    assert [e["id"] for e in entries] == [m["id"] for m in DEFAULT_MODELS]
    stats = registry.stats()
    assert stats["last_error"] and stats["cached_models"] == 3


def test_registry_empty_when_never_fetched():
    def fetch():
        raise RuntimeError("down")

    registry = ChatModelRegistry(fetch, ttl=10.0)
    assert registry.entries() == []      # 首拉失败 ⇒ 空清单（端点只剩视频条目）


def test_api_models_without_cookie(fake_upstream, settings):
    """/api/models 免鉴权（2026-09-24 实测）⇒ 请求头不得带 Cookie。"""
    from app.upstream.qwen.client import QwenClient

    client = QwenClient(settings, transport=fake_upstream.transport())
    models_list = client.list_upstream_models()
    assert [m["id"] for m in models_list] == [m["id"] for m in DEFAULT_MODELS]
    request = fake_upstream.calls("/api/models")[0]
    assert "cookie" not in request.headers


# ---------------------------------------------------------------- 附件上传链

def test_file_attachment_upload_chain(upload_app):
    """🔴 文件解析主路径：file 分段 → 解析来源 → getstsToken → OSS PUT → files[] 条目。"""
    tc, fake, s, captured = upload_app
    body = {"model": "qwen3.7-plus", "messages": [{"role": "user", "content": [
        {"type": "file", "file": {"url": "https://arxiv.org/pdf/1706.03762"}},
        {"type": "text", "text": "这份文档讲什么？"}]}]}
    resp = tc.post(CHAT_PATH, json=body, headers=AUTH_A)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["choices"][0]["message"]["content"] == "你好，世界"
    assert "x-qwen-fallback" not in resp.headers, "上传链走 qwen，不触发回退"

    # 解析器被调用（来源透传、大小上限生效）
    assert captured["kind"] == "file"
    assert captured["source"] == "https://arxiv.org/pdf/1706.03762"
    assert captured["max_bytes"] == s.upload_max_bytes
    # 上游收到 getstsToken（filename/filetype/filesize 为前端契约键，filesize 是字符串）
    sts = fake.bodies("/api/v2/files/getstsToken")[0]
    assert sts["filename"] == "test-attachment.pdf"
    assert sts["filetype"] == "file"
    assert sts["filesize"] == str(len(b"ATTACHMENT-BYTES"))
    # OSS PUT 带 V4 签名头（Authorization OSS4-HMAC-SHA256 + STS token）
    put_req = next(r for r in fake.requests if r.method == "PUT")
    assert put_req.headers["Authorization"].startswith("OSS4-HMAC-SHA256 Credential=STSTESTID/")
    assert put_req.headers["x-oss-security-token"] == "ststest-token"
    assert put_req.headers["x-oss-content-sha256"] == "UNSIGNED-PAYLOAD"
    # 提交体 files[0] = 前端完整形状（file 类 file_class=document；url=带签名 file_url）
    entry = fake.bodies("/api/v2/chat/completions")[0]["messages"][0]["files"][0]
    assert entry["type"] == "file" and entry["file_class"] == "document"
    assert entry["name"] == "test-attachment.pdf" and entry["status"] == "uploaded"
    assert entry["url"].startswith("https://qwen-webui-prod.oss-accelerate.aliyuncs.com/")
    assert entry["file"] == {} and entry["greenNet"] == "success"


def test_data_image_uploads_to_qwen(upload_app):
    """data: URI 图片 ⇒ 转上传链（不再 400），qwen 收到 image/vision 条目。"""
    tc, fake, _, _ = upload_app
    body = {"model": "qwen3.7-plus", "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        {"type": "text", "text": "图里是什么"}]}]}
    resp = tc.post(CHAT_PATH, json=body, headers=AUTH_A)
    assert resp.status_code == 200, resp.text
    entry = fake.bodies("/api/v2/chat/completions")[0]["messages"][0]["files"][0]
    assert entry["type"] == "image" and entry["file_class"] == "vision"


def test_attachment_upload_disabled_with_fallback_goes_to_ark(ark_app):
    """上传未启用 + 回退启用 ⇒ 文件附件转方舟（回退应答 model 回显请求值——脱敏口径）。"""
    tc, _, fake_ark, _ = ark_app
    body = {"model": "qwen3.7-plus", "messages": [{"role": "user", "content": [
        {"type": "file", "file": {"url": "https://arxiv.org/pdf/1706.03762"}},
        {"type": "text", "text": "总结"}]}]}
    resp = tc.post(CHAT_PATH, json=body, headers=AUTH_A)
    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] == "qwen3.7-plus", "回退应答 model 回显请求值（脱敏）"
    assert resp.headers.get("x-qwen-fallback") == "ark"
    assert "doubao-test" not in json.dumps(data)


def test_attachment_upload_disabled_without_fallback_is_400(client_app):
    """上传未启用 + 无回退 ⇒ 400 且指明 QWEN_UPLOAD_ENABLED。"""
    tc, _, _, _ = client_app
    resp = tc.post(CHAT_PATH, json={"model": "qwen3.7-plus", "messages": [
        {"role": "user", "content": [
            {"type": "file", "file": {"url": "https://arxiv.org/pdf/x.pdf"}},
            {"type": "text", "text": "总结"}]}]}, headers=AUTH_A)
    assert resp.status_code == 400
    assert "QWEN_UPLOAD_ENABLED" in resp.json()["error"]["message"]


def test_attachment_ssrf_guard(settings, fake_upstream):
    """私网 URL ⇒ SSRF 防护 400（真实解析器；upload 启用、回退关闭）。"""
    import dataclasses

    from fastapi.testclient import TestClient

    from app.main import create_app

    s = dataclasses.replace(settings, upload_enabled=True)
    store = TaskStore(s.task_db)
    pool = AccountPool(s, mint=lambda account: f"tok-{account.email}")
    client = QwenClient(s, transport=fake_upstream.transport())
    app = create_app(s, store=store, pool=pool, client=client)   # 默认真实解析器
    with TestClient(app) as tc:
        resp = tc.post(CHAT_PATH, json={"model": "qwen3.7-plus", "messages": [
            {"role": "user", "content": [
                {"type": "file", "file": {"url": "http://127.0.0.1:1/secret.pdf"}},
                {"type": "text", "text": "总结"}]}]}, headers=AUTH_A)
    assert resp.status_code == 400
    message = resp.json()["error"]["message"]
    assert "非公网" in message or "无法解析" in message


# ---------------------------------------------------------------- 思考泄漏加固
# 🔴 2026-10-02 实测缺陷：qwen3.8-omni-flash 在 auto/thinking 档把思考过程写进正文
# content（无 reasoning_content、无 </analysis>），复现 3/3 含流式；fast 档干净。
# 同请求 qwen3.7-plus 流式正确分离 ⇒ 模型侧行为，非本层翻译 bug。

LEAK_MODEL = "qwen3.8-omni-flash"


def _gear_of(body: dict, **kw) -> str:
    return openai_chat.parse_openai_chat_request(body, **kw).thinking_gear


def test_thinking_leak_model_defaults_to_fast_gear():
    """缺省（auto）命中泄漏名单 ⇒ 静默降 fast，且写明降级理由（不静默改语义）。"""
    req = openai_chat.parse_openai_chat_request(
        {"model": LEAK_MODEL, "messages": [{"role": "user", "content": "1+1等于几？只给数字"}]})
    assert req.thinking_gear == "fast"
    assert req.model == LEAK_MODEL, "只该动档位，模型名原样转发"
    assert any(LEAK_MODEL in d and "快速档" in d for d in req.degradations), req.degradations


def test_non_leak_model_keeps_auto_gear():
    """🔴 反向：未登记的模型**不得**被降档（防"名单一失守就全量降档"的过度加固）。"""
    assert _gear_of({"model": "qwen3.7-plus",
                     "messages": [{"role": "user", "content": "x"}]}) == "auto"
    assert _gear_of({"model": "qwen3.8-max",
                     "messages": [{"role": "user", "content": "x"}]}) == "auto"


def test_explicit_high_is_respected_but_warned():
    """调用方显式声明 high ⇒ 照发不静默覆盖，但必须写 degradation 告知思考会并入正文。"""
    req = openai_chat.parse_openai_chat_request(
        {"model": LEAK_MODEL, "reasoning_effort": "high",
         "messages": [{"role": "user", "content": "x"}]})
    assert req.thinking_gear == "thinking", "显式意图优先，不静默改调用方语义"
    assert any("reasoning_content" in d for d in req.degradations), req.degradations


def test_explicit_none_stays_fast_without_extra_warning():
    """显式 none 本来就是 fast ⇒ 不再叠加多余的泄漏告警（不刷屏）。"""
    req = openai_chat.parse_openai_chat_request(
        {"model": LEAK_MODEL, "reasoning_effort": "none",
         "messages": [{"role": "user", "content": "x"}]})
    assert req.thinking_gear == "fast"
    assert not any(LEAK_MODEL in d for d in req.degradations), req.degradations


def test_qwen_prefix_form_also_matched():
    """调用方写 `qwen/` 前缀形态也要命中（parse 已剥离前缀 ⇒ 比裸名即可）。"""
    assert _gear_of({"model": f"qwen/{LEAK_MODEL}",
                     "messages": [{"role": "user", "content": "x"}]}) == "fast"


def test_leak_list_is_overridable_and_can_be_disabled():
    """名单可换（可关）：传空列表 ⇒ 完全恢复旧行为（上游修好后的回退开关）。"""
    body = {"model": LEAK_MODEL, "messages": [{"role": "user", "content": "x"}]}
    assert _gear_of(body, thinking_leak_models=[]) == "auto"
    # 换名单：把 plus 登记进去 ⇒ 它降档，omni-flash 不再降
    assert _gear_of({**body, "model": "qwen3.7-plus"},
                    thinking_leak_models=["qwen3.7-plus"]) == "fast"


def test_fast_gear_has_no_heartbeat_for_leak_model(client_app):
    """端到端：泄漏模型缺省请求 ⇒ 上游收到 thinking_enabled false，且流式无心跳空格。"""
    test_client, fake, _, _ = client_app
    with test_client.stream("POST", CHAT_PATH,
                            json={"model": LEAK_MODEL,
                                  "messages": [{"role": "user", "content": "你好"}],
                                  "stream": True},
                            headers=AUTH_A) as resp:
        assert resp.status_code == 200
        raw = "".join(resp.iter_text())
    events = [json.loads(line[len("data:"):]) for line in raw.splitlines()
              if line.startswith("data:") and line != "data: [DONE]"]
    texts = [e["choices"][0]["delta"].get("content", "") for e in events if e.get("choices")]
    assert " " not in texts[:1], "fast 档不该发心跳空格"
    assert any(LEAK_MODEL in (e.get("degradations") or [""])[0] or
               any(LEAK_MODEL in x for x in (e.get("degradations") or []))
               for e in events), "降级说明须随流式首片下发"
    fc = fake.bodies("/api/v2/chat/completions")[0]["messages"][0]["feature_config"]
    assert fc["thinking_enabled"] is False and fc["thinking_mode"] == "Fast"


def test_leak_guard_survives_config_default():
    """🔴 变异证明：加固真在链路上（不是死代码）—— 关掉名单即失效、默认即生效。"""
    from app.config import Settings
    assert LEAK_MODEL in Settings().thinking_leak_models, "默认名单必须含实测缺陷模型"
    assert Settings.from_env({}).thinking_leak_models, "未设 env ⇒ 加固默认生效"
    assert Settings.from_env({"QWEN_THINKING_LEAK_MODELS": ""}).thinking_leak_models == [], \
        "显式空串 ⇒ 刻意关闭加固"
    assert Settings.from_env({"QWEN_THINKING_LEAK_MODELS": "a , b"}).thinking_leak_models \
        == ["a", "b"], "逗号分隔 + 去空格"
