import httpx

from backend.agents.tools.registry import TOOLS, tool_json
from backend.config import AgentSettings
from backend.models.projects import ProjectError


class ModelClient:
    def __init__(self, settings: AgentSettings):
        self.settings = settings
        self.client = httpx.AsyncClient(
            timeout=settings.timeout_seconds,
            trust_env=False,
            limits=httpx.Limits(
                max_connections=settings.capacity, max_keepalive_connections=settings.capacity
            ),
        )

    async def complete(
        self,
        messages,
        *,
        allow_tools,
        max_output_tokens=4000,
        tools=None,
        json_output=False,
        timeout_seconds=None,
    ):
        if len(tool_json(messages)) > 100000:
            raise ProjectError("CONTEXT_BUDGET", "资料超出本轮预算，请按模块分别整理", 503)
        payload = {
            "model": self.settings.model,
            "messages": messages,
            "max_tokens": max_output_tokens,
            "temperature": 0.2,
        }
        if allow_tools:
            payload["tools"] = TOOLS if tools is None else tools
        if json_output:
            payload["response_format"] = {"type": "json_object"}
        response = await self.client.post(
            self.settings.base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + self.settings.api_key.get_secret_value()},
            json=payload,
            timeout=timeout_seconds or self.settings.timeout_seconds,
        )
        if response.status_code == 429:
            raise ProjectError("MODEL_BUSY", "模型服务限流，本轮未保存草稿，请稍后重新发送", 503)
        response.raise_for_status()
        result = response.json()
        choice = result["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ProjectError(
                "MODEL_TRUNCATED", "模型输出超出预算，本轮未保存，请缩小到一个模块后重试", 503
            )
        return choice["message"], result.get("usage", {})
