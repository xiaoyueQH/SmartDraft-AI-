from datetime import UTC, timedelta
from uuid import UUID, uuid4

from sqlalchemy import literal, or_, select, union_all

from backend.db.job_store import enqueue, protected
from backend.db.project_store import ProjectStore, fingerprint, now, remember_revision, view
from backend.models.documents import content_hash
from backend.models.projects import Project, ProjectError, ProjectItem, ProjectMessage, ProjectRun
from backend.models.review import DocumentReview, DocumentRevision


class ReviewStore(ProjectStore):
    @staticmethod
    def source_query(project_id):
        messages = select(
            literal("message").label("kind"),
            ProjectMessage.id.label("source_id"),
            ProjectMessage.content.label("content"),
            ProjectMessage.created_at.label("created_at"),
        ).where(
            ProjectMessage.project_id == project_id,
            ProjectMessage.role == "user",
            ProjectMessage.sender_type.in_(["customer", "legacy"]),
            ProjectMessage.run_id.is_(None),
        )
        runs = select(
            literal("run").label("kind"),
            ProjectRun.id.label("source_id"),
            ProjectRun.prompt.label("content"),
            ProjectRun.created_at.label("created_at"),
        ).where(
            ProjectRun.project_id == project_id,
            ProjectRun.actor_id
            == select(Project.owner_id).where(Project.id == project_id).scalar_subquery(),
            or_(
                ProjectRun.options["silent"].as_boolean().is_(None),
                ProjectRun.options["silent"].as_boolean().is_(False),
            ),
        )
        return union_all(messages, runs).subquery()

    async def read_sources(
        self, principal, project_id, item_id, review_id, *, ids=(), query="", offset=0
    ):
        """Read only sources pinned by this review, with ownership and hash checks."""
        async with self.database.session() as session:
            await self._document(session, principal, project_id, item_id)
            review = await self._review(session, project_id, item_id, review_id)
            manifest = review.source.get("source_manifest", [])
            allowed = {entry["id"]: entry for entry in manifest}
            if ids and (len(ids) > 4 or not set(ids) <= allowed.keys()):
                raise ValueError("请选择本次检查来源目录中的真实编号，每次最多4条。")
            table = self.source_query(project_id)
            target_ids = list(ids) if ids else list(allowed)
            if not target_ids:
                return {"sources": [], "has_more": False}
            selected_ids = [UUID(sid.split(":", 1)[1]) for sid in target_ids]
            statement = select(table).where(table.c.source_id.in_(selected_ids))
            if query:
                statement = statement.where(table.c.content.contains(query, autoescape=True))
            rows = (
                (
                    await session.execute(
                        statement.order_by(table.c.created_at, table.c.source_id)
                        .offset(offset)
                        .limit(5)
                    )
                )
                .mappings()
                .all()
            )
            sources, size = [], 0
            for row in rows[:4]:
                sid = f"{row['kind']}:{row['source_id']}"
                if sid not in allowed or content_hash(row["content"]) != allowed[sid]["sha256"]:
                    raise ProjectError(
                        "SOURCE_CHANGED", "客户原话已变化，请重新检查，原稿保持不变", 409
                    )
                if size + len(row["content"]) > 20000:
                    break
                sources.append(
                    {"id": sid, "content": row["content"], "sha256": allowed[sid]["sha256"]}
                )
                size += len(row["content"])
            return {
                "sources": sources,
                "has_more": len(rows) > len(sources),
                "next_offset": offset + len(sources),
            }

    async def _document(self, session, principal, project_id, item_id, write=False):
        project, actor = await self._project(session, principal, project_id, write=write)
        query = select(ProjectItem).where(
            ProjectItem.id == item_id, ProjectItem.project_id == project_id
        )
        row = await session.scalar(query.with_for_update() if write else query)
        if not row:
            raise ProjectError("ITEM_NOT_FOUND", "资料不存在", 404)
        if row.kind not in {"specification", "change"}:
            raise ProjectError("INVALID_KIND", "仅需求书和变更支持文档检查", 422)
        return project, actor, row

    async def _review(self, session, project_id, item_id, review_id, write=False):
        query = select(DocumentReview).where(
            DocumentReview.id == review_id,
            DocumentReview.project_id == project_id,
            DocumentReview.item_id == item_id,
        )
        row = await session.scalar(query.with_for_update() if write else query)
        if row is None:
            raise ProjectError("REVIEW_NOT_FOUND", "检查记录不存在", 404)
        return row

    @staticmethod
    def review_view(row, internal=False):
        report = dict(row.report)
        if report and not report.get("findings"):
            report["summary"] = (
                "本次检查未能形成有效结论，不能据此判断草案没有问题；原稿保持不变。"
                if report.get("partial")
                else "本次未提交有依据的修改建议，正文未改变；这不代表草案已经通过工程验证。"
            )
            if report.get("partial") and not report.get("stop_reasons"):
                report["stop_reasons"] = [
                    "检查建议未通过依据核对，尚未形成可应用的修改。"
                    if any(
                        event.get("type") == "tool" and event.get("ok") is False
                        for event in row.events
                    )
                    else "本次记录没有留下完整检查结论，请根据最新草案重新检查。"
                ]
        result = {
            "id": str(row.id),
            "project_id": str(row.project_id),
            "item_id": str(row.item_id),
            "status": row.status,
            "source_version": row.source["version"],
            "source_content": row.source["content"],
            "candidate": row.candidate,
            "report": report,
            "events": row.events,
            "error": row.error,
            "created_at": row.created_at.isoformat(),
        }
        if internal:
            result.update(source=row.source, blocks=row.blocks)
        return result

    async def start(self, principal, project_id, item_id, data):
        digest = fingerprint(f"review:{project_id}:{item_id}", data.model_dump())
        async with self._write_session() as session:
            project, actor, document = await self._document(
                session, principal, project_id, item_id, True
            )
            previous = await session.scalar(
                select(DocumentReview).where(
                    DocumentReview.actor_id == actor.id,
                    DocumentReview.request_id == data.request_id,
                )
            )
            if previous:
                if previous.fingerprint != digest:
                    raise ProjectError("REQUEST_REUSED", "请求编号已用于其他检查")
                return self.review_view(previous), False
            if project.conversation_mode == "pending":
                raise ProjectError(
                    "SUPPORT_PENDING", "正在交接人工，请等待当前处理完成后再检查", 409
                )
            if document.version != data.expected_version:
                raise ProjectError("VERSION_CONFLICT", "正文已更新，请重新打开后检查")
            active = await session.scalar(
                select(DocumentReview)
                .where(DocumentReview.item_id == item_id, DocumentReview.status == "running")
                .with_for_update()
            )
            if active:
                started = (
                    active.created_at.replace(tzinfo=UTC)
                    if active.created_at.tzinfo is None
                    else active.created_at
                )
                if now() - started < timedelta(seconds=180) or await protected(
                    session, "review", active.id
                ):
                    raise ProjectError("REVIEW_BUSY", "这份文档正在检查，请等待结果")
                active.status, active.error = "failed", "上次检查已中断，原稿未改变。"
                await session.flush()
            previous = await session.scalar(
                select(DocumentReview)
                .where(DocumentReview.item_id == item_id)
                .order_by(DocumentReview.created_at.desc())
                .limit(1)
            )
            table = self.source_query(project_id)
            cursor = await session.stream(
                select(table)
                .order_by(table.c.created_at.desc(), table.c.source_id.desc())
                .limit(2001)
            )
            manifest, quotes, initial_ids, size, overflow = [], [], [], 0, False
            async for row in cursor.mappings():
                if len(manifest) == 2000:
                    overflow = True
                    break
                sid = f"{row['kind']}:{row['source_id']}"
                manifest.append(
                    {
                        "id": sid,
                        "sha256": content_hash(row["content"]),
                        "characters": len(row["content"]),
                        "preview": row["content"][:100],
                    }
                )
                if size + len(row["content"]) <= 6000:
                    quotes.append(row["content"])
                    initial_ids.append(sid)
                    size += len(row["content"])
            await cursor.close()
            source = {
                "version": document.version,
                "project_version": project.version,
                "content": document.content,
                "hash": content_hash(document.content),
                "title": document.title,
                "questions": document.questions,
                "facts": project.discovery,
                "client_quotes": list(reversed(quotes)),
                "source_manifest": list(reversed(manifest)),
                "initial_source_ids": list(reversed(initial_ids)),
                "source_overflow": overflow,
                "assumptions": (project.consultation or {}).get("draft_assumptions", []),
            }
            index = await self._document_index(session, document)
            if index.status != "ready":
                raise ProjectError("DOCUMENT_INDEX_LIMIT", "文档结构较多，请分阶段检查", 503)
            blocks = index.read_blocks(document.content)
            row = DocumentReview(
                id=uuid4(),
                project_id=project_id,
                item_id=item_id,
                actor_id=actor.id,
                request_id=data.request_id,
                fingerprint=digest,
                status="running",
                source=source,
                blocks=blocks,
                report={},
                candidate="",
                events=[],
                error="",
                created_at=now(),
            )
            session.add(row)
            await session.flush()
            await enqueue(
                session,
                principal,
                project_id,
                row.id,
                kind="review",
                item_id=item_id,
                settings=self.queue,
            )
            row.events = [{"type": "queue", "summary": "检查请求已保存，等待处理", "sequence": 1}]
            return self.review_view(row), True

    async def load(self, principal, project_id, item_id, review_id, internal=False):
        async with self.database.session() as session:
            await self._document(session, principal, project_id, item_id)
            row = await self._review(session, project_id, item_id, review_id, True)
            await self.recover_stale(session, row)
            return self.review_view(row, internal)

    async def validate_execution(self, principal, project_id, item_id, review_id):
        """Do not spend model calls on a source edited while its job was waiting."""
        async with self.database.session() as session:
            project, _, document = await self._document(session, principal, project_id, item_id)
            row = await self._review(session, project_id, item_id, review_id)
            if row.status != "running":
                raise ProjectError("REVIEW_STATE_CHANGED", "检查状态已变化，请查看最新结果", 409)
            if (
                document.version != row.source["version"]
                or project.version != row.source["project_version"]
                or content_hash(document.content) != row.source["hash"]
            ):
                raise ProjectError(
                    "VERSION_CONFLICT",
                    "等待期间资料已更新，请按最新内容重新检查，原稿保持不变。",
                    409,
                )

    @staticmethod
    async def recover_stale(session, row):
        if await protected(session, "review", row.id):
            return
        started = (
            row.created_at.replace(tzinfo=UTC) if row.created_at.tzinfo is None else row.created_at
        )
        if row.status == "running" and now() - started > timedelta(seconds=180):
            row.status, row.error = "failed", "检查已中断，原稿保持不变，可以重新检查。"

    async def latest(self, principal, project_id, item_id):
        async with self.database.session() as session:
            await self._document(session, principal, project_id, item_id)
            row = await session.scalar(
                select(DocumentReview)
                .where(DocumentReview.item_id == item_id)
                .order_by(DocumentReview.created_at.desc())
                .limit(1)
                .with_for_update()
            )
            if row:
                await self.recover_stale(session, row)
            return self.review_view(row) if row else None

    async def event(self, review_id, summary, **extra):
        async with self.database.session() as session:
            row = await session.scalar(
                select(DocumentReview).where(DocumentReview.id == review_id).with_for_update()
            )
            if row and row.status == "running":
                row.events = [
                    *row.events,
                    {"sequence": len(row.events) + 1, "summary": summary, **extra},
                ][-80:]

    async def save_blocks(self, review_id, blocks):
        async with self.database.session() as session:
            row = await session.scalar(
                select(DocumentReview).where(DocumentReview.id == review_id).with_for_update()
            )
            if not row or row.status != "running":
                raise ProjectError("REVIEW_STATE_CHANGED", "检查状态已变化")
            row.blocks = blocks

    async def result(self, principal, project_id, item_id, review_id, candidate, report):
        async with self.database.session() as session:
            await self._document(session, principal, project_id, item_id)
            row = await self._review(session, project_id, item_id, review_id, True)
            if row.status != "running":
                raise ProjectError("REVIEW_STATE_CHANGED", "检查状态已变化")
            row.candidate, row.report = candidate, report

    async def ready(self, review_id):
        async with self.database.session() as session:
            row = await session.scalar(
                select(DocumentReview).where(DocumentReview.id == review_id).with_for_update()
            )
            if row and row.status == "running":
                row.status = "ready"

    async def fail_review(self, review_id, message):
        async with self.database.session() as session:
            row = await session.scalar(
                select(DocumentReview).where(DocumentReview.id == review_id).with_for_update()
            )
            if row and row.status == "running":
                row.status, row.error = "failed", message[:500]

    async def replay_decision(self, principal, project_id, item_id, review_id, data):
        digest = fingerprint(
            f"review_decision:{project_id}:{item_id}:{review_id}", data.model_dump()
        )
        async with self.database.session() as session:
            await self._document(session, principal, project_id, item_id)
            return await self._replay(session, principal, data.request_id, digest)

    async def apply(self, principal, project_id, item_id, review_id, data):
        digest = fingerprint(
            f"review_decision:{project_id}:{item_id}:{review_id}", data.model_dump()
        )
        async with self._write_session() as session:
            project, actor, document = await self._document(
                session, principal, project_id, item_id, True
            )
            previous = await self._replay(session, principal, data.request_id, digest)
            if previous:
                return previous
            review = await self._review(session, project_id, item_id, review_id, True)
            if review.status != "ready":
                raise ProjectError("REVIEW_STATE_CHANGED", "检查结果已处理或尚未就绪")
            if data.decision == "discard":
                review.status = "discarded"
            else:
                if document.status not in {"draft", "submitted"}:
                    raise ProjectError("IMMUTABLE_ITEM", "已发布内容不可覆盖，请新建需求变更")
                if (
                    document.version != data.expected_version
                    or document.version != review.source["version"]
                    or project.version != review.source["project_version"]
                    or content_hash(document.content) != review.source["hash"]
                ):
                    raise ProjectError(
                        "VERSION_CONFLICT", "正文或客户要求已变化，候选稿已保留，请重新检查"
                    )
                if not review.report.get("verified") or not review.candidate:
                    raise ProjectError("REVIEW_NOT_VERIFIED", "候选稿尚未通过复核，原稿保持不变")
                if await session.scalar(
                    select(ProjectRun.id)
                    .where(ProjectRun.project_id == project_id, ProjectRun.status == "running")
                    .limit(1)
                ):
                    raise ProjectError("PROJECT_BUSY", "顾问正在处理本项目，请稍后应用修改")
                await remember_revision(session, document, actor.id, "before_review", review.blocks)
                await self._clear_document_change(session, document.id)
                document.content = review.candidate
                document.questions = list(
                    dict.fromkeys([*document.questions, *review.report.get("questions", [])])
                )
                document.status, document.customer_confirmed, document.staff_confirmed = (
                    "draft",
                    False,
                    False,
                )
                document.base_revision = project.revision
                document.version += 1
                project.version += 1
                review.status = "applied"
                await self._document_index(session, document)
            await session.flush()
            return self._record(
                session,
                principal,
                project_id,
                data.request_id,
                digest,
                f"review_{data.decision}:{item_id}",
                {"item": view(document), "review": self.review_view(review)},
            )

    async def history(self, principal, project_id, item_id):
        async with self.database.session() as session:
            await self._document(session, principal, project_id, item_id)
            rows = list(
                await session.scalars(
                    select(DocumentRevision)
                    .where(DocumentRevision.item_id == item_id)
                    .order_by(DocumentRevision.version.desc())
                    .limit(30)
                )
            )
            return [view(row) for row in rows]

    async def restore(self, principal, project_id, item_id, revision_id, data):
        digest = fingerprint(
            f"restore_revision:{project_id}:{item_id}:{revision_id}", data.model_dump()
        )
        async with self._write_session() as session:
            project, actor, document = await self._document(
                session, principal, project_id, item_id, True
            )
            previous = await self._replay(session, principal, data.request_id, digest)
            if previous:
                return previous
            revision = await session.scalar(
                select(DocumentRevision).where(
                    DocumentRevision.id == revision_id,
                    DocumentRevision.item_id == item_id,
                    DocumentRevision.project_id == project_id,
                )
            )
            if revision is None:
                raise ProjectError("REVISION_NOT_FOUND", "历史内容不存在", 404)
            if document.status not in {"draft", "submitted"}:
                raise ProjectError("IMMUTABLE_ITEM", "正式版本不可覆盖，请新建需求变更")
            if document.version != data.expected_version:
                raise ProjectError("VERSION_CONFLICT", "正文已更新，请重新核对后恢复")
            index = await self._document_index(session, document)
            blocks = index.read_blocks(document.content) if index.status == "ready" else None
            await remember_revision(session, document, actor.id, "before_restore", blocks)
            await self._clear_document_change(session, document.id)
            document.title, document.content, document.questions = (
                revision.title,
                revision.content,
                revision.questions,
            )
            document.status, document.customer_confirmed, document.staff_confirmed = (
                "draft",
                False,
                False,
            )
            document.base_revision = project.revision
            document.version += 1
            project.version += 1
            await self._document_index(session, document)
            await session.flush()
            return self._record(
                session,
                principal,
                project_id,
                data.request_id,
                digest,
                f"restore_revision:{item_id}",
                {"item": view(document)},
            )
