from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.db.identity import lock_identity
from backend.models.consultation import SetTool, ToolChange, ToolSetting
from backend.models.projects import ProjectError


class ToolStore:
    def __init__(self, database):
        self.database = database

    async def get(self, principal):
        async with self.database.session() as session:
            await lock_identity(session, principal, shared=True)
            row = await session.get(ToolSetting, "web_search")
            return {
                "name": "web_search",
                "enabled": bool(row and row.enabled),
                "version": row.version if row else 0,
                "provider": "DuckDuckGo",
                "description": "公开资料搜索摘要；结果作为待确认建议",
            }

    async def set(self, principal, data: SetTool):
        try:
            async with self.database.session() as session:
                actor = await lock_identity(session, principal)
                if actor.role != "admin":
                    raise ProjectError("FORBIDDEN", "需要管理员权限", 403)
                previous = await session.scalar(
                    select(ToolChange).where(
                        ToolChange.actor_id == actor.id, ToolChange.request_id == data.request_id
                    )
                )
                if previous:
                    if (
                        previous.enabled != data.enabled
                        or previous.expected_version != data.expected_version
                    ):
                        raise ProjectError("REQUEST_REUSED", "请求编号已用于其他设置")
                    return {"enabled": previous.enabled, "version": previous.version}
                row = await session.scalar(
                    select(ToolSetting).where(ToolSetting.name == "web_search").with_for_update()
                )
                if (row.version if row else 0) != data.expected_version:
                    raise ProjectError("VERSION_CONFLICT", "工具设置已更新，请刷新后重试")
                if row is None:
                    row = ToolSetting(name="web_search", enabled=data.enabled, version=1)
                    session.add(row)
                else:
                    row.enabled, row.version = data.enabled, row.version + 1
                session.add(
                    ToolChange(
                        actor_id=actor.id,
                        request_id=data.request_id,
                        enabled=data.enabled,
                        expected_version=data.expected_version,
                        version=row.version,
                        created_at=datetime.now(UTC),
                    )
                )
                await session.flush()
                return {"enabled": row.enabled, "version": row.version}
        except IntegrityError:
            raise ProjectError("VERSION_CONFLICT", "工具设置发生并发更新，请刷新后重试") from None
