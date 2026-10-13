"""共享夹具：假上游（记录每个请求）+ 可注入的 Settings / Store / Pool / Client。

零网络纪律：全部用例走 `httpx.MockTransport`，不发任何真实请求（含 DNS）。
测试确定性：夹具里 **协调器关闭**（生产默认开启）—— 否则后台线程会与用例断言抢推进。
"""
from __future__ import annotations

import base64
import json
import time

import httpx
import pytest

from app.config import Settings
from app.service import QwenVideoService
from app.store import TaskStore
from app.upstream.qwen.accounts import AccountPool
from app.upstream.qwen.client import QwenClient

CDN_IMAGE = "https://cdn.qwenlm.ai/output/u/image_gen/m1/1.png"
SUCCESS_URL = "https://cdn.qwenlm.ai/output/u/i2v/c/{task}.mp4?key=k"

#: 风控信封（HTTP 200 + ret 数组）——服务端的"可证明未提交"失败样本
RISK_BODY = {"ret": ["FAIL_SYS_USER_VALIDATE", "RGV587_ERROR::SM::哎哟喂,被挤爆啦,请稍后重试"],
             "data": {"url": "https://chat.qwen.ai/_____tmd_____/punish"}}

#: 401 信封（HTTP 200 + 业务码）——"token 提前失效"的样本，用于续期兜底用例
UNAUTHORIZED_BODY = {"success": False,
                     "data": {"code": "Unauthorized", "details": "您没有权限访问此资源"}}

#: t2t 流式响应样本（形状 = 2026-09-24 上游实测，UPSTREAM §4.5 / U-12 已关闭）：
#: thinking 摘要事件 content 恒空、正文在 phase:"answer" 的 delta.content、
#: usage 随 answer 事件出现（逐事件递增）、结束 = status:"finished"、**没有 [DONE]**。
SSE_T2T = (
    'data: {"response.created":{"chat_id":"chat-1","response_index":"0"}}\n\n'
    'data: {"choices":[{"delta":{"role":"assistant","content":"","phase":"thinking_summary",'
    '"extra":{"summary_title":{"content":["想一下"]}}}}]}\n\n'
    'data: {"choices":[{"delta":{"role":"assistant","content":"","phase":"thinking_summary",'
    '"status":"finished"}}],"response_id":"r1"}\n\n'
    'data: {"choices":[{"delta":{"role":"assistant","content":"你好","phase":"answer",'
    '"status":"typing"}}],"response_id":"r1",'
    '"usage":{"input_tokens":2421,"output_tokens":9,"total_tokens":2430}}\n\n'
    'data: {"choices":[{"delta":{"role":"assistant","content":"，世界","phase":"answer",'
    '"status":"typing"}}],"response_id":"r1",'
    '"usage":{"input_tokens":2421,"output_tokens":12,"total_tokens":2433}}\n\n'
    'data: {"choices":[{"delta":{"content":"","role":"assistant","status":"finished",'
    '"phase":"answer"}}],"response_id":"r1"}\n\n'
)

#: 流内错误事件样本（HTTP 200 包错误；audio/video 探针实测形态，UPSTREAM §4.5）
SSE_ERROR_EVENT = (
    'data: {"response.created":{"chat_id":"chat-1","response_index":"0"}}\n\n'
    'data: {"error":{"code":"invalid_input","details":"输入或附件无效。请检查后重试。"},'
    '"response_id":"r1","response_index":0}\n\n'
)


def _model_item(mid: str, name: str, chat_types: list, active: bool = True) -> dict:
    """/api/models 条目最小样本（形状照 2026-09-24 实测响应裁剪）。"""
    return {"id": mid, "name": name, "object": "model", "owned_by": "qwen",
            "info": {"id": mid, "name": name, "is_active": active,
                     "created_at": 1732711466,
                     "meta": {"chat_type": chat_types,
                              "max_context_length": 1000000,
                              "capabilities": {"vision": True, "thinking": True}}}}


#: 与真实上游同构的默认清单（3 个模型都含 t2t）
DEFAULT_MODELS = [
    _model_item("qwen3.7-plus", "Qwen3.7-Plus", ["t2t", "t2v", "t2i", "search"]),
    _model_item("qwen3.8-max", "Qwen3.8-Max", ["t2t", "t2v", "t2i"]),
    _model_item("qwen3.8-omni-flash", "Qwen3.8-Omni-Flash", ["t2t", "t2i", "vqa"]),
]


