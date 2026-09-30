"""凭据铸造（signin + refresh）—— 出口只走 HTTP(S) 代理。零网络（注入 `httpx.MockTransport`）。

覆盖四件事：
  ① signin 请求形状：POST 到 **auth 域** `/v2/auths/signin`（2026-09-30 改版）、body 是
     `sha256hex(password)`、带 web 端头集（version 0.3.12 / x-request-origin / same-site）；
  ② token 读取：**新契约 = 响应体 `data.access_token`**（15 分钟），Set-Cookie `token=`
     是旧契约兜底；jar（pair 门）= 预热 + signin 种下的 cookie 全集 + token；
  ③ **refresh**（续期主路）：cookie 驱动 GET、返回新 token 并更新 jar（RT 有则覆盖）、
     无 RT 是 no-op、失败 best-effort 回 `("", jar)`；
  ④ 失败分类：WAF 挑战页 → `WallError`；非 200 / 拿不到 token → `MintError`。
     🔴 生产路径**直连**（auth 域对池代理出口回 502，2026-10-01 实测）。
"""
from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from app.upstream.qwen.signin import (
    MintError,
    WallError,
    mint_token,
    refresh_auth,
)

SIGNIN = "/v2/auths/signin"
REFRESH = "/v2/auths/refresh"
WARM = "/auth"


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_signin_posts_to_auth_domain_and_reads_body_access_token():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == WARM and request.url.host == "chat.qwen.ai":
            return httpx.Response(
                200, text="<html>warmup</html>",
                headers={"set-cookie": "acw_tc=WARM-TC; Path=/; HttpOnly"})
        seen["host"] = request.url.host
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        seen["version"] = request.headers.get("version", "")
        seen["origin"] = request.headers.get("x-request-origin", "")
        seen["site"] = request.headers.get("sec-fetch-site", "")
        seen["ua"] = request.headers.get("user-agent", "")
        return httpx.Response(
            200, json={"success": True,
                       "data": {"access_token": "JWT-abc.def.ghi",
                                "refresh_token": "RT-1"}},
            headers={"set-cookie": "refresh_token=RT-COOKIE; Path=/; HttpOnly; "
                                   "Domain=.qwen.ai"})

    token, jar = mint_token("a@x.cn", "s3cret",
                            transport=_transport(handler))

    assert token == "JWT-abc.def.ghi"
    # 新契约：token 在响应体；Set-Cookie 只有 refresh_token —— 两者都要落进 jar
    assert seen["host"] == "auth.qwen.ai" and seen["path"] == SIGNIN
    assert seen["body"]["email"] == "a@x.cn"
    assert seen["body"]["password"] == hashlib.sha256(b"s3cret").hexdigest()
    assert "s3cret" not in json.dumps(seen["body"]), "口令绝不明文外发"
    assert seen["version"] == "0.3.12", "auth 域自报版本（不是写端点的 0.2.0）"
    assert seen["origin"] == "https://chat.qwen.ai"
    assert seen["site"] == "same-site", "chat → auth 是跨源同站"
    assert "Chrome" in seen["ua"], "必须带浏览器特征头"
    # pair 门：jar = 预热章 + refresh_token cookie + token，一套身份
    assert "token=JWT-abc.def.ghi" in jar
    assert "refresh_token=RT-COOKIE" in jar
    assert "acw_tc=WARM-TC" in jar


