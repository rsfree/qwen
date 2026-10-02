"""`/llms.txt` 与落地页渲染 —— fleet 约定：**内容必须派生，不许手抄**。

派生来源（改动注册表/异常类/配置时，本文件输出自动跟随）：
  · 能力表          ← `models.catalog()`（视频）+ 路由传入的 chat 注册 id（`/v1/models` 同源）；
  · 错误表          ← `app/errors.py` 的 AdapterError 子类（code/status/retryable/首行 docstring）；
  · chat 门错误 type ← `openai_chat.CHAT_ERROR_TYPE_BY_STATUS`；
  · 鉴权/回退/上传  ← `Settings`（不打印任何 Key/模型名——回退模型名脱敏纪律）。
只有小节骨架与 curl 示例是静态文案。
"""
from __future__ import annotations

import html

from . import __version__, openai_chat
from . import errors as _errors
from .config import Settings
from .models import catalog

SERVICE_NAME = "qwen-service"


def _error_rows() -> list[dict]:
    """从异常类派生错误表（表与源码同源）。"""
    rows: list[dict] = []
    for name in _errors.__all__:
        cls = getattr(_errors, name)
        if not (isinstance(cls, type) and issubclass(cls, _errors.AdapterError)):
            continue
        if cls is _errors.AdapterError:
            continue
        when = (cls.__doc__ or "").strip().splitlines()[0].strip() if cls.__doc__ else ""
        rows.append({"http": cls.status_code, "code": cls.code, "type": cls.err_type,
                     "retryable": cls.retryable, "when": when})
    return rows