class FakeQwen:
    """内存假上游：按路径分流，记录**每一个**收到的请求（供"上游实际收到什么"断言）。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.chat_seq = 0
        self.task_seq = 0
        #: task_id -> 待回放的 [(data, actual_status_code)]；只剩一条时重复回放
        self.status_scripts: dict[str, list[tuple[dict, int]]] = {}
        #: 非 None 时，视频提交端点固定回这个 body（用于错误映射用例）
        self.submit_response: dict | None = None
        self.submit_status_code = 200
        #: 提交端点的一次性失败队列（先到先消费；空了才走正常成功路径）
        self.fail_submits: list[dict] = []
        # ---- chat 门 / 模型清单注入点 ----
        self.models_payload: list[dict] | None = None   # None ⇒ DEFAULT_MODELS
        self.models_fail = False                        # True ⇒ /api/models 回 503
        self.chat_sse_body: str | None = None           # None ⇒ SSE_T2T
        self.chat_sse_delay: float = 0.0                # >0 ⇒ 每行 SSE 间隔秒数（测 ping）

    # ------------------------------------------------------------ 断言工具

    def calls(self, path_prefix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.startswith(path_prefix)]

    def bodies(self, path_prefix: str) -> list[dict]:
        return [json.loads(r.content) for r in self.calls(path_prefix)]

    def cookies(self, path_prefix: str) -> list[str]:
        return [r.headers.get("cookie", "") for r in self.calls(path_prefix)]

    # ------------------------------------------------------------ 假上游实现

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "PUT":
            # OSS 附件上传（绝对 URL 到 oss host；MockTransport 一并拦截）
            return httpx.Response(200)
        path = request.url.path
        if path == "/api/v2/files/getstsToken":
            body = json.loads(request.content)
            return httpx.Response(200, json={"success": True, "request_id": "r", "data": {
                "access_key_id": "STSTESTID", "access_key_secret": "ststest-secret",
                "security_token": "ststest-token", "bucketname": "qwen-webui-prod",
                "endpoint": "oss-accelerate.aliyuncs.com",
                "file_path": f"u/{body.get('file_name', 'x')}",
                "file_id": "fid-1", "region": "oss-ap-southeast-1",
                "file_url": "https://qwen-webui-prod.oss-accelerate.aliyuncs.com/u/x?sig=1"}})
        if path == "/api/models":
            if self.models_fail:
                return httpx.Response(503, json={"error": "upstream down"})
            return httpx.Response(200, json={"data": self.models_payload or DEFAULT_MODELS})
        if path == "/api/v2/chats/new":
            self.chat_seq += 1
            return httpx.Response(
                200, json={"success": True, "data": {"id": f"chat-{self.chat_seq}"}})
        if path == "/api/v2/chat/completions":
            body = json.loads(request.content)
            first = (body.get("messages") or [{}])[0]
            if first.get("chat_type") == "t2t":
                sse_text = self.chat_sse_body or SSE_T2T
                if self.chat_sse_delay:
                    import time as _time

                    def _slow_lines():
                        for ln in sse_text.splitlines(keepends=True):
                            _time.sleep(self.chat_sse_delay)
                            yield ln.encode()

                    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                          content=_slow_lines())
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      content=sse_text.encode())
            if self.fail_submits:
                return httpx.Response(200, json=self.fail_submits.pop(0))
            if self.submit_response is not None:
                return httpx.Response(self.submit_status_code, json=self.submit_response)
            self.task_seq += 1
            task_id = f"tid-{self.task_seq}"
            self.status_scripts.setdefault(task_id, [
                ({"task_status": "running"}, 200),
                ({"task_status": "success",
                  "content": SUCCESS_URL.format(task=task_id)}, 200),
            ])
            return httpx.Response(200, json={
                "success": True, "request_id": "r",
                "data": {"chat_id": "chat", "chat_type": "t2v",
                         "messages": [{"role": "assistant", "content": "",
                                       "extra": {"wanx": {"task_id": task_id}}}]},
            })
        if path.startswith("/api/v2/task/status/"):
            task_id = path.rsplit("/", 1)[-1]
            script = self.status_scripts.get(task_id)
            if script is None:
                return httpx.Response(200, headers={"x-actual-status-code": "404"},
                                      json={"success": False,
                                            "data": {"code": "Not_Found", "details": "Task not found"}})
            data, actual = script[0] if len(script) == 1 else script.pop(0)
            return httpx.Response(200, headers={"x-actual-status-code": str(actual)},
                                  json={"success": actual == 200, "data": data})
        raise AssertionError(f"unexpected upstream path: {path}")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def fake_upstream() -> FakeQwen:
    return FakeQwen()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        accounts={"a@x.cn": "pw-a", "b@x.cn": "pw-b"},
        task_db=f"sqlite:///{tmp_path}/qwen.db",
        data_dir=str(tmp_path),
        key_secret="secret-for-tests",
        signin_proxy="http://127.0.0.1:9",  # 不会被真的使用（mint 已注入）
        signin_min_interval=0.0,
        signin_wait_timeout=1.0,
        submit_min_interval=0.0,
        daily_video_cap=3,
        account_wait_timeout=0.05,
        coordinator_enabled=False,   # 测试确定性：不让后台线程抢推进
        queue_retry_base=0.0,        # 排队重试不等待（用例内即时出队）
        upload_enabled=False,        # 默认关（存量用例零网络）；上传用例用 upload_app
    )


@pytest.fixture
def piped(settings, fake_upstream):
    """(service, store, pool, client, fake) —— 全部走 mock 上游，零网络。"""
    store = TaskStore(settings.task_db)
    pool = AccountPool(settings, mint=lambda account: f"tok-{account.email}")
    client = QwenClient(settings, transport=fake_upstream.transport())
    service = QwenVideoService(settings, store, pool, client)
    return service, store, pool, client, fake_upstream


@pytest.fixture
def client_app(settings, fake_upstream):
    """FastAPI 应用（注入 mock 上游）。产出 (TestClient, fake, store, settings)。"""
    from fastapi.testclient import TestClient

    from app.main import create_app

    store = TaskStore(settings.task_db)
    pool = AccountPool(settings, mint=lambda account: f"tok-{account.email}")
    client = QwenClient(settings, transport=fake_upstream.transport())
    app = create_app(settings, store=store, pool=pool, client=client)
    with TestClient(app) as test_client:
        yield test_client, fake_upstream, store, settings


def make_jwt(payload: dict) -> str:
    """最小 JWT 形状（header.payload.signature）—— 本服务只读载荷、**不验签**。"""
    b64 = lambda o: base64.urlsafe_b64encode(  # noqa: E731
        json.dumps(o).encode()).rstrip(b"=").decode()
    return (f"{b64({'alg': 'HS256', 'kid': 'legacy', 'typ': 'JWT'})}."
            f"{b64(payload)}.sig")


def _access_token(uid: str) -> str:
    return make_jwt({"id": uid, "type": "access_token",
                     "exp": time.time() + 365 * 86400, "iat": time.time()})


#: 形态① **账号串**（`邮箱|口令`）：白名单已删 ⇒ **形状合规即收**，无需登记。
#: 用夹具里真实存在的两个账号，保证「两个不同凭据 ⇒ 两个不同属主指纹」的语义不变。
AUTH_A = {"Authorization": "Bearer a@x.cn|pw-a"}
AUTH_B = {"Authorization": "Bearer b@x.cn|pw-b"}
TASKS_PATH = "/api/v3/contents/generations/tasks"
CHAT_PATH = "/v1/chat/completions"


class FakeArk:
    """内存假方舟（能力回退通道用）：记录请求，按 stream 与否回 SSE / JSON。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.fail = False              # True ⇒ 一律 500（回退通道故障用例）
        # 对这些**模型名**回 429（方舟限流语义保留 / 多模型 failover 用例）
        self.rate_limited_for: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        if body.get("model") in self.rate_limited_for:
            # 方舟实测限流形态：RequestBurstTooFast（Retry-After 语义保留给调用方）
            return httpx.Response(429, headers={"Retry-After": "7"},
                                  json={"error": {"code": "RequestBurstTooFast",
                                                  "message": "System protection triggered"}})
        if self.fail:
            # 🔴 故障报文里故意带"模型名"——验证出站报文脱敏（<redacted-model>）
            return httpx.Response(500, json={"error": {"message": "model doubao-test not found"}})
        if request.headers.get("authorization") != "Bearer ark-test-key":
            return httpx.Response(401, json={"error": {"message": "bad key"}})
        body = json.loads(request.content)
        if request.url.path.endswith("/responses"):
            if body.get("stream"):
                sse = (
                    'data: {"type":"response.created","response":{"id":"resp_ark",'
                    '"object":"response","status":"in_progress","model":"doubao-test",'
                    '"output":[]}}\n\n'
                    'data: {"type":"response.output_text.delta","item_id":"msg_ark",'
                    '"delta":"（方舟 responses 流式）"}\n\n'
                    'data: {"type":"response.completed","response":{"id":"resp_ark",'
                    '"object":"response","status":"completed","model":"doubao-test",'
                    '"output":[{"type":"message","role":"assistant","content":'
                    '[{"type":"output_text","text":"（方舟 responses 流式）"}]}],'
                    '"usage":{"input_tokens":5,"output_tokens":3,"total_tokens":8}}}\n\n'
                )
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      content=sse.encode())
            return httpx.Response(200, json={
                "id": "resp_ark", "object": "response", "status": "completed",
                "model": "doubao-test",
                "output": [{"type": "message", "role": "assistant",
                            "content": [{"type": "output_text",
                                         "text": "（方舟 responses 应答）"}]}],
                "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
            })
        if body.get("stream"):
            sse = (
                'data: {"id":"ark-1","object":"chat.completion.chunk","model":"doubao-test",'
                '"choices":[{"index":0,"delta":{"role":"assistant","content":""},'
                '"finish_reason":null}]}\n\n'
                'data: {"id":"ark-1","object":"chat.completion.chunk","model":"doubao-test",'
                '"choices":[{"index":0,"delta":{"content":"（方舟流式）"},'
                '"finish_reason":null}]}\n\n'
                'data: {"id":"ark-1","object":"chat.completion.chunk","model":"doubao-test",'
                '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                "data: [DONE]\n\n"
            )
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=sse.encode())
        return httpx.Response(200, json={
            "id": "ark-1", "object": "chat.completion", "model": "doubao-test",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "（方舟应答）"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
        })

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def fake_ark() -> FakeArk:
    return FakeArk()


