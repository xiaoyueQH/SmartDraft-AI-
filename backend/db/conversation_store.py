"""Project conversation ownership, human messages and silent fact extraction."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import aliased

from backend.config import RecoverySettings
from backend.db.identity import lock_identity
from backend.db.job_store import enqueue, protected
from backend.db.project_store import ProjectStore, append_message, fingerprint, now, view
from backend.models.consultation import ConsultationState, FactEvidence
from backend.models.discovery import merge_discovery
from backend.models.orm import User
from backend.models.projects import (
    Project,
    ProjectError,
    ProjectItem,
    ProjectMessage,
    ProjectRun,
    human_control_only,
    human_help_requested,
    human_only_requested,
)
from backend.models.review import DocumentReview


class ConversationStore(ProjectStore):
    def __init__(self, database, cache=None, recovery=None, queue=None):
        super().__init__(database, cache, queue)
        self.recovery = recovery or RecoverySettings()

    @staticmethod
    def utc(value):
        return value.replace(tzinfo=value.tzinfo or UTC) if value else None

    async def unanswered(self, session, project):
        return list(
            await session.scalars(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.sender_type == "customer",
                    ProjectMessage.sequence > project.answered_through,
                    ProjectMessage.run_id.is_(None),
                )
                .order_by(ProjectMessage.sequence)
                .limit(100)
            )
        )

    async def deadline(self, session, project):
        waiting = await session.scalar(
            select(func.min(ProjectMessage.created_at)).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.sender_type == "customer",
                ProjectMessage.sequence > project.answered_through,
                ProjectMessage.run_id.is_(None),
            )
        )
        return self.reception_due(project, waiting, now())

    def reception_due(self, project, waiting, clock):
        if project.conversation_mode != "human":
            return None
        if not project.support_id:
            result = self.utc(project.support_wait_since) or clock
            result += timedelta(seconds=self.recovery.support_wait_seconds)
        elif waiting:
            result = max(self.utc(waiting), self.utc(project.support_wait_since) or clock)
            result += timedelta(seconds=self.recovery.support_reply_seconds)
        else:
            result = self.utc(project.support_activity_at) or clock
            result += timedelta(seconds=self.recovery.support_idle_seconds)
        return max(result, self.utc(project.support_extended_until) or result)

    async def switch_ai(self, session, project, message):
        rows = await self.unanswered(session, project)
        # A handover request alone is a control action, not a question for the model.
        candidates = [row for row in rows if not human_control_only(row.content)]
        project.conversation_mode, project.support_id = "ai", None
        project.support_wait_since = project.support_extended_until = None
        project.support_notice = ""
        project.conversation_version += 1
        project.auto_reply_message_id = candidates[-1].id if candidates else None
        project.auto_reply_request_id = uuid4() if candidates else None
        append_message(session, project, message, "system")

    async def requests(self, principal, *, reason="", scope="available", q="", offset=0, limit=50):
        async with self.database.session() as session:
            actor = await lock_identity(session, principal, shared=True)
            if actor.role != "admin":
                raise ProjectError("FORBIDDEN", "此列表仅供客服查看", 403)
            clock, policy = now(), self.recovery
            support = aliased(User)
            unanswered_at = (
                select(func.min(ProjectMessage.created_at))
                .where(
                    ProjectMessage.project_id == Project.id,
                    ProjectMessage.sender_type == "customer",
                    ProjectMessage.sequence > Project.answered_through,
                    ProjectMessage.run_id.is_(None),
                )
                .correlate(Project)
                .scalar_subquery()
            )
            latest_run = (
                select(ProjectRun.status)
                .where(
                    ProjectRun.project_id == Project.id,
                    or_(
                        ProjectRun.options["silent"].as_boolean().is_(None),
                        ProjectRun.options["silent"].as_boolean().is_(False),
                    ),
                )
                .order_by(ProjectRun.created_at.desc(), ProjectRun.id.desc())
                .limit(1)
                .correlate(Project)
                .scalar_subquery()
            )
            newer_review = aliased(DocumentReview)
            review_failure = (
                select(DocumentReview.id)
                .join(ProjectItem, ProjectItem.id == DocumentReview.item_id)
                .where(
                    DocumentReview.project_id == Project.id,
                    DocumentReview.source["version"].as_integer() == ProjectItem.version,
                    DocumentReview.status == "failed",
                    ~select(newer_review.id)
                    .where(
                        newer_review.item_id == DocumentReview.item_id,
                        or_(
                            newer_review.created_at > DocumentReview.created_at,
                            (newer_review.created_at == DocumentReview.created_at)
                            & (newer_review.id > DocumentReview.id),
                        ),
                    )
                    .correlate(DocumentReview)
                    .exists(),
                )
                .correlate(Project)
                .exists()
            )
            source = (
                select(
                    Project.id,
                    Project.title,
                    Project.owner_id,
                    Project.conversation_mode,
                    Project.support_id,
                    Project.support_notice,
                    Project.human_only,
                    Project.support_wait_since,
                    Project.support_activity_at,
                    Project.support_extended_until,
                    Project.extraction_error,
                    Project.created_at,
                    User.display_name.label("customer_name"),
                    User.username.label("customer_username"),
                    support.display_name.label("support_name"),
                    unanswered_at.label("unanswered_at"),
                    latest_run.label("run_status"),
                    case((review_failure, "failed"), else_="").label("review_status"),
                )
                .join(User, User.id == Project.owner_id)
                .outerjoin(support, support.id == Project.support_id)
                .where(Project.deleted_at.is_(None))
                .subquery()
            )
            c = source.c
            human = c.conversation_mode == "human"
            assigned = c.support_id.is_not(None)
            since = case(
                (c.unanswered_at > func.coalesce(c.support_wait_since, clock), c.unanswered_at),
                else_=func.coalesce(c.support_wait_since, clock),
            )
            soon = clock + timedelta(seconds=policy.support_notice_seconds)
            flags = {
                "waiting": c.conversation_mode.in_(["human", "pending"]) & ~assigned,
                "handover": c.conversation_mode == "pending",
                "unanswered": human & assigned & c.unanswered_at.is_not(None),
                "human_only": c.conversation_mode.in_(["human", "pending"]) & c.human_only,
                "failed": or_(
                    c.run_status == "failed",
                    c.review_status == "failed",
                    c.extraction_error != "",
                    c.support_notice.in_(["ai_unavailable", "resume_required"]),
                ),
                "expiring": human
                & ~c.human_only
                & or_(c.support_extended_until.is_(None), c.support_extended_until <= soon)
                & or_(
                    ~assigned
                    & (
                        func.coalesce(c.support_wait_since, clock)
                        <= soon - timedelta(seconds=policy.support_wait_seconds)
                    ),
                    assigned
                    & c.unanswered_at.is_not(None)
                    & (since <= soon - timedelta(seconds=policy.support_reply_seconds)),
                    assigned
                    & c.unanswered_at.is_(None)
                    & (
                        func.coalesce(c.support_activity_at, clock)
                        <= soon - timedelta(seconds=policy.support_idle_seconds)
                    ),
                ),
            }
            conditions = [or_(*flags.values())]
            if scope == "available":
                conditions.append(
                    or_(
                        c.support_id.is_(None),
                        c.support_id == actor.id,
                        c.support_notice.in_(
                            ["human_only_wait", "ai_unavailable", "resume_required"]
                        ),
                    )
                )
            if q:
                conditions.append(
                    or_(
                        c.title.contains(q, autoescape=True),
                        c.customer_name.contains(q, autoescape=True),
                        c.customer_username.contains(q, autoescape=True),
                    )
                )
            counts = (
                (
                    await session.execute(
                        select(
                            func.count().label("total"),
                            *(
                                func.coalesce(func.sum(case((flag, 1), else_=0)), 0).label(name)
                                for name, flag in flags.items()
                            ),
                        )
                        .select_from(source)
                        .where(*conditions)
                    )
                )
                .mappings()
                .one()
            )
            if reason:
                conditions.append(flags[reason])
            rows = (
                (
                    await session.execute(
                        select(source, *(flag.label(name) for name, flag in flags.items()))
                        .where(*conditions)
                        .order_by(
                            case(
                                (flags["failed"], 0),
                                (flags["expiring"], 1),
                                (flags["human_only"], 2),
                                else_=3,
                            ),
                            func.coalesce(c.unanswered_at, c.support_wait_since, c.created_at),
                            c.id,
                        )
                        .offset(offset)
                        .limit(limit + 1)
                    )
                )
                .mappings()
                .all()
            )
            items = []
            for row in rows[:limit]:
                reasons = [name for name in flags if row[name]]
                due = self.reception_due(row, row.unanswered_at, clock)
                failures = []
                if row.run_status == "failed":
                    failures.append("顾问或需求生成未完成")
                if row.review_status == "failed":
                    failures.append("文档检查未完成")
                if row.extraction_error:
                    failures.append("客户信息待整理")
                if row.support_notice == "ai_unavailable":
                    failures.append("AI服务暂不可用")
                if row.support_notice == "resume_required":
                    failures.append("客户需重新登录")
                items.append(
                    {
                        "id": str(row.id),
                        "title": row.title,
                        "owner_id": str(row.owner_id),
                        "customer_name": row.customer_name,
                        "conversation_mode": row.conversation_mode,
                        "support_id": str(row.support_id) if row.support_id else None,
                        "support_name": row.support_name or "",
                        "support_notice": row.support_notice,
                        "human_only": row.human_only,
                        "reasons": reasons,
                        "failure_summary": "；".join(failures),
                        "deadline_at": due.isoformat() if due else None,
                        "wait_since": self.utc(row.support_wait_since).isoformat()
                        if row.support_wait_since
                        else None,
                        "created_at": row.created_at.isoformat(),
                    }
                )
            return {
                "items": items,
                "has_more": len(rows) > limit,
                "counts": dict(counts),
                "checked_at": clock.isoformat(),
            }

    async def customer_history(
        self,
        principal,
        *,
        customer_id=None,
        project_id=None,
        q="",
        sender="",
        cursor="",
        start=None,
        end=None,
        limit=40,
    ):
        async with self.database.session() as session:
            actor = await lock_identity(session, principal, shared=True)
            if actor.role != "admin":
                if customer_id is not None and customer_id != actor.id:
                    raise ProjectError("PROJECT_NOT_FOUND", "记录不存在", 404)
                customer_id = actor.id
            query = (
                select(ProjectMessage, Project.title, Project.owner_id, User.display_name)
                .join(Project, Project.id == ProjectMessage.project_id)
                .join(User, User.id == Project.owner_id)
                .where(Project.deleted_at.is_(None))
            )
            if customer_id:
                query = query.where(Project.owner_id == customer_id)
            if project_id:
                project, _ = await self._project(session, principal, project_id)
                if customer_id and project.owner_id != customer_id:
                    raise ProjectError("PROJECT_NOT_FOUND", "记录不存在", 404)
                query = query.where(Project.id == project_id)
            if q:
                query = query.where(ProjectMessage.content.contains(q, autoescape=True))
            if sender:
                query = query.where(ProjectMessage.sender_type == sender)
            if start:
                query = query.where(ProjectMessage.created_at >= start)
            if end:
                query = query.where(ProjectMessage.created_at < end)
            if cursor:
                try:
                    stamp, sid = cursor.split("|", 1)
                    stamp, sid = datetime.fromisoformat(stamp), UUID(sid)
                    if stamp.tzinfo is None:
                        raise ValueError("timezone required")
                except (ValueError, TypeError):
                    raise ProjectError("INVALID_CURSOR", "记录位置无效，请重新查询", 422) from None
                query = query.where(
                    or_(
                        ProjectMessage.created_at < stamp,
                        (ProjectMessage.created_at == stamp) & (ProjectMessage.id < sid),
                    )
                )
            rows = (
                await session.execute(
                    query.order_by(
                        ProjectMessage.created_at.desc(), ProjectMessage.id.desc()
                    ).limit(limit + 1)
                )
            ).all()
            values = [
                {
                    **view(row),
                    "project_title": title,
                    "customer_id": str(owner),
                    "customer_name": name,
                }
                for row, title, owner, name in rows[:limit]
            ]
            last = rows[limit - 1][0] if len(rows) >= limit else None
            stamp = (
                last.created_at.replace(tzinfo=last.created_at.tzinfo or UTC).isoformat()
                if last
                else ""
            )
            return {
                "items": values,
                "has_more": len(rows) > limit,
                "next_cursor": f"{stamp}|{last.id}" if last else "",
            }

    async def _busy(self, session, project_id):
        cutoff = now() - timedelta(seconds=self.recovery.stalled_run_seconds)
        old_runs = list(
            await session.scalars(
                select(ProjectRun)
                .where(
                    ProjectRun.project_id == project_id,
                    ProjectRun.status == "running",
                    ProjectRun.created_at < cutoff,
                )
                .with_for_update()
            )
        )
        for row in old_runs:
            if await protected(session, "run", row.id):
                continue
            project = await session.get(Project, project_id)
            await self._recover_run(session, project, row)
        old_reviews = list(
            await session.scalars(
                select(DocumentReview)
                .where(
                    DocumentReview.project_id == project_id,
                    DocumentReview.status == "running",
                    DocumentReview.created_at < cutoff,
                )
                .with_for_update()
            )
        )
        for row in old_reviews:
            if await protected(session, "review", row.id):
                continue
            row.status, row.error = "failed", "上次检查已中断，原稿和已完成内容已保留。"
        await session.flush()
        return bool(
            await session.scalar(
                select(ProjectRun.id)
                .where(ProjectRun.project_id == project_id, ProjectRun.status == "running")
                .limit(1)
            )
            or await session.scalar(
                select(DocumentReview.id)
                .where(DocumentReview.project_id == project_id, DocumentReview.status == "running")
                .limit(1)
            )
        )

    async def _settle(self, session, project):
        clock = now()
        if project.support_id:
            support = await session.get(User, project.support_id)
            if not support or not support.is_active or support.role != "admin":
                project.support_id = None
                project.support_wait_since = clock
                project.support_notice = ""
                project.conversation_version += 1
                append_message(
                    session, project, "原接待客服暂不可用，已重新等待人工接入。", "system"
                )
        if project.conversation_mode == "pending" and not await self._busy(session, project.id):
            project.conversation_mode = "human"
            project.support_wait_since = project.support_activity_at = clock
            project.conversation_version += 1
            append_message(session, project, "本轮AI处理已完成，已转入人工接待。", "system")
            return True
        if project.conversation_mode != "human":
            return False
        project.support_wait_since = project.support_wait_since or clock
        project.support_activity_at = project.support_activity_at or clock
        waiting = await self.unanswered(session, project)
        policy = self.recovery
        if not project.support_id:
            due = self.utc(project.support_wait_since) + timedelta(
                seconds=policy.support_wait_seconds
            )
            warning = "当前人工客服繁忙，暂时无人接待。您可以稍后再次联系人工。"
            reason = "unclaimed"
        elif waiting:
            since = max(self.utc(waiting[0].created_at), self.utc(project.support_wait_since))
            due = since + timedelta(seconds=policy.support_reply_seconds)
            warning = "客服暂未回复，请及时处理客户留言；到期将恢复AI接待。"
            reason = "unanswered"
            if (
                clock >= since + timedelta(seconds=policy.support_reply_warn_seconds)
                and not project.support_notice
            ):
                project.support_notice = "reply_warning"
                append_message(session, project, "客户留言尚待人工回复，已提醒接待客服。", "system")
        else:
            due = self.utc(project.support_activity_at) + timedelta(
                seconds=policy.support_idle_seconds
            )
            warning = "本次人工会话长时间没有互动，即将恢复AI接待；如仍需处理，客服可延长接待。"
            reason = "idle"
        if project.support_extended_until:
            due = max(due, self.utc(project.support_extended_until))
        if (
            project.support_id
            and clock >= due - timedelta(seconds=policy.support_notice_seconds)
            and not project.support_notice
        ):
            project.support_notice = "idle_warning"
            append_message(session, project, warning, "system")
        if clock < due or await self._busy(session, project.id):
            return False
        if project.human_only:
            if project.support_notice != "human_only_wait":
                project.support_notice = "human_only_wait"
                append_message(
                    session,
                    project,
                    "人工客服暂时繁忙，已保留您只需人工的请求，等待接入。",
                    "system",
                )
            return False
        notice = (
            "当前人工客服繁忙，暂时无人接待，已由AI助手继续为您服务。您也可以稍后再次联系人工。"
            if reason == "unclaimed"
            else "人工客服暂未回复，已恢复AI接待。您可以稍后再次联系人工。"
            if reason == "unanswered"
            else "本次人工接待已因长时间无互动结束，之后的新消息由AI接待。"
        )
        await self.switch_ai(session, project, notice)
        return True

    async def settle(self, project_id):
        async with self.database.session() as session:
            project = await session.scalar(
                select(Project).where(Project.id == project_id).with_for_update()
            )
            if project:
                await self._settle(session, project)

    async def _state(self, session, project):
        support = await session.get(User, project.support_id) if project.support_id else None
        return {
            "mode": project.conversation_mode,
            "version": project.conversation_version,
            "support_id": str(project.support_id) if project.support_id else None,
            "support_name": support.display_name if support else "",
            "last_sequence": project.message_seq,
            "facts_cursor": project.facts_cursor,
            "extraction_pending": bool(
                await session.scalar(
                    select(ProjectMessage.id)
                    .where(
                        ProjectMessage.project_id == project.id,
                        ProjectMessage.sender_type == "customer",
                        ProjectMessage.sequence > project.facts_cursor,
                    )
                    .limit(1)
                )
            ),
            "extraction_error": project.extraction_error,
            "human_only": project.human_only,
            "support_notice": project.support_notice,
            "wait_since": project.support_wait_since.isoformat()
            if project.support_wait_since
            else None,
            "extended_until": (
                project.support_extended_until.isoformat()
                if project.support_extended_until
                else None
            ),
            "deadline_at": (await self.deadline(session, project)).isoformat()
            if project.conversation_mode == "human"
            else None,
        }

    async def action(self, principal, project_id, data):
        digest = fingerprint(f"conversation:{project_id}", data.model_dump())
        async with self._write_session() as session:
            project, actor = await self._project(session, principal, project_id, write=True)
            replay = await self._replay(session, principal, data.request_id, digest)
            if replay:
                return replay
            await self._settle(session, project)
            if project.conversation_version != data.expected_version:
                raise ProjectError("VERSION_CONFLICT", "接待状态已更新，请重新查看", 409)
            owner = actor.id == project.owner_id
            admin = actor.role == "admin"
            if data.action == "request_human":
                if not owner:
                    raise ProjectError("FORBIDDEN", "请由项目客户发起人工求助", 403)
                if project.conversation_mode == "ai":
                    project.auto_reply_message_id = project.auto_reply_request_id = None
                    project.support_wait_since = project.support_activity_at = now()
                    project.support_notice = ""
                    project.conversation_mode = (
                        "pending" if await self._busy(session, project.id) else "human"
                    )
                    project.conversation_version += 1
                    append_message(
                        session,
                        project,
                        "客户已联系人工；正在执行的AI会先完成本轮处理。"
                        if project.conversation_mode == "pending"
                        else "客户已联系人工，等待客服接入。",
                        "system",
                    )
            elif data.action == "wait_human" and owner:
                if project.conversation_mode == "ai":
                    raise ProjectError("INVALID_STATE", "请先联系人工", 409)
                project.human_only = True
                project.auto_reply_message_id = project.auto_reply_request_id = None
                project.conversation_version += 1
                append_message(session, project, "客户选择只等人工，不自动恢复AI。", "system")
            elif data.action == "resume_ai" and owner:
                if await self._busy(session, project.id):
                    raise ProjectError("PROJECT_BUSY", "本轮操作完成后即可恢复AI", 409)
                project.human_only = False
                await self.switch_ai(session, project, "客户已选择恢复AI接待。")
            elif not admin:
                raise ProjectError("FORBIDDEN", "接管操作需要客服权限", 403)
            elif data.action == "takeover":
                if project.support_id and project.support_id != actor.id:
                    assigned = await session.get(User, project.support_id)
                    if assigned and assigned.is_active and assigned.role == "admin":
                        raise ProjectError(
                            "SUPPORT_ASSIGNED", "已有客服接待，请由当前客服转交", 409
                        )
                project.support_id = actor.id
                project.support_wait_since = project.support_activity_at = now()
                project.support_extended_until = None
                project.support_notice = ""
                project.auto_reply_message_id = project.auto_reply_request_id = None
                project.conversation_mode = (
                    "pending" if await self._busy(session, project.id) else "human"
                )
                project.conversation_version += 1
                append_message(
                    session,
                    project,
                    f"客服{actor.display_name}已接受会话"
                    + ("，本轮AI完成后接入。" if project.conversation_mode == "pending" else "。"),
                    "system",
                    sender=actor,
                )
            elif data.action == "release":
                if project.support_id != actor.id:
                    raise ProjectError("FORBIDDEN", "请由当前接待客服交还AI", 403)
                if await self._busy(session, project.id):
                    raise ProjectError("PROJECT_BUSY", "本轮处理尚未结束，请完成后交还AI", 409)
                if project.human_only:
                    raise ProjectError(
                        "HUMAN_ONLY", "客户明确只需人工，请转交客服或由客户恢复AI", 409
                    )
                await self.switch_ai(session, project, "客服已交还AI，之后的新消息由AI接待。")
            elif data.action == "extend":
                if project.support_id != actor.id or project.conversation_mode != "human":
                    raise ProjectError("FORBIDDEN", "只有当前接待客服可以延长处理", 403)
                if (
                    project.support_extended_until
                    and self.utc(project.support_extended_until) > now()
                ):
                    raise ProjectError("EXTENSION_ACTIVE", "本次延长仍有效，请及时向客户回复", 409)
                due = await self.deadline(session, project)
                project.support_extended_until = max(now(), due) + timedelta(
                    seconds=self.recovery.support_extend_seconds
                )
                project.conversation_version += 1
                append_message(
                    session,
                    project,
                    "客服仍在处理您的问题，已延长本次接待等待时间。",
                    "system",
                    sender=actor,
                )
            elif data.action == "transfer":
                if project.support_id != actor.id:
                    raise ProjectError("FORBIDDEN", "请由当前接待客服转交", 403)
                target = await session.get(User, data.support_id) if data.support_id else None
                if not target or not target.is_active or target.role != "admin":
                    raise ProjectError("INVALID_SUPPORT", "请选择有效客服", 422)
                project.support_id = target.id
                project.support_wait_since = project.support_activity_at = now()
                project.support_extended_until = None
                project.support_notice = ""
                project.conversation_version += 1
                append_message(
                    session,
                    project,
                    f"客服{actor.display_name}已将会话转交给{target.display_name}。",
                    "system",
                    sender=actor,
                )
            elif data.action == "retry_extraction":
                project.extraction_started_at, project.extraction_error = None, ""
            await session.flush()
            return self._record(
                session,
                principal,
                project_id,
                data.request_id,
                digest,
                f"conversation_{data.action}",
                {"conversation": await self._state(session, project)},
            )

    async def delivery(self, principal, project_id, data):
        """Resolve durable receipts before checking current mode or model availability."""
        digest = fingerprint(f"chat:{project_id}", data.model_dump())
        async with self.database.session() as session:
            project, actor = await self._project(session, principal, project_id, write=True)
            run = await session.scalar(
                select(ProjectRun).where(
                    ProjectRun.actor_id == actor.id, ProjectRun.request_id == data.request_id
                )
            )
            if run:
                options = data.model_dump(exclude={"content", "request_id"})
                if (
                    run.project_id != project_id
                    or run.prompt != data.content
                    or run.options != options
                ):
                    raise ProjectError("REQUEST_REUSED", "请求编号已用于其他内容", 409)
                return {"receipt": self.run_view(run)}
            replay = await self._replay(session, principal, data.request_id, digest)
            if replay:
                return {"receipt": replay}
            await self._settle(session, project)
            is_owner = actor.id == project.owner_id
            if not is_owner and (actor.role != "admin" or project.support_id != actor.id):
                raise ProjectError("SUPPORT_REQUIRED", "请先接管会话，再直接回复客户", 409)
            if not is_owner and project.conversation_mode == "pending":
                raise ProjectError("SUPPORT_PENDING", "正在等待本轮AI完成，结束后即可回复客户", 409)
            chat = (
                human_help_requested(data.content)
                or human_only_requested(data.content)
                or project.conversation_mode != "ai"
            )
            if (
                is_owner
                and data.intent == "draft"
                and project.conversation_mode != "pending"
                and not (human_help_requested(data.content) or human_only_requested(data.content))
            ):
                chat = False
            return {"chat": chat, "version": project.conversation_version}

    async def send_chat(self, principal, project_id, data):
        digest = fingerprint(f"chat:{project_id}", data.model_dump())
        async with self._write_session() as session:
            project, actor = await self._project(session, principal, project_id, write=True)
            replay = await self._replay(session, principal, data.request_id, digest)
            if replay:
                return replay
            await self._settle(session, project)
            owner = actor.id == project.owner_id
            if not owner and (
                actor.role != "admin"
                or project.support_id != actor.id
                or project.conversation_mode != "human"
            ):
                raise ProjectError("SUPPORT_REQUIRED", "请等待接管生效后再回复客户", 409)
            requested = owner and (
                human_help_requested(data.content) or human_only_requested(data.content)
            )
            if owner and human_only_requested(data.content):
                project.human_only = True
            if project.conversation_mode == "ai" and not requested:
                raise ProjectError("CHAT_MODE_CHANGED", "接待状态已变化，请重新发送", 409)
            message = append_message(
                session,
                project,
                data.content,
                "customer" if owner else "support",
                sender=actor,
                login_session_id=principal.session_id,
            )
            project.support_activity_at = now()
            if not owner:
                project.answered_through = message.sequence
                project.support_notice = ""
                project.support_extended_until = None
            if requested and project.conversation_mode == "ai":
                project.auto_reply_message_id = project.auto_reply_request_id = None
                project.support_wait_since = now()
                project.support_notice = ""
                project.conversation_mode = (
                    "pending" if await self._busy(session, project.id) else "human"
                )
                project.conversation_version += 1
                append_message(
                    session,
                    project,
                    "已联系人工，AI会先完成正在执行的本轮处理。"
                    if project.conversation_mode == "pending"
                    else "已联系人工，等待客服接入。",
                    "system",
                )
            if owner:
                project.extraction_error = ""
                project.consultation = {**(project.consultation or {}), "memory_pending": True}
            await session.flush()
            result = {
                "id": str(message.id),
                "status": "delivered",
                "delivery": "chat",
                "message": view(message),
                "conversation": await self._state(session, project),
            }
            return self._record(
                session, principal, project_id, data.request_id, digest, "chat_message", result
            )

    async def snapshot(self, principal, project_id):
        async with self.database.session() as session:
            project, _ = await self._project(session, principal, project_id, write=True)
            await self._settle(session, project)
            state = await self._state(session, project)
            rows = list(
                await session.scalars(
                    select(ProjectMessage)
                    .where(ProjectMessage.project_id == project_id)
                    .order_by(
                        ProjectMessage.sequence.desc().nullslast(),
                        ProjectMessage.created_at.desc(),
                        ProjectMessage.id,
                    )
                    .limit(40)
                )
            )
            run = await session.scalar(
                select(ProjectRun)
                .where(
                    ProjectRun.project_id == project_id,
                    or_(
                        ProjectRun.options["silent"].as_boolean().is_(None),
                        ProjectRun.options["silent"].as_boolean().is_(False),
                    ),
                )
                .order_by(ProjectRun.created_at.desc())
                .limit(1)
            )
            return {
                "conversation": state,
                "project_version": project.version,
                "discovery": project.discovery,
                "memory_pending": (project.consultation or {}).get("memory_pending", False),
                "messages": [view(row) for row in reversed(rows)],
                "run": self.run_view(run) if run else None,
            }

    async def history(
        self, principal, project_id, *, before=None, after=None, q="", sender="", limit=40
    ):
        async with self.database.session() as session:
            await self._project(session, principal, project_id)
            query = select(ProjectMessage).where(ProjectMessage.project_id == project_id)
            if before is not None:
                query = query.where(ProjectMessage.sequence < before)
            if after is not None:
                query = query.where(ProjectMessage.sequence > after)
            if q:
                query = query.where(ProjectMessage.content.contains(q, autoescape=True))
            if sender:
                query = query.where(ProjectMessage.sender_type == sender)
            rows = list(
                await session.scalars(
                    query.order_by(
                        ProjectMessage.sequence.desc().nullslast(),
                        ProjectMessage.created_at.desc(),
                        ProjectMessage.id,
                    ).limit(limit + 1)
                )
            )
            return {
                "items": [view(row) for row in rows[:limit]],
                "has_more": len(rows) > limit,
                "next_before": rows[limit - 1].sequence if len(rows) >= limit else None,
            }

    async def context(self, principal, project_id, message_id):
        async with self.database.session() as session:
            await self._project(session, principal, project_id)
            target = await session.scalar(
                select(ProjectMessage).where(
                    ProjectMessage.id == message_id, ProjectMessage.project_id == project_id
                )
            )
            if not target:
                raise ProjectError("MESSAGE_NOT_FOUND", "消息不存在", 404)
            before = list(
                await session.scalars(
                    select(ProjectMessage)
                    .where(
                        ProjectMessage.project_id == project_id,
                        ProjectMessage.sequence < target.sequence,
                    )
                    .order_by(ProjectMessage.sequence.desc())
                    .limit(10)
                )
            )
            after = list(
                await session.scalars(
                    select(ProjectMessage)
                    .where(
                        ProjectMessage.project_id == project_id,
                        ProjectMessage.sequence >= target.sequence,
                    )
                    .order_by(ProjectMessage.sequence)
                    .limit(11)
                )
            )
            return {
                "message_id": str(message_id),
                "items": [view(row) for row in [*reversed(before), *after]],
            }

    async def claim_facts(self, principal, project_id):
        async with self.database.session() as session:
            project, _ = await self._project(session, principal, project_id, write=True)
            await self._settle(session, project)
            if project.conversation_mode != "human" or await self._busy(session, project.id):
                return None
            if project.extraction_error:
                # A failed record needs new input or an explicit retry, not another paid poll loop.
                return None
            started = project.extraction_started_at
            if started:
                started = started.replace(tzinfo=UTC) if started.tzinfo is None else started
                if now() - started < timedelta(seconds=30):
                    return None
            rows = list(
                await session.scalars(
                    select(ProjectMessage)
                    .where(
                        ProjectMessage.project_id == project_id,
                        ProjectMessage.sender_type == "customer",
                        ProjectMessage.sequence > project.facts_cursor,
                    )
                    .order_by(ProjectMessage.sequence)
                    .limit(8)
                )
            )
            if not rows:
                return None
            selected, size = [], 0
            for row in rows:
                if selected and size + len(row.content) > 10000:
                    break
                selected.append(row)
                size += len(row.content)
            rows = selected
            project.extraction_until, project.extraction_started_at = rows[-1].sequence, now()
            rows = [row for row in rows if not human_control_only(row.content)]
            if not rows:
                project.facts_cursor = project.extraction_until
                project.extraction_started_at = None
                pending = bool(
                    await session.scalar(
                        select(ProjectMessage.id)
                        .where(
                            ProjectMessage.project_id == project_id,
                            ProjectMessage.sender_type == "customer",
                            ProjectMessage.sequence > project.facts_cursor,
                        )
                        .limit(1)
                    )
                )
                project.consultation = {**(project.consultation or {}), "memory_pending": pending}
                return None
            run = ProjectRun(
                id=uuid4(),
                project_id=project.id,
                actor_id=principal.account.id,
                request_id=uuid4(),
                prompt="",
                options={
                    "silent": True,
                    "fact_version": project.version,
                    "fact_through": project.extraction_until,
                },
                status="running",
                events=[],
                answer="",
                created_at=now(),
            )
            session.add(run)
            await session.flush()
            await enqueue(session, principal, project_id, run.id, settings=self.queue)
            return {
                "run_id": str(run.id),
                "version": project.version,
                "through": project.extraction_until,
                "messages": [view(row) for row in rows],
                "project": view(project),
            }

    async def load_facts(self, principal, project_id, run):
        async with self.database.session() as session:
            project, _ = await self._project(session, principal, project_id)
            if project.version != run.options["fact_version"]:
                raise ProjectError("VERSION_CONFLICT", "业务信息已变化，请重新整理", 409)
            rows = list(
                await session.scalars(
                    select(ProjectMessage)
                    .where(
                        ProjectMessage.project_id == project_id,
                        ProjectMessage.sender_type == "customer",
                        ProjectMessage.sequence > project.facts_cursor,
                        ProjectMessage.sequence <= run.options["fact_through"],
                    )
                    .order_by(ProjectMessage.sequence)
                    .limit(8)
                )
            )
            return {
                "run_id": str(run.id),
                "version": project.version,
                "through": run.options["fact_through"],
                "messages": [view(row) for row in rows],
                "project": view(project),
            }

    async def finish_facts(self, principal, project_id, job, patch, sources):
        async with self._write_session() as session:
            project, _ = await self._project(session, principal, project_id, write=True)
            run = await session.scalar(
                select(ProjectRun)
                .where(ProjectRun.id == UUID(job["run_id"]), ProjectRun.project_id == project_id)
                .with_for_update()
            )
            if not run or run.status != "running" or not run.options.get("silent"):
                raise ProjectError("RUN_EXPIRED", "信息整理任务已结束", 409)
            if project.version != job["version"]:
                raise ProjectError(
                    "VERSION_CONFLICT", "业务信息已变化，将根据最新内容重新整理", 409
                )
            project.discovery = merge_discovery(project.discovery, patch)
            if project.phase == "discovery":
                if "subject" in patch and not await self._has_custom_title(session, project.id):
                    project.title = project.discovery.get("subject") or project.title
                project.brief = project.discovery.get("business_background") or project.brief
                if "technology_preference" in patch or "technology_preference" in patch.get(
                    "remove_facts", {}
                ):
                    project.stack = project.discovery["technology_preference"]
            state = ConsultationState.model_validate(project.consultation or {})
            removed = patch.get("remove_facts", {})
            state.sources = [
                s for s in [*state.sources, *sources] if s.value not in removed.get(s.field, [])
            ][-100:]
            state.memory_pending = bool(
                await session.scalar(
                    select(ProjectMessage.id)
                    .where(
                        ProjectMessage.project_id == project_id,
                        ProjectMessage.sender_type == "customer",
                        ProjectMessage.sequence > job["through"],
                    )
                    .limit(1)
                )
            )
            project.consultation = state.model_dump(mode="json")
            for source in sources:
                payload = source.model_dump(mode="json")
                key = fingerprint(f"fact_source:{project_id}", payload)
                if await session.get(FactEvidence, key) is None:
                    session.add(
                        FactEvidence(
                            id=key,
                            project_id=project_id,
                            recorded_run_id=run.id,
                            source=payload,
                            created_at=now(),
                        )
                    )
            project.facts_cursor = max(project.facts_cursor, job["through"])
            project.extraction_error = ""
            project.extraction_started_at = None
            project.version += 1
            run.status, run.answer = "completed", "客户信息已在后台整理。"
            run.cache_version += 1

    async def fail_facts(self, project_id, run_id):
        async with self.database.session() as session:
            project = await session.scalar(
                select(Project).where(Project.id == project_id).with_for_update()
            )
            run = await session.scalar(
                select(ProjectRun)
                .where(ProjectRun.id == run_id, ProjectRun.project_id == project_id)
                .with_for_update()
            )
            if run and run.status == "running":
                run.status, run.answer = "failed", "后台信息整理暂未完成，聊天记录已保存。"
                run.cache_version += 1
            if project:
                project.extraction_error = "部分信息尚待整理，聊天不受影响。"
                project.extraction_started_at = now()
                project.consultation = {**(project.consultation or {}), "memory_pending": True}
