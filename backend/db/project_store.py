import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select

from backend.db.identity import lock_identity
from backend.db.session import Database
from backend.models.auth import Principal
from backend.models.consultation import ConsultationState
from backend.models.discovery import merge_discovery
from backend.models.orm import User
from backend.models.projects import (
    CreateItem,
    DraftItem,
    ItemAction,
    ManageRecords,
    Project,
    ProjectError,
    ProjectItem,
    ProjectMessage,
    ProjectOperation,
    ProjectRecordEvent,
    ProjectRun,
    SendMessage,
    StartConsultation,
)


def now():
    return datetime.now(UTC)


def view(row):
    return {
        column.name: (
            str(value)
            if isinstance(value, UUID)
            else value.isoformat()
            if isinstance(value, datetime)
            else value
        )
        for column in row.__table__.columns
        for value in [getattr(row, column.name)]
    }


def fingerprint(action, data):
    return hashlib.sha256(
        json.dumps([action, data], sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


class ProjectStore:
    def __init__(self, database: Database, cache=None):
        self.database = database
        self.cache = cache

    @asynccontextmanager
    async def _write_session(self):
        async with self.database.session() as session:
            yield session
        if self.cache:
            await self.cache.invalidate_summary()

    async def _has_custom_title(self, session, project_id):
        return (
            await session.scalar(
                select(ProjectRecordEvent.id)
                .where(
                    ProjectRecordEvent.project_id == project_id,
                    ProjectRecordEvent.action == "rename",
                )
                .limit(1)
            )
            is not None
        )

    async def draft_history(self, principal, project_id, expected_version, max_chars):
        async with self.database.session() as session:
            project, _ = await self._project(session, principal, project_id)
            if project.version != expected_version:
                raise ProjectError("VERSION_CONFLICT", "沟通内容已更新，请根据最新信息整理", 409)
            count, size = (
                await session.execute(
                    select(
                        func.count(),
                        func.coalesce(func.sum(func.length(ProjectMessage.content)), 0),
                    ).where(ProjectMessage.project_id == project_id)
                )
            ).one()
            if count >= 2000 or size > max_chars:
                raise ProjectError(
                    "DRAFT_HISTORY_LIMIT",
                    "历史资料较多，请联系负责人分阶段整理；原有资料已保留",
                    503,
                )
            rows = list(
                await session.scalars(
                    select(ProjectMessage)
                    .where(ProjectMessage.project_id == project_id)
                    .order_by(ProjectMessage.created_at, ProjectMessage.id)
                )
            )
            return [
                {
                    "id": str(row.id),
                    "role": row.role,
                    "content": row.content,
                    "run_id": str(row.run_id) if row.run_id else "",
                }
                for row in rows
            ]

    async def _project(self, session, principal, project_id, *, write=False):
        user = await lock_identity(session, principal, shared=not write)
        query = select(Project).where(Project.id == project_id, Project.deleted_at.is_(None))
        if user.role != "admin":
            query = query.where(Project.owner_id == user.id)
        if write:
            query = query.with_for_update()
        project = await session.scalar(query)
        if not project:
            raise ProjectError("PROJECT_NOT_FOUND", "项目不存在", 404)
        return project, user

    async def _replay(self, session, principal, request_id, digest):
        operation = await session.scalar(
            select(ProjectOperation).where(
                ProjectOperation.actor_id == principal.account.id,
                ProjectOperation.request_id == request_id,
            )
        )
        if operation:
            if operation.fingerprint != digest:
                raise ProjectError("REQUEST_REUSED", "请求编号已用于其他内容，请刷新后重新操作")
            return operation.result
        return None

    def _record(self, session, principal, project_id, request_id, digest, action, result):
        session.add(
            ProjectOperation(
                actor_id=principal.account.id,
                project_id=project_id,
                request_id=request_id,
                fingerprint=digest,
                action=action,
                result=result,
                created_at=now(),
            )
        )
        return result

    async def projects(self, principal, offset=0, limit=20, *, deleted=False, query=""):
        async with self.database.session() as session:
            user = await lock_identity(session, principal, shared=True)
            statement = select(Project).where(
                Project.deleted_at.is_not(None) if deleted else Project.deleted_at.is_(None)
            )
            if user.role != "admin":
                statement = statement.where(Project.owner_id == user.id)
            if query:
                statement = statement.where(Project.title.contains(query, autoescape=True))
            rows = list(
                await session.scalars(
                    statement.order_by(Project.created_at.desc(), Project.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            return {"items": [view(row) for row in rows[:limit]], "has_more": len(rows) > limit}

    async def manage_records(self, principal, data: ManageRecords):
        targets = {target.id: target.expected_version for target in data.targets}
        if len(targets) != len(data.targets):
            raise ProjectError("DUPLICATE_TARGET", "同一批次不能重复选择记录", 422)
        if data.action == "rename" and (len(targets) != 1 or not data.title):
            raise ProjectError("TITLE_REQUIRED", "重命名需要选择一条记录并填写名称", 422)
        digest = fingerprint("manage_records", data.model_dump())
        async with self._write_session() as session:
            actor = await lock_identity(session, principal)
            statement = (
                select(Project)
                .where(Project.id.in_(targets))
                .order_by(Project.id)
                .with_for_update()
            )
            if actor.role != "admin":
                statement = statement.where(Project.owner_id == actor.id)
            rows = list(await session.scalars(statement))
            if len(rows) != len(targets):
                raise ProjectError("PROJECT_NOT_FOUND", "部分记录不存在或不可管理，请刷新列表", 404)
            if actor.role != "admin" and any(row.phase == "project" for row in rows):
                raise ProjectError(
                    "FORBIDDEN", "正式项目由负责人管理，客户可管理自己的沟通记录", 403
                )
            replay = await self._replay(session, principal, data.request_id, digest)
            if replay:
                return replay
            for row in rows:
                if row.version != targets[row.id]:
                    raise ProjectError("VERSION_CONFLICT", "记录内容已更新，请刷新列表后重新确认")
                if (row.deleted_at is not None) != (data.action == "restore"):
                    raise ProjectError("INVALID_STATE", "记录状态已变化，请刷新列表")
            runs = list(
                await session.scalars(
                    select(ProjectRun)
                    .where(ProjectRun.project_id.in_(targets), ProjectRun.status == "running")
                    .order_by(ProjectRun.id)
                    .with_for_update()
                )
            )
            for run in runs:
                started = (
                    run.created_at.replace(tzinfo=UTC)
                    if run.created_at.tzinfo is None
                    else run.created_at
                )
                if now() - started <= timedelta(seconds=180):
                    raise ProjectError("PROJECT_BUSY", "选中记录有顾问正在处理，请等待完成后再管理")
                run.status, run.answer = "failed", "上次执行已中断，请恢复记录后重新发送。"
            for row in rows:
                before = {
                    "title": row.title,
                    "deleted": row.deleted_at is not None,
                    "version": row.version,
                }
                if data.action == "rename":
                    row.title = data.title
                elif data.action == "trash":
                    row.deleted_at, row.deleted_by = now(), actor.id
                else:
                    row.deleted_at, row.deleted_by = None, None
                row.version += 1
                session.add(
                    ProjectRecordEvent(
                        project_id=row.id,
                        actor_id=actor.id,
                        request_id=data.request_id,
                        action=data.action,
                        snapshot={
                            "before": before,
                            "after": {
                                "title": row.title,
                                "deleted": row.deleted_at is not None,
                                "version": row.version,
                            },
                        },
                        created_at=now(),
                    )
                )
            await session.flush()
            result = {"action": data.action, "items": [view(row) for row in rows]}
            return self._record(
                session,
                principal,
                rows[0].id,
                data.request_id,
                digest,
                "records:" + data.action,
                result,
            )

    async def start_consultation(self, principal: Principal, data: StartConsultation):
        digest = fingerprint("start_consultation", data.model_dump())
        async with self._write_session() as session:
            actor = await lock_identity(session, principal)
            previous = await self._replay(session, principal, data.request_id, digest)
            if previous:
                return previous
            owner_id = data.owner_id or actor.id
            if owner_id != actor.id and actor.role != "admin":
                raise ProjectError("FORBIDDEN", "不能为其他客户创建项目", 403)
            owner = await session.get(User, owner_id)
            if not owner or not owner.is_active or owner.role == "admin":
                raise ProjectError("CUSTOMER_REQUIRED", "请选择有效的客户账号，管理员负责评审", 422)
            project = Project(
                id=uuid4(),
                owner_id=owner_id,
                title="需求沟通",
                brief="",
                stack="待确认",
                revision=0,
                phase="discovery",
                discovery={},
                created_at=now(),
            )
            session.add(project)
            await session.flush()
            return self._record(
                session,
                principal,
                project.id,
                data.request_id,
                digest,
                "start_consultation",
                view(project),
            )

    async def detail(self, principal, project_id):
        async with self.database.session() as session:
            project, _ = await self._project(session, principal, project_id)
            items = list(
                await session.scalars(
                    select(ProjectItem)
                    .where(ProjectItem.project_id == project_id)
                    .order_by(ProjectItem.created_at.desc(), ProjectItem.id)
                    .limit(41)
                )
            )
            messages = list(
                await session.scalars(
                    select(ProjectMessage)
                    .where(ProjectMessage.project_id == project_id)
                    .order_by(ProjectMessage.created_at.desc(), ProjectMessage.id)
                    .limit(40)
                )
            )
            runs = list(
                await session.scalars(
                    select(ProjectRun)
                    .where(
                        ProjectRun.project_id == project_id,
                        or_(
                            ProjectRun.id.in_([row.run_id for row in messages if row.run_id]),
                            ProjectRun.id.in_(
                                select(ProjectRun.id)
                                .where(ProjectRun.project_id == project_id)
                                .order_by(ProjectRun.created_at.desc(), ProjectRun.id)
                                .limit(5)
                                .correlate(None)
                            ),
                        ),
                    )
                    .order_by(ProjectRun.created_at.desc(), ProjectRun.id)
                )
            )
            baseline = (
                await session.scalar(
                    select(ProjectItem).where(
                        ProjectItem.project_id == project_id,
                        ProjectItem.published_revision == project.revision,
                    )
                )
                if project.revision
                else None
            )
            return {
                "project": view(project),
                "items": [view(row) for row in items[:40]],
                "has_more_items": len(items) > 40,
                "messages": [view(row) for row in reversed(messages)],
                "runs": [self.run_view(row) for row in runs],
                "baseline": view(baseline) if baseline else None,
            }

    async def item(self, principal, project_id, item_id):
        async with self.database.session() as session:
            await self._project(session, principal, project_id)
            row = await session.scalar(
                select(ProjectItem).where(
                    ProjectItem.id == item_id, ProjectItem.project_id == project_id
                )
            )
            if not row:
                raise ProjectError("ITEM_NOT_FOUND", "资料不存在", 404)
            return view(row)

    async def items(self, principal, project_id, offset, limit):
        async with self.database.session() as session:
            await self._project(session, principal, project_id)
            rows = list(
                await session.scalars(
                    select(ProjectItem)
                    .where(ProjectItem.project_id == project_id)
                    .order_by(ProjectItem.created_at.desc(), ProjectItem.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            return {"items": [view(row) for row in rows[:limit]], "has_more": len(rows) > limit}

    async def comparison(self, principal, project_id, item_id, expected_version):
        async with self.database.session() as session:
            await self._project(session, principal, project_id)
            row = await session.scalar(
                select(ProjectItem).where(
                    ProjectItem.id == item_id, ProjectItem.project_id == project_id
                )
            )
            if not row:
                raise ProjectError("ITEM_NOT_FOUND", "资料不存在", 404)
            if row.version != expected_version:
                raise ProjectError("VERSION_CONFLICT", "资料已更新，请重新打开后比较")
            if row.kind not in {"specification", "change"}:
                raise ProjectError("INVALID_KIND", "仅需求书和变更支持基线对比", 422)
            baseline = (
                await session.scalar(
                    select(ProjectItem).where(
                        ProjectItem.project_id == project_id,
                        ProjectItem.published_revision == row.base_revision,
                    )
                )
                if row.base_revision
                else None
            )
            return {
                "before": baseline.content if baseline else "",
                "after": row.content,
                "base_revision": row.base_revision,
                "published_revision": row.published_revision,
            }

    async def create_item(self, principal, project_id, data: CreateItem):
        digest = fingerprint(f"create_item:{project_id}", data.model_dump())
        async with self._write_session() as session:
            project, actor = await self._project(session, principal, project_id, write=True)
            previous = await self._replay(session, principal, data.request_id, digest)
            if previous:
                return previous
            if data.base_revision != project.revision:
                raise ProjectError("BASELINE_CHANGED", "需求基线已变化，请重新核对后提交")
            if data.kind in {"task", "acceptance"} and actor.role != "admin":
                raise ProjectError("FORBIDDEN", "任务和交付记录由负责人创建", 403)
            if data.kind != "specification" and project.revision == 0:
                raise ProjectError("BASELINE_REQUIRED", "请先完成需求书的双方确认")
            row = ProjectItem(
                id=uuid4(),
                project_id=project_id,
                created_by=actor.id,
                created_at=now(),
                base_revision=project.revision,
                **DraftItem.model_validate(
                    data.model_dump(include=set(DraftItem.model_fields))
                ).model_dump(),
            )
            session.add(row)
            project.version += 1
            await session.flush()
            return self._record(
                session, principal, project_id, data.request_id, digest, "create_item", view(row)
            )

    async def act(self, principal, project_id, item_id, data: ItemAction):
        digest = fingerprint(f"item:{project_id}:{item_id}", data.model_dump())
        async with self._write_session() as session:
            project, actor = await self._project(session, principal, project_id, write=True)
            previous = await self._replay(session, principal, data.request_id, digest)
            if previous:
                return previous
            row = await session.scalar(
                select(ProjectItem)
                .where(ProjectItem.id == item_id, ProjectItem.project_id == project_id)
                .with_for_update()
            )
            if not row:
                raise ProjectError("ITEM_NOT_FOUND", "资料不存在", 404)
            if row.version != data.expected_version:
                raise ProjectError("VERSION_CONFLICT", "内容已更新，请刷新后重试")
            admin = actor.role == "admin"
            customer = actor.id == project.owner_id
            document = row.kind in {"specification", "change"}
            if (
                row.kind in {"task", "acceptance"}
                and not admin
                and data.action not in {"accept", "reopen", "supplement"}
            ):
                raise ProjectError("FORBIDDEN", "内部任务与交付记录由负责人维护", 403)
            if data.action == "supplement":
                if document or row.status in {"closed", "rejected", "withdrawn"}:
                    raise ProjectError(
                        "INVALID_STATE", "当前记录不能补充说明，请另行创建需求或工单"
                    )
                if not data.note:
                    raise ProjectError("NOTE_REQUIRED", "请填写补充说明", 422)
            elif data.action == "edit":
                if row.status not in {"draft", "submitted"}:
                    raise ProjectError(
                        "IMMUTABLE_ITEM", "已确认或处理中的记录不可覆盖，请创建新变更"
                    )
                if not document and row.created_by != actor.id and not admin:
                    raise ProjectError("FORBIDDEN", "不能编辑他人提交的记录", 403)
                if not data.title or not data.content:
                    raise ProjectError("CONTENT_REQUIRED", "请填写标题和正文", 422)
                row.title, row.content, row.questions = data.title, data.content, data.questions
                row.status, row.customer_confirmed, row.staff_confirmed = "draft", False, False
                row.base_revision = project.revision
            elif data.action == "submit":
                if row.status != "draft":
                    raise ProjectError("INVALID_STATE", "只有草稿可以提交")
                if document and row.base_revision != project.revision:
                    raise ProjectError("BASELINE_CHANGED", "需求基线已变化，请先编辑并重新核对内容")
                row.status = "submitted"
            elif data.action == "confirm":
                if not document or row.status != "submitted":
                    raise ProjectError("INVALID_STATE", "请先提交需求书或变更草稿")
                if row.questions:
                    raise ProjectError("QUESTIONS_OPEN", "仍有待确认问题，请补齐后重新提交")
                if row.base_revision != project.revision:
                    raise ProjectError("BASELINE_CHANGED", "基线已更新，请重新评审这份内容")
                if admin:
                    row.staff_confirmed = True
                elif customer:
                    row.customer_confirmed = True
                else:
                    raise ProjectError("FORBIDDEN", "需要客户或负责人确认", 403)
                if row.staff_confirmed and row.customer_confirmed:
                    project.revision += 1
                    row.published_revision, row.status = project.revision, "approved"
                    if project.phase == "discovery":
                        project.phase = "project"
                        if not await self._has_custom_title(session, project.id):
                            project.title = row.title[:120]
            elif data.action in {"reject", "withdraw"}:
                if row.status not in {"draft", "submitted"}:
                    raise ProjectError("INVALID_STATE", "当前状态不能驳回或撤回")
                if (data.action == "reject" and not admin) or (
                    data.action == "withdraw" and row.created_by != actor.id
                ):
                    raise ProjectError("FORBIDDEN", "当前账号不能执行此操作", 403)
                if not data.note:
                    raise ProjectError("NOTE_REQUIRED", "请填写处理原因", 422)
                row.status = "rejected" if data.action == "reject" else "withdrawn"
                row.resolution = data.note
            elif data.action in {"start", "deliver"}:
                if not admin:
                    raise ProjectError("FORBIDDEN", "需要负责人处理", 403)
                if document or row.status != (
                    "submitted" if data.action == "start" else "in_progress"
                ):
                    raise ProjectError("INVALID_STATE", "当前状态不能执行此操作")
                if data.action == "deliver" and not data.note:
                    raise ProjectError("NOTE_REQUIRED", "请填写处理结果、验证依据或交付说明", 422)
                row.status = "in_progress" if data.action == "start" else "awaiting_acceptance"
                if data.action == "deliver":
                    row.resolution = data.note
            elif data.action in {"accept", "reopen"}:
                if not customer:
                    raise ProjectError("FORBIDDEN", "由项目客户确认验收结果", 403)
                if document or row.status != "awaiting_acceptance":
                    raise ProjectError("INVALID_STATE", "当前记录尚未提交验收")
                if not data.note:
                    raise ProjectError("NOTE_REQUIRED", "请填写验收结论", 422)
                row.status = "closed" if data.action == "accept" else "in_progress"
                row.acceptance_note = data.note
            row.version += 1
            project.version += 1
            await session.flush()
            return self._record(
                session,
                principal,
                project_id,
                data.request_id,
                digest,
                f"{data.action}:{item_id}",
                {"item": view(row), "revision": project.revision, "note": data.note},
            )

    @staticmethod
    def run_view(run):
        return {
            "id": str(run.id),
            "status": run.status,
            "events": run.events,
            "answer": run.answer,
            "prompt": run.prompt,
            "options": run.options,
            "created_at": run.created_at.isoformat(),
        }

    async def run(self, principal, project_id, run_id):
        if self.cache and self.cache.settings.enabled:
            async with self.database.session() as session:
                actor = await lock_identity(session, principal, shared=True)
                project_query = select(Project.id).where(
                    Project.id == project_id, Project.deleted_at.is_(None)
                )
                if actor.role != "admin":
                    project_query = project_query.where(Project.owner_id == actor.id)
                if await session.scalar(project_query) is None:
                    raise ProjectError("PROJECT_NOT_FOUND", "项目不存在", 404)
                state = (
                    await session.execute(
                        select(
                            ProjectRun.cache_version, ProjectRun.status, ProjectRun.created_at
                        ).where(ProjectRun.id == run_id, ProjectRun.project_id == project_id)
                    )
                ).one_or_none()
                if state is None:
                    raise ProjectError("RUN_NOT_FOUND", "执行记录不存在", 404)
            started = (
                state.created_at.replace(tzinfo=UTC)
                if state.created_at.tzinfo is None
                else state.created_at
            )
            if state.status != "running" or now() - started <= timedelta(seconds=180):
                identity = f"{actor.id}:{project_id}:{run_id}:{state.cache_version}"
                return await self.cache.get_or_load(
                    "run",
                    identity,
                    lambda: self._run(principal, project_id, run_id),
                    self.cache.settings.run_ttl_seconds,
                )
        return await self._run(principal, project_id, run_id)

    async def _run(self, principal, project_id, run_id):
        async with self.database.session() as session:
            await self._project(session, principal, project_id)
            row = await session.scalar(
                select(ProjectRun)
                .where(ProjectRun.id == run_id, ProjectRun.project_id == project_id)
                .with_for_update()
            )
            if not row:
                raise ProjectError("RUN_NOT_FOUND", "执行记录不存在", 404)
            # Recover an abandoned execution without silently replaying business operations.
            started = (
                row.created_at.replace(tzinfo=UTC)
                if row.created_at.tzinfo is None
                else row.created_at
            )
            if row.status == "running" and now() - started > timedelta(seconds=180):
                row.cache_version += 1
                if row.answer:
                    await self._interrupt_discussion(session, row)
                    row.status = "completed"
                else:
                    row.status, row.answer = "failed", "执行中断，请重新发送；正式需求未被修改。"
                    await self._save_failed_discussion(session, row)
            return self.run_view(row)

    async def start_run(self, principal, project_id, data: SendMessage):
        async with self.database.session() as session:
            project, _ = await self._project(session, principal, project_id, write=True)
            previous = await session.scalar(
                select(ProjectRun).where(
                    ProjectRun.actor_id == principal.account.id,
                    ProjectRun.request_id == data.request_id,
                )
            )
            if previous:
                options = data.model_dump(exclude={"request_id", "content"})
                previous_options = SendMessage(
                    request_id=data.request_id, content=data.content
                ).model_dump(exclude={"request_id", "content"}) | (previous.options or {})
                if (
                    previous.project_id != project_id
                    or previous.prompt != data.content
                    or previous_options != options
                ):
                    raise ProjectError("REQUEST_REUSED", "请求编号已用于其他内容")
                return self.run_view(previous), False
            active = await session.scalar(
                select(ProjectRun).where(
                    ProjectRun.project_id == project_id, ProjectRun.status == "running"
                )
            )
            if active:
                started = (
                    active.created_at.replace(tzinfo=UTC)
                    if active.created_at.tzinfo is None
                    else active.created_at
                )
                if now() - started <= timedelta(seconds=180):
                    raise ProjectError(
                        "PROJECT_BUSY", "项目已有 Agent 正在处理，请等待当前结果", 409
                    )
                if active.answer:
                    await self._interrupt_discussion(session, active)
                    active.status = "completed"
                else:
                    active.status, active.answer = "failed", "上次执行中断，请重新发送。"
                    await self._save_failed_discussion(session, active)
                active.cache_version += 1
                await session.flush()
            run = ProjectRun(
                id=uuid4(),
                project_id=project.id,
                actor_id=principal.account.id,
                request_id=data.request_id,
                prompt=data.content,
                options=data.model_dump(exclude={"request_id", "content"}),
                status="running",
                events=[],
                answer="",
                created_at=now(),
            )
            session.add(run)
            await session.flush()
            return self.run_view(run), True

    async def event(self, run_id, event):
        async with self.database.session() as session:
            run = await session.scalar(
                select(ProjectRun).where(ProjectRun.id == run_id).with_for_update()
            )
            if run and run.status == "running":
                run.events = [*run.events, {**event, "sequence": len(run.events) + 1}][:120]
                run.cache_version += 1

    async def finish(
        self,
        principal,
        project_id,
        run_id,
        revision,
        answer,
        drafts,
        notes=None,
        *,
        consultation=None,
        expected_version=None,
        defer_completion=False,
        discussion_only=False,
        continue_discussion=False,
    ):
        async with self._write_session() as session:
            project, actor = await self._project(session, principal, project_id, write=True)
            run = await session.scalar(
                select(ProjectRun).where(ProjectRun.id == run_id).with_for_update()
            )
            if (
                discussion_only
                and run
                and run.answer
                and project.consultation.get("discussion_run_id") == str(run_id)
            ):
                if (
                    run.project_id != project_id
                    or run.actor_id != actor.id
                    or expected_version is None
                    or project.version != expected_version + 1
                ):
                    raise ProjectError("VERSION_CONFLICT", "沟通保存后资料已变化，请刷新后继续")
                return project.version
            if (
                run
                and consultation
                and project.consultation.get("last_run_id") == str(run_id)
                and run.answer == answer
            ):
                return project.version
            if (
                not run
                or run.project_id != project_id
                or run.actor_id != actor.id
                or run.status != "running"
            ):
                raise ProjectError("RUN_EXPIRED", "本次执行已终止")
            if project.revision != revision:
                raise ProjectError("BASELINE_CHANGED", "处理期间需求基线变化，请根据新版本重新发送")
            if expected_version is not None and project.version != expected_version:
                raise ProjectError(
                    "VERSION_CONFLICT", "沟通期间资料发生变化，请读取新资料后重新发送"
                )
            if continue_discussion and project.consultation.get("discussion_run_id") != str(run_id):
                raise ProjectError("RUN_EXPIRED", "本轮沟通保存记录不匹配，请刷新后继续")
            if consultation is not None:
                project.consultation = ConsultationState.model_validate(consultation).model_dump(
                    mode="json"
                )
            if notes:
                for patch in notes:
                    project.discovery = merge_discovery(project.discovery, patch)
                if project.phase == "discovery":
                    if any(
                        "subject" in patch for patch in notes
                    ) and not await self._has_custom_title(session, project.id):
                        project.title = project.discovery.get("subject") or project.title
                    project.brief = project.discovery.get("business_background") or project.brief
                    if any("technology_preference" in patch for patch in notes):
                        project.stack = project.discovery["technology_preference"]
            for draft in drafts:
                data = DraftItem.model_validate(draft)
                if data.kind in {"task", "acceptance"} and actor.role != "admin":
                    raise ProjectError(
                        "FORBIDDEN", "客户不能通过 Agent 创建内部任务或交付记录", 403
                    )
                if data.kind != "specification" and not project.revision:
                    raise ProjectError("BASELINE_REQUIRED", "请先确认初版需求书，再处理变更与售后")
                session.add(
                    ProjectItem(
                        id=uuid4(),
                        project_id=project_id,
                        created_by=actor.id,
                        created_at=now(),
                        base_revision=revision,
                        **data.model_dump(),
                    )
                )
            if continue_discussion:
                assistant = await session.scalar(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project_id,
                        ProjectMessage.run_id == run_id,
                        ProjectMessage.role == "assistant",
                    )
                )
                if assistant is None:
                    raise ProjectError("RUN_EXPIRED", "本轮沟通记录不存在，请刷新后继续")
                assistant.content = answer
            else:
                session.add_all(
                    [
                        ProjectMessage(
                            project_id=project_id,
                            run_id=run_id,
                            role="user",
                            content=run.prompt,
                            created_at=now(),
                        ),
                        ProjectMessage(
                            project_id=project_id,
                            run_id=run_id,
                            role="assistant",
                            content=answer,
                            created_at=now() + timedelta(microseconds=1),
                        ),
                    ]
                )
            run.status, run.answer = "running" if defer_completion else "completed", answer
            run.cache_version += 1
            project.version += 1
            run.events = [
                *run.events,
                {
                    "sequence": len(run.events) + 1,
                    "type": "saved",
                    "summary": ("对话与沟通纪要已保存" if notes else "对话已保存")
                    + (f"，新增 {len(drafts)} 份待确认草稿" if drafts else ""),
                },
            ]
            return project.version

    async def _save_failed_discussion(self, session, run):
        project = await session.get(Project, run.project_id)
        if not project or project.phase != "discovery" or project.deleted_at is not None:
            return
        if await session.scalar(
            select(ProjectMessage.id).where(ProjectMessage.run_id == run.id).limit(1)
        ):
            return
        session.add_all(
            [
                ProjectMessage(
                    project_id=run.project_id,
                    run_id=run.id,
                    role="user",
                    content=run.prompt,
                    created_at=run.created_at,
                ),
                ProjectMessage(
                    project_id=run.project_id,
                    run_id=run.id,
                    role="assistant",
                    content=run.answer,
                    created_at=run.created_at + timedelta(microseconds=1),
                ),
            ]
        )

    async def _interrupt_discussion(self, session, run):
        state = await session.scalar(
            select(Project.consultation).where(Project.id == run.project_id)
        )
        if (state or {}).get("discussion_run_id") != str(run.id) or state.get("last_run_id") == str(
            run.id
        ):
            return
        assistant = await session.scalar(
            select(ProjectMessage).where(
                ProjectMessage.project_id == run.project_id,
                ProjectMessage.run_id == run.id,
                ProjectMessage.role == "assistant",
            )
        )
        if assistant:
            run.answer = assistant.content = (
                run.answer.rsplit("\n\n", 1)[0]
                + "\n\n"
                + (
                    "本次沟通和已核实的信息已保存，草案整理中断，已有草案保留不变。"
                    "您可以继续沟通或重新整理，无需重复提供这些信息。"
                )
            )

    async def fail(self, run_id, message):
        async with self.database.session() as session:
            row = await session.scalar(
                select(ProjectRun).where(ProjectRun.id == run_id).with_for_update()
            )
            if row and row.status == "running":
                row.cache_version += 1
                if row.answer:
                    await self._interrupt_discussion(session, row)
                    row.status = "completed"
                    row.events = [
                        *row.events,
                        {
                            "sequence": len(row.events) + 1,
                            "type": "node",
                            "summary": "业务结果已保存，断点恢复将在下轮重新核对",
                        },
                    ]
                    return
                row.status, row.answer = "failed", message
                await self._save_failed_discussion(session, row)

    async def checkpoint_ready(self, run_id):
        async with self.database.session() as session:
            row = await session.scalar(
                select(ProjectRun).where(ProjectRun.id == run_id).with_for_update()
            )
            if row and row.status == "running" and row.answer:
                row.status = "completed"
                row.cache_version += 1

    async def activity(self, principal, project_id, item_id=None):
        async with self.database.session() as session:
            await self._project(session, principal, project_id)
            statement = select(ProjectOperation).where(
                ProjectOperation.project_id == project_id,
                ~ProjectOperation.action.startswith("records:"),
            )
            if item_id is not None:
                if not await session.scalar(
                    select(ProjectItem.id).where(
                        ProjectItem.id == item_id, ProjectItem.project_id == project_id
                    )
                ):
                    raise ProjectError("ITEM_NOT_FOUND", "资料不存在", 404)
                statement = statement.where(
                    or_(
                        ProjectOperation.action.endswith(f":{item_id}"),
                        and_(
                            ProjectOperation.action == "create_item",
                            ProjectOperation.result["id"].as_string() == str(item_id),
                        ),
                    )
                )
            rows = await session.scalars(
                statement.order_by(ProjectOperation.created_at.desc(), ProjectOperation.id).limit(
                    60
                )
            )
            result = [
                {
                    "id": str(row.id),
                    "actor_id": str(row.actor_id),
                    "action": row.action,
                    "note": row.result.get("note", ""),
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]
            if item_id is not None:
                return result
            events = await session.scalars(
                select(ProjectRecordEvent)
                .where(ProjectRecordEvent.project_id == project_id)
                .order_by(ProjectRecordEvent.created_at.desc(), ProjectRecordEvent.id)
                .limit(60)
            )
            result += [
                {
                    "id": str(event.id),
                    "actor_id": str(event.actor_id),
                    "action": "record:" + event.action,
                    "note": event.snapshot["after"]["title"],
                    "created_at": event.created_at.isoformat(),
                }
                for event in events
            ]
            return sorted(
                result, key=lambda event: (event["created_at"], event["id"]), reverse=True
            )[:60]
