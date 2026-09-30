"""账号池 —— 多账号轮换 / 额度计数 / 冷却 / **凭据缓存（token+jar 成对）** / 状态可持久化。

设计要点（沿既有项目的教训，2026-09-30 随上游改版订正）：
  · **登录走轮换出口、出图/出片走正常出口**（token 是无状态 JWT）⇒ signin 与使用分离；
  · 🔴 **凭据 = (token, jar) 二元组**（pair 门）：jar 是**产出这份 token 的那次会话**
    种下的 cookie 全集（WAF 冷启动章 + refresh_token + token），写请求必须整套装出去
    —— token 配别人的 jar ⇒ x5sec（2026-09-22 实测）；
  · **续期主路 = auth 域 refresh**（cookie 驱动 GET，免密码/免预热/不拦滑块/无 IP 墙；
    access token 只有 **15 分钟**）⇒ 每次取凭据先走 refresh；**signin 只兜底**
    （RT 失效时，≈30 天一次）。signin 有 IP 级频率墙（≈12 次/6 分钟）⇒ 照旧走
    轮换出口 + 跨账号节流；
  · **按账号分格缓存**（单槽会让两个账号互相挤掉凭据，且每次请求都去 signin）；
  · 额度按 **UTC 日**分桶（上游额度窗口是 UTC 日，本地日会提前 8 小时"误判恢复"）；
  · 状态端点**不含凭据原文**（只给是否缓存/指纹级信息），邮箱做半脱敏；
  · 🔴 **持久化范围（2026-09-30 政策修订）**：额度计数 / 冷却 / **jar（含 refresh_token）**
    都进 KV —— jar 是 30 天续期命脉，丢了 = 人工重新浏览器登录。**access token 仍不落盘**
    （15 分钟寿命，重启重铸走 refresh，免费且无墙）。
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from ...config import Settings
from ...errors import CredentialUnavailableError, RateLimitedError
from .signin import (
    MintError,
    WallError,
    cookie_entry,
    mint_token,
    refresh_auth,
    with_token,
)

logger = logging.getLogger("qwen.accounts")

#: 失败种类 → 冷却时长（秒）；"quota" 特殊：冷到下一个 UTC 日。
COOLDOWN_SECONDS = {
    "risk": 300.0,      # x5sec / RGV587：别连打，等冷却
    "auth": 900.0,      # token 失效：强制重新铸造后再说
    "transport": 30.0,  # 网络抖动：短冷
    "refused": 60.0,    # 其它上游拒绝
    "wall": 900.0,      # 🔴 WAF 挑战页（实测墙的持续 > 5 分钟，照 auth 档给足）
}


def utc_day(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now))


def next_utc_midnight(now: float | None = None) -> float:
    now = now if now is not None else time.time()
    lt = time.gmtime(now)
    seconds_today = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
    return now + (86400 - seconds_today)


def mask_email(email: str) -> str:
    """`user@example.com` → `use***@example.com`（够分辨、不泄露完整地址）。"""
    local, _, domain = email.partition("@")
    head = local[:3]
    return f"{head}***@{domain}" if domain else f"{head}***"


# ---------------------------------------------------------------- token 续期口径

#: 续期提前量：token 生命的 10%，夹在 [1 分钟, 6 小时] —— 不"卡着最后一秒"用过期 token。
#: 15 分钟的 access token ⇒ 提前量 = 90 秒，即每 13.5 分钟经 refresh 续一次。
REFRESH_MARGIN_RATIO = 0.1
REFRESH_MARGIN_MIN = 60.0
REFRESH_MARGIN_MAX = 6 * 3600.0


def jwt_exp(token: str) -> float | None:
    """从 JWT 载荷取 `exp`（**不验签** —— 只用于调度续期，不参与任何信任判定）。

    2026-09-30 起 access token `exp` = 签发 + **900 秒**（15 分钟）；载荷键
    `{exp, id, last_password_change, type:"access_token"}`（**没有 `iat`**）。
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return float(exp) if exp else None
    except Exception:  # noqa: BLE001 - 任何异常都视为"这个 token 没有可解析的 exp"
        return None


