from contextlib import asynccontextmanager
import os

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.frontend_origins import frontend_origins
from app.features.trends.router import router as trends_router
from app.features.finance.router import router as finance_router
from app.features.receipts.router import router as receipts_router
from app.features.purchases.router import attach_routes, init_db
from app.features.purchases.mobile import control_router
from app.features.purchases.inbox import router as purchase_inbox_router
from app.features.purchases.price_history import router as purchase_price_router
from app.features.mail_inbox import router as mail_inbox_router
from app.features.mail_work import router as mail_work_router
from app.features.peer_performance import router as peer_performance_router

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
public_demo = os.getenv("AUTOFLOW_PUBLIC_DEMO") == "1"
origins = frontend_origins()
if origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )
for router in (trends_router, finance_router, mail_inbox_router, mail_work_router, peer_performance_router):
    app.include_router(router, prefix="/api/v1")
if not public_demo:
    for router in (receipts_router, documents_router, statements_router, purchase_inbox_router, purchase_price_router):
        app.include_router(router, prefix="/api/v1")
    app.include_router(control_router, prefix="/api/v1/receipts")


@app.get("/api/health")
def health():
    return {"status": "ok"}
