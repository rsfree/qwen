"""env 门禁（**只覆盖 `.env.example` + 代码**，不碰 `.env`）—— 零网络。

依据 skill `env-template-sync`：
  · 三向一致 = 代码读的键 ⇄ 模板登记的键 ⇄ 生效文件的取值；
  · **`.env` 不入库**（gitignored，CI 上没有它）⇒ 涉及 `.env` 的核对**不能写成测试**
    （条件跳过 = 假绿灯），改用脚本 `scripts/env_sync_check.py` 手工/发版前跑；
  · 门禁**不能空转**：范围写错时它会静默永远绿 ⇒ 每条断言都带下界 + 钉住被扫的权威文件。

模板的两态语义：`KEY=值` = 显式；`# KEY=值` = 「注释态登记」= 用代码默认（注释里的值必须与默认一致）。
"""
from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / ".env.example"
CONFIG_PY = REPO / "app" / "config.py"
GUNICORN_PY = REPO / "gunicorn_conf.py"

#: 代码侧权威文件（少扫一个文件 ⇒ 门禁范围就是错的）
AUTHORITATIVE_FILES = (CONFIG_PY, GUNICORN_PY)

#: 键 → `Settings` 字段名；`None` = 由解析函数内部读（无同名字段）。
#: 未列出的键会被 `test_key_mapping_is_complete` 判红 —— 新增配置项必须同步这张表。
KEY_TO_FIELD: dict[str, str | None] = {
    "QWEN_BASE_URL": "upstream_base",
    "QWEN_CHAT_MODEL": "chat_model",
    "QWEN_USER_AGENT": "user_agent",
    "QWEN_VERSION_HEADER": "version_header",
    "QWEN_UPSTREAM_TIMEOUT": "upstream_timeout",
    "QWEN_TRUST_ENV": "trust_env",
    "POLL_INTERVAL": "poll_interval",
    "QWEN_ACCOUNTS": None,
    "QWEN_ACCOUNT_PASSWORD": None,
    "QWEN_ACCOUNTS_FILE": None,
    "QWEN_DAILY_VIDEO_CAP": "daily_video_cap",
    "QWEN_ACCOUNT_WAIT_TIMEOUT": "account_wait_timeout",
    "QWEN_SUBMIT_MIN_INTERVAL": "submit_min_interval",
    "QWEN_ACCOUNT_COOKIES": None,
    "QWEN_ACCOUNT_COOKIES_FILE": None,
    "QWEN_SIGNIN_PROXY": "signin_proxy",
    "QWEN_TOKEN_URL": "token_url",
    "QWEN_AUTH_BASE": "auth_base",
    "QWEN_TOKEN_TTL": "token_ttl",
    "QWEN_SIGNIN_MIN_INTERVAL": "signin_min_interval",
    "QWEN_SIGNIN_WAIT_TIMEOUT": "signin_wait_timeout",
    "SUBMIT_QUEUE_ENABLED": "submit_queue_enabled",
    "QUEUE_MAX_DEPTH": "queue_max_depth",
    "SUBMIT_MAX_ATTEMPTS": "submit_max_attempts",
    "QUEUE_RETRY_BASE": "queue_retry_base",
    "API_KEYS": "api_keys",
    "KEY_SECRET": "key_secret",
    "MODELS_CACHE_TTL": "models_cache_ttl",
    "QWEN_THINKING_LEAK_MODELS": "thinking_leak_models",
    "ARK_FALLBACK_BASE": "ark_fallback_base",
    "ARK_FALLBACK_KEY": "ark_fallback_key",
    "ARK_FALLBACK_MODEL": "ark_fallback_model",
    "ARK_FALLBACK_MODELS": "ark_fallback_models",
    "ARK_FALLBACK_TIMEOUT": "ark_fallback_timeout",
    "QWEN_UPLOAD_ENABLED": "upload_enabled",
    "QWEN_UPLOAD_MAX_BYTES": "upload_max_bytes",
    "TASK_DB": "task_db",
    "DATA_DIR": "data_dir",
    "TASK_TIMEOUT": "task_timeout",
    "TASK_RETENTION_DAYS": "task_retention_days",
    "COORDINATOR_ENABLED": "coordinator_enabled",
    "COORDINATOR_TICK": "coordinator_tick",
    "HOST": "host",
    "PORT": "port",
    "LOG_LEVEL": "log_level",
    "WORKERS": None,             # gunicorn_conf.py 读（不是本应用的字段）
    "GUNICORN_TIMEOUT": None,    # 同上
}

