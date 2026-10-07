"""Loopback-only web API. Source text is untrusted data, never executable input."""

import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from absa import CONTENT_LABELS, FEEDBACK_LABELS, TAXONOMY
from analysis import Analyzer, Settings
from repository import MODULES, SENTIMENTS, TYPES, Repository

ROOT = Path(__file__).resolve().parent


class InputFile(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    content: str


class ImportRequest(BaseModel):
    files: list[InputFile] = Field(min_length=1, max_length=20)
    site: str = "rednote"


class ReviewRequest(BaseModel):
    relevance: str
    review_status: str
    opinions: list[dict[str, Any]] = Field(default_factory=list, max_length=30)
    brand_relevance: str | None = None
    product_scope: str | None = None
    content_types: list[str] | None = None
    review_reasons: list[str] | None = None
    analysis_schema: str | None = None


class EstimateRequest(BaseModel):
    mode: str = "pending"
    source_keys: list[str] = Field(default_factory=list, max_length=10000)


class StartRequest(BaseModel):
    estimate_token: str


class SettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    base_url: str | None = None
    model: str | None = None
    input_price: float | None = Field(default=None, ge=0, le=100000)
    output_price: float | None = Field(default=None, ge=0, le=100000)
    monthly_budget: float | None = Field(default=None, ge=0, le=10000)
    max_output_tokens: int | None = Field(default=None, ge=200, le=8000)
    max_input_chars: int | None = Field(default=None, ge=100, le=100000)
    api_key: str | None = Field(default=None, max_length=1000)
    clear_key: bool = False
    concurrency: int | None = Field(default=None, ge=1, le=4)
    reasoning_effort: Literal["low", "high", "max"] | None = None
    format_retries: int | None = Field(default=None, ge=0, le=1)


def create_app(
    db_path: Path | None = None, *, testing: bool = False, env_path: Path | None = None
) -> FastAPI:
    repo = Repository(db_path or Path(os.environ.get("FIREFLY_DB", str(ROOT / "data/feedback.sqlite3"))))
    settings = Settings(repo.db, env_path=env_path or (None if testing else ROOT / ".env"))
    analyzer = Analyzer(repo, settings)
    csrf = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        analyzer.stop()

    app = FastAPI(
        title="FIREFLY 座舱反馈", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.repo, app.state.analyzer = repo, analyzer

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "").split(":")[0]
        allowed = {"127.0.0.1", "localhost"} | ({"testserver"} if testing else set())
        if host not in allowed:
            return JSONResponse({"detail": "仅允许本机访问"}, status_code=403)
        if request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if request.headers.get("sec-fetch-site") == "cross-site" or (
                origin and urlparse(origin).netloc != request.headers.get("host")
            ):
                return JSONResponse({"detail": "外部页面不能访问本地数据"}, status_code=403)
            if request.method not in ("GET", "HEAD", "OPTIONS") and not secrets.compare_digest(
                request.headers.get("x-firefly-token", ""), csrf
            ):
                return JSONResponse({"detail": "页面会话已失效，请刷新后重试"}, status_code=403)
            length = request.headers.get("content-length", "0")
            if not length.isdigit() or int(length) > 32_000_000:
                return JSONResponse({"detail": "单次上传不能超过 30 MB"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        return response

    @app.exception_handler(ValueError)
    async def invalid_value(request: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing(request: Request, exc: KeyError):
        return JSONResponse({"detail": "记录不存在，可能已删除"}, status_code=404)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        return JSONResponse({"detail": "输入格式不正确，请检查必填项与数值范围"}, status_code=422)

    @app.exception_handler(ValidationError)
    async def invalid_config(request: Request, exc: ValidationError):
        return JSONResponse({"detail": "配置值不正确，请检查模型价格与限制"}, status_code=400)

    def require_idle():
        if analyzer.state()["running"]:
            raise ValueError("请先停止分析，等待当前请求结束后再修改数据或设置")

    @app.get("/api/state")
    def state():
        public = settings.public()
        return {
            "csrf_token": csrf,
            "sources": repo.sources(),
            "opinions": repo.opinions(),
            "batches": repo.batches(),
            "settings": public,
            "budget": analyzer.budget.summary(public["monthly_budget"]),
            "job": analyzer.state(),
            "options": {
                "modules": MODULES,
                "types": TYPES,
                "sentiments": SENTIMENTS,
                "submodules": TAXONOMY,
                "feedback_types": FEEDBACK_LABELS,
                "content_types": CONTENT_LABELS,
            },
            "analysis_metrics": analyzer.budget.metrics(),
            "ledger": analyzer.budget.ledger(),
        }

    @app.post("/api/import")
    def import_data(value: ImportRequest):
        with analyzer.lock:
            require_idle()
            return repo.import_files([f.model_dump() for f in value.files], value.site)

    @app.get("/api/sources/{key}")
    def detail(key: str):
        return repo.detail(key)

    @app.put("/api/sources/{key}/review")
    def review(key: str, value: ReviewRequest):
        with analyzer.lock:
            repo.review(key, value.model_dump(exclude_none=True))
        return {"ok": True}

    @app.post("/api/sources/{key}/retry-confirm")
    def retry_confirm(key: str):
        with analyzer.lock:
            analyzer.retry_confirm(key)
        return {"ok": True}

    @app.delete("/api/batches/{bid}")
    def delete_batch(bid: str):
        with analyzer.lock:
            require_idle()
            return repo.delete_batch(bid)

    @app.put("/api/settings")
    def save_settings(value: SettingsRequest):
        with analyzer.lock:
            require_idle()
            return settings.update(value.model_dump(exclude_unset=True))

    @app.post("/api/analysis/estimate")
    def estimate(value: EstimateRequest):
        with analyzer.lock:
            return analyzer.estimate(value.model_dump())

    @app.post("/api/analysis/start")
    def start(value: StartRequest):
        return analyzer.start(value.estimate_token)

    @app.post("/api/analysis/stop")
    def stop():
        analyzer.stop()
        return {"ok": True}

    @app.get("/api/export.csv")
    def export(request: Request):
        return Response(
            repo.export_csv(dict(request.query_params)),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="firefly-feedback.csv"'},
        )

    @app.get("/api/export-scope.txt")
    def scope(request: Request):
        from db import packed

        text = (
            "FIREFLY 导出说明\n每行一个有效观点；同一来源可有多个观点。仅相关且已分析或人工标注的观点进入导出。\n筛选条件："
            + packed(dict(request.query_params))
            + "\n模块讨论量按来源编号去重；情感按观点统计。结果仅代表导入样本。\n"
        )
        return Response(text, media_type="text/plain; charset=utf-8")

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.get("/")
    def index():
        return FileResponse(ROOT / "static/index.html")

    return app
