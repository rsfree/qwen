"""qwen 网页端 HTTP 客户端 —— 头、请求体、响应判读。

🔴 三条最容易做错、且都有实测判决：
  1. **请求头必须完整**：缺 `Sec-Fetch-*` / `Timezone` / `X-Accel-Buffering`、或
     `Accept` 形态不对，会被判自动化 ⇒ `RGV587` 风控（2026-09-18 一整天的误诊，
     根因就是探针请求头不全；修法是逐字段对齐 `biz-api::build_headers`）。
  2. **`version: 0.2.0` 是 write 端点的硬门槛**：缺它 → HTTP 200 +
     `{"code":"Bad_Request"}`（文案像"请求体写错了"，极易误诊）；`chats/new` 不要求。
  3. **查询端点 HTTP 恒 200**：真码在响应头 `x-actual-status-code`；body 里 `success`
     才可信。**不得把 `task_status` 缺失解释成"还在跑"**（会无限轮询）。

chat（t2t）门（2026-09-24 新增）：请求体逐字对齐当日用户抓包；**流式响应事件形态未证实**
（UPSTREAM §9 U-12）⇒ `stream_chat` 的文本提取走宽容多路径，**零提取 ⇒ 响亮失败**。
"""
from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from datetime import datetime

import httpx

from ...config import Settings
from ...errors import (
    AuthenticationError,
    InvalidParameterError,
    NotFoundError,
    QuotaExhaustedError,
    RiskControlError,
    UpstreamError,
    UpstreamTimeoutError,
)
from .accounts import mask_email

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May",
           "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

#: 与抓包逐字一致的提交体固定片段（视频 t2v/i2v：thinking 关）。
FEATURE_CONFIG = {
    "thinking_enabled": False,
    "output_schema": "phase",
    "research_mode": "normal",
    "auto_thinking": False,
    "thinking_mode": "Fast",
    "auto_search": True,
}

#: t2t 思考档位 —— 2026-09-24 前端抓包逐字（"自动/思考/快速"三档，UI 选择器"自动 ▾"）：
#:   自动 = thinking_mode "Auto" + auto_thinking true（前端默认）；
#:   思考 = thinking_mode "Thinking" + auto_thinking false（强制思考）；
#:   快速 = **thinking_enabled false**（关思考 —— 提速档，首字最快）。
#: 共同：output_schema "phase"、research_mode "normal"、auto_search true。
THINKING_GEARS: dict[str, dict] = {
    "auto": {
        "thinking_enabled": True, "output_schema": "phase", "research_mode": "normal",
        "auto_thinking": True, "thinking_mode": "Auto", "thinking_format": "summary",
        "auto_search": True,
    },
    "thinking": {
        "thinking_enabled": True, "output_schema": "phase", "research_mode": "normal",
        "auto_thinking": False, "thinking_mode": "Thinking", "thinking_format": "summary",
        "auto_search": True,
    },
    "fast": {
        "thinking_enabled": False, "output_schema": "phase", "research_mode": "normal",
        "auto_thinking": False, "thinking_mode": "Fast", "auto_search": True,
    },
}

#: t2t 抓包（2026-09-24）的 feature_config —— **逐字**：thinking 开、thinking_mode=Thinking、
#: thinking_format=summary（视频片段里没有这个键）。别拿它跟 FEATURE_CONFIG"合并"。
#: 🔴 默认档位已对齐前端"自动"档（thinking_mode Auto）——`THINKING_GEARS["auto"]`。
CHAT_FEATURE_CONFIG = dict(THINKING_GEARS["auto"])


def tz_header() -> str:
    now = datetime.now()
    return (f"{_WEEKDAYS[now.weekday()]} {_MONTHS[now.month - 1]} {now.day:02d} "
            f"{now.year} {now.hour:02d}:{now.minute:02d}:{now.second:02d} GMT+0800")