def test_set_cookie_token_is_the_legacy_fallback():
    """老形状（token 只在 Set-Cookie）必须继续可读 —— 平滑过渡期不炸。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == WARM:
            return httpx.Response(200, text="<html>warmup</html>")
        return httpx.Response(200, json={"success": True, "data": {"email": "x"}},
                              headers={"set-cookie": "token=LEGACY-jwt; Path=/"})

    token, jar = mint_token("a@x.cn", "pw",
                            transport=_transport(handler))
    assert token == "LEGACY-jwt"
    assert "token=LEGACY-jwt" in jar


def test_warmup_is_best_effort_and_does_not_block():
    """预热（GET /auth）失败不能阻断铸造。"""
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.path)
        if request.url.path == WARM:
            raise httpx.ConnectError("warmup down")
        return httpx.Response(200, json={"success": True,
                                         "data": {"access_token": "T2"}})

    token, jar = mint_token("a@x.cn", "pw",
                            transport=_transport(handler))
    assert token == "T2" and "token=T2" in jar
    assert hits == [WARM, SIGNIN]


def test_waf_challenge_page_is_wall_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>aliyun_waf_xxx challenge</html>")

    with pytest.raises(WallError):
        mint_token("a@x.cn", "pw",
                   transport=_transport(handler))


def test_non_200_and_missing_token_are_mint_errors():
    with pytest.raises(MintError, match="HTTP 403"):
        mint_token("a@x.cn", "pw",
                   transport=_transport(lambda r: httpx.Response(403, json={})))

    with pytest.raises(MintError, match="没有 token"):
        mint_token("a@x.cn", "pw",
                   transport=_transport(lambda r: httpx.Response(200, json={"success": True})))


def test_proxy_transport_failure_is_mint_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ProxyError("proxy auth failed")

    with pytest.raises(MintError, match="传输失败"):
        mint_token("a@x.cn", "pw",
                   transport=_transport(handler))


def test_production_path_is_direct_and_never_env_proxy(monkeypatch):
    """生产路径必须**直连**（auth 域对池代理出口回 502，2026-10-01 实测）+ `trust_env=False`。

    为什么单独测它：注入 `transport` 时走的是显式 transport 分支，证明不了
    "默认分支真的直连" ⇒ 这里用 spy 断言构造 kwargs（无 proxy、trust_env=False）。
    """
    captured: dict = {}
    real_client = httpx.Client

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real_client(*args, transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"data": {"access_token": "T"}})))

    monkeypatch.setattr(httpx, "Client", spy)
    token, jar = mint_token("a@x.cn", "pw")
    assert token == "T"
    assert "proxy" not in captured, "auth 域 signin 必须直连（池代理出口 502）"
    assert captured["trust_env"] is False, "别让宿主的 HTTP(S)_PROXY 静默接管"


# ------------------------------------------------------------------ refresh（续期主路）


def _refresh_handler(rocket: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        rocket["method"] = request.method
        rocket["path"] = request.url.path
        rocket["cookie"] = request.headers.get("cookie", "")
        rocket["version"] = request.headers.get("version", "")
        return rocket["_resp"]
    return handler


def test_refresh_returns_new_token_and_updates_jar():
    rocket: dict = {"_resp": httpx.Response(
        200, json={"success": True,
                   "data": {"access_token": "AT-2", "refresh_token": "RT-2"}})}
    jar = "acw_tc=W1; refresh_token=RT-1; token=AT-1"
    token, fresh = refresh_auth(jar, transport=_transport(_refresh_handler(rocket)))
    assert token == "AT-2"
    # token 替换（不是追加）、RT 有则覆盖、其余条目原样
    assert "token=AT-2" in fresh and "token=AT-1" not in fresh
    assert "refresh_token=RT-2" in fresh
    assert "acw_tc=W1" in fresh
    assert rocket["method"] == "GET" and rocket["path"] == REFRESH
    assert "refresh_token=RT-1" in rocket["cookie"], "续期凭据就是 jar 里的 RT"
    assert rocket["version"] == "0.3.12"


def test_refresh_without_rt_is_noop():
    assert refresh_auth("token=AT-1") == ("", "token=AT-1")


def test_refresh_failure_is_best_effort():
    jar = "refresh_token=RT-1; token=AT-1"
    # 非 200 / 体里没 token / 传输炸 —— 一律 ("", 原 jar)，不抛
    for resp in (httpx.Response(500, text="boom"),
                 httpx.Response(200, json={"success": False}),
                 httpx.Response(200, text="<html>aliyun_waf</html>")):
        assert refresh_auth(jar, transport=_transport(lambda r, _r=resp: _r)) == ("", jar)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert refresh_auth(jar, transport=_transport(down)) == ("", jar)
