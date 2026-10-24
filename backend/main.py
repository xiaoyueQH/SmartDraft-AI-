import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.api.admission import RequestAdmissionMiddleware
from backend.api.errors import register_error_handlers
from backend.api.router import router as api_router
from backend.config import Settings, get_settings
from backend.core.request_context import RequestContextMiddleware
from backend.core.security import JwtCodec, PasswordWork, load_signing_key
from backend.db.auth_store import AuthStore
from backend.db.session import create_database
from backend.services.auth import AuthService


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings if settings is not None else get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = create_database(resolved_settings.database)
        app.state.database = database
        app.state.settings = resolved_settings
        app.state.request_slots = asyncio.Semaphore(resolved_settings.app.request_capacity)
        passwords = PasswordWork(resolved_settings.auth.password_workers)
        try:
            await passwords.initialize()
            app.state.auth_service = AuthService(
                AuthStore(database),
                JwtCodec(resolved_settings.auth, load_signing_key(resolved_settings.auth)),
                passwords,
                resolved_settings.auth,
            )
            yield
        finally:
            await passwords.close()
            await database.close()
            if hasattr(app.state, "auth_service"):
                del app.state.auth_service
            del app.state.database
            del app.state.settings
            del app.state.request_slots

    app = FastAPI(title="SupportFlow API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestAdmissionMiddleware)
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)
    app.include_router(api_router)
    return app
