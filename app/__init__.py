"""qwen-service —— chat.qwen.ai 视频生成（t2v / i2v）的火山方舟 Seedance 契约出口。

上游：`chat.qwen.ai` 网页端内部接口（异步任务：提交拿 task_id → 轮询拿产物）。
对外：`POST|GET /api/v3/contents/generations/tasks`（逐字段对齐方舟原生契约）。
"""
__version__ = "0.0.22"  # 单一事实源：发版流程（release.yml 的 bump job）只改这里