def render_llms_txt(settings: Settings, chat_models: list[str] | None = None) -> str:
    """渲染 `/llms.txt`（markdown）。`chat_models` = /v1/models 注册的 chat 模型 id。"""
    chat_models = list(chat_models or [])
    video = catalog()[0]
    fallback_on = settings.ark_fallback_enabled
    auth_on = bool(settings.api_keys)
    upload_on = settings.upload_enabled
    max_mb = int(settings.upload_max_bytes // 1_000_000)

    lines: list[str] = []
    lines.append(f"# {SERVICE_NAME}")
    lines.append("")
    lines.append("chat.qwen.ai 的**双门出口**：视频门（火山方舟 Seedance 契约，t2v/i2v）+ "
                 "chat 门（OpenAI 形态，t2t 文本 + 图片/文件/音频/视频解析）。"
                 "多账号池 + 完整指纹请求头 + 轻量排队重试。给 OpenAI 兼容客户端 / 网关使用——"
                 "Base URL 指向本服务、Key 换成本服务的即可。")
    lines.append("")
    lines.append("最小可用（chat 门）：")
    lines.append("")
    lines.append("```bash")
    lines.append('curl -s $BASE/v1/chat/completions -H "Authorization: Bearer $KEY" '
                 '-H "Content-Type: application/json" \\')
    lines.append('  -d \'{"model":"qwen3.7-plus","messages":[{"role":"user","content":"你好"}]}\'')
    lines.append("```")
    lines.append("")
    lines.append("## ⚠️ 先读（额度与边界）")
    lines.append("")
    lines.append("- 🔴 **视频额度 3 次/天/账号**（UTC 日，t2v+i2v 共用）——视频生成是计费动作，"
                 "队列重试只覆盖\"可证明未提交\"的失败；**对话与解析不消耗视频额度**。")
    lines.append("- 🔴 **对话上下文实测 ≈5 万汉字内可靠**（上游自报 1M tokens，但 WAF 在"
                 " ~6 万字触发风控，换出口不可绕过）——超长请分段或摘要。")
    lines.append("- **视频时长固定 ~5s**（duration 参数只接受 5，其余吸附/拒绝）；`ratio` 只吃 "
                 f"{'/'.join(video['ratios'])}，枚举外落 1:1。")
    lines.append("- **一次 1 个附件**（图片/文件/音频/视频），"
                 f"≤{max_mb}MB，http(s) URL 或 data: URI；多轮对话请把附件放在**最后一条** user 消息。")
    lines.append("- 上游风控 429（`ServerOverloaded`）**勿连打**——重试会加深标记。")
    lines.append("")
    lines.append("## 能力")
    lines.append("")
    lines.append("| model | 通路 | 必填输入 | 附件 | 说明 |")
    lines.append("|---|---|---|---|---|")
    lines.append(f"| `{video['id']}` | 视频生成（异步任务） | text prompt；图片可选（单张首帧） "
                 f"| 不支持附件混合 | 时长固定 ~5s；产物 URL 7 天内可取 |")
    chat_note = "、".join(f"`{m}`" for m in chat_models) if chat_models \
        else "以 `GET /v1/models` 实时为准"
    lines.append(f"| {chat_note} | 对话 / 多模态解析（同步） | messages（最后一条 user 可带 1 个附件） "
                 f"| 图片(直链/data:)/文件/音频/视频 | 流式与非流式均支持 |")
    lines.append("")
    lines.append(f"已注册 {len(chat_models)} 个上游 chat 模型（TTL 缓存，来自上游 "
                 f"`GET /api/models`，仅注册真正可跑 t2t 的条目）。")
    lines.append("思考档位：`reasoning_effort`（`none`/`minimal`=快速、`high`=强制思考、缺省=自动）或 "
                 "`enable_thinking:false`；流式思考期发空格心跳变相加速首字（正文前导一个空格）。")
    lines.append("注意：少数模型（如 `qwen3.8-omni-flash`）在自动/强制思考档会把思考过程并入正文 `content`"
                 "（无 `reasoning_content` 字段）——本服务对这类模型**缺省自动切快速档**保证正文干净；"
                 "显式传 `reasoning_effort:\"high\"` 则照发并在 `degradations` 告警。")
    lines.append("")
    lines.append("### 附件解析（chat 门）")
    lines.append("")
    lines.append("| 分段类型 | 来源形态 | 处理 |")
    lines.append("|---|---|---|")
    lines.append("| `image_url` | http(s) 直链 | 直接引用（上游域内图已实测；外链照发+告警） |")
    lines.append("| `image_url` | data: URI | 服务端转上传链 |")
    lines.append("| `file` / `input_file` | http(s) URL | 服务端代下载 → 上传 → 文档解析 |")
    lines.append("| `input_audio` / `audio_url` | base64 / URL | 服务端转上传链 → 音频理解 |")
    lines.append("| `video_url` | URL / data: URI | 服务端转上传链 → 视频理解 |")
    lines.append("| 多个附件 | — | **400**（一次一个，不替你挑） |")
    lines.append("")
    lines.append("## 端点")
    lines.append("")
    lines.append("| 端点 | 鉴权 | 说明 |")
    lines.append("|---|---|---|")
    lines.append("| `GET /llms.txt` | 免 | 本文件 |")
    lines.append("| `GET /` | 免 | HTML 落地页 |")
    lines.append("| `GET /v1/models` | 免 | 能力清单（OpenAI 形态） |")
    lines.append("| `POST /v1/chat/completions` | **Key** | 对话/解析（t2t；`tools` 等见回退节） |")
    lines.append("| `POST /v1/responses` | **Key** | Responses 形态（语义同上） |")
    lines.append("| `POST /api/v3/contents/generations/tasks` | **Key** | 视频·创建（方舟契约，只回 id） |")
    lines.append("| `GET /api/v3/contents/generations/tasks/{id}` | 允许无 Key | 视频·查询（id 即凭据） |")
    lines.append("| `GET /healthz` `/readyz` | 免 | 探活（`/stats` 建议反代层封禁） |")
    lines.append("")
    auth_line = (f"已启用（{len(settings.api_keys)} 个 Key）" if auth_on
                 else "**未启用**（仅限内网部署；公网务必配置 `API_KEYS`）")
    lines.append(f"鉴权：`Authorization: Bearer <Key>`，当前部署 {auth_line}。")
    lines.append("")
    lines.append("## 回退通道")
    lines.append("")
    if fallback_on:
        lines.append("**已启用**：对话门遇到 qwen 做不了的能力（`tools` 函数调用、"
                     "文件/音频/视频附件、多图、data: 图片）时，**整单转方舟 chat/responses "
                     "原生执行**——函数调用与内置搜索真实生效。应答 `model` 字段回显请求值、"
                     "带 `x-qwen-fallback: ark` 头与 `degradations` 说明；"
                     "回退模型名全链路脱敏（含报错报文）。回退通道故障 ⇒ 502，不静默降回。")
    else:
        lines.append("**未配置**：`tools` 会被忽略并写入 `degradations`（上游实测不支持函数调用）；"
                     "文件/音频/视频附件 ⇒ 400（除非启用附件上传链）；data: 图片 ⇒ 400。"
                     "启用方式：配置 `ARK_FALLBACK_KEY` + `ARK_FALLBACK_MODEL`。")
    lines.append("")
    lines.append("## 附件上传链")
    lines.append("")
    up_line = ("**已启用**：附件由服务端代下载（SSRF 防护）→ 上传上游 OSS → 原生解析。"
               if upload_on else "**未启用**（`QWEN_UPLOAD_ENABLED=0`）。")
    lines.append(f"上传链：{up_line}单附件 ≤{max_mb}MB（`QWEN_UPLOAD_MAX_BYTES`）。")
    lines.append("")
    lines.append("## 错误")
    lines.append("")
    lines.append("视频门 `type` 为方舟词汇；chat 门按 HTTP 映射 OpenAI 词汇"
                 f"（400 `{openai_chat.CHAT_ERROR_TYPE_BY_STATUS[400]}`、"
                 f"401 `{openai_chat.CHAT_ERROR_TYPE_BY_STATUS[401]}`、"
                 f"429 `{openai_chat.CHAT_ERROR_TYPE_BY_STATUS[429]}`、"
                 f"5xx `server_error`）。所有响应带 `x-request-id`。")
    lines.append("")
    lines.append("| HTTP | code | retryable | 何时 |")
    lines.append("|---|---|---|---|")
    for row in sorted(_error_rows(), key=lambda r: (r["http"], r["code"])):
        retry = "✅ 可退避重试" if row["retryable"] else "❌ 勿自动重试"
        lines.append(f"| {row['http']} | `{row['code']}` | {retry} | {html.escape(row['when'])} |")
    lines.append("")
    lines.append("## 版本与文档")
    lines.append("")
    lines.append(f"- 版本：`v{__version__}`")
    lines.append("- 契约：`docs/INTERFACE.md`（对外）、`docs/UPSTREAM.md`（上游）——仓库内相对路径")
    lines.append("- 相关端点：[/llms.txt](/llms.txt) · [/v1/models](/v1/models) · [/healthz](/healthz)")
    lines.append("")
    return "\n".join(lines)


def render_landing(settings: Settings, chat_models: list[str] | None = None) -> str:
    """`GET /` 落地页（给浏览器看的最小 HTML；契约正文链到 /llms.txt）。"""
    chat_models = list(chat_models or [])
    n_chat = len(chat_models)
    video = catalog()[0]
    auth_line = "已启用" if settings.api_keys else "未启用（仅限内网）"
    fallback_line = "已启用（能力不足自动转方舟）" if settings.ark_fallback_enabled else "未配置"
    esc = html.escape
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{SERVICE_NAME}</title>
<style>body{{font-family:-apple-system,system-ui,sans-serif;max-width:720px;margin:48px auto;
padding:0 20px;color:#1a1a1a;line-height:1.6}} code{{background:#f4f4f4;padding:2px 6px;
border-radius:4px}} a{{color:#0a66d0}}</style></head>
<body>
<h1>{SERVICE_NAME} <small style="color:#888">v{__version__}</small></h1>
<p>chat.qwen.ai 双门出口：<b>视频生成</b>（方舟 Seedance 契约，t2v/i2v，固定 ~5s）
与 <b>AI 对话 / 多模态解析</b>（OpenAI 形态：文本、图片、文件、音频、视频）。
多账号池，对话不消耗视频额度。</p>
<ul>
  <li>视频能力：<code>{esc(video['id'])}</code>（{esc('/'.join(video['ratios']))}，时长 ~{video['duration_s']}s）</li>
  <li>chat 模型：已注册 {n_chat} 个（见 <a href="/v1/models">/v1/models</a>）</li>
  <li>鉴权：Bearer Key（{esc(auth_line)}）</li>
  <li>回退通道：{esc(fallback_line)}</li>
</ul>
<p>给 LLM/Agent 读的完整契约：<a href="/llms.txt">/llms.txt</a> ·
健康：<a href="/healthz">/healthz</a></p>
</body></html>"""


__all__ = ["render_landing", "render_llms_txt"]
