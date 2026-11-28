"""应用配置：读取 backend/.env，集中数据库参数与脚本路径。"""

import os
from pathlib import Path

from dotenv import load_dotenv

# 接口版本号，/health 与 OpenAPI 元数据共用。
API_VERSION = "0.1.0"

# 读 backend/.env：路径按文件位置推算，不受启动目录影响。
_DOTENV_PATH = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(_DOTENV_PATH)

# 环境变量缺省值与本机开发环境一致；生产部署用环境变量覆盖。
DB_NAME = os.getenv("DB_NAME", "ledger")
_DB_CONFIG = {
    "host": os.getenv("DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "charset": "utf8mb4",
}

# 建表脚本：backend/schema.sql 是唯一的表结构来源，应用启动时读取并执行。
# 脚本里的 create database / use 由代码跳过，库名统一走 DB_NAME。
_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema.sql"