#: **模板显式值 ≠ 代码默认** 的登记表（每项一句理由）。门禁会反向断言"不得过期"：
#: 表里有、实际却不差 = 红（说明有人把值改了却忘了撤登记）。
ACTIVE_VALUE_DIFFS: dict[str, str] = {
    "QWEN_CHAT_MODEL": "模板给实测出片的 qwen3.8-max；代码默认是 qwen3.7-plus（未实测）",
    "QWEN_SIGNIN_PROXY": "模板是占位符（真实地址只在 .env）；代码默认是空串",
    "DATA_DIR": "模板给容器路径 /app/var；代码默认是 <仓库>/var",
}

#: **注释态写的是示例值**（不是代码默认）的登记表 —— 第三类，必须显式，否则门禁一直喊狼来了。
ILLUSTRATIVE_COMMENTED: dict[str, str] = {
    "TASK_DB": "注释里是 PostgreSQL 示例 DSN；代码默认空 ⇒ sqlite:///<DATA_DIR>/qwen.db",
    "QWEN_TOKEN_URL": "注释里是外部 token 服务示例地址；代码默认空 ⇒ 走 signin 铸造",
    "ARK_FALLBACK_MODELS": "注释里是备用模型链示例（逗号分隔）；代码默认空列表 ⇒ 无备用链",
}


# ---------------------------------------------------------------- 解析


def parse_template() -> tuple[dict[str, str], dict[str, str]]:
    """→ (显式键值, 注释态键值)。行内 ` # 注释` 会被剥掉。"""
    active: dict[str, str] = {}
    commented: dict[str, str] = {}
    for line in TEMPLATE.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        m = re.match(r"^#\s*([A-Z0-9_]+)=(.*)$", s)
        if m:
            commented[m.group(1)] = re.split(r"\s+#", m.group(2), maxsplit=1)[0].strip()
            continue
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            active[k.strip()] = re.split(r"\s+#", v, maxsplit=1)[0].strip()
    return active, commented


def code_keys() -> set[str]:
    """生产代码**实际会读**的环境变量键（扫描范围：配置权威文件）。"""
    src = CONFIG_PY.read_text(encoding="utf-8")
    keys = set(re.findall(r'_(?:env|bool|num|int)\(\s*env\s*,\s*"([A-Z0-9_]+)"', src))
    keys |= set(re.findall(r'env\.get\(\s*"([A-Z0-9_]+)"', src))
    assert len(keys) >= 30, f"config.py 只扫到 {len(keys)} 个键 ⇒ 正则/范围写错，门禁在空转"

    gun = GUNICORN_PY.read_text(encoding="utf-8")
    gun_keys = set(re.findall(r'os\.environ\.get\(\s*"([A-Z0-9_]+)"', gun))
    assert gun_keys, "gunicorn_conf.py 一个键都没扫到 ⇒ 范围写错"
    return keys | gun_keys


def gunicorn_defaults() -> dict[str, str]:
    """gunicorn_conf.py 里 `os.environ.get("K", "d")` 的默认值（另一进程的配置）。"""
    src = GUNICORN_PY.read_text(encoding="utf-8")
    return dict(re.findall(r'os\.environ\.get\(\s*"([A-Z0-9_]+)"\s*,\s*"([^"]*)"\)', src))


def resolve_default(key: str) -> object | None:
    """该键的代码默认值：先在 gunicorn 那份表里找，再落到 Settings 字段。"""
    gun = gunicorn_defaults()
    if key in gun:
        return gun[key]
    field = KEY_TO_FIELD.get(key)
    return getattr(Settings(), field) if field else None


