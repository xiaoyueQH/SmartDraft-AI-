from sqlalchemy import func, or_, select

from backend.db.identity import lock_identity
from backend.db.project_store import view
from backend.models.orm import User
from backend.models.projects import Project, ProjectError, ProjectItem

CATEGORIES = {
    "documents": ("specification", "change"),
    "tasks": ("task",),
    "deliveries": ("acceptance",),
    "service": ("bug", "support"),
}
CLOSED = ("approved", "closed", "rejected", "withdrawn")


class ProjectAdminStore:
    def __init__(self, database, cache=None):
        self.database = database
        self.cache = cache

    async def _authorize(self, session, principal):
        actor = await lock_identity(session, principal, shared=True)
        if actor.role != "admin":
            raise ProjectError("FORBIDDEN", "需要管理员权限", 403)

    async def summary(self, principal):
        if self.cache and self.cache.settings.enabled:
            async with self.database.session() as session:
                await self._authorize(session, principal)
            return await self.cache.get_or_load(
                "summary",
                str(principal.account.id),
                lambda: self._summary(principal),
                self.cache.settings.summary_ttl_seconds,
            )
        return await self._summary(principal)

    async def _summary(self, principal):
        async with self.database.session() as session:
            await self._authorize(session, principal)
            phases = dict(
                (
                    await session.execute(
                        select(Project.phase, func.count())
                        .where(Project.deleted_at.is_(None))
                        .group_by(Project.phase)
                    )
                ).all()
            )
            grouped = (
                await session.execute(
                    select(
                        ProjectItem.kind,
                        ProjectItem.status,
                        ProjectItem.staff_confirmed,
                        func.count(),
                    )
                    .join(Project, Project.id == ProjectItem.project_id)
                    .where(Project.deleted_at.is_(None))
                    .group_by(ProjectItem.kind, ProjectItem.status, ProjectItem.staff_confirmed)
                )
            ).all()
            return {
                "consultations": phases.get("discovery", 0),
                "projects": phases.get("project", 0),
                "pending_reviews": sum(
                    count
                    for kind, status, confirmed, count in grouped
                    if kind in CATEGORIES["documents"] and status == "submitted" and not confirmed
                ),
                "active_tasks": sum(
                    count
                    for kind, status, _, count in grouped
                    if kind == "task" and status not in CLOSED
                ),
                "active_deliveries": sum(
                    count
                    for kind, status, _, count in grouped
                    if kind == "acceptance" and status not in CLOSED
                ),
                "open_service": sum(
                    count
                    for kind, status, _, count in grouped
                    if kind in CATEGORIES["service"] and status not in CLOSED
                ),
            }

    async def projects(self, principal, *, offset=0, limit=20, query="", phase=""):
        async with self.database.session() as session:
            await self._authorize(session, principal)
            statement = (
                select(Project, User.display_name, User.username)
                .join(User, User.id == Project.owner_id)
                .where(Project.deleted_at.is_(None))
            )
            if phase:
                statement = statement.where(Project.phase == phase)
            if query:
                statement = statement.where(
                    or_(
                        Project.title.contains(query, autoescape=True),
                        User.username.contains(query, autoescape=True),
                        User.display_name.contains(query, autoescape=True),
                    )
                )
            rows = (
                await session.execute(
                    statement.order_by(Project.created_at.desc(), Project.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
            return {
                "items": [
                    view(row) | {"customer_name": name, "customer_username": username}
                    for row, name, username in rows[:limit]
                ],
                "has_more": len(rows) > limit,
            }

    async def items(self, principal, *, category, offset=0, limit=20, query="", status="active"):
        async with self.database.session() as session:
            await self._authorize(session, principal)
            statement = (
                select(ProjectItem, Project.title, User.display_name, User.username)
                .join(Project, Project.id == ProjectItem.project_id)
                .join(User, User.id == Project.owner_id)
                .where(Project.deleted_at.is_(None), ProjectItem.kind.in_(CATEGORIES[category]))
            )
            if status == "active":
                statement = statement.where(ProjectItem.status.not_in(CLOSED))
            elif status == "pending_review":
                statement = statement.where(
                    ProjectItem.status == "submitted", ProjectItem.staff_confirmed.is_(False)
                )
            elif status != "all":
                statement = statement.where(ProjectItem.status == status)
            if query:
                statement = statement.where(
                    or_(
                        ProjectItem.title.contains(query, autoescape=True),
                        Project.title.contains(query, autoescape=True),
                        User.display_name.contains(query, autoescape=True),
                        User.username.contains(query, autoescape=True),
                    )
                )
            rows = (
                await session.execute(
                    statement.order_by(ProjectItem.created_at.desc(), ProjectItem.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
            return {
                "items": [
                    {
                        key: value
                        for key, value in view(row).items()
                        if key not in {"content", "questions", "resolution", "acceptance_note"}
                    }
                    | {"project_title": title, "customer_name": name, "customer_username": username}
                    for row, title, name, username in rows[:limit]
                ],
                "has_more": len(rows) > limit,
            }
