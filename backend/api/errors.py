import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.exceptions import HTTPException

from backend.models.auth import AuthError
from backend.models.cancellation import CancellationError
from backend.models.catalog import CandidateNotFound
from backend.models.commerce_errors import CommerceError
from backend.models.orders import OrderNotFound

logger = logging.getLogger("supportflow.errors")


def error_response(request: Request, status: int, code: str, message: str) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "")
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}, "request_id": request_id},
        headers={"X-Request-ID": request_id},
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(CommerceError)
    async def handle_commerce_error(request: Request, exc: CommerceError) -> JSONResponse:
        response = error_response(request, exc.status, exc.code, exc.message)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(CandidateNotFound)
    async def handle_missing_candidate(request: Request, exc: CandidateNotFound) -> JSONResponse:
        response = error_response(request, 404, "CANDIDATE_NOT_FOUND", "候选商品不存在，请重新搜索")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(CancellationError)
    async def handle_cancellation_error(request: Request, exc: CancellationError) -> JSONResponse:
        response = error_response(request, exc.status, exc.code, exc.message)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(PoolTimeoutError)
    @app.exception_handler(DBAPIError)
    async def handle_database_error(request: Request, exc: Exception) -> JSONResponse:
        code = getattr(getattr(exc, "orig", None), "sqlstate", None)
        retryable = isinstance(exc, PoolTimeoutError) or code in {"55P03", "57014", "40P01"}
        response = error_response(
            request,
            503 if retryable else 500,
            "DATABASE_BUSY" if retryable else "INTERNAL_ERROR",
            "数据库繁忙，请稍后重试原操作" if retryable else "服务暂时不可用，请稍后重试",
        )
        response.headers["Cache-Control"] = "no-store"
        if retryable:
            response.headers["Retry-After"] = "2"
        logger.error("database_request_failed error_type=%s sqlstate=%s", type(exc).__name__, code)
        return response

    @app.exception_handler(OrderNotFound)
    async def handle_missing_order(request: Request, exc: OrderNotFound) -> JSONResponse:
        response = error_response(request, 404, "ORDER_NOT_FOUND", "订单不存在")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AuthError)
    async def handle_auth_error(request: Request, exc: AuthError) -> JSONResponse:
        response = error_response(request, exc.status, exc.code, exc.message)
        response.headers["Cache-Control"] = "no-store"
        if exc.status == 429:
            response.headers["Retry-After"] = "60"
        sid = getattr(request.state, "clear_refresh_sid", None)
        if sid:
            from backend.api.v1.auth import clear_refresh

            clear_refresh(response, sid, request.app.state.settings.auth.cookie_secure)
        return response

    @app.exception_handler(HTTPException)
    async def handle_http_error(request: Request, exc: HTTPException) -> JSONResponse:
        response = error_response(
            request,
            exc.status_code,
            f"HTTP_{exc.status_code}",
            str(exc.detail) if isinstance(exc.detail, str) else "请求无法处理",
        )
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return error_response(request, 422, "VALIDATION_ERROR", "请求参数不符合要求")

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "request_failed request_id=%s error_type=%s",
            getattr(request.state, "request_id", ""),
            type(exc).__name__,
        )
        return error_response(request, 500, "INTERNAL_ERROR", "服务暂时不可用，请稍后重试")
