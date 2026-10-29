import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr

from backend.config import AgentSettings
from backend.core.llm.factory import ModelFactory
from backend.core.llm.models import ModelError, ModelProfile


async def factory(settings, handler):
    models = ModelFactory(settings)
    await models.http.aclose()
    models.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return models


async def test_task_profiles_select_real_request_model_and_record_usage():
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
            },
        )

    settings = AgentSettings(
        api_key=SecretStr("private-test-key"),
        consultation=ModelProfile(model="talk"),
        draft_planner=ModelProfile(model="plan"),
        draft_writer=ModelProfile(model="write"),
    )
    models = await factory(settings, respond)
    try:
        for purpose, name in (
            ("consultation", "talk"),
            ("draft_planner", "plan"),
            ("draft_writer", "write"),
        ):
            message, usage = await models.complete(
                [{"role": "user", "content": "test"}], purpose=purpose
            )
            assert (
                message["content"] == "ok"
                and usage["model"] == name
                and usage["purpose"] == purpose
            )
        assert [request["model"] for request in requests] == ["talk", "plan", "write"]
        assert "private-test-key" not in str(requests)
    finally:
        await models.close()


async def test_aliases_share_model_limit_and_cancelled_wait_does_not_leak_slots():
    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(request):
        entered.set()
        await release.wait()
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    models = await factory(
        AgentSettings(consultation=ModelProfile(concurrency=1, wait_seconds=0.01)), respond
    )
    first = asyncio.create_task(models.complete([], purpose="consultation"))
    try:
        await entered.wait()
        with pytest.raises(ModelError, match="繁忙"):
            await models.complete([], purpose="draft_planner")
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert (await models.complete([], purpose="draft_writer"))[0]["content"] == "ok"
    finally:
        release.set()
        await models.close()


@pytest.mark.parametrize(
    "status,code",
    [(429, "MODEL_BUSY"), (403, "MODEL_CONFIGURATION"), (502, "MODEL_RESPONSE_FAILED")],
)
async def test_provider_errors_are_safe_and_do_not_retry_or_switch_silently(status, code):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, text="private-provider-key-and-payload")

    models = await factory(AgentSettings(), respond)
    try:
        with pytest.raises(ModelError) as caught:
            await models.complete([])
        assert caught.value.code == code and "private" not in str(caught.value)
        assert len(calls) == 1
    finally:
        await models.close()


async def test_capability_rejected_before_transmission_and_slot_is_released():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    models = await factory(AgentSettings(consultation=ModelProfile(supports_tools=False)), respond)
    try:
        with pytest.raises(ModelError, match="工具调用"):
            await models.complete([], tools=[{"type": "function"}])
        assert not calls
        assert (await models.complete([]))[0]["content"] == "ok"
    finally:
        await models.close()


async def test_explicit_backup_is_bounded_and_records_actual_model():
    calls = []

    def respond(request):
        name = json.loads(request.content)["model"]
        calls.append(name)
        return (
            httpx.Response(429, text="private")
            if name == "primary"
            else httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "backup result"}}]},
            )
        )

    models = await factory(
        AgentSettings(
            consultation=ModelProfile(model="primary"),
            fallbacks={"consultation": ModelProfile(model="backup")},
        ),
        respond,
    )
    try:
        result, usage = await models.complete([], max_attempts=2)
        assert result["content"] == "backup result"
        assert calls == ["primary", "backup"]
        assert usage["model"] == "backup" and usage["attempts"] == 2
        assert usage["fallback_from"] == "primary"
        calls.clear()
        with pytest.raises(ModelError):
            await models.complete([], max_attempts=1)
        assert calls == ["primary"]
    finally:
        await models.close()


async def test_backup_never_replays_already_visible_stream_or_auth_errors():
    calls, received = [], []
    mode = "stream"

    def respond(request):
        calls.append(json.loads(request.content)["model"])
        if mode == "auth":
            return httpx.Response(403, text="private")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=StreamBody(
                [
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "already visible"},
                                "finish_reason": None,
                            }
                        ]
                    },
                ]
            ),
        )

    async def write(text):
        received.append(text)

    models = await factory(
        AgentSettings(
            consultation=ModelProfile(model="primary"),
            fallbacks={"consultation": ModelProfile(model="backup")},
        ),
        respond,
    )
    try:
        with pytest.raises(ModelError):
            await models.complete([], max_attempts=2, on_text=write)
        assert calls == ["primary"] and "already visible" in received
        mode = "auth"
        calls.clear()
        with pytest.raises(ModelError) as error:
            await models.complete([], max_attempts=2)
        assert error.value.code == "MODEL_CONFIGURATION" and calls == ["primary"]
    finally:
        await models.close()


@pytest.mark.parametrize(
    "result", [[], {"choices": [None]}, {"choices": [{"message": {}}], "usage": [1]}]
)
async def test_malformed_response_is_a_safe_recoverable_error(result):
    models = await factory(AgentSettings(), lambda request: httpx.Response(200, json=result))
    try:
        with pytest.raises(ModelError) as error:
            await models.complete([])
        assert error.value.code == "MODEL_RESPONSE_FAILED"
    finally:
        await models.close()


