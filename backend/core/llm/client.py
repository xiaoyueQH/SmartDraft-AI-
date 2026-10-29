import asyncio

import httpx
from langchain_core.messages import convert_to_openai_messages
from langchain_openai import ChatOpenAI
from openai import APIError, APIStatusError, APITimeoutError

from backend.core.llm.models import ModelError, ModelProfile


def build_model(http, sync_http, settings, profile):
    return ChatOpenAI(
        model=profile.model,
        base_url=settings.base_url,
        api_key=settings.api_key.get_secret_value() or "not-configured",
        temperature=profile.temperature,
        http_async_client=http,
        http_client=sync_http,
        max_retries=0,
        timeout=settings.timeout_seconds,
        use_responses_api=False,
        stream_usage=True,
        http_socket_options=(),
    )


async def complete(
    model,
    *,
    profile: ModelProfile,
    purpose,
    messages,
    tools=None,
    json_output=False,
    max_output_tokens=4000,
    timeout_seconds=30,
    on_text=None,
    tool_choice=None,
):
    if tools and not profile.supports_tools:
        raise ModelError("MODEL_CAPABILITY", "当前模型不支持工具调用，请联系负责人调整配置")
    if json_output and not profile.supports_json:
        raise ModelError("MODEL_CAPABILITY", "当前模型不支持结构化输出，请联系负责人调整配置")
    kwargs = {"max_tokens": max_output_tokens, "timeout": timeout_seconds}
    if tools:
        kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
    if json_output:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        async with asyncio.timeout(timeout_seconds):
            if on_text is None:
                response = await model.ainvoke(messages, **kwargs)
            else:
                response = None
                chunks = model.astream(messages, **kwargs)
                try:
                    async for chunk in chunks:
                        response = chunk if response is None else response + chunk
                        # Only public answer text; never reasoning or function arguments.
                        if isinstance(chunk.content, str) and chunk.content:
                            await on_text(chunk.content)
                finally:
                    await chunks.aclose()
                    await on_text("")
                if response is None or not response.response_metadata.get("finish_reason"):
                    raise ModelError("MODEL_RESPONSE_FAILED", "回复连接中断，已保存内容保留")
        if response.response_metadata.get("finish_reason") == "length":
            raise ModelError("MODEL_TRUNCATED", "模型输出未完整生成，已完成内容保留")
        message = convert_to_openai_messages(response)
        # Preserve malformed calls for the existing tool validation/fallback boundary.
        if response.additional_kwargs.get("tool_calls"):
            message["tool_calls"] = response.additional_kwargs["tool_calls"]
        usage = response.usage_metadata or {}
        return message, {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            **({"total_tokens": usage["total_tokens"]} if usage.get("total_tokens") else {}),
            "model": profile.model,
            "purpose": purpose,
        }
    except APITimeoutError as error:
        raise ModelError("MODEL_TIMEOUT", "模型处理超时，已保存内容保留") from error
    except APIStatusError as error:
        if error.status_code == 429:
            raise ModelError("MODEL_BUSY", "模型服务限流，请稍后重试；已保存内容保留") from error
        if error.status_code in {401, 403, 404}:
            raise ModelError(
                "MODEL_CONFIGURATION", "模型暂不可用，请联系负责人检查模型权限与配置"
            ) from error
        raise ModelError("MODEL_RESPONSE_FAILED", "模型暂未完成本次处理，已保存内容保留") from error
    except (APIError, httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as error:
        raise ModelError("MODEL_RESPONSE_FAILED", "模型暂未完成本次处理，已保存内容保留") from error