def norm(value: object) -> str:
    """归一化后再比：`0/False/off`、`60`/`60.0` 都是同一语义（否则会产出一屏假阳性）。

    🔴 列表/元组按**逗号拼接**归一（与 env 里逗号分隔的书写形式对齐）——否则列表型
    配置项（如 `ARK_FALLBACK_MODELS` / `QWEN_THINKING_LEAK_MODELS`）的默认值永远核不上：
    代码默认是 `['a', 'b']`、模板写 `a,b`，字符串化后必然不等 ⇒ 门禁只能靠"注释留空"
    绕过默认值核对。空列表归一为空串（⇒ 注释态 `# KEY=` 仍按"留空"处理，不核值）。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v).strip() for v in value)
    text = str(value).strip()
    low = text.lower()
    if low in ("0", "false", "no", "off"):
        return "false"
    if low in ("1", "true", "yes", "on"):
        return "true"
    try:
        return str(float(text))
    except ValueError:
        return text


# ---------------------------------------------------------------- 门禁


def test_key_mapping_is_complete():
    """映射表必须覆盖生产代码读到的每个键 —— 否则下面的默认值核对会**静默漏项**。"""
    missing = code_keys() - set(KEY_TO_FIELD)
    assert not missing, f"新配置项没登记进 KEY_TO_FIELD：{sorted(missing)}"


def test_template_and_code_declare_the_same_keys():
    """双向一致：代码读的键 == 模板登记的键（显式 ∪ 注释态）。"""
    active, commented = parse_template()
    declared = set(active) | set(commented)
    assert len(declared) >= 30, f"模板只登记了 {len(declared)} 个键 ⇒ 解析写错或漏登记"
    assert declared == code_keys(), {
        "模板有、代码不读": sorted(declared - code_keys()),
        "代码读、模板没登记": sorted(code_keys() - declared),
    }


def test_no_empty_value_with_inline_comment():
    """🔴 `KEY=  # 说明` 会让**注释整段变成值**（dotenv 系解析器的坑，失效方向永远是静默打开）。"""
    bad = [
        line for line in TEMPLATE.read_text(encoding="utf-8").splitlines()
        if re.match(r"^\s*[A-Z0-9_]+\s*=\s*#", line)
    ]
    assert not bad, f"模板里出现「空值 + 行内注释」：{bad}"


def test_commented_defaults_match_code_defaults():
    """注释态登记的值必须 == 代码默认（注释是"字段说明的权威来源"，过期注释比没注释更坏）。

    · 空注释态（`# KEY=`）= "留空" ⇒ 不核值（默认本就可能是空串/空列表）；
    · 注释态是**示例值**的（如 PG DSN）在 `ILLUSTRATIVE_COMMENTED` 里显式登记 —— 登记不得过期。
    """
    _, commented = parse_template()
    mismatched: set[str] = set()
    for key, text in commented.items():
        if not text:
            continue
        default = resolve_default(key)
        if default is None:
            continue
        if norm(text) != norm(default):
            mismatched.add(key)
    assert mismatched == set(ILLUSTRATIVE_COMMENTED), {
        "注释值与默认不一致、却没登记为示例值": sorted(mismatched - set(ILLUSTRATIVE_COMMENTED)),
        "登记为示例值、实际却与默认一致（已过期）": sorted(set(ILLUSTRATIVE_COMMENTED) - mismatched),
    }
    for key, reason in ILLUSTRATIVE_COMMENTED.items():
        assert len(reason) >= 8, f"{key} 的示例说明太短，等于没写"


def test_active_value_diffs_are_documented_and_not_stale():
    """模板**显式值**与代码默认不一致时必须登记理由；且登记不得过期（"实际不差"即红）。"""
    active, _ = parse_template()
    measured: set[str] = set()
    for key, text in active.items():
        default = resolve_default(key)
        if default is None:
            continue
        if norm(text) != norm(default) and text:
            measured.add(key)
    assert measured == set(ACTIVE_VALUE_DIFFS), {
        "有差异但没登记": sorted(measured - set(ACTIVE_VALUE_DIFFS)),
        "登记了其实没差异（已过期）": sorted(set(ACTIVE_VALUE_DIFFS) - measured),
    }
    for key, reason in ACTIVE_VALUE_DIFFS.items():
        assert len(reason) >= 8, f"{key} 的差异理由太短，等于没写"


def test_api_keys_placeholder_does_not_look_like_a_credential():
    """模板里的凭据类项必须是**空**或**显眼占位**：非空的像真凭据会被当成真凭据（噪音/401）。"""
    active, _ = parse_template()
    assert active.get("API_KEYS", "") == "", "API_KEYS 模板值必须留空"
    assert active.get("QWEN_ACCOUNT_PASSWORD", "") == "", "口令模板值必须留空"
    proxy = active.get("QWEN_SIGNIN_PROXY", "")
    assert "POOL_USER" in proxy, "出口模板值必须是显眼占位符（POOL_USER:POOL_PASS@…）"
