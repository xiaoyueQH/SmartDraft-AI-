import asyncio

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class RequestAdmissionMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == "/api/health/live":
            await self.app(scope, receive, send)
            return

        state = scope["app"].state
        slots: asyncio.Semaphore = state.request_slots
        try:
            async with asyncio.timeout(state.settings.app.request_wait_seconds):
                await slots.acquire()
        except TimeoutError:
            response = JSONResponse(
                status_code=503,
                content={
                    "error": {"code": "SERVER_BUSY", "message": "服务繁忙，请稍后重试"},
                    "request_id": scope.get("state", {}).get("request_id", ""),
                },
                headers={"Retry-After": "1", "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        finally:
            slots.release()
