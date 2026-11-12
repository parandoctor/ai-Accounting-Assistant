"""记账助手后端。

分层：
    api/      路由层：收请求、校验参数、返回响应
    schemas/  Pydantic 模型：请求/响应的结构定义（同时是 OpenAPI 契约来源）
    core/     配置、错误处理等横切关注点
    store.py  数据访问（当前是内存实现，等 DB 同学给 schema 后换成 SQL）
"""
