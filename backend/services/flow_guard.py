"""Bounded lifecycle-owned reconciliation; all decisions survive in PostgreSQL."""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import or_, select

from backend.db.auth_store import account, utc
from backend.db.conversation_store import ConversationStore
from backend.db.project_store import append_message, now
from backend.models.auth import Principal
from backend.models.orm import LoginSession, User
from backend.models.projects import (
    Project,
    ProjectItem,
    ProjectMessage,
    ProjectRun,
    SendMessage,
)
from backend.models.review import DocumentReview
from backend.services.job_worker import reconcile_jobs

logger = logging.getLogger("supportflow.recovery")


class FlowGuard:
    def __init__(self, database, cache, agent, settings):
        self.database, self.cache, self.agent, self.settings = database, cache, agent, settings
        self.queue = getattr(agent, "queue_settings", None)
        self.store = ConversationStore(database, cache, settings, self.queue)
        self.cursor = None
        self.task = None

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("recovery_scan_deferred error_type=%s", type(error).__name__)
            await asyncio.sleep(self.settings.scan_seconds)

    async def tick(self):
        await reconcile_jobs(self.database, self.cache, self.settings, self.queue)
        clock = now()
        stale = clock - timedelta(seconds=self.settings.stalled_run_seconds)
        async with self.database.session() as session:
            due_items = (
                select(ProjectItem.id)
                .where(
                    ProjectItem.project_id == Project.id,
                    ProjectItem.status.in_(["submitted", "in_progress", "awaiting_acceptance"]),
                    ProjectItem.workflow_reminded_at.is_(None),
                    or_(
                        ProjectItem.workflow_activity_at.is_(None),
                        ProjectItem.workflow_activity_at
                        <= clock - timedelta(seconds=self.settings.business_remind_seconds),
                    ),
                )
                .exists()
            )
            query = select(Project.id).where(
                Project.deleted_at.is_(None),
                or_(
                    Project.conversation_mode.in_(["human", "pending"]),
                    Project.auto_reply_message_id.is_not(None),
                    select(ProjectRun.id)
                    .where(
                        ProjectRun.project_id == Project.id,
                        ProjectRun.status == "running",
                        ProjectRun.created_at < stale,
                    )
                    .exists(),
                    select(DocumentReview.id)
                    .where(
                        DocumentReview.project_id == Project.id,
                        DocumentReview.status == "running",
                        DocumentReview.created_at < stale,
                    )
                    .exists(),
                    due_items,
                ),
            )
            if self.cursor:
                query = query.where(Project.id > self.cursor)
            ids = list(
                await session.scalars(query.order_by(Project.id).limit(self.settings.batch_size))
            )
        self.cursor = ids[-1] if len(ids) == self.settings.batch_size else None
        for pid in ids:
            try:
                await self.reconcile(pid)
                await self.dispatch(pid)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "project_recovery_deferred project_id=%s error_type=%s",
                    pid,
                    type(error).__name__,
                )

    async def reconcile(self, pid):
        async with self.database.session() as session:
            project = await session.scalar(
                select(Project).where(Project.id == pid).with_for_update()
            )
            if not project or project.deleted_at:
                return
            await self.store._busy(session, project.id)
            await self.store._settle(session, project)
            rows = list(
                await session.scalars(
                    select(ProjectItem)
                    .where(
                        ProjectItem.project_id == pid,
                        ProjectItem.status.in_(["submitted", "in_progress", "awaiting_acceptance"]),
                        ProjectItem.workflow_reminded_at.is_(None),
                    )
                    .order_by(ProjectItem.created_at, ProjectItem.id)
                    .limit(20)
                    .with_for_update()
                )
            )
            for row in rows:
                since = row.workflow_activity_at or row.created_at
                if now() - utc(since) < timedelta(seconds=self.settings.business_remind_seconds):
                    continue
                row.workflow_reminded_at = now()
                append_message(
                    session,
                    project,
                    f"“{row.title}”仍待处理或确认，已提醒负责人和客户。请在对应记录中核对；不会自动确认、发布或关闭。",
                    "system",
                )

    async def dispatch(self, pid):
        # Retain the original sender's login context. A different/new login never inherits a job.
        async with self.database.session() as session:
            project = await session.get(Project, pid)
            if not project or project.deleted_at:
                return
            mid, rid = project.auto_reply_message_id, project.auto_reply_request_id
            mode = project.conversation_mode
            source = (
                await session.get(ProjectMessage, mid)
                if mid
                else await session.scalar(
                    select(ProjectMessage)
                    .where(
                        ProjectMessage.project_id == pid,
                        ProjectMessage.sender_type == "customer",
                        ProjectMessage.sequence > project.facts_cursor,
                    )
                    .order_by(ProjectMessage.sequence.desc())
                    .limit(1)
                )
            )
            if not source or source.project_id != pid or not source.login_session_id:
                principal = None
                content = ""
                version = project.conversation_version
            else:
                login = await session.get(LoginSession, source.login_session_id)
                user = await session.get(User, source.sender_id) if source.sender_id else None
                valid = (
                    login
                    and user
                    and user.is_active
                    and login.user_id == user.id
                    and user.id == project.owner_id
                    and login.revoked_at is None
                    and utc(login.expires_at) > now()
                )
                principal = Principal(account(user), login.id) if valid else None
                content, version = source.content, project.conversation_version
        if not principal:
            if mid:
                await self.park_reply(
                    pid,
                    mid,
                    rid,
                    "resume_required",
                    "本次自动回复未继续执行，客户原话已保留。请重新登录后继续沟通，或联系负责人。",
                    clear=True,
                )
            return
        if mode == "human":
            await self.agent.try_facts(self.store, principal, pid)
            return
        if mode != "ai" or not mid or not rid:
            return
        if not self.agent.configured:
            await self.park_reply(
                pid,
                mid,
                rid,
                "ai_unavailable",
                "AI助手目前也暂不可用，客户原话已保存，待服务恢复后继续处理；您也可以联系负责人。",
                clear=False,
            )
            return
        await self.store.start_run(
            principal,
            pid,
            SendMessage(request_id=rid, content=content),
            conversation_version=version,
            source_message_id=mid,
        )

    async def park_reply(self, pid, mid, rid, reason, message, *, clear):
        async with self.database.session() as session:
            project = await session.scalar(
                select(Project).where(Project.id == pid).with_for_update()
            )
            if (
                not project
                or project.deleted_at
                or project.conversation_mode != "ai"
                or project.auto_reply_message_id != mid
                or project.auto_reply_request_id != rid
            ):
                return
            if project.support_notice != reason:
                project.support_notice = reason
                project.conversation_version += 1
                append_message(session, project, message, "system")
            if clear:
                project.auto_reply_message_id = project.auto_reply_request_id = None
