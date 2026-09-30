"""配置 —— 全部来自环境变量，`Settings.from_env()` 一处收拢。

约定（沿用 jimeng / hailuo / video-adapter）：
  · 凭据只从 env / env 指定的文件来，**绝不写进源码**；
  · 写错配置要**响亮失败**（缺账号 / 缺 signin 出口 / DSN 非法），不静默退回默认。
"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

UA_DEFAULT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

#: 能力回退通道（火山方舟 chat，`app/ark_fallback.py`）的默认端点（北京 region）。
ARK_DEFAULT_BASE = "https://ark.cn-beijing.volces.com/api/v3"

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env(env: dict, name: str, default: str = "") -> str:
    return (env.get(name) or default).strip()


def _bool(env: dict, name: str, default: bool = False) -> bool:
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _num(env: dict, name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return float(raw)


def _int(env: dict, name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return int(raw)


def parse_accounts(env: dict | None = None) -> dict[str, str]:
    """账号表：`QWEN_ACCOUNTS=email:pass,email2`（无密码条目取 `QWEN_ACCOUNT_PASSWORD`）
    或 `QWEN_ACCOUNTS_FILE`（每行一条 `email:pass`，`#` 注释）。

    ⚠️ 密码里含逗号时请改用文件形态（逗号是列表分隔符）。
    """
    env = env if env is not None else os.environ
    default_pw = _env(env, "QWEN_ACCOUNT_PASSWORD")
    accounts: dict[str, str] = {}

    def _add(item: str) -> None:
        item = item.strip()
        if not item or item.startswith("#"):
            return
        email, sep, pw = item.partition(":")
        accounts[email.strip()] = (pw if sep else default_pw).strip()

    for item in (_env(env, "QWEN_ACCOUNTS")).split(","):
        _add(item)
    path = _env(env, "QWEN_ACCOUNTS_FILE")
    if path and Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            _add(line)
    return accounts


def parse_account_cookies(env: dict | None = None) -> dict[str, str]:
    """可选：每账号附加 cookie 串（整份浏览器 jar 或 `ssxmod_itna` / `acw_tc` 等指纹 cookie）。

    来源 `QWEN_ACCOUNT_COOKIES`（JSON：`{"email": "k=v; k=v"}`）或
    `QWEN_ACCOUNT_COOKIES_FILE`（同格式 JSON 文件）。默认空 ⇒ 只发 `Cookie: token=<JWT>`
    （登录态最小凭据，见 `docs/UPSTREAM.md` §2）。
    """
    env = env if env is not None else os.environ
    raw = _env(env, "QWEN_ACCOUNT_COOKIES")
    path = _env(env, "QWEN_ACCOUNT_COOKIES_FILE")
    data: dict = {}
    if raw:
        data = json.loads(raw)
    elif path and Path(path).exists():
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("QWEN_ACCOUNT_COOKIES 必须是 JSON 对象：{email: cookie_string}")
    return {str(k): str(v) for k, v in data.items()}


@dataclass
class Settings:
    # —— 上游 ——
    upstream_base: str = "https://chat.qwen.ai"
    chat_model: str = "qwen3.7-plus"
    user_agent: str = UA_DEFAULT
    version_header: str = "0.2.0"
    upstream_timeout: float = 60.0
    trust_env: bool = False

    # —— 凭据池（7 账号等） ——
    accounts: dict[str, str] = field(default_factory=dict)
    account_cookies: dict[str, str] = field(default_factory=dict)
    #: signin（铸造 token）的**轮换出口**：`http(s)://` 代理（推荐形态，也是唯一形态 ——
    #: SOCKS 分支已按用户决策删除）。实测池语义：**每连接换 IP + 同连接复用同 IP**
    #: ⇒ 每次铸造换一个 IP、一次铸造全程一个 IP。
    #: 🔴 直连登录会把出口打进 WAF 墙，必须配轮换出口（或改用 `token_url`）。
    signin_proxy: str = ""
    token_url: str = ""
    #: 🔴 认证域基址（**含 `/api` 前缀**）：2026-09-30 起认证在独立域 **auth.qwen.ai** ——
    #: 旧域 signin 对纯 HTTP 客户端回滑块挑战页。auth 域还住着 refresh（续期主路，
    #: 免密码免预热不拦滑块）。正常情况**不需要改**；厂商再换域时用这个键接管，不发版。
    auth_base: str = "https://auth.qwen.ai/api"
    #: token 缓存**上限**（秒）—— 主动续期取 `min(JWT 的 exp - 提前量, 铸后本值)`。
    #: 🔴 6 天 = **给"实际可能 7 天失效"预留 1 天**：`exp` 是上游**自称**的（实测 30 天），
    #: 服务端可能提前失效 ⇒ 用本值压住，别赌到最后一刻。
    #: `<=0` 归一到 6 天（**刻意不提供"关掉上限"的开关**：那正是会咬人的位置）；
    #: token 无 `exp` 时（不透明 token）本值即兜底缓存时长。
    token_ttl: float = 518400.0
    signin_min_interval: float = 45.0
    signin_wait_timeout: float = 45.0
    submit_min_interval: float = 15.0
    daily_video_cap: int = 3
    account_wait_timeout: float = 30.0

    # —— 轻量队列 / 重试（"自己排队、自己重试"；关闭则回到严格 429） ——
    submit_queue_enabled: bool = True
    queue_max_depth: int = 50
    submit_max_attempts: int = 5
    queue_retry_base: float = 30.0

    # —— 对外鉴权 ——
    api_keys: list[str] = field(default_factory=list)
    key_secret: str = ""

    # —— OpenAI chat 门（/v1/chat/completions + /v1/models 注册） ——
    #: 上游模型清单（`GET /api/models`，免鉴权）的缓存时长（秒）。
    #: 到期才拉一次；拉取失败回退上一份好清单（负缓存同样顺延本值）。
    models_cache_ttl: float = 300.0

    # —— 能力回退通道（chat 门；qwen 不支持的能力 → 方舟 chat，见 app/ark_fallback.py） ——
    #: 🔴 KEY 与 MODEL **同时**配置才启用；只配一个 ⇒ from_env 响亮失败（半启用最容易误判）。
    ark_fallback_base: str = ARK_DEFAULT_BASE
    ark_fallback_key: str = ""
    ark_fallback_model: str = ""
    #: **备用回退模型链**（逗号分隔，按序 failover）：主模型被方舟限流（429）⇒
    #: 依次切换重试；全部被限 ⇒ 429 原样转发。所有模型名都只从 env 来，出站报文一律脱敏。
    ark_fallback_models: list[str] = field(default_factory=list)
    ark_fallback_timeout: float = 120.0

    # —— 附件上传链（chat 门；文件/音频/视频/data: 图 → getstsToken → OSS PUT，UPSTREAM §4.7） ——
    upload_enabled: bool = True
    #: 附件大小上限（字节）——服务端代下载与上传共用此闸门。
    upload_max_bytes: int = 20_000_000
    #: 流式静默 ping 间隔（秒）：上游静默超过该时长就发一条 SSE 注释（`: ping`）
    #: 保活中间层（防 nginx/CDN/NAT idle 掐流 —— curl 92 根治）。纯代码字段，无 env 键。
    ping_interval: float = 15.0

    # —— 任务持久化 ——
    task_db: str = ""
    data_dir: str = ""
    poll_interval: float = 3.0
    task_timeout: float = 900.0
    task_retention_days: int = 7
    coordinator_enabled: bool = True
    coordinator_tick: float = 5.0

    # —— 进程 ——
    host: str = "0.0.0.0"
    port: int = 8400
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, environ: dict | None = None) -> Settings:
        env = environ if environ is not None else os.environ
        data_dir = _env(env, "DATA_DIR") or str(REPO_ROOT / "var")
        task_db = _env(env, "TASK_DB") or f"sqlite:///{data_dir}/qwen.db"
        ark_base = _env(env, "ARK_FALLBACK_BASE")
        ark_key = _env(env, "ARK_FALLBACK_KEY")
        ark_model = _env(env, "ARK_FALLBACK_MODEL")
        ark_models = [m.strip() for m in _env(env, "ARK_FALLBACK_MODELS").split(",") if m.strip()]
        if (ark_base or ark_key or ark_model or ark_models) and not (ark_key and ark_model):
            raise ValueError(
                "回退通道配置不完整：启用需 ARK_FALLBACK_KEY 与 ARK_FALLBACK_MODEL 同时设置"
                "（ARK_FALLBACK_BASE / ARK_FALLBACK_MODELS 可选）"
                "—— 半启用状态最容易误判，刻意拒绝启动")
        return cls(
            upstream_base=_env(env, "QWEN_BASE_URL") or "https://chat.qwen.ai",
            chat_model=_env(env, "QWEN_CHAT_MODEL") or "qwen3.7-plus",
            user_agent=_env(env, "QWEN_USER_AGENT") or UA_DEFAULT,
            version_header=_env(env, "QWEN_VERSION_HEADER") or "0.2.0",
            upstream_timeout=_num(env, "QWEN_UPSTREAM_TIMEOUT", 60.0),
            trust_env=_bool(env, "QWEN_TRUST_ENV", False),
            accounts=parse_accounts(env),
            account_cookies=parse_account_cookies(env),
            signin_proxy=_env(env, "QWEN_SIGNIN_PROXY"),
            token_url=_env(env, "QWEN_TOKEN_URL"),
            auth_base=_env(env, "QWEN_AUTH_BASE") or "https://auth.qwen.ai/api",
            token_ttl=_num(env, "QWEN_TOKEN_TTL", 518400.0),
            signin_min_interval=_num(env, "QWEN_SIGNIN_MIN_INTERVAL", 45.0),
            signin_wait_timeout=_num(env, "QWEN_SIGNIN_WAIT_TIMEOUT", 45.0),
            submit_min_interval=_num(env, "QWEN_SUBMIT_MIN_INTERVAL", 15.0),
            daily_video_cap=_int(env, "QWEN_DAILY_VIDEO_CAP", 3),
            account_wait_timeout=_num(env, "QWEN_ACCOUNT_WAIT_TIMEOUT", 30.0),
            submit_queue_enabled=_bool(env, "SUBMIT_QUEUE_ENABLED", True),
            queue_max_depth=_int(env, "QUEUE_MAX_DEPTH", 50),
            submit_max_attempts=_int(env, "SUBMIT_MAX_ATTEMPTS", 5),
            queue_retry_base=_num(env, "QUEUE_RETRY_BASE", 30.0),
            api_keys=[p.strip() for p in _env(env, "API_KEYS").split(",") if p.strip()],
            key_secret=_env(env, "KEY_SECRET"),
            models_cache_ttl=_num(env, "MODELS_CACHE_TTL", 300.0),
            ark_fallback_base=ark_base or ARK_DEFAULT_BASE,
            ark_fallback_key=ark_key,
            ark_fallback_model=ark_model,
            ark_fallback_models=ark_models,
            ark_fallback_timeout=_num(env, "ARK_FALLBACK_TIMEOUT", 120.0),
            upload_enabled=_bool(env, "QWEN_UPLOAD_ENABLED", True),
            upload_max_bytes=_num(env, "QWEN_UPLOAD_MAX_BYTES", 20_000_000.0),
            task_db=task_db,
            data_dir=data_dir,
            poll_interval=_num(env, "POLL_INTERVAL", 3.0),
            task_timeout=_num(env, "TASK_TIMEOUT", 900.0),
            task_retention_days=_int(env, "TASK_RETENTION_DAYS", 7),
            coordinator_enabled=_bool(env, "COORDINATOR_ENABLED", True),
            coordinator_tick=_num(env, "COORDINATOR_TICK", 5.0),
            host=_env(env, "HOST") or "0.0.0.0",
            port=_int(env, "PORT", 8400),
            log_level=_env(env, "LOG_LEVEL") or "INFO",
        )

    # ------------------------------------------------------------------ 派生

    @property
    def base_url(self) -> str:
        return self.upstream_base.rstrip("/")

    @property
    def ready(self) -> bool:
        return bool(self.accounts)

    @property
    def ark_fallback_enabled(self) -> bool:
        """回退通道开关：KEY + MODEL 都配置才启用（空 = 关闭，回到降级/400 行为）。"""
        return bool(self.ark_fallback_key and self.ark_fallback_model)

    def resolved_key_secret(self) -> str:
        """HMAC 指纹密钥：env 优先；否则在 data_dir 落一个 600 的随机值（重启不变）。

        任务记录里只存 `credential_id = hmac-sha256(secret, key)` 指纹 —— API Key 是
        低熵可枚举空间，裸 sha256 不够（见 video-adapter `ADR-003`）。
        """
        if self.key_secret:
            return self.key_secret
        path = Path(self.data_dir) / "hmac_secret"
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        path.parent.mkdir(parents=True, exist_ok=True)
        value = secrets.token_hex(32)
        path.write_text(value, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:  # pragma: no cover - 平台差异
            pass
        return value
