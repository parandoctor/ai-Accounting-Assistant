"""路由依赖：请求级数据库连接。"""

from typing import Annotated

from fastapi import Depends
from pymysql.connections import Connection

from app.core.database import get_conn

ConnDep = Annotated[Connection, Depends(get_conn)]