@pytest.fixture
def ark_app(settings, fake_upstream, fake_ark):
    """回退通道启用的应用：方舟走假 transport，qwen 走假上游。产出 (tc, fake_qwen, fake_ark, s)。"""
    import dataclasses

    from fastapi.testclient import TestClient

    from app.main import create_app

    s = dataclasses.replace(settings, ark_fallback_key="ark-test-key",
                            ark_fallback_model="doubao-test",
                            ark_fallback_models=["doubao-test-turbo"],
                            ark_fallback_base="https://ark.example/api/v3",
                            upload_enabled=False)
    store = TaskStore(s.task_db)
    pool = AccountPool(s, mint=lambda account: f"tok-{account.email}")
    client = QwenClient(s, transport=fake_upstream.transport())
    app = create_app(s, store=store, pool=pool, client=client,
                     ark_transport=fake_ark.transport())
    with TestClient(app) as test_client:
        yield test_client, fake_upstream, fake_ark, s


@pytest.fixture
def upload_app(settings, fake_upstream):
    """附件上传链启用的应用：来源解析器注入假实现（零网络）。产出 (tc, fake_qwen, s, captured)。"""
    import dataclasses

    from fastapi.testclient import TestClient

    from app.main import create_app

    s = dataclasses.replace(settings, upload_enabled=True)
    captured: dict = {}

    def fake_resolver(kind, source, *, max_bytes, param):
        captured["kind"] = kind
        captured["source"] = source
        captured["max_bytes"] = max_bytes
        return b"ATTACHMENT-BYTES", "test-attachment.pdf", "application/pdf"

    store = TaskStore(s.task_db)
    pool = AccountPool(s, mint=lambda account: f"tok-{account.email}")
    client = QwenClient(s, transport=fake_upstream.transport())
    app = create_app(s, store=store, pool=pool, client=client,
                     attachment_resolver=fake_resolver)
    with TestClient(app) as test_client:
        yield test_client, fake_upstream, s, captured
