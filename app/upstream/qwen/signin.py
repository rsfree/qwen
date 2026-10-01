"""qwen 账号凭据铸造：token **+ 同源会话 jar**（signin 与 refresh 两条路）。

🔴 2026-09-30 上游改版（当日实测，契约移植自 image-adapter
`script_store/qwen/images@v1.py` 的生产验证版）：

  · 认证搬去独立域 **auth.qwen.ai** —— 旧域（chat.qwen.ai）的 signin 对**一切纯 HTTP
    客户端**回滑块挑战页（httpx / curl_cffi chrome 模拟 / 任意出口 / 任意账号 ⇒ 全灭，
    挑战页 200 + text/html、25×captcha+slider，无可计算项，纯 HTTP 无解）；
  · **signin** = `POST {auth}/v2/auths/signin`（`email` + `sha256hex(password)`），
    新契约：会话 token 在**响应体** `data.access_token`（**15 分钟**寿命），
    Set-Cookie 只剩 `refresh_token`（30 天、HttpOnly、`Domain=.qwen.ai`）；
    旧契约（`token=` cookie）保留为兜底读法。头集缺一件 ⇒ `Invalid request header`
    （报错原文就在响应 `details` 里；`version` 必须是 **0.3.12** —— auth 域自报的版本，
    不是写端点的 0.2.0）；
  · **refresh** = `GET {auth}/v2/auths/refresh`，`Cookie` 带会话 jar（内含
    refresh_token）⇒ 体 `data{access_token, refresh_token}`。免密码、免预热、
    **不拦滑块**、无 IP 墙 ⇒ 正常续期（15 分钟一次）全走这条路；signin 只在 RT
    也失效时发生（≈30 天一次）。refresh_token 实测**不轮换**（同一份复用），
    但响应给了新值就照收（写回 jar，"有则覆盖"）；
  · 🔑 **pair 门（token×jar 成对）**：token 与**产出它的那次会话**种下的 cookie
    （预热拿的 WAF 冷启动章 `acw_tc`/`x-ap` + signin 的 Set-Cookie 全集）是一套
    身份，写请求必须整套装出去 —— 实测 2026-09-22，token 配别人的 jar ⇒ x5sec，
    而账号本身登录毫无问题。所以本模块返回的是 `(token, jar)` 二元组，jar 与
    token 同源同缓存；
  · signin 有 **IP 级频率墙**（实测 ≈12 次/6 分钟触发；挑战页 200 + text/html，
    持续数分钟，只有换出口才恢复）⇒ 调用方要节流（账号池的 `_pace_signin` 负责，
    不在本模块）。服务的 signin 频率 ≈ 每账号 30 天一次，远够不着墙 ⇒ **直连**。
    🔴 代理池出口打 auth 域回 502（2026-10-01 实测；主机名不入库）⇒ signin/refresh 一律直连，
    `QWEN_SIGNIN_PROXY` 不再参与铸造。

⚠️ httpx 的 `proxy=` 是 mounts，会覆盖自定义 `transport` —— 测试注入 MockTransport
时必须跳过 proxy（`transport` 参数仅测试用，给了就不走代理）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from datetime import datetime

import httpx

from ...config import UA_DEFAULT

SIGNIN_PATH = "/v2/auths/signin"
REFRESH_PATH = "/v2/auths/refresh"
WARM_PATH = "/auth"
#: auth 域基址（**含 `/api` 前缀**，signin/refresh 路径直接拼在后面）。
AUTH_BASE_DEFAULT = "https://auth.qwen.ai/api"
#: auth 域 web 端自报版本（2026-09-30 抓包实测值）。厂商改版后若 signin 开始拒头
#: （`Invalid request header`），先怀疑这个值漂移了。
AUTH_WEB_VERSION = "0.3.12"


class MintError(RuntimeError):
    """signin/refresh 未能产出 token（网络 / 出口 / 被拒）。"""


class WallError(MintError):
    """出口被打进了 WAF 挑战页。"""


class AlbadError(MintError):
    """auth 域 alb 层 502 —— 路径/前缀打错或上游故障（不是凭据问题）。

    🔴 2026-10-01 两小时误诊的教训：把 alb 502 当"上游故障/代理不通"排查了整整
    两轮，真因是铸造路径丢了 `/api` 前缀（alb 对未路由路径一律 502）。单独成类 +
    错误信息里写明怀疑方向，让下一个见到 502 的人第一眼就走对路。
    """


def browser_hint_headers(user_agent: str) -> dict[str, str]:
    return {
        "Accept-Language": "zh-CN,zh;q=0.9",
        "User-Agent": user_agent,
        "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    }


def _tz_header() -> str:
    """`Date().toString()` 形状（浏览器同款），每次现做。"""
    return datetime.now().astimezone().strftime("%a %b %d %Y %H:%M:%S GMT%z")


# ------------------------------------------------------------------ jar 工具


def jar_parts(cookie: str, drop_token: bool = False) -> list[str]:
    """jar 串的条目列表；`drop_token` 时剔除 `token=` 条。"""
    parts = [p.strip() for p in str(cookie or "").split(";") if p.strip()]
    if drop_token:
        parts = [p for p in parts if p.partition("=")[0].strip() != "token"]
    return parts


def with_entry(cookie: str, name: str, value: str) -> str:
    """jar 里替换/追加 `name=value`，其余条目原样保留。

    `token=` 必须**替换**而不是追加：旧 token 挨着新 token 留在 jar 里，
    哪份生效就交给上游猜了 —— 认证路径上不掷硬币。
    """
    parts = [p for p in jar_parts(cookie) if p.partition("=")[0].strip() != name]
    return "; ".join(parts + [f"{name}={value}"])


def with_token(cookie: str, token: str) -> str:
    return with_entry(cookie, "token", token)


def cookie_entry(cookie: str, name: str) -> str:
    """jar 里 `name` 的值（没有则空串）。手写解析：JWT 满是 `=`/`.`，SimpleCookie 会啃坏。"""
    for part in jar_parts(cookie):
        key, _, value = part.partition("=")
        if key.strip() == name:
            return value.strip()
    return ""


def cookie_header_from_setcookies(set_cookies: list[str]) -> str:
    """`Set-Cookie` 值列表 → 一条 Cookie 头（只留 name=value，属性丢弃）。

    同名**后出现者为准**（浏览器语义）；没有 `=` 的段跳过。
    """
    pairs: dict[str, str] = {}
    for raw in set_cookies:
        pair = str(raw).split(";", 1)[0].strip()
        if "=" not in pair:
            continue
        name = pair.split("=", 1)[0].strip()
        if name:
            pairs[name] = pair
    return "; ".join(pairs.values())


def extract_token_from_setcookie(set_cookies: list[str]) -> str:
    r"""旧契约：`Set-Cookie` 里的 `token=<jwt>`。

    ⚠️ 2026-09-30 起 Set-Cookie 只剩 `refresh_token` —— `refresh_token=` 是
    `token=` 的子串陷阱，正则带 `(?:^|[;\s])` 边界就是为了不吃它。
    """
    for raw in set_cookies:
        m = re.search(r"(?:^|[;\s])token=([^;]+)", str(raw))
        if m:
            return m.group(1).strip()
    return ""


def jwt_exp(token: str) -> float:
    """JWT 载荷 `exp`（不验签，只看新鲜度）；取不到回 0。"""
    try:
        part = str(token).split(".")[1]
        part += "=" * (-len(part) % 4)
        return float(json.loads(base64.urlsafe_b64decode(part)).get("exp") or 0)
    except Exception:  # noqa: BLE001 - 坏 token 一律当已过期
        return 0.0


def _body_access_token(text: str) -> str:
    """新契约：signin/refresh 响应体 `data.access_token`。"""
    try:
        doc = json.loads(text or "{}")
    except ValueError:
        return ""
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, dict):
        return ""
    return str(data.get("access_token") or "").strip()


def _body_refresh_token(text: str) -> str:
    """refresh 响应体里的 `data.refresh_token`（实测不轮换；有就照收）。"""
    try:
        doc = json.loads(text or "{}")
    except ValueError:
        return ""
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, dict):
        return ""
    return str(data.get("refresh_token") or "").strip()


def _response_set_cookies(resp: httpx.Response) -> list[str]:
    """httpx 的多值 Set-Cookie（`headers.get_list`）。"""
    return [raw for raw in resp.headers.get_list("set-cookie") if raw]


# ------------------------------------------------------------------ 头集


def _signin_headers(user_agent: str) -> dict[str, str]:
    """signin/refresh 共用的 web 端头集 + 每发新铸的 Timezone / X-Request-Id。

    后两者与浏览器行为一致：每次调用都该是新的。缺任何一件（尤其
    `version: 0.3.12` 与 `x-request-origin`）⇒ `Invalid request header`。
    """
    head = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://chat.qwen.ai",
        "Referer": "https://chat.qwen.ai/",
        # chat → auth 是跨源同站（同属 *.qwen.ai），浏览器实际报 same-site。
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "source": "web",
        "version": AUTH_WEB_VERSION,
        "x-request-origin": "https://chat.qwen.ai",
        "Timezone": _tz_header(),
        "X-Request-Id": str(uuid.uuid4()),
    }
    head.update(browser_hint_headers(user_agent))
    return head


def _warm_headers(user_agent: str) -> dict[str, str]:
    """预热（导航形态）：只为拿 WAF 冷启动 cookie（acw_tc / x-ap）。"""
    head = {
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }
    head.update(browser_hint_headers(user_agent))
    return head


# ------------------------------------------------------------------ 铸造 / 续期


def mint_token(email: str, password: str, *,
               base_url: str = "https://chat.qwen.ai",
               auth_base: str = AUTH_BASE_DEFAULT,
               user_agent: str = "",
               timeout: float = 40.0,
               proxy_url: str | None = None,
               transport: httpx.BaseTransport | None = None) -> tuple[str, str]:
    """登录一个账号，返回 `(token, jar)`。**直连**（不经代理）。

    🔴 2026-10-01 实测订正：auth 域对**池代理出口回 502**（CONNECT 打 auth 域直接
    CONNECT 打 auth.qwen.ai 直接 Bad Gateway），而**直连** signin/refresh 一路绿灯
    （auth 域不拦滑块；IP 级频率墙 ≈12 次/6 分钟，服务的 signin 频率 ≈ 每账号
    30 天一次 + 45s 跨账号节流，远够不着）。旧域"signin 必须走轮换出口"的纪律
    随旧域一起作废；`QWEN_SIGNIN_PROXY` 降级为应急覆盖（当前铸造路径不使用）。

    流程：`GET {chat 源}/auth` 预热（best-effort，收集 WAF 冷启动 cookie）→
    `POST {auth 源}/v2/auths/signin`。token 先读响应体（新契约），回落
    Set-Cookie（旧契约）；jar = 预热 + signin 种下的 cookie 全集，`token=`
    已写入（pair 门：token 与 jar 必须同源）。

    失败分类：挑战页 ⇒ `WallError`；其余（非 200 / 拿不到 token / 传输失败）⇒
    `MintError`。
    """
    digest = hashlib.sha256(password.encode()).hexdigest()
    ua = user_agent or UA_DEFAULT

    kwargs: dict = {"timeout": timeout, "trust_env": False}
    if transport is not None:      # 测试注入：显式 transport（proxy= 会覆盖 transport）
        kwargs["transport"] = transport
    elif proxy_url:
        kwargs["proxy"] = proxy_url

    warm_cookies: list[str] = []
    with httpx.Client(**kwargs) as client:
        try:  # 预热 best-effort：只决定 WAF 冷启动 cookie 的有无，失败不阻断登录
            resp = client.get(f"{base_url}{WARM_PATH}", headers=_warm_headers(ua))
            warm_cookies = _response_set_cookies(resp)
        except httpx.HTTPError:
            pass
        try:
            resp = client.post(
                f"{auth_base.rstrip('/')}{SIGNIN_PATH}",
                content=json.dumps({"email": email, "password": digest}).encode(),
                headers=_signin_headers(ua))
        except httpx.HTTPError as exc:
            raise MintError(f"signin 传输失败：{type(exc).__name__}: {exc}") from exc

    text = resp.content.decode("utf-8", "replace")
    if "aliyun_waf" in text:
        raise WallError("this egress is answering the WAF challenge page")
    if resp.status_code == 502 and "alb" in text:
        raise AlbadError(
            "auth 域 alb 502：先查 auth_base 是否含 /api 前缀（未路由路径一律 502，"
            "2026-10-01 两小时误诊的根源），再查上游是否故障")
    if resp.status_code != 200:
        raise MintError(f"signin answered HTTP {resp.status_code}")
    signed = _response_set_cookies(resp)
    token = _body_access_token(text) or extract_token_from_setcookie(signed)
    if not token:
        raise MintError(
            "signin 回了 200 但没有 token（body access_token / Set-Cookie token= 均无）；"
            f"body 头部：{text[:120]!r}")
    jar = with_token(cookie_header_from_setcookies(warm_cookies + signed), token)
    return token, jar


def refresh_auth(jar: str, *, auth_base: str = AUTH_BASE_DEFAULT,
                 user_agent: str = "", timeout: float = 30.0,
                 proxy_url: str | None = None,
                 transport: httpx.BaseTransport | None = None) -> tuple[str, str]:
    """用会话 jar（内含 refresh_token）换一份新 access token，返回 `(token, jar)`。

    **续期主路**：免密码、免预热、不拦滑块、无 IP 墙（直连即可）。jar 更新两条：
    新 `token=`（写请求认的就是它）与响应体里的 `refresh_token`（有则覆盖；
    实测同一份复用不轮换，但轮换与否不该由我们猜）。

    **best-effort**：任何失败（RT 缺失/失效/网络问题）都返回 `("", 原 jar)`，
    由调用方回落 signin —— 这条捷径不该成为新的失败点。
    """
    if not cookie_entry(jar, "refresh_token"):
        return "", jar
    ua = user_agent or UA_DEFAULT
    headers = {**_signin_headers(ua), "Cookie": jar}
    url = f"{auth_base.rstrip('/')}{REFRESH_PATH}"
    try:
        if transport is not None:  # 测试注入：显式 transport
            with httpx.Client(transport=transport, trust_env=False, timeout=timeout) as client:
                resp = client.get(url, headers=headers)
        elif proxy_url:
            resp = httpx.get(url, headers=headers, timeout=timeout,
                             trust_env=False, proxy=proxy_url)
        else:
            resp = httpx.get(url, headers=headers, timeout=timeout, trust_env=False)
    except httpx.HTTPError:
        return "", jar
    try:
        text = resp.content.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - 解不出体即当失败
        return "", jar
    token = _body_access_token(text)
    if not token:
        return "", jar
    fresh_jar = with_token(jar, token)
    fresh_rt = _body_refresh_token(text)
    if fresh_rt:
        fresh_jar = with_entry(fresh_jar, "refresh_token", fresh_rt)
    return token, fresh_jar
