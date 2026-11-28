"""路由汇总：把各业务的 router 挂到同一个 api_router。"""

from fastapi import APIRouter

from app.api.routes import agent, budgets, meta, query, receipts, stats, transactions

api_router = APIRouter()
api_router.include_router(transactions.transactions_router)
api_router.include_router(query.query_router)
api_router.include_router(receipts.receipts_router)
api_router.include_router(stats.stats_router)
api_router.include_router(budgets.budgets_router)
api_router.include_router(agent.agent_router)
api_router.include_router(meta.router)