#: `QWEN_TOKEN_TTL` 缺省/非法时的保守上限：**6 天**。🔴 2026-09-30 起会话 token 只有
#: 15 分钟 ⇒ 本上限对 auth 域 token **不再起作用**（exp 提前量远小于它）——它只压
#: "自称长寿"的旧形状/外部 token；** freshness 的裁判是 exp 提前量，被动 401 自愈兜底**。
TOKEN_TTL_DEFAULT = 6 * 86400.0
#: 没有 `exp` 的不透明 token 的兜底缓存时长（6 小时）——只在外部 token 服务形态下用到。
OPAQUE_TOKEN_TTL = 6 * 3600.0


def needs_refresh(minted_at: float, expires_at: float, ttl: float, now: float) -> bool:
    """要不要重新铸造 token。

    判定 = **以 JWT 的 `exp` 为准（留提前量）** ∧ **上限恒生效**：

      · `exp` 是**上游自称**的有效期 ⇒ 用 `QWEN_TOKEN_TTL`（默认 **6 天**）把"自称
        长寿"的 token 压住（历史教训：自称 30 天、实际可能 7 天失效）；
      · `ttl <= 0` ⇒ 归一到 `TOKEN_TTL_DEFAULT`（**刻意不提供"关掉上限"的开关**）；
      · 没有 `exp`（不透明 token，如外部 token 服务）⇒ 用 `ttl`，`ttl<=0` 时兜底
        `OPAQUE_TOKEN_TTL`（6 小时）。

    ⚠️ **真正兜底的不是本函数，而是被动路径**：上游一旦判 401 ⇒ 清缓存 → **立即重铸
    → 原请求重试一次**（见 `app/service.py::_authed_call`；重铸先走 refresh，见
    `_mint_default`）。
    """
    if expires_at:
        ttl_eff = ttl if ttl > 0 else TOKEN_TTL_DEFAULT
        life = max(expires_at - minted_at, 1.0)
        margin = min(max(life * REFRESH_MARGIN_RATIO, REFRESH_MARGIN_MIN), REFRESH_MARGIN_MAX)
        return now >= min(expires_at - margin, minted_at + ttl_eff)
    return (now - minted_at) >= (ttl if ttl > 0 else OPAQUE_TOKEN_TTL)


@dataclass
class AccountState:
    email: str
    password: str
    token: str = ""
    #: 🔴 与 token **同源**的会话 jar（含 refresh_token；pair 门：写请求必须整套装出）。
    #: 不为空时恒含当前 `token`（见 `_sync_credential`）。
    jar: str = field(default="", repr=False)  # jar 含凭据原文，repr 里不落
    minted_at: float = 0.0
    #: 该 token 的到期时刻（取自 JWT 的 `exp`；取不到则 0 ⇒ 按 `token_ttl` 兜底）
    expires_at: float = 0.0
    cooldown_until: float = 0.0
    cooldown_reason: str = ""
    day: str = ""
    day_used: int = 0
    inflight: int = 0
    last_submit_at: float = 0.0
    mints: int = 0
    last_error: str = ""


