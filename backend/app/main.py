from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from app.features.trends.router import router as trends_router
from app.features.finance.router import router as finance_router
from app.features.receipts.router import router as receipts_router
from app.features.purchases.router import attach_routes, init_db
from app.features.purchases.mobile import control_router

documents_router = APIRouter(prefix="/documents", tags=["documents"])
statements_router = APIRouter(prefix="/statements", tags=["statements"])
attach_routes(receipts_router, "receipt")
attach_routes(documents_router, "document")
attach_routes(statements_router, "statement")


@asynccontextmanager
async def lifespan(app):
    init_db()
    yield


app = FastAPI(title="Final Project API", lifespan=lifespan)
for router in (trends_router, finance_router, receipts_router, documents_router, statements_router):
    app.include_router(router, prefix="/api/v1")
app.include_router(control_router, prefix="/api/v1/receipts")


@app.get("/api/health")
def health():
    return {"status": "ok"}
