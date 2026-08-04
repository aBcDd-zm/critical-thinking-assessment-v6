from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import models  # noqa: F401 - registers SQLAlchemy metadata
from app.api.router import router
from app.core.config import Settings, settings
from app.core.database import Base, engine
from app.services.session_service import ServiceError


def create_app(config: Settings = settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if config.auto_create_db:
            Base.metadata.create_all(bind=engine)
        yield

    production = config.app_env == "production"
    application = FastAPI(
        title=config.app_name,
        version="6.0.0",
        lifespan=lifespan,
        docs_url=None if production else "/docs",
        redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json",
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(router, prefix="/api/v1")

    @application.exception_handler(ServiceError)
    async def service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code, "message": exc.message})

    return application


app = create_app()
