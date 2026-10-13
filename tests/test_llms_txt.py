"""`/llms.txt` 与落地页 —— fleet 约定门禁（公开性 / 注册表对账 / 错误表派生 / 开关联动）。"""
from __future__ import annotations

import re

from app import __version__, llms_txt, openai_chat
from app import errors as err_mod
from app.config import Settings
from app.models import catalog


def _error_section(body: str) -> str:
    """截取"## 错误"小节（到下一个 ## 为止），供幽灵码检查。"""
    start = body.index("## 错误")
    rest = body[start:]
    nxt = rest.find("\n## ", 1)
    return rest[:nxt] if nxt > 0 else rest


def test_public_shape_and_no_auth(client_app):
    """/llms.txt 200 + text/markdown + # 开头；/ 200 + text/html；两者免 Key。"""
    tc, _, _, _ = client_app
    r = tc.get("/llms.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert r.text.startswith("# qwen-service")
    page = tc.get("/")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "/llms.txt" in page.text and "qwen-service" in page.text
    # 全程未带 Authorization —— 免鉴权口径由这两个请求本身证明


def test_covers_registry_and_derived_counts(client_app):
    """/v1/models 的每个 id 都在正文；chat 注册数按注册表实时计数（不写死）。"""
    tc, _, _, _ = client_app
    models = tc.get("/v1/models").json()["data"]
    body = tc.get("/llms.txt").text
    for m in models:
        assert m["id"] in body, f"能力清单 id {m['id']} 未出现在 /llms.txt"
    n_chat = len([m for m in models if m["id"] != "qwen/video"])
    assert f"已注册 {n_chat} 个上游 chat 模型" in body
    video = catalog()[0]
    assert f"`{video['id']}`" in body
    assert body.count("`qwen/video`") >= 1


def test_error_table_derived_from_exceptions():
    """错误表与异常类**双向一致**：类都在表里；表里没有幽灵码。"""
    body = llms_txt.render_llms_txt(Settings(), [])
    rows = llms_txt._error_rows()
    assert len(rows) == len(err_mod.__all__) - 1, "AdapterError 基类不计入；子类一个不落"

    section = _error_section(body)
    for row in rows:
        assert f"`{row['code']}`" in section, f"错误码 {row['code']} 未登记"
        assert str(row["http"]) in section
    codes_in_section = set(re.findall(r"\| `([A-Za-z0-9.]+)` \|", section))
    known = {row["code"] for row in rows}
    assert codes_in_section <= known, f"幽灵码（表里写死的）：{codes_in_section - known}"

    # chat 门错误 type 与 openai_chat 映射同源：用真实异常类走一遍映射
    probe = {400: err_mod.InvalidParameterError, 401: err_mod.AuthenticationError,
             429: err_mod.RateLimitedError, 502: err_mod.UpstreamError}
    for http, cls in probe.items():
        exc = cls("x")
        got = openai_chat.openai_error_body(exc, "rid")["error"]["type"]
        assert got == openai_chat.CHAT_ERROR_TYPE_BY_STATUS[http]


def test_fallback_auth_upload_lines_reflect_settings():
    """回退 / 鉴权 / 上传 三行随配置联动（防止两套文案都说满）。"""
    s_on = Settings(ark_fallback_key="k", ark_fallback_model="m",
                    upload_enabled=True)
    s_off = Settings()   # 无回退；upload 默认 True → 单独关一次
    s_no_upload = Settings(upload_enabled=False)
    on = llms_txt.render_llms_txt(s_on, ["qwen3.7-plus"])
    off = llms_txt.render_llms_txt(s_off, [])
    no_up = llms_txt.render_llms_txt(s_no_upload, [])

    # 回退：启用 → 已启用段；关闭 → 未配置段
    fb_on = on.split("## 回退通道")[1].split("\n## ")[0]
    fb_off = off.split("## 回退通道")[1].split("\n## ")[0]
    assert "**已启用**" in fb_on and "整单转方舟" in fb_on
    assert "**未配置**" in fb_off and "ARK_FALLBACK_KEY" in fb_off
    assert "已启用" not in fb_off, "回退关闭时不得声称已启用"
    # 上传链开关
    up = no_up.split("## 附件上传链")[1].split("\n## ")[0]
    assert "未启用" in up and "QWEN_UPLOAD_ENABLED=0" in up
    # 鉴权行（端点节内）
    ep_on = on.split("## 端点")[1].split("\n## ")[0]
    ep_off = off.split("## 端点")[1].split("\n## ")[0]
    # 白名单已删除（2026-10-13）⇒ 两态都说明「账号串 + access token」两种形态
    assert "账号串" in ep_on and "账号串" in ep_off
    # 版本号
    assert f"v{__version__}" in on and f"v{__version__}" in off


def test_no_fallback_model_leak():
    """🔴 脱敏纪律：回退模型名/Key 不得出现在 /llms.txt。"""
    s = Settings(ark_fallback_key="ark-secret-xyz", ark_fallback_model="doubao-secret-model",
                 ark_fallback_models=["alt-secret-model"])
    body = llms_txt.render_llms_txt(s, ["qwen3.7-plus"])
    for secret in ("doubao-secret-model", "alt-secret-model", "ark-secret-xyz"):
        assert secret not in body, f"🔴 泄漏：{secret}"


def test_version_and_relative_links(client_app):
    tc, _, _, _ = client_app
    body = tc.get("/llms.txt").text
    assert f"v{__version__}" in body
    assert "](/llms.txt)" in body and "](/v1/models)" in body, "链接用相对路径"
    assert "https://chat.qwen.ai" not in body.split("最小可用")[0], "正文不写死域名（相对路径纪律）"