def extract_stream_text(event: object) -> str:
    """从一条 SSE 事件里提取**增量文本**（形态 2026-09-24 实测，见 UPSTREAM §4.5）。

    覆盖的候选路径（按序尝试，取到即返回；宽容多路径 = 防上游改形态时静默挂死）：
      · `choices[0].delta.content`（实测路径，`phase:"answer"` 的正文增量）
      · `choices[0].message.content`（整段形态）
      · `data.choices[0].delta.content` / `data.choices[0].message.content`
        （上游其它端点习惯把业务体包在 `data` 里，防它把这套习惯带进 SSE）
    `phase:"thinking_summary"` 事件的 content 恒为空串 ⇒ 天然不提取（thinking 摘要在
    `extra.summary_*` 里，v1 不透传）；`reasoning_content` 等旁路字段同样不提取。
    """
    if not isinstance(event, dict):
        return ""
    candidates: list[object] = [event, event.get("data")]
    for root in candidates:
        if not isinstance(root, dict):
            continue
        choices = root.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        first = choices[0]
        if not isinstance(first, dict):
            continue
        for holder in (first.get("delta"), first.get("message")):
            if isinstance(holder, dict) and isinstance(holder.get("content"), str) \
                    and holder["content"]:
                return holder["content"]
    return ""


def extract_stream_error(event: object) -> str:
    """流内错误事件（**HTTP 200 包着错误**，2026-09-24 实测两种形态）：
    `{"error": "Internal error!"}` 与 `{"error": {"code": "invalid_input", "details": …}}`。
    有错误给一句可读描述，没有回空串。"""
    if not isinstance(event, dict) or "error" not in event:
        return ""
    error = event.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        code = str(error.get("code") or "")
        details = str(error.get("details") or error.get("message") or "")
        return f"{code} {details}".strip() or json.dumps(error, ensure_ascii=False)
    return str(error)


def extract_stream_finished(event: object) -> bool:
    """"结束"事件判据（2026-09-24 实测：`delta.status == "finished"` **且 `phase == "answer"`**；
    流没有 `data: [DONE]`。⚠️ thinking 摘要的结束事件也带 `status:"finished"` ——
    只差 phase 一个键，判宽了会把整条流在思考阶段就掐断）。"""
    if not isinstance(event, dict):
        return False
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return False
    delta = choices[0].get("delta")
    return (isinstance(delta, dict) and delta.get("status") == "finished"
            and delta.get("phase") == "answer")


# ------------------------------------------------------------------ 拒绝归类：额度 vs 积分


#: 归到"额度耗尽"的**码**（已证实：`RateLimited` 是图片/视频侧的日常额度码；
#: `quota_limit` 是 3.0-pro 的流内额度码 —— 均来自参考脚本实测）。
QUOTA_CODES = ("RateLimited", "quota_limit")
#: 归到"额度耗尽"的**词**。分两级证据：
#:   · 已证实：`额度` / `quota`（本仓 09-22 起就按这两个词归类）；
#:   · 🔴 **未实测**：`积分` / `credits` / `credit` / `insufficient` / `not enough` /
#:     `余额` —— 2026-10-02 才确认视频按 **25 积分/条**计费、每日免费 +40 且**日清**，
#:     但"积分不足时上游到底回什么文案"尚未抓到实证（余额 15 分的第二单一撞滑块就没跑成）。
#:     这里按关键词保守匹配：**命中即换号重试**，判据是"积分/额度不足 ⇒ 上游根本没开始
#:     生成 ⇒ 未计费 ⇒ 重试安全"；命中不了仍按原样落 `UpstreamError`（不瞎猜）。
QUOTA_WORDS_PROVEN = ("额度", "quota")
QUOTA_WORDS_UNPROVEN = ("积分", "credits", "credit", "insufficient",
                        "not enough", "余额")


def _is_quota_or_credit_refusal(code: str, details: str) -> bool:
    """这次拒绝是"没额度/没积分"吗 ⇒ 是则换号重试（未受理 ⇒ 未计费 ⇒ 重试安全）。"""
    if code in QUOTA_CODES:
        return True
    low = f"{code} {details}".lower()
    if "额度" in details or "quota" in low:
        return True
    return any(w in low for w in QUOTA_WORDS_UNPROVEN)


