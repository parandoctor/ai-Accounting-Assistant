"""记账助手后端。

分层结构：
    api/      路由层，负责参数校验与响应组装
    schemas/  Pydantic 模型，同时作为 OpenAPI 契约来源
    core/     配置与错误处理
    store.py  数据访问层
"""
