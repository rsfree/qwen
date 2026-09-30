"""业务编排 —— 创建 / 查询 / 轮询 / **轻量排队重试**；账号归属与凭据绑定在这里收口。

职责边界：
  · `ark.py` 只做纯翻译（无 IO）；`openai_chat.py` 同理（OpenAI chat 门）；
  · `upstream/qwen/*` 只做上游交互（不写任务表）；
  · **本模块**决定"谁来跑、状态怎么落、错误怎么分类回报、要不要排队重试"。

轻量排队重试（`SUBMIT_QUEUE_ENABLED=1`，默认开；**复用任务表 + 协调器 + 账号池三件现有件**）：
  · **排队**：容量不足（账号全忙/冷却/额度尽）⇒ 不再回 429，而是落库为 `queued` 并立即
    返回 `cgt-…`（Ark 契约里 queued 是合法初始态）——由协调器或后续 GET 出队提交；
  · **重试**：只重试**可证明未提交**的失败（风控/额度/鉴权/铸造失败/会话失效）——
    建任务是计费动作，**含义不明的失败（5xx/超时）绝不自动重试**（防重复计费）；
  · **背压仍保留**：排队深度超 `QUEUE_MAX_DEPTH` ⇒ 照旧 429 + Retry-After；
  · **重启不丢**：queued / running 都在任务表里；重启后协调器 / GET 继续推进。

chat 门（2026-09-24 新增，`chat_stream`）：**同步链路、无任务表、无排队** ——
chat 不消耗视频额度（取号走 `acquire_chat`，不看 `day_used`），失败照常按类冷却账号；
每个请求新建上游会话（无状态；多轮历史由调用方自带，见 `openai_chat.py` 的拍平口径）。
"""
from __future__ import annotations

import logging
import random
import string
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

from .ark import CreatePlan, ark_task_view, translate_ark_create
from .config import Settings
from .errors import (
    AdapterError,
    AuthenticationError,
    CredentialUnavailableError,
    NotFoundError,
    QuotaExhaustedError,
    RateLimitedError,
    RiskControlError,
    UpstreamError,
    UpstreamTimeoutError,
)
from .openai_chat import ChatRequest
from .store import TERMINAL_STATUSES, TaskRecord, TaskStore
from .upstream.qwen.accounts import AccountPool, AccountState, mask_email
from .upstream.qwen.client import QwenClient

logger = logging.getLogger("qwen.service")

_LOCAL_ID_ALPHABET = string.ascii_lowercase + string.digits


