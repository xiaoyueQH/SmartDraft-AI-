from typing import Annotated

from fastapi import Depends, Request

from backend.config import Settings
from backend.db.cache import ReadCache
from backend.db.session import Database


def get_database(request: Request) -> Database:
    return request.app.state.database


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_read_cache(request: Request) -> ReadCache:
    return request.app.state.read_cache


DatabaseDep = Annotated[Database, Depends(get_database)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
CacheDep = Annotated[ReadCache, Depends(get_read_cache)]
