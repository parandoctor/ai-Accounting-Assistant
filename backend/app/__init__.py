"""记账助手后端。

文件分工：
    main.py      应用入口：建库建表、CORS、挂路由、基础接口
    api.py       HTTP 层：Pydantic 模型与六个路由
    error.py     统一错误结构、异常处理器
    db.py        存储层：领域枚举、MySQL 连接、建表与全部 SQL
    ai_client.py 模型调用：传输重试 + OpenAI 兼容接口 + 字段清洗
    dify.py      Dify 接入：任务工作流、文件上传、对话应用
    ai_tasks.py  抽取任务：一句话记账、查账解析、小票识别、规则兜底
    ai_chat.py   多轮对话：Dify / 兼容接口 / 本地兜底三路降级
"""
