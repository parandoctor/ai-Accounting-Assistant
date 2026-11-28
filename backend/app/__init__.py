"""记账助手后端。

目录分工：
    main.py                   应用入口：建表、CORS、挂路由
    api/routes/               各业务接口，一个业务一个文件
    api/dependencies.py       路由依赖：请求级数据库连接
    core/config.py            配置：数据库参数、脚本路径、版本号
    core/database.py          连接、建库建表与补列
    core/errors.py            统一错误结构与异常处理器
    models.py                 类别与来源枚举、数据库行结构
    schemas.py                接口请求与响应模型
    operations.py             全部 SQL 与行映射
    utils.py                  时区校验、金额元分换算
    ai/client.py              模型调用与输出清洗
    ai/dify.py                Dify 工作流与对话应用
    ai/tasks.py               一句话记账、小票识别、查账解析
    ai/chat.py                多轮对话三路降级
"""
