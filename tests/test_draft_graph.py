# ruff: noqa: F811
import asyncio
import os
from uuid import UUID, uuid4

import pytest
from test_auth import auth_api, login  # noqa: F401
from test_draft_sections import SPECS, AgentFlow, call, tools
from test_postgresql import postgres, postgres_auth  # noqa: F401

from backend.agents.drafting.graph import create_draft
from backend.db.checkpoints import DatabaseCheckpointer
from backend.db.project_store import ProjectStore
from backend.models.projects import ProjectError, SendMessage, StartConsultation


class CrashAfterChapter(DatabaseCheckpointer):
    def __init__(self, database, project_id):
        super().__init__(database, project_id)
        self.crashed = False

    async def aput(self, config, checkpoint, metadata, new_versions):
        result = await super().aput(config, checkpoint, metadata, new_versions)
        values = checkpoint.get("channel_values", {})
        if not self.crashed and values.get("draft", {}).get("sections", {}).get("goals"):
            self.crashed = True
            raise RuntimeError("simulated process interruption after committed chapter")
        return result


async def exercise_recovery(database, principal, mode="resume"):
    store = ProjectStore(database)
    project = await store.start_consultation(principal, StartConsultation(request_id=uuid4()))
    pid = UUID(project["id"])
    run, _ = await store.start_run(
        principal, pid, SendMessage(request_id=uuid4(), content="生成草案")
    )

    def flow(script=()):
        result = AgentFlow(script)
        result.store, result.principal = store, principal
        result.project_id, result.run_id = pid, UUID(run["id"])
        result.snapshot["project"] = project
        return result

    first = flow(
        [
            tools(
                call("write_prd_section", SPECS[0]),
                call("write_prd_section", SPECS[1]),
                call("finish_draft", {}),
            )
        ]
    )
    saver = CrashAfterChapter(database, pid)
    with pytest.raises(RuntimeError, match="simulated"):
        await create_draft(first, checkpointer=saver)
    assert first.section_writes == ["goals"]
    second = flow()
    if mode == "changed":
        second.notes["scope"] = ["新范围"]
        with pytest.raises(ProjectError) as error:
            await create_draft(second)
        assert error.value.code == "VERSION_CONFLICT" and not second.models
        return
    if mode == "expired":
        second.execution_deadline = asyncio.get_running_loop().time() + 5
    result = await create_draft(second)
    if mode == "expired":
        assert result["partial"] and not second.models
        assert "店主确认收款后扣库存" in result["document"]["content"]
        return
    assert not result["partial"] and second.section_writes == ["functions"]
    assert second.rounds == 3 and second.calls == 2
    # A completed execution is reused without asking a model again.
    third = flow()
    repeated = await create_draft(third)
    assert repeated == result and not third.models and third.rounds == 3


@pytest.mark.parametrize("mode", ["resume", "changed", "expired"])
async def test_draft_graph_persists_each_tool_and_resumes_without_rewriting(auth_api, mode):
    client, auth, database = auth_api
    principal = await auth.authenticate((await login(client))["access_token"])
    await exercise_recovery(database, principal, mode)


@pytest.mark.skipif(
    os.environ.get("SUPPORTFLOW_TEST_POSTGRES") != "1", reason="Requires PostgreSQL"
)
async def test_draft_graph_real_postgres_checkpoint_recovery(postgres_auth):
    principal = await postgres_auth.authenticate(
        (await postgres_auth.login("alice", "Postgres-test-password")).access_token
    )
    await exercise_recovery(postgres_auth.store.database, principal)
