"""客户端（`app/upstream/qwen/client.py`）：头完整性 / 响应判读 / 错误分类。零网络。"""
from __future__ import annotations

import json

import httpx
import pytest

from app.errors import (
    AuthenticationError,
    InvalidParameterError,
    NotFoundError,
    QuotaExhaustedError,
    RiskControlError,
    UpstreamError,
)
from app.upstream.qwen.client import QwenClient


def make_client(settings, handler) -> QwenClient:
    return QwenClient(settings, transport=httpx.MockTransport(handler))


def submit(client: QwenClient):
    return client.submit_video("tok-1", chat_id="chat-9", prompt="一只猫",
                               ratio="16:9", chat_type="t2v", image_url=None)


# ------------------------------------------------------------------ 头部

def test_headers_carry_the_full_browser_fingerprint(settings):
    client = make_client(settings, lambda r: httpx.Response(200, json={}))
    headers = client.headers("tok-1", referer="https://chat.qwen.ai/c/abc",
                             extra_cookies="acw_tc=1")
    assert headers["version"] == "0.2.0"          # 缺它 ⇒ 写端点必被拒（Bad_Request）
    assert headers["source"] == "web"
    assert headers["Accept"] == "application/json"
    assert headers["Sec-Fetch-Dest"] == "empty"
    assert headers["Sec-Fetch-Mode"] == "cors"
    assert headers["Sec-Fetch-Site"] == "same-origin"
    assert headers["X-Accel-Buffering"] == "no"
    assert headers["Cookie"] == "token=tok-1; acw_tc=1"
    assert headers["Timezone"].endswith("GMT+0800")
    assert headers["Referer"] == "https://chat.qwen.ai/c/abc"


def test_submit_sends_expected_body_and_reads_task_id(settings):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["headers"] = request.headers
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={
            "success": True,
            "data": {"messages": [{"extra": {"wanx": {"task_id": "T-1"}}}]}})

    client = make_client(settings, handler)
    assert submit(client) == "T-1"
    assert seen["params"]["chat_id"] == "chat-9"
    body = seen["body"]
    assert body["chat_id"] == body["chatId"] == "chat-9"
    assert body["stream"] is False
    assert body["size"] == body["messages"][0]["extra"]["meta"]["size"] == "16:9"
    assert "files" not in body["messages"][0]
    assert seen["headers"]["version"] == "0.2.0"


def test_missing_task_id_is_loud(settings):
    client = make_client(settings, lambda r: httpx.Response(
        200, json={"success": True, "data": {"messages": []}}))
    with pytest.raises(UpstreamError, match="task_id"):
        submit(client)


# ------------------------------------------------------------------ 错误判读

def test_x5sec_envelope_maps_to_risk_control_with_retry_after(settings):
    envelope = {"ret": ["FAIL_SYS_USER_VALIDATE",
                        "RGV587_ERROR::SM::哎哟喂,被挤爆啦,请稍后重试"],
                "data": {"url": "https://chat.qwen.ai/_____tmd_____/punish?action=captcha"}}
    client = make_client(settings, lambda r: httpx.Response(200, json=envelope))
    with pytest.raises(RiskControlError) as excinfo:
        submit(client)
    assert excinfo.value.status_code == 429
    assert excinfo.value.retry_after


def test_bad_request_points_at_the_version_header(settings):
    client = make_client(settings, lambda r: httpx.Response(
        200, json={"success": False, "code": "Bad_Request", "details": "Internal error"}))
    with pytest.raises(InvalidParameterError, match="version"):
        submit(client)


def test_unauthorized_maps_to_401(settings):
    client = make_client(settings, lambda r: httpx.Response(
        200, json={"success": False, "data": {"code": "Unauthorized", "details": "nope"}}))
    with pytest.raises(AuthenticationError):
        submit(client)


def test_quota_exhausted_maps_to_429(settings):
    client = make_client(settings, lambda r: httpx.Response(
        200, json={"success": False, "data": {"code": "RateLimited",
                                              "details": "今日生成额度已用完"}}))
    with pytest.raises(QuotaExhaustedError):
        submit(client)


def test_credit_shortage_maps_to_quota_exhausted(settings):
    """积分不足 ⇒ 同"额度耗尽"一类（未受理、未计费）⇒ 换号重试，而不是当未知错误炸掉。

    ⚠️ 文案形态**未实测**（余额 15 分的第二单一撞滑块没跑成）⇒ 按关键词保守匹配，
    判据见 `client.QUOTA_WORDS_UNPROVEN`。
    """
    for details in ("积分不足，无法生成视频", "credits not enough",
                    "Insufficient credits", "余额不足"):
        client = make_client(settings, lambda r, d=details: httpx.Response(
            200, json={"success": False, "data": {"code": "RateLimited", "details": d}}))
        with pytest.raises(QuotaExhaustedError):
            submit(client)


def test_unrelated_refusal_is_not_mistaken_for_quota(settings):
    """关键词不得误伤：与额度/积分无关的拒绝仍走 `UpstreamError`（不瞎猜、不换号）。"""
    client = make_client(settings, lambda r: httpx.Response(
        200, json={"success": False, "data": {"code": "InternalError",
                                              "details": "服务暂时不可用"}}))
    with pytest.raises(UpstreamError):
        submit(client)


def test_chat_not_found_maps_to_notfound(settings):
    client = make_client(settings, lambda r: httpx.Response(
        200, json={"success": False, "data": {"code": "Not_Found",
                                              "details": "CHAT_NOT_FOUND"}}))
    with pytest.raises(NotFoundError):
        submit(client)


def test_waf_page_is_upstream_error(settings):
    client = make_client(settings, lambda r: httpx.Response(
        200, text="<html><body>aliyun_waf challenge</body></html>",
        headers={"content-type": "text/html"}))
    with pytest.raises(UpstreamError, match="WAF"):
        submit(client)


# ------------------------------------------------------------------ 查询端点

def test_task_status_reads_x_actual_status_code(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"x-actual-status-code": "404"},
                              json={"success": False,
                                    "data": {"code": "Not_Found", "details": "Task not found"}})

    client = make_client(settings, handler)
    result = client.task_status("tok", "T-x")
    assert result["actual_status_code"] == 404
    assert result["success"] is False


def test_task_status_success_shape_passthrough(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"x-actual-status-code": "200"},
                              json={"success": True,
                                    "data": {"task_status": "success",
                                             "content": "https://cdn.qwenlm.ai/x.mp4"}})

    client = make_client(settings, handler)
    result = client.task_status("tok", "T-x")
    assert result["actual_status_code"] == 200
    assert result["data"]["task_status"] == "success"
    assert result["data"]["content"].endswith(".mp4")


def test_new_chat_returns_data_id(settings):
    client = make_client(settings, lambda r: httpx.Response(
        200, json={"success": True, "data": {"id": "chat-42"}}))
    assert client.new_chat("tok") == "chat-42"


def test_new_chat_without_id_is_loud(settings):
    client = make_client(settings, lambda r: httpx.Response(200, json={"success": True,
                                                                      "data": {}}))
    with pytest.raises(UpstreamError, match="chats/new"):
        client.new_chat("tok")