class AccountPool:
    """同步实现（FastAPI 侧用 `asyncio.to_thread` 包）；互斥用细粒度锁。"""

    def __init__(self, settings: Settings, *, mint=None, now=None) -> None:
        self.settings = settings
        self._accounts: dict[str, AccountState] = {
            email: AccountState(email=email, password=pw)
            for email, pw in settings.accounts.items()
        }
        self._lock = threading.RLock()
        self._token_locks: dict[str, threading.Lock] = {e: threading.Lock() for e in self._accounts}
        self._signin_lock = threading.Lock()
        self._last_signin_at = 0.0
        self._mint = mint or self._mint_default
        self._now = now or time.time
        #: 状态变更回调（装配层注入 ⇒ 落 KV）。失败只记日志，绝不阻断主流程。
        self.on_change: Callable[[], None] | None = None

    # ------------------------------------------------------------------ 耐久化

    def snapshot(self) -> dict:
        """额度计数 + 冷却 + **jar（含 refresh_token）**的快照（不含 access token）。

        🔴 2026-09-30 政策修订：jar/RT 进持久层 —— 它是 30 天续期命脉（auth 域
        refresh 的唯一凭据），丢了 = 人工重新浏览器登录。access token 仍不落盘
        （15 分钟寿命，重启后第一件事就是拿 jar 走 refresh，免费且无墙）。
        """
        with self._lock:
            return {
                "version": 2,
                "saved_at": int(self._now()),
                "accounts": {
                    a.email: {
                        "day": a.day,
                        "day_used": a.day_used,
                        "cooldown_until": round(a.cooldown_until, 3),
                        "cooldown_reason": a.cooldown_reason,
                        "jar": a.jar,
                    }
                    for a in self._accounts.values()
                },
            }

    def restore(self, data: dict | None) -> None:
        """从快照恢复（宽容解析：未知账号/坏行一律忽略，**绝不因坏数据拒绝启动**）。"""
        if not isinstance(data, dict):
            return
        rows = data.get("accounts")
        if not isinstance(rows, dict):
            return
        with self._lock:
            for email, row in rows.items():
                account = self._accounts.get(email)
                if account is None or not isinstance(row, dict):
                    continue
                try:
                    account.day = str(row.get("day") or account.day)
                    account.day_used = max(0, int(row.get("day_used") or 0))
                    account.cooldown_until = float(row.get("cooldown_until") or 0.0)
                    account.cooldown_reason = str(row.get("cooldown_reason") or "")
                    # v1 快照没有 jar ⇒ 保持空串（等价于"重启后重新 signin 兜底"）
                    account.jar = str(row.get("jar") or "")
                except (TypeError, ValueError):
                    continue

    def _notify(self) -> None:
        callback = self.on_change
        if callback is None:
            return
        try:
            callback()
        except Exception:  # noqa: BLE001 - 回调不许影响主流程
            logger.warning("账号池状态持久化回调失败（忽略）", exc_info=True)

    # ------------------------------------------------------------------ 铸造

    def _mint_default(self, account: AccountState) -> str:
        """真实铸造：**refresh 优先，signin 兜底**；返回 token，jar 记在账号状态上。

        ① jar 里有 refresh_token ⇒ auth 域 refresh（直连、无墙、免费）——
           15 分钟一次的正常续期全走这条路，不占 signin 的 IP 频率墙；
        ② RT 缺失/失效 ⇒ 走 `QWEN_TOKEN_URL`（外部 token 服务，出口在服务侧轮换）；
        ③ 都没有 ⇒ **auth 域 signin**（经 `QWEN_SIGNIN_PROXY` 轮换出口；旧 chat 域
           signin 已被滑块墙死，2026-09-30 删除）。签到成功时 jar 一并捕获（pair 门），
           RT 从此就在池子里，下一次续期就走 ①。
        """
        s = self.settings
        # ① 续期主路：直连、无墙 —— 失败（RT 缺失/失效/网络）静默回落
        if cookie_entry(account.jar, "refresh_token"):
            token, fresh_jar = refresh_auth(account.jar, auth_base=s.auth_base,
                                            user_agent=s.user_agent)
            if token:
                account.jar = fresh_jar
                return token
            logger.warning("账号 %s 的 refresh 未产出 token（RT 可能失效）⇒ 回落 signin",
                           mask_email(account.email))
        # ② 外部 token 服务（出口在服务侧轮换，本机不碰墙）
        if s.token_url:
            url = s.token_url + ("&" if "?" in s.token_url else "?") + \
                "account=" + urllib.parse.quote(account.email)
            resp = httpx.get(url, timeout=30.0, trust_env=False)
            resp.raise_for_status()
            payload = resp.json()
            token = str(payload.get("token") or "")
            if not token:
                raise MintError(f"token_url 未返回 token（{url.split('?')[0]}）")
            # 服务只给 token、给不出那次登录的 jar ⇒ 保留既有 jar（有则并入新 token）
            return token
        # ③ auth 域 signin（轮换出口；IP 级频率墙由 _pace_signin 节流）
        if not s.signin_proxy:
            raise CredentialUnavailableError(
                "未配置 QWEN_SIGNIN_PROXY / QWEN_TOKEN_URL，且账号 jar 里没有 "
                "refresh_token —— 无法铸造 token")
        token, jar = mint_token(s.signin_proxy, account.email, account.password,
                                base_url=s.base_url, auth_base=s.auth_base,
                                user_agent=s.user_agent)
        account.jar = jar
        return token

    def _pace_signin(self) -> None:
        """跨账号共享的 signin 节奏（防止连登把出口打进 WAF 墙）。

        ⚠️ 只该拦在 **signin** 这条腿上：refresh 无墙，被它连坐只会白白拖慢续期。
        """
        with self._signin_lock:
            now = self._now()
            wait = self.settings.signin_min_interval - (now - self._last_signin_at)
            if wait > 0:
                if wait > self.settings.signin_wait_timeout:
                    raise CredentialUnavailableError(
                        f"signin 节流中（跨账号共享节奏），请 {wait:.0f}s 后重试",
                        retry_after=wait)
                time.sleep(wait)
            self._last_signin_at = self._now()

    def _fresh(self, account: AccountState, now: float) -> bool:
        """缓存里的 token 是否还在可复用窗口内（纯判定：不铸造、不加锁）。"""
        return bool(account.token) and not needs_refresh(
            minted_at=account.minted_at, expires_at=account.expires_at,
            ttl=self.settings.token_ttl, now=now)

    def token_for(self, email: str) -> str:
        """取（必要时铸造）该账号的 token。失败抛 `CredentialUnavailableError`。

        **过期自动续期**走两条路（缺一不可）：
          ① **主动**：按 token 自己的 `exp`（留提前量）在到期前重铸 —— `needs_refresh()`；
             重铸先走 refresh（jar 里的 RT），RT 失效才 signin；
          ② **被动**：被上游判 401/Unauthorized 时清缓存（`invalidate_token()` /
             `report_failure(…, "auth")`），下一次取用即自动重铸（jar 保留 ⇒ 先 refresh）。
        """
        with self._token_locks[email]:
            return self._token_for_locked(email)

    def credential_for(self, email: str) -> str:
        """取该账号的**线上凭据**：同源 jar（含最新 token）；无 jar 时退 `token=<jwt>`。

        这是写/查请求该用的东西（pair 门）：jar 与 token 同源，身份自洽。
        """
        with self._token_locks[email]:
            token = self._token_for_locked(email)
            account = self.get_state(email)
            if account is not None and account.jar:
                return account.jar  # 不变式：jar 恒含当前 token
            return f"token={token}"

    def _token_for_locked(self, email: str) -> str:
        """`token_for` 的锁内实现（调用方必须已持有 `_token_locks[email]`）。"""
        account = self.get_state(email)
        if account is None:
            raise CredentialUnavailableError(f"账号不在当前配置中：{mask_email(email)}")
        now = self._now()
        if self._fresh(account, now):
            return account.token
        # 🔴 只有 signin 这条腿受跨账号节流；但"要不要节流"得先知道走哪条腿 ——
        # refresh 无墙不该被节流连坐。做法：先试 refresh（不节流），失败要 signin 时
        # 在 `_mint_default` 之前过闸（见下方 pace 分支）。
        jar_has_rt = bool(cookie_entry(account.jar, "refresh_token"))
        if not jar_has_rt and not self.settings.token_url:
            # 无 RT 且无 token 服务 ⇒ 这一铸必是 signin ⇒ 过跨账号节流闸
            self._pace_signin()
        try:
            token = self._mint(account)
        except WallError as exc:
            account.last_error = f"WallError: {exc}"
            now2 = self._now()
            account.cooldown_until = now2 + COOLDOWN_SECONDS["wall"]
            account.cooldown_reason = "wall"
            self._notify()
            raise CredentialUnavailableError(
                f"账号 {mask_email(email)} 铸造 token 失败（WAF 挑战页）：{exc}",
                retry_after=COOLDOWN_SECONDS["wall"]) from exc
        except CredentialUnavailableError:
            raise
        except (MintError, httpx.HTTPError, OSError) as exc:
            account.last_error = f"{type(exc).__name__}: {exc}"
            raise CredentialUnavailableError(
                f"账号 {mask_email(email)} 铸造 token 失败：{type(exc).__name__}: {exc}",
                retry_after=60.0) from exc
        self._sync_credential(account, token)
        return token

    def _sync_credential(self, account: AccountState, token: str) -> None:
        """token 落账 + **jar 不变式**：jar 非空时必须含最新 token。"""
        account.token = token
        account.minted_at = self._now()
        account.expires_at = jwt_exp(token) or 0.0
        account.mints += 1
        if account.jar:
            account.jar = with_token(account.jar, token)

    def invalidate_token(self, email: str) -> None:
        """401 自愈入口：清 access token，**保留 jar**（RT 大概率仍有效 ⇒ 下次先 refresh）。"""
        account = self.get_state(email)
        if account is not None:
            account.token = ""
            account.minted_at = 0.0
            account.expires_at = 0.0

    # ------------------------------------------------------------------ 取号

    def acquire(self) -> tuple[AccountState | None, float]:
        """选一个可用账号。

        返回 `(account, wait_seconds)`：
          · `(acct, 0)`   立即用；
          · `(acct, w>0)` 该账号在提交节奏窗内，等 w 秒再用（调用方 sleep）；
          · `(None, w)`   全部冷却/额度用尽 —— w 秒内不会有号（w 可能很大）。
        """
        now = self._now()
        day = utc_day(now)
        with self._lock:
            for account in self._accounts.values():
                if account.day != day:  # UTC 日滚动
                    account.day = day
                    account.day_used = 0
            candidates = [
                a for a in self._accounts.values()
                if a.cooldown_until <= now and a.day_used < self.settings.daily_video_cap
            ]
            if not candidates:
                soonest = min((a.cooldown_until for a in self._accounts.values()), default=now)
                return None, max(0.0, soonest - now)

            def ready_at(a: AccountState) -> float:
                return a.last_submit_at + self.settings.submit_min_interval

            ready = [a for a in candidates if ready_at(a) <= now]
            if ready:
                return min(ready, key=lambda a: a.last_submit_at), 0.0
            earliest = min(candidates, key=ready_at)
            return earliest, max(0.0, ready_at(earliest) - now)

    def acquire_with_wait(self) -> AccountState:
        """在 `account_wait_timeout` 内等到一个账号；等不到抛 429（调用方/队列层接住）。"""
        if not self._accounts:
            raise CredentialUnavailableError(
                "未配置任何账号（QWEN_ACCOUNTS）—— 部署问题，不是调用方的错")
        deadline = self._now() + self.settings.account_wait_timeout
        last_hint = 1.0
        while True:
            account, wait = self.acquire()
            if account is not None and wait <= 0:
                return account
            hint = wait if account is not None else max(wait, 1.0)
            last_hint = hint
            if self._now() + hint > deadline:
                break
            time.sleep(min(hint, 1.0))
        reason = "全部账号在冷却中或今日额度已用尽（3 次/天/账号，UTC 日重置）"
        raise RateLimitedError(reason, retry_after=min(last_hint, 3600.0))

    # ---------------------------------------------------------------- 取号：chat 门

    def acquire_chat(self) -> tuple[AccountState | None, float]:
        """chat（t2t）取号：**不看视频额度**（chat 不消耗 3 次/天的视频池），
        但仍受冷却与同账号提交节奏约束（写端点的突发纪律对 t2t 同样适用）。"""
        now = self._now()
        with self._lock:
            candidates = [a for a in self._accounts.values() if a.cooldown_until <= now]
            if not candidates:
                soonest = min((a.cooldown_until for a in self._accounts.values()), default=now)
                return None, max(0.0, soonest - now)

            def ready_at(a: AccountState) -> float:
                return a.last_submit_at + self.settings.submit_min_interval

            ready = [a for a in candidates if ready_at(a) <= now]
            if ready:
                return min(ready, key=lambda a: a.last_submit_at), 0.0
            earliest = min(candidates, key=ready_at)
            return earliest, max(0.0, ready_at(earliest) - now)

    def acquire_chat_with_wait(self) -> AccountState:
        """chat 门的等待版：等不到 ⇒ 429（chat 是同步链路，**没有任务表可排队**）。"""
        if not self._accounts:
            raise CredentialUnavailableError(
                "未配置任何账号（QWEN_ACCOUNTS）—— 部署问题，不是调用方的错")
        deadline = self._now() + self.settings.account_wait_timeout
        last_hint = 1.0
        while True:
            account, wait = self.acquire_chat()
            if account is not None and wait <= 0:
                return account
            hint = wait if account is not None else max(wait, 1.0)
            last_hint = hint
            if self._now() + hint > deadline:
                break
            time.sleep(min(hint, 1.0))
        raise RateLimitedError(
            "无可用账号（全部冷却中或同账号提交间隔未到）—— chat 请求未提交",
            retry_after=min(last_hint, 3600.0))

    # ------------------------------------------------------------------ 回报

    def report_submitted(self, email: str) -> None:
        now = self._now()
        with self._lock:
            account = self._accounts.get(email)
            if account is None:
                return
            day = utc_day(now)
            if account.day != day:
                account.day = day
                account.day_used = 0
            account.day_used += 1
            account.last_submit_at = now
            account.inflight += 1
        self._notify()

    def report_chat_submitted(self, email: str) -> None:
        """chat 提交回执：**只更新节奏戳** —— 不动视频额度计数（day_used）、不动 inflight
        （chat 没有完成回报路径，动 inflight 必泄漏）。节奏戳不进持久层 ⇒ 不 notify。"""
        now = self._now()
        with self._lock:
            account = self._accounts.get(email)
            if account is not None:
                account.last_submit_at = now

    def report_finished(self, email: str) -> None:
        with self._lock:
            account = self._accounts.get(email)
            if account is not None and account.inflight > 0:
                account.inflight -= 1

    def report_failure(self, email: str, kind: str) -> None:
        """按种类冷却。`kind ∈ {risk, auth, transport, refused, wall, quota}`。"""
        now = self._now()
        with self._lock:
            account = self._accounts.get(email)
            if account is None:
                return
            if kind == "quota":
                account.cooldown_until = next_utc_midnight(now)
                account.cooldown_reason = "quota"
            else:
                seconds = COOLDOWN_SECONDS.get(kind, 60.0)
                account.cooldown_until = now + seconds
                account.cooldown_reason = kind
            account.last_error = kind
            if kind == "auth":
                # access token 失效；jar（RT）保留 —— 下次取用先走 refresh
                account.token = ""
                account.minted_at = 0.0
                account.expires_at = 0.0
        self._notify()

    # ------------------------------------------------------------------ 观测

    def get_state(self, email: str) -> AccountState | None:
        return self._accounts.get(email)

    def stats(self) -> dict:
        now = self._now()
        with self._lock:
            rows = []
            for a in self._accounts.values():
                rows.append({
                    "account": mask_email(a.email),
                    "day_used": a.day_used,
                    "cap": self.settings.daily_video_cap,
                    "token_cached": self._fresh(a, now),
                    "token_age_s": int(now - a.minted_at) if a.token else None,
                    "token_expires_in_s": int(a.expires_at - now) if a.expires_at else None,
                    "jar_cached": bool(a.jar),
                    "mints": a.mints,
                    "inflight": a.inflight,
                    "cooldown_for_s": max(0, int(a.cooldown_until - now)),
                    "cooldown_reason": a.cooldown_reason or None,
                    "last_error": a.last_error or None,
                })
            available = sum(
                1 for a in self._accounts.values()
                if a.cooldown_until <= now and a.day_used < self.settings.daily_video_cap)
            return {"total": len(self._accounts), "available": available, "accounts": rows}