class StreamBody(httpx.AsyncByteStream):
    def __init__(self, packets, pause=None):
        self.packets, self.pause, self.closed = packets, pause, False

    async def __aiter__(self):
        for index, packet in enumerate(self.packets):
            if self.pause and index == 1:
                await self.pause.wait()
            yield ("data: " + json.dumps(packet, ensure_ascii=False) + "\n\n").encode()
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed = True


def stream_chunk(delta=None, finish=None, usage=None):
    return {
        "id": "stream-test",
        "object": "chat.completion.chunk",
        "model": "talk",
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]
        if usage is None
        else [],
        **({"usage": usage} if usage is not None else {}),
    }


async def test_stream_delivers_text_before_completion_and_keeps_tool_arguments_private():
    release, first = asyncio.Event(), asyncio.Event()
    packets = [
        stream_chunk(
            {"role": "assistant", "content": "您好", "reasoning_content": "private-thought"}
        ),
        stream_chunk(
            {
                "content": "，已了解。",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-one",
                        "type": "function",
                        "function": {"name": "record", "arguments": '{"x":'},
                    }
                ],
            }
        ),
        stream_chunk({"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}),
        stream_chunk(finish="tool_calls"),
        stream_chunk(usage={"prompt_tokens": 6, "completion_tokens": 5, "total_tokens": 11}),
    ]
    body = StreamBody(packets, release)
    requests, text = [], []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, stream=body, headers={"content-type": "text/event-stream"})

    models = await factory(
        AgentSettings(consultation=ModelProfile(concurrency=1, wait_seconds=0.01)), respond
    )

    async def receive(delta):
        text.append(delta)
        first.set()

    task = asyncio.create_task(models.complete([], on_text=receive))
    try:
        await asyncio.wait_for(first.wait(), 2)
        assert "".join(text) == "您好" and not task.done()
        with pytest.raises(ModelError, match="繁忙"):
            await models.complete([], purpose="draft_planner")
        release.set()
        message, usage = await task
        assert message["content"] == "您好，已了解。"
        assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"x": 1}
        assert "".join(text) == message["content"] and "private" not in "".join(text)
        assert usage["total_tokens"] == 11 and body.closed
        assert requests[0]["stream"] is True
    finally:
        release.set()
        await models.close()


async def test_stream_cancel_closes_response_and_releases_shared_model_slot():
    release, first = asyncio.Event(), asyncio.Event()
    body = StreamBody(
        [stream_chunk({"role": "assistant", "content": "开始"}), stream_chunk(finish="stop")],
        release,
    )
    models = await factory(
        AgentSettings(consultation=ModelProfile(concurrency=1)),
        lambda request: httpx.Response(
            200, stream=body, headers={"content-type": "text/event-stream"}
        ),
    )

    async def receive(delta):
        first.set()

    task = asyncio.create_task(models.complete([], on_text=receive))
    try:
        await asyncio.wait_for(first.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert body.closed and models.slots[models.profiles["consultation"].model]._value == 1
    finally:
        release.set()
        await models.close()


@pytest.mark.parametrize(
    "finish,code", [(None, "MODEL_RESPONSE_FAILED"), ("length", "MODEL_TRUNCATED")]
)
async def test_incomplete_stream_never_becomes_a_successful_result(finish, code):
    body = StreamBody(
        [stream_chunk({"role": "assistant", "content": "半截内容"}), stream_chunk(finish=finish)]
    )
    models = await factory(
        AgentSettings(),
        lambda request: httpx.Response(
            200, stream=body, headers={"content-type": "text/event-stream"}
        ),
    )

    async def receive(delta):
        pass

    try:
        with pytest.raises(ModelError) as caught:
            await models.complete([], on_text=receive)
        assert caught.value.code == code and body.closed
    finally:
        await models.close()


async def test_per_call_timeout_overrides_adapter_default_without_entering_provider_payload():
    captured = []

    def respond(request):
        captured.append(request)
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    models = await factory(AgentSettings(api_key=SecretStr("test"), timeout_seconds=30), respond)
    try:
        await models.complete([{"role": "user", "content": "test"}], timeout_seconds=55)
        assert captured[0].extensions["timeout"]["read"] == 55
        assert "timeout" not in json.loads(captured[0].content)
    finally:
        await models.close()


async def test_provider_read_timeout_has_a_distinct_safe_code_and_releases_slot():
    def respond(request):
        raise httpx.ReadTimeout("private upstream details", request=request)

    models = await factory(AgentSettings(api_key=SecretStr("test")), respond)
    try:
        with pytest.raises(ModelError) as error:
            await models.complete([{"role": "user", "content": "test"}], timeout_seconds=55)
        assert error.value.code == "MODEL_TIMEOUT"
        assert "private upstream" not in str(error.value)
        assert all(not slot.locked() for slot in models.slots.values())
    finally:
        await models.close()
