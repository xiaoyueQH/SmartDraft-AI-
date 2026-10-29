from datetime import UTC, datetime, timedelta

from langgraph.checkpoint.base import WRITES_IDX_MAP, BaseCheckpointSaver, CheckpointTuple
from sqlalchemy import String, cast, delete, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.models.consultation import GraphCheckpoint, GraphWrite


class DatabaseCheckpointer(BaseCheckpointSaver):
    async def cleanup_terminal(self, *, limit=25, success_days=1, failure_days=7):
        """Bounded housekeeping, never delete a waiting or currently executing thread."""
        from backend.models.projects import ProjectOperation, ProjectRun
        from backend.models.review import DocumentReview

        cutoff = datetime.now(UTC) - timedelta(days=success_days)
        failure_cutoff = datetime.now(UTC) - timedelta(days=failure_days)
        async with self.database.session() as session:
            runs = list(
                await session.scalars(
                    select(ProjectRun.id)
                    .where(
                        ProjectRun.project_id == self.project_id,
                        or_(
                            (ProjectRun.status == "completed") & (ProjectRun.created_at < cutoff),
                            (ProjectRun.status == "failed")
                            & (ProjectRun.created_at < failure_cutoff),
                        ),
                        select(GraphCheckpoint.checkpoint_id)
                        .where(
                            GraphCheckpoint.project_id == self.project_id,
                            func.replace(GraphCheckpoint.thread_id, "-", "")
                            == literal("draft:")
                            + func.replace(cast(ProjectRun.id, String), "-", ""),
                        )
                        .exists(),
                    )
                    .order_by(ProjectRun.created_at)
                    .limit(limit)
                )
            )
            reviews = list(
                await session.scalars(
                    select(DocumentReview.id)
                    .where(
                        DocumentReview.project_id == self.project_id,
                        ~select(ProjectOperation.id)
                        .where(
                            ProjectOperation.project_id == self.project_id,
                            ProjectOperation.created_at >= cutoff,
                            func.replace(
                                ProjectOperation.result["review"]["id"].as_string(), "-", ""
                            )
                            == func.replace(cast(DocumentReview.id, String), "-", ""),
                        )
                        .exists(),
                        or_(
                            DocumentReview.status.in_(["applied", "discarded"])
                            & (DocumentReview.created_at < cutoff),
                            (DocumentReview.status == "failed")
                            & (DocumentReview.created_at < failure_cutoff),
                        ),
                        select(GraphCheckpoint.checkpoint_id)
                        .where(
                            GraphCheckpoint.project_id == self.project_id,
                            func.replace(GraphCheckpoint.thread_id, "-", "")
                            == literal("review:")
                            + func.replace(cast(DocumentReview.id, String), "-", ""),
                        )
                        .exists(),
                    )
                    .order_by(DocumentReview.created_at)
                    .limit(limit)
                )
            )
            threads = [*(f"draft:{rid}" for rid in runs), *(f"review:{rid}" for rid in reviews)]
            if threads:
                for model in (GraphWrite, GraphCheckpoint):
                    await session.execute(
                        delete(model).where(
                            model.project_id == self.project_id, model.thread_id.in_(threads)
                        )
                    )
            return len(threads)

    """Project-scoped LangGraph persistence on the existing asynchronous session pool."""

    def __init__(self, database, project_id):
        super().__init__()
        self.database, self.project_id = database, project_id
        self.insert = pg_insert if database.engine.dialect.name == "postgresql" else sqlite_insert

    def scope(self, model, config):
        values = config["configurable"]
        return (
            model.project_id == self.project_id,
            model.thread_id == values["thread_id"],
            model.namespace == values.get("checkpoint_ns", ""),
        )

    async def aget_tuple(self, config):
        async with self.database.session() as session:
            statement = select(GraphCheckpoint).where(*self.scope(GraphCheckpoint, config))
            checkpoint_id = config["configurable"].get("checkpoint_id")
            if checkpoint_id:
                statement = statement.where(GraphCheckpoint.checkpoint_id == checkpoint_id)
            row = await session.scalar(
                statement.order_by(GraphCheckpoint.checkpoint_id.desc()).limit(1)
            )
            if not row:
                return None
            writes = list(
                await session.scalars(
                    select(GraphWrite)
                    .where(
                        *self.scope(GraphWrite, config),
                        GraphWrite.checkpoint_id == row.checkpoint_id,
                    )
                    .order_by(GraphWrite.task_id, GraphWrite.position)
                )
            )

            def pointer(checkpoint):
                return {
                    "configurable": {
                        "thread_id": row.thread_id,
                        "checkpoint_ns": row.namespace,
                        "checkpoint_id": checkpoint,
                    }
                }

            return CheckpointTuple(
                config=pointer(row.checkpoint_id),
                checkpoint=self.serde.loads_typed((row.encoding, row.payload)),
                metadata=self.serde.loads_typed((row.metadata_encoding, row.metadata_payload)),
                parent_config=pointer(row.parent_id) if row.parent_id else None,
                pending_writes=[
                    (
                        write.task_id,
                        write.channel,
                        self.serde.loads_typed((write.encoding, write.payload)),
                    )
                    for write in writes
                ],
            )

    async def aput(self, config, checkpoint, metadata, new_versions):
        values = config["configurable"]
        encoding, payload = self.serde.dumps_typed(checkpoint)
        meta_encoding, meta_payload = self.serde.dumps_typed(metadata)
        data = {
            "project_id": self.project_id,
            "thread_id": values["thread_id"],
            "namespace": values.get("checkpoint_ns", ""),
            "checkpoint_id": checkpoint["id"],
            "parent_id": values.get("checkpoint_id"),
            "encoding": encoding,
            "payload": payload,
            "metadata_encoding": meta_encoding,
            "metadata_payload": meta_payload,
        }
        async with self.database.session() as session:
            await session.execute(
                self.insert(GraphCheckpoint).values(**data).on_conflict_do_nothing()
            )
            retained = list(
                await session.scalars(
                    select(GraphCheckpoint.checkpoint_id)
                    .where(*self.scope(GraphCheckpoint, config))
                    .order_by(GraphCheckpoint.checkpoint_id.desc())
                    .limit(30)
                )
            )
            await session.execute(
                delete(GraphWrite).where(
                    *self.scope(GraphWrite, config), GraphWrite.checkpoint_id.not_in(retained)
                )
            )
            await session.execute(
                delete(GraphCheckpoint).where(
                    *self.scope(GraphCheckpoint, config),
                    GraphCheckpoint.checkpoint_id.not_in(retained),
                )
            )
        return {
            "configurable": {
                "thread_id": values["thread_id"],
                "checkpoint_ns": values.get("checkpoint_ns", ""),
                "checkpoint_id": checkpoint["id"],
            }
        }

    async def aput_writes(self, config, writes, task_id, task_path=""):
        values = config["configurable"]
        async with self.database.session() as session:
            for index, (channel, value) in enumerate(writes):
                encoding, payload = self.serde.dumps_typed(value)
                position = WRITES_IDX_MAP.get(channel, index)
                statement = self.insert(GraphWrite).values(
                    project_id=self.project_id,
                    thread_id=values["thread_id"],
                    namespace=values.get("checkpoint_ns", ""),
                    checkpoint_id=values["checkpoint_id"],
                    task_id=task_id,
                    position=position,
                    channel=channel,
                    encoding=encoding,
                    payload=payload,
                )
                statement = (
                    statement.on_conflict_do_update(
                        index_elements=[
                            "project_id",
                            "thread_id",
                            "namespace",
                            "checkpoint_id",
                            "task_id",
                            "position",
                        ],
                        set_={"channel": channel, "encoding": encoding, "payload": payload},
                    )
                    if position < 0
                    else statement.on_conflict_do_nothing()
                )
                await session.execute(statement)

    async def alist(self, config, *, filter=None, before=None, limit=None):
        if config is None:
            return
        async with self.database.session() as session:
            statement = select(GraphCheckpoint.checkpoint_id).where(
                *self.scope(GraphCheckpoint, config)
            )
            if before and before.get("configurable", {}).get("checkpoint_id"):
                statement = statement.where(
                    GraphCheckpoint.checkpoint_id < before["configurable"]["checkpoint_id"]
                )
            ids = list(
                await session.scalars(
                    statement.order_by(GraphCheckpoint.checkpoint_id.desc()).limit(
                        min(limit or 30, 30)
                    )
                )
            )
        for checkpoint_id in ids:
            row = await self.aget_tuple(
                {"configurable": {**config["configurable"], "checkpoint_id": checkpoint_id}}
            )
            if row and (
                not filter or all(row.metadata.get(key) == value for key, value in filter.items())
            ):
                yield row

    async def adelete_thread(self, thread_id):
        async with self.database.session() as session:
            for model in (GraphWrite, GraphCheckpoint):
                await session.execute(
                    delete(model).where(
                        model.project_id == self.project_id, model.thread_id == thread_id
                    )
                )