class QwenVideoService:
    def __init__(self, settings: Settings, store: TaskStore, pool: AccountPool,
                 client: QwenClient, *, attachment_resolver=None) -> None:
        self.settings = settings
        self.store = store
        self.pool = pool
        self.client = client
        #: 附件来源解析器（http(s)/data: → bytes）：默认 `media.resolve_attachment`
        #: （SSRF 防护 + 大小上限）；测试可注入假实现。
        from . import media as _media
        self._resolve = attachment_resolver or _media.resolve_attachment
        #: 同一任务的提交尝试互斥：协调器与 GET 可能并发推进同一条 queued 记录
        self._submit_guard = threading.Lock()
        self._submitting: set[str] = set()

    # ------------------------------------------------------------------ 创建（方舟门）

    def create(self, body: dict, credential_id: str, *, dry_run: bool = False) -> dict:
        """方舟创建请求 → `{"id": "cgt-…"}`（dry_run 时返回"将要发出的请求"）。"""
        plan = translate_ark_create(body)
        if dry_run:
            return self._dry_run_view(plan)

        try:
            account = self.pool.acquire_with_wait()
        except RateLimitedError as exc:
            if not self.settings.submit_queue_enabled:
                raise
            # 轻量排队：容量不足不再硬失败 —— 落 queued，等协调器 / GET 出队
            return {"id": self._enqueue(plan, credential_id,
                                        next_delay=self._clamp_delay(exc.retry_after))}

        try:
            record = self._submit_once(plan, account, credential_id)
        except AdapterError as exc:
            self._report_submit_failure(account.email, exc)
            if self.settings.submit_queue_enabled and self._retryable_unsubmitted(exc):
                return {"id": self._enqueue(plan, credential_id, attempts=1,
                                            next_delay=self.settings.queue_retry_base)}
            raise
        return {"id": record.local_id}

    def _dry_run_view(self, plan: CreatePlan) -> dict:
        """跑完完整翻译、直接返回将要发出的 payload —— **不提交、不占号、不落库**。"""
        headers = self.client.headers("<token>", referer=f"{self.settings.base_url}/c/<chat_id>")
        headers["Cookie"] = "token=<redacted>"
        return {
            "dry_run": True,
            "upstream": {
                "method": "POST",
                "url": f"{self.settings.base_url}/api/v2/chat/completions?chat_id=<chat_id>",
                "headers": headers,
                "body": self.client.build_submit_body(
                    "<chat_id>", prompt=plan.prompt, ratio=plan.ratio,
                    chat_type=plan.chat_type, image_url=plan.image_url),
            },
            "degradations": plan.degradations,
        }

    @staticmethod
    def _clamp_delay(retry_after: float | None) -> float:
        return float(min(max(int(retry_after or 5), 5), 120))

    def _enqueue(self, plan: CreatePlan, credential_id: str, *, attempts: int = 0,
                 next_delay: float | None = None) -> str:
        """轻量队列的入队口：落一条 `queued` 记录。深度超限 ⇒ 429 背压。"""
        depth = self.store.count_queued()
        if depth >= self.settings.queue_max_depth:
            raise RateLimitedError(
                f"提交队列已满（{depth}/{self.settings.queue_max_depth}）—— 背压保留",
                retry_after=60.0)
        now = int(time.time())
        record = TaskRecord(
            local_id=self._new_local_id(now),
            credential_id=credential_id,
            model_requested=plan.model_requested,
            prompt=plan.prompt,
            ratio=plan.ratio,
            image_url=plan.image_url or "",
            status="queued",
            attempts=attempts,
            next_attempt_at=now + int(next_delay if next_delay is not None
                                      else self.settings.queue_retry_base),
        )
        record.set_degradations(plan.degradations)
        self.store.put(record)
        logger.info("任务入队 %s（attempts=%s，深度=%s）", record.local_id, attempts, depth + 1)
        return record.local_id

    def _extra_cookies(self, email: str) -> str:
        return (self.settings.account_cookies or {}).get(email, "")

    def _authed_call(self, email: str, fn: Callable[[str], Any]) -> Any:
        """带该账号的**凭据**（同源 jar，含 token；pair 门）调用 `fn(credential)`；
        **判 401 时立即重铸凭据并重试一次**。

        这是"token 提前失效"的**兜底**：auth 域 access token 只有 15 分钟，服务端也可能
        更早失效 ⇒ 与其等下一次调用，不如当场重铸重试（重铸先走 refresh，RT 失效才
        signin）。判据也成立：401 = 上游**未受理**，重试不会重复计费。

        只重试**一次**：第二次仍 401 ⇒ 不是"token 过期"，而是凭据/账号本身的问题 ⇒
        `report_failure(auth)`（冷却 900s）后照实上抛。
        """
        attempts = 2
        for attempt in range(1, attempts + 1):
            credential = self.pool.credential_for(email)
            try:
                return fn(credential)
            except AuthenticationError:
                if attempt == attempts:
                    self.pool.report_failure(email, "auth")
                    raise
                logger.warning("上游判 401 —— 立即重铸 token 并重试一次（%s）", mask_email(email))
                self.pool.invalidate_token(email)

    def _submit_once(self, plan: CreatePlan, account: AccountState, credential_id: str,
                     record: TaskRecord | None = None) -> TaskRecord:
        """在指定账号上提交一次；成功即落库（`record` 为空则新建一条）。

        ⚠️ 本函数**不**做失败回报（由调用方分类）；异常原样上抛。
        """
        email = account.email
        extra = self._extra_cookies(email)

        def attempt(token: str) -> tuple[str, str]:
            """token → (task_id, chat_id)；会话被上游回收时重建一次（`CHAT_NOT_FOUND`）。"""
            chat_id = self._chat_id_for(account, token, extra)
            try:
                return self._submit(token, chat_id, plan, extra), chat_id
            except NotFoundError:
                # 会话可能被上游回收（"CHAT_NOT_FOUND"）—— 重建一次，只重试这一种
                logger.warning("chat_id 失效，重建后重试一次（%s）", mask_email(email))
                self.store.kv_delete(f"chat:{email}")
                chat_id = self._chat_id_for(account, token, extra)
                return self._submit(token, chat_id, plan, extra), chat_id

        task_id, chat_id = self._authed_call(email, attempt)

        self.pool.report_submitted(email)
        now = int(time.time())
        rec = record or TaskRecord(local_id=self._new_local_id(now))
        rec.upstream_task_id = task_id
        rec.chat_id = chat_id
        rec.account = email
        rec.credential_id = credential_id
        rec.model_requested = plan.model_requested
        rec.prompt = plan.prompt
        rec.ratio = plan.ratio
        rec.image_url = plan.image_url or ""
        rec.status = "running"
        rec.error_code = None
        rec.error_message = None
        rec.set_degradations(plan.degradations)
        return self.store.put(rec)

    def _submit(self, token: str, chat_id: str, plan: CreatePlan, extra: str) -> str:
        return self.client.submit_video(
            token, chat_id=chat_id, prompt=plan.prompt, ratio=plan.ratio,
            chat_type=plan.chat_type, image_url=plan.image_url, extra_cookies=extra)

    def _report_submit_failure(self, email: str, exc: AdapterError) -> None:
        if isinstance(exc, RiskControlError):
            self.pool.report_failure(email, "risk")
        elif isinstance(exc, AuthenticationError):
            self.pool.report_failure(email, "auth")
        elif isinstance(exc, QuotaExhaustedError):
            self.pool.report_failure(email, "quota")
        elif isinstance(exc, (UpstreamError, UpstreamTimeoutError)):
            self.pool.report_failure(email, "transport")
        else:
            self.pool.report_failure(email, "refused")

    @staticmethod
    def _retryable_unsubmitted(exc: AdapterError) -> bool:
        """**可证明上游未受理**的失败集合（未计费/未占额度）⇒ 可安全排队重试。

        ⚠️ 含义不明的失败（`UpstreamError` / `UpstreamTimeoutError`）**刻意不在其中**：
        建任务是计费动作，"可能已经提交成功"的重试等于赌重复计费。
        """
        return isinstance(exc, (RiskControlError, QuotaExhaustedError, AuthenticationError,
                                CredentialUnavailableError, NotFoundError))

    def _chat_id_for(self, account: AccountState, token: str, extra: str) -> str:
        key = f"chat:{account.email}"
        cached = self.store.kv_get(key)
        if cached:
            return cached
        chat_id = self.client.new_chat(token, extra_cookies=extra)
        self.store.kv_set(key, chat_id)
        return chat_id

    @staticmethod
    def _new_local_id(now: int) -> str:
        stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime(now))
        tail = "".join(random.choices(_LOCAL_ID_ALPHABET, k=5))
        return f"cgt-{stamp}-{tail}"

    # ------------------------------------------------------------------ chat 门（OpenAI）

    def chat_dry_run(self, req: ChatRequest) -> dict:
        """chat 门的预演：跑完翻译直接返回"将要发出的请求"——**不提交、不占号**。"""
        headers = self.client.headers("<token>", referer=f"{self.settings.base_url}/c/<chat_id>")
        headers["Cookie"] = "token=<redacted>"
        return {
            "dry_run": True,
            "upstream": {
                "method": "POST",
                "url": f"{self.settings.base_url}/api/v2/chat/completions?chat_id=<chat_id>",
                "headers": headers,
                "body": self.client.build_chat_submit_body(
                    "<chat_id>", model=req.model, prompt=req.prompt,
                    files=req.files or None, gear=req.thinking_gear),
            },
            "degradations": req.degradations,
        }

    def chat_stream(self, req: ChatRequest, meta: dict | None = None
                    ) -> Iterator[tuple[str, str]]:
        """chat（t2t）→ 上游增量文本流。

        · 每次请求**新建上游会话**（无状态；OpenAI 客户端自带完整历史）；
        · 失败回报沿用视频门的分类；**QuotaExhausted 例外**：chat 不消耗视频额度，
          按"refused"短冷（60s）而不是把账号冷到 UTC 日界（那是视频额度语义）；
        · 取号走 `acquire_chat_with_wait`（不看视频额度）；等不到 ⇒ 429 背压
          （chat 是同步链路，没有任务表可排队）；
        · **附件上传链**（2026-09-24）：file/audio/video/data: 图 附件在拿到账号 token 后
          「解析来源 → getstsToken → OSS V1 PUT → files[] 条目」再提交（UPSTREAM §4.7）；
        · 产出 `(kind, text)`：answer = 正文（转 `delta.content`）；reasoning = 思考摘要
          （上游 thinking_summary.extra 的分步增量，转 `delta.reasoning_content`，
          官网 UI 同款数据，2026-09-24 实测）。
        · `meta`：可选的回传口袋 —— 上游流里的 `usage`（真实值，最后一份）写在
          `meta["usage"]`，由路由层透传（见 `openai_chat.openai_usage`）。
        """
        account = self.pool.acquire_chat_with_wait()
        email = account.email
        extra = self._extra_cookies(email)

        try:
            def attempt(token: str) -> Iterator[str]:
                files = list(req.files)
                if req.attachment:
                    kind, source = req.attachment
                    data, filename, ctype = self._resolve(
                        kind, source, max_bytes=self.settings.upload_max_bytes,
                        param="messages")
                    entry = self.client.upload_attachment(
                        token, kind=kind, filename=filename, content_type=ctype,
                        data=data, extra_cookies=extra)
                    files.append(entry)
                chat_id = self.client.new_chat(token, chat_type="t2t", model=req.model,
                                               extra_cookies=extra)
                body = self.client.build_chat_submit_body(chat_id, model=req.model,
                                                          prompt=req.prompt,
                                                          files=files or None,
                                                          gear=req.thinking_gear)
                stream = self.client.stream_chat(token, chat_id, body, extra_cookies=extra,
                                                 meta=meta)
                self.pool.report_chat_submitted(email)
                return stream

            yield from self._authed_call(email, attempt)
        except AdapterError as exc:
            if isinstance(exc, QuotaExhaustedError):
                self.pool.report_failure(email, "refused")
            else:
                self._report_submit_failure(email, exc)
            raise

    # ------------------------------------------------------------------ 查询

    def get(self, local_id: str, credential_id: str | None) -> dict:
        """按 id 读任务。

        `credential_id is None` ⇒ **免鉴权读**（`task_id` 本身即凭据，方舟语义，同 `../jimeng`：
        调用方可以把结果链接直接分享出去）；给了指纹 ⇒ 走**归属校验**，不匹配一律本地 404
        **且不发上游**（ADR-003：放行到上游就是用错的钥匙去查，返回的 404/空无法区分
        "任务真没了"与"钥匙不对"）。
        """
        record = self.store.get(local_id)
        if record is None:
            raise NotFoundError(f"任务 {local_id} 不存在")
        if credential_id is not None and record.credential_id != credential_id:
            # 归属不符 ⇒ 本地直接 404（不带 Key 的读不需要走这里）
            raise NotFoundError(f"任务 {local_id} 不存在")
        if record.status not in TERMINAL_STATUSES:
            record = self.advance_record(record)
        return ark_task_view(record)

    def advance_record(self, record: TaskRecord) -> TaskRecord:
        """把非终态任务推进一格：`queued` ⇒ 尝试提交；`running` ⇒ 回查上游。"""
        if record.status == "queued":
            return self._attempt_submit(record)
        return self.poll_record(record)

    # ------------------------------------------------------------------ 出队提交

    def _attempt_submit(self, record: TaskRecord) -> TaskRecord:
        """给 `queued` 任务尝试提交一次（出队口）。只重试**可证明未提交**的失败。"""
        now = int(time.time())
        if now < record.next_attempt_at:        # 退避窗内：不动
            return record
        if now - record.created_at > self.settings.task_timeout:
            record.status = "failed"
            record.error_code = "internal"
            record.error_message = "排队超时：未能在 TASK_TIMEOUT 内取得提交窗口（任务未提交，未消耗额度）"
            return self.store.put(record)
        if record.attempts >= self.settings.submit_max_attempts:
            record.status = "failed"
            record.error_code = "internal"
            record.error_message = (f"排队重试 {record.attempts} 次仍未提交成功"
                                    f"（上限 {self.settings.submit_max_attempts}；未提交，未消耗额度）")
            return self.store.put(record)

        if not self._claim(record.local_id):
            return record                        # 已有线程在推进它
        try:
            account, wait = self.pool.acquire()
            if account is None or wait > 0:
                record.next_attempt_at = now + int(min(max(wait, 5.0), 120.0))
                return self.store.put(record)
            plan = CreatePlan(
                model_requested=record.model_requested,
                prompt=record.prompt,
                ratio=record.ratio,
                chat_type="i2v" if record.image_url else "t2v",
                image_url=record.image_url or None,
                degradations=record.degradations,
            )
            try:
                return self._submit_once(plan, account, record.credential_id, record=record)
            except AdapterError as exc:
                self._report_submit_failure(account.email, exc)
                if self._retryable_unsubmitted(exc):
                    record.attempts += 1
                    delay = min(self.settings.queue_retry_base * (2 ** min(record.attempts - 1, 5)),
                                600.0)
                    record.next_attempt_at = now + int(delay)
                    record.status = "queued"
                    logger.info("任务 %s 第 %s 次提交未成功（%s），退避 %ss 后重试",
                                record.local_id, record.attempts, exc.code, int(delay))
                    return self.store.put(record)
                record.status = "failed"
                record.error_code = "internal"
                record.error_message = f"提交失败（不可自动重试）：{exc.message[:200]}"
                return self.store.put(record)
        finally:
            self._release(record.local_id)

    def _claim(self, local_id: str) -> bool:
        with self._submit_guard:
            if local_id in self._submitting:
                return False
            self._submitting.add(local_id)
            return True

    def _release(self, local_id: str) -> None:
        with self._submit_guard:
            self._submitting.discard(local_id)

    # ------------------------------------------------------------------ 轮询

    def _task_status_with_remint(self, email: str, upstream_task_id: str,
                                 extra: str) -> tuple[dict, int, dict]:
        """查询任务；**被判 401 时重铸凭据再试一次**（查询只读 ⇒ 重试零风险、零额度）。

        两次都 401 ⇒ 不是"token 过期"而是账号/凭据问题 ⇒ 冷却 900s + 503
        （**部署问题，不是调用方 Key 的错** —— 混淆会让对方去改自己的请求）。
        """
        credential = self.pool.credential_for(email)
        result = self.client.task_status(credential, upstream_task_id, extra_cookies=extra)
        actual = int(result["actual_status_code"])
        data: dict = result["data"] or {}
        if actual == 401 or data.get("code") == "Unauthorized":
            logger.warning("查询被判 401 —— 重铸凭据后重试一次（%s）", mask_email(email))
            self.pool.invalidate_token(email)
            credential = self.pool.credential_for(email)
            result = self.client.task_status(credential, upstream_task_id, extra_cookies=extra)
            actual = int(result["actual_status_code"])
            data = result["data"] or {}
            if actual == 401 or data.get("code") == "Unauthorized":
                self.pool.report_failure(email, "auth")
                raise CredentialUnavailableError(
                    "上游持续判 401（已重铸 token 重试一次）—— 账号凭据/账号状态问题")
        return result, actual, data

    def poll_record(self, record: TaskRecord) -> TaskRecord:
        """回查上游一次并落库。**不改写非终态之外的语义**：解析不出来按失败报。"""
        now = int(time.time())
        if now - record.created_at > self.settings.task_timeout:
            record.status = "expired"
            return self.store.put(record)
        account = self.pool.get_state(record.account)
        if account is None:
            record.status = "failed"
            record.error_code = "internal"
            record.error_message = "任务所属账号不在当前配置中"
            return self.store.put(record)

        extra = self._extra_cookies(account.email)
        result, actual, data = self._task_status_with_remint(
            account.email, record.upstream_task_id, extra)

        if actual == 404 or data.get("code") == "Not_Found":
            record.status = "failed"
            record.error_code = "NotFound"
            record.error_message = "上游任务不存在（Not_Found）"
        elif result["success"]:
            upstream_status = str(data.get("task_status") or "")
            if upstream_status == "success":
                url = str(data.get("content") or "").strip()
                if url:
                    record.status = "succeeded"
                    record.video_url = url
                else:
                    record.status = "failed"
                    record.error_code = "internal"
                    record.error_message = "上游报成功但未返回产物地址（零产物按失败，不报成功）"
            elif upstream_status == "running":
                record.status = "running"
            else:
                record.status = "failed"
                record.error_code = "internal"
                record.error_message = f"上游任务失败：{self._failure_detail(data) or '未给出原因'}"
        else:
            record.status = "failed"
            record.error_code = "internal"
            record.error_message = f"上游查询被拒：{self._failure_detail(data) or '未给出原因'}"

        if record.status in TERMINAL_STATUSES:
            self.pool.report_finished(account.email)
        return self.store.put(record)

    @staticmethod
    def _failure_detail(data: dict) -> str:
        detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
        return str(data.get("message") or detail.get("info")
                   or data.get("details") or "").strip()

    # ------------------------------------------------------------------ 协调器

    def poll_active_once(self) -> int:
        """给后台协调器用：把所有非终态任务各推进一格（queued 出队 / running 回查）。

        失败吞掉并记日志 —— 协调器不许把一条任务的异常带崩整轮。
        """
        advanced = 0
        for record in self.store.list_active():
            try:
                self.advance_record(record)
                advanced += 1
            except AdapterError as exc:
                logger.warning("协调器推进 %s 失败（保持原状态）：%s", record.local_id, exc)
        return advanced


__all__ = ["QwenVideoService"]
