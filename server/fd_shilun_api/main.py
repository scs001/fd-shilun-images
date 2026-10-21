"""实论门面 API。

/healthz 探活、/api/version 版本。业务路由由各 feature 模块提供（子代理交付后统一挂载）。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .gongwen.api import router as gongwen_router
from .gov import router as gov_router
from .proofread.api import router as proofread_router
from .search import router as search_router
from .writing import router as writing_router

app = FastAPI(title="fd-shilun-api", version=__version__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(search_router)
app.include_router(gov_router)
app.include_router(writing_router)
app.include_router(gongwen_router)
app.include_router(proofread_router)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "service": "fd-shilun-api", "version": __version__}


@app.get("/api/version")
def version() -> dict:
    return {"service": "fd-shilun-api", "version": __version__}