class QwenClient:
    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self._client = httpx.Client(
            base_url=settings.base_url,
            timeout=settings.upstream_timeout,
            transport=transport,
            trust_env=settings.trust_env,
        )

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ 头部

    def headers(self, token: str | None = None, *, referer: str | None = None,
                extra_cookies: str = "") -> dict[str, str]:
        """请求头（照 `biz-api::build_headers` 逐字段对齐）。

        `token` 参数实为**凭据**，两种形态按形状自动识别（pair 门，2026-09-30 起）：
          · **完整 jar**（含 `;` 分隔的多个 cookie，或以 `token=` 开头）——账号池
            `credential_for()` 给的同源会话 jar（`token=` + `refresh_token=` +
            WAF 冷启动章），**原样**作为 `Cookie` 发出 —— token 与 jar 同源是
            身份自洽的前提（token 配别人的 jar ⇒ x5sec，2026-09-22 实测）；
          · **裸 JWT**（无 `;` 不以 `token=` 开头）—— 旧形态，包成 `token=<jwt>`。
        `None` ⇒ 不带 `Cookie`（`GET /api/models` 实测免鉴权，见 UPSTREAM §1）；
        `extra_cookies` 只在给了凭据时拼接（无凭据的公共端点没有"附加 cookie"语义）。
        """
        s = self.settings
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": s.user_agent,
            "Origin": s.base_url,
            "Referer": referer or f"{s.base_url}/",
            "source": "web",
            "version": s.version_header,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Timezone": tz_header(),
            "X-Request-Id": str(uuid.uuid4()),
            "X-Accel-Buffering": "no",
            "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
        }
        if token or extra_cookies:
            is_jar = bool(token) and (";" in token or token.startswith("token="))
            cookie = token if is_jar else (f"token={token}" if token else "")
            if extra_cookies:
                cookie = f"{cookie}; {extra_cookies}" if cookie else extra_cookies
            headers["Cookie"] = cookie
        return headers

    # ------------------------------------------------------------------ 判读

    def _decode(self, resp: httpx.Response, *, op: str, raise_business: bool = True) -> dict:
        """解包 + 归类。`raise_business=False` 时把 `success:false` 原样返回 ——
        查询端点用它：404 / 401 是**任务状态**，由 service 层解释，不是抛异常的场景。"""
        text = resp.text or ""
        if "aliyun_waf" in text:
            raise UpstreamError(f"{op}: 上游返回 WAF 挑战页（凭据/出口问题）")
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            ret = payload.get("ret")
            if isinstance(ret, list) and any("RGV587" in str(x) or "FAIL_SYS" in str(x) for x in ret):
                raise RiskControlError(
                    f"{op}: 上游 x5sec 风控（RGV587）—— 按 429 退避，勿连续重试", retry_after=60.0)
        if "RGV587" in text or "FAIL_SYS_USER_VALIDATE" in text:
            raise RiskControlError(f"{op}: 上游 x5sec 风控（RGV587）", retry_after=60.0)
        if resp.status_code >= 400:
            raise UpstreamError(f"{op}: 上游 HTTP {resp.status_code}: {text[:200]}")
        if payload is None:
            raise UpstreamError(f"{op}: 上游响应不是 JSON（{resp.headers.get('content-type', '?')}）")
        if raise_business and isinstance(payload, dict) and payload.get("success") is False:
            self._raise_business_error(payload, op=op)
        return payload

    def _raise_business_error(self, payload: dict, *, op: str) -> None:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        code = str(payload.get("code") or data.get("code") or "")
        details = str(payload.get("details") or data.get("details") or "")
        if code in ("Bad_Request",) or "Bad_Request" in details:
            raise InvalidParameterError(
                f"{op}: 上游判 Bad_Request —— 若确认请求体与抓包一致，先检查 `version: "
                f"{self.settings.version_header}` 头是否存在（缺该头必被判 Bad_Request）。"
                + (f" 上游原文：{details[:160]}" if details else ""))
        if code in ("Unauthorized",) or "Unauthorized" in details:
            raise AuthenticationError(f"{op}: 上游凭据失效（Unauthorized）")
        if code in ("Not_Found",) or "Task not found" in details or "CHAT_NOT_FOUND" in details:
            raise NotFoundError(f"{op}: 上游不存在（Not_Found）：{details[:160] or code}")
        if _is_quota_or_credit_refusal(code, details):
            raise QuotaExhaustedError(f"{op}: 上游额度/积分已用尽：{details[:160] or code}")
        raise UpstreamError(f"{op}: 上游拒绝：{code or '(无码)'} {details[:200]}")


    # ------------------------------------------------------------------ 端点

    def new_chat(self, token: str, *, chat_type: str = "t2v", model: str | None = None,
                 extra_cookies: str = "") -> str:
        """建会话。返回 `data.id`（作 chat_id，可长期复用）。

        `chat_type` / `model` 可参数化：chat（t2t）门用 `chat_type="t2t"` + 请求的模型
        （⚠️ 这两个参数对 t2t 会话的影响未证实，见 UPSTREAM §9 U-13；视频门行为不变）。
        """
        body = {
            "title": "New Chat",
            "models": [model or self.settings.chat_model],
            "chat_mode": "normal",
            "chat_type": chat_type,
            "timestamp": int(time.time() * 1000),
            "project_id": "",
        }
        resp = self._client.post(
            "/api/v2/chats/new", json=body,
            headers=self.headers(token, referer=f"{self.settings.base_url}/",
                                 extra_cookies=extra_cookies))
        payload = self._decode(resp, op="chats/new")
        chat_id = str(((payload.get("data") or {}).get("id")) or "").strip()
        if not chat_id:
            raise UpstreamError("chats/new 未返回 data.id")
        return chat_id

    def build_submit_body(self, chat_id: str, *, prompt: str, ratio: str, chat_type: str,
                          image_url: str | None, chat_model: str | None = None,
                          ts: int | None = None) -> dict:
        """视频提交体构造（纯函数；dry_run 也走这里，保证"预演即真发"）。"""
        from ... import media

        model = chat_model or self.settings.chat_model
        now = int(ts if ts is not None else time.time())
        message: dict = {
            "id": None,
            "fid": str(uuid.uuid4()),
            "parentId": None,
            "childrenIds": [str(uuid.uuid4())],
            "role": "user",
            "content": prompt,
            "user_action": "chat",
            "timestamp": now,
            "models": [model],
            "model": "",
            "chat_type": chat_type,
            "feature_config": dict(FEATURE_CONFIG),
            "extra": {"meta": {"subChatType": chat_type, "size": ratio}},
            "sub_chat_type": chat_type,
            "parent_id": None,
        }
        if image_url:
            message["files"] = [media.image_entry(image_url)]
        return {
            "stream": False,
            "version": "2.1",
            "incremental_output": True,
            "chatId": chat_id,
            "parentId": "",
            "chat_id": chat_id,
            "chat_mode": "normal",
            "model": model,
            "parent_id": None,
            "messages": [message],
            "timestamp": now,
            "size": ratio,
        }

    def build_chat_submit_body(self, chat_id: str, *, model: str, prompt: str,
                               files: list[dict] | None = None,
                               ts: int | None = None,
                               gear: str = "auto") -> dict:
        """t2t 提交体 —— 逐字对齐 2026-09-24 用户抓包（纯函数；dry_run 也走这里）。

        与视频体（`build_submit_body`）的实证差异：
          · `stream: True`（视频用 stream:false 拿同步 task_id；t2t 抓包即流式）；
          · **没有 `size`**（顶层与 extra.meta 都没有 —— 文本任务无画幅）；
          · `feature_config` 按**思考档位**取（`THINKING_GEARS`：auto/thinking/fast，
            前端三档抓包逐字；缺省 auto = 前端默认）。
        与视频体**相同**的（2026-09-24 真实一发已证）：
          · 顶层 `chatId` 与小写 `chat_id` **双写**（缺小写 ⇒ 上游 400
            `RequestValidationError: Field 'chat_id': Field required`）；
          · `messages[0].id` 为 `null`；
          · `messages[0].files` 恒在（纯文本为 `[]`；多模态解析时放 files 条目）。
        """
        now = int(ts if ts is not None else time.time())
        message = {
            "id": None,
            "fid": str(uuid.uuid4()),
            "parentId": None,
            "childrenIds": [str(uuid.uuid4())],
            "role": "user",
            "content": prompt,
            "user_action": "chat",
            "files": list(files or []),
            "timestamp": now,
            "models": [model],
            "model": "",
            "chat_type": "t2t",
            "feature_config": dict(THINKING_GEARS.get(gear) or CHAT_FEATURE_CONFIG),
            "extra": {"meta": {"subChatType": "t2t"}},
            "sub_chat_type": "t2t",
            "parent_id": None,
        }
        return {
            "stream": True,
            "version": "2.1",
            "incremental_output": True,
            "chatId": chat_id,
            "parentId": "",
            "chat_id": chat_id,
            "chat_mode": "normal",
            "model": model,
            "parent_id": None,
            "messages": [message],
            "timestamp": now,
        }

    def submit_video(self, token: str, *, chat_id: str, prompt: str, ratio: str,
                     chat_type: str, image_url: str | None, extra_cookies: str = "",
                     chat_model: str | None = None) -> str:
        """提交生成；`stream:false` 时**同步**返回 task_id。"""
        body = self.build_submit_body(chat_id, prompt=prompt, ratio=ratio,
                                      chat_type=chat_type, image_url=image_url,
                                      chat_model=chat_model)
        try:
            resp = self._client.post(
                "/api/v2/chat/completions", params={"chat_id": chat_id}, json=body,
                headers=self.headers(token,
                                     referer=f"{self.settings.base_url}/c/{chat_id}",
                                     extra_cookies=extra_cookies))
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError(f"提交超时：{exc}") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(f"提交传输失败：{type(exc).__name__}: {exc}") from exc
        payload = self._decode(resp, op="chat/completions")
        messages = ((payload.get("data") or {}).get("messages") or [])
        task_id = ""
        if messages and isinstance(messages[0], dict):
            wanx = (messages[0].get("extra") or {}).get("wanx") or {}
            task_id = str(wanx.get("task_id") or "").strip()
        if not task_id:
            raise UpstreamError(
                "提交响应里没有 task_id（期望路径 data.messages[0].extra.wanx.task_id）")
        return task_id

    def stream_chat(self, token: str, chat_id: str, body: dict, *,
                    extra_cookies: str = "", meta: dict | None = None
                    ) -> Iterator[tuple[str, str]]:
        """提交 t2t 并打开上游流，返回 `(kind, text)` 增量迭代器。

        kind ∈ {"answer", "reasoning"}：answer = 正文增量（翻译层转 `delta.content`）；
        reasoning = 思考摘要增量（`thinking_summary.extra` 的分步标题/要点，
        转成 `delta.reasoning_content` —— 官网 UI 同款数据，2026-09-24 实测）。

        🔴 请求建立（HTTP 状态 / `x-actual-status-code` / WAF / RGV587）在**返回前**完成：
        失败在这里就抛，调用方还能回正经 HTTP 错误；进入迭代后的失败只能走流内错误事件。
        🔴 401 在这里抛 `AuthenticationError` —— `_authed_call` 会重铸 token 后重试一次
        （上游 401 = 未受理，重试不会重复提交）。

        流形态（2026-09-24 单发实测，UPSTREAM §4.5；U-12 已关闭）：
          · 增量正文在 `choices[0].delta.content`（`phase:"answer"`）；
          · `phase:"thinking_summary"` 事件 content 恒空，**思考摘要在 `delta.extra`**
            （summary_title / summary_thought 数组逐步累加）⇒ diff 后走 reasoning 通道；
          · `data: {"error": …}` = **HTTP 200 里的流内错误事件** ⇒ 响亮失败（绝不静默吞）；
          · `delta.status=="finished"` = 结束事件（流**没有** `data: [DONE]`）⇒ 读到即收流；
          · `usage`（input/output/total_tokens）随 answer 事件出现且**逐事件递增** ⇒
            写入 `meta["usage"]`（最后一份即终值，真实数据，由调用方透传）。
        """
        headers = self.headers(token, referer=f"{self.settings.base_url}/c/{chat_id}",
                               extra_cookies=extra_cookies)
        try:
            request = self._client.build_request(
                "POST", "/api/v2/chat/completions", params={"chat_id": chat_id},
                json=body, headers=headers)
            # 🔴 流式 read 单独放宽（per-request extensions）：thinking 期上游静默
            # 可达 50s+（U-18），客户端级 60s read 会把长思考误杀成 UpstreamTimeoutError。
            # 注意 httpx 的 per-request timeout 是 **dict** 形态（传 Timeout 对象会
            # AttributeError: 'Timeout' object has no attribute 'get'，2026-09-24 实测踩坑）。
            request.extensions["timeout"] = {
                "connect": self.settings.upstream_timeout,
                "read": 180.0,
                "write": self.settings.upstream_timeout,
                "pool": self.settings.upstream_timeout,
            }
            resp = self._client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError(f"chat 提交超时：{exc}") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(f"chat 提交传输失败：{type(exc).__name__}: {exc}") from exc

        raw_actual = resp.headers.get("x-actual-status-code", "")
        try:
            actual = int(raw_actual) if raw_actual.strip().isdigit() else resp.status_code
        except ValueError:  # pragma: no cover - 防御
            actual = resp.status_code
        if resp.status_code >= 400 or actual >= 400:
            snippet = resp.read()[:200].decode("utf-8", "replace")
            resp.close()
            if actual == 401 or resp.status_code == 401:
                raise AuthenticationError("chat 提交：上游凭据失效（Unauthorized）")
            if actual == 404:
                raise NotFoundError("chat 提交：上游会话不存在")
            raise UpstreamError(
                f"chat 提交：上游 HTTP {resp.status_code}/actual {actual}: {snippet[:200]}")

        def iterate() -> Iterator[tuple[str, str]]:
            """产出 `(kind, text)`：kind ∈ {"answer", "reasoning"}。

            🔴 reasoning 通道（2026-09-24 实测，用户报「接口看不到思考内容」）：
            `thinking_summary` 事件的 `delta.extra.summary_title / summary_thought`
            是**逐步累加**的数组（官网 UI 的分步思考摘要）——对上一事件做 diff，
            新增条目格式化为文本，经 `delta.reasoning_content` 透传（真实上游数据，
            非编造；U-18 的「content 恒空」仅指 delta.content，extra 里一直有货）。
            """
            extracted = 0
            seen_title = 0
            seen_thought = 0

            def _reasoning_from(delta: dict) -> str:
                nonlocal seen_title, seen_thought
                extra = delta.get("extra") or {}
                parts: list[str] = []
                titles = ((extra.get("summary_title") or {}).get("content")) or []
                thoughts = ((extra.get("summary_thought") or {}).get("content")) or []
                for t in titles[seen_title:]:
                    parts.append(f"【{str(t).strip()}】")
                seen_title = len(titles)
                for t in thoughts[seen_thought:]:
                    parts.append(str(t).strip())
                seen_thought = len(thoughts)
                return "\n".join(p for p in parts if p)

            try:
                for line in resp.iter_lines():
                    if not line:
                        continue
                    if "aliyun_waf" in line:
                        raise UpstreamError("chat 流：上游返回 WAF 挑战页（凭据/出口问题）")
                    if "RGV587" in line or "FAIL_SYS_USER_VALIDATE" in line:
                        raise RiskControlError(
                            "chat 流：上游 x5sec 风控（RGV587）", retry_after=60.0)
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":     # 实测流没有 DONE；容忍网关注入
                        break
                    try:
                        event = json.loads(payload)
                    except ValueError:
                        continue    # 心跳/注释行：忽略，不算失败
                    if not isinstance(event, dict):
                        continue
                    error_text = extract_stream_error(event)
                    if error_text:
                        raise UpstreamError(f"chat 流：上游流内错误事件：{error_text[:200]}")
                    if isinstance(event.get("usage"), dict) and meta is not None:
                        meta["usage"] = event["usage"]
                    delta = (event.get("choices") or [{}])[0].get("delta", {}) \
                        if isinstance(event.get("choices"), list) else {}
                    if str(delta.get("phase") or "") == "thinking_summary":
                        reasoning = _reasoning_from(delta)
                        if reasoning:
                            yield ("reasoning", reasoning)
                    text = extract_stream_text(event)
                    if text:
                        extracted += len(text)
                        yield ("answer", text)
                    if extract_stream_finished(event):
                        break
            finally:
                resp.close()
            if extracted == 0:
                raise UpstreamError(
                    "chat 流：上游流式响应未提取到任何文本 —— SSE 事件形态与实测不符"
                    "（对照 UPSTREAM §4.5；请带原始响应到 docs/UPSTREAM.md 登记）")

        return iterate()

    def list_upstream_models(self) -> list[dict]:
        """`GET /api/models` —— 上游模型清单（**免鉴权**，2026-09-24 实测：无任何 cookie 即 200）。

        只读、免费、短超时（5s）：它挂在免 Key 的 `/v1/models` 后面，不能拖慢能力探测。
        返回原始条目列表（映射/过滤在 `app/models.py`）。
        """
        try:
            resp = self._client.get("/api/models", headers=self.headers(None), timeout=5.0)
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError(f"api/models 超时：{exc}") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(f"api/models 传输失败：{type(exc).__name__}: {exc}") from exc
        payload = self._decode(resp, op="api/models")
        data = payload.get("data") if isinstance(payload.get("data"), list) else []
        return [item for item in data if isinstance(item, dict)]

    def upload_attachment(self, token: str, *, kind: str, filename: str,
                          content_type: str, data: bytes,
                          extra_cookies: str = "") -> dict:
        """附件上传链：getstsToken → OSS V4 签名 PUT → files[] 条目（UPSTREAM §4.7 实测契约）。

        🔴 预签名 `file_url` 不可用（SignatureDoesNotMatch）——必须用 STS 凭证自签，
        派生前缀是 **`aliyun_v4`**（非 aliyun_v4_request，见 `upload.py` docstring）。
        """
        from .upload import upload_attachment as _upload

        return _upload(self._client, token, kind=kind, filename=filename,
                       content_type=content_type, data=data,
                       headers_fn=lambda tok, referer=None: self.headers(
                           tok, referer=referer, extra_cookies=extra_cookies))

    def task_status(self, token: str, task_id: str, *, extra_cookies: str = "") -> dict:
        """查询任务。返回 `{actual_status_code, success, data}`（不做状态解释）。"""
        try:
            resp = self._client.get(
                f"/api/v2/task/status/{task_id}",
                headers=self.headers(token, referer=f"{self.settings.base_url}/",
                                     extra_cookies=extra_cookies))
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError(f"查询超时：{exc}") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(f"查询传输失败：{type(exc).__name__}: {exc}") from exc
        raw_actual = resp.headers.get("x-actual-status-code")
        try:
            actual = int(raw_actual) if raw_actual else resp.status_code
        except ValueError:
            actual = resp.status_code
        payload = self._decode(resp, op="task/status", raise_business=False)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return {"actual_status_code": actual, "success": bool(payload.get("success")), "data": data}


__all__ = [
    "CHAT_FEATURE_CONFIG",
    "THINKING_GEARS",
    "FEATURE_CONFIG",
    "QwenClient",
    "extract_stream_text",
    "mask_email",
    "tz_header",
]
