from backend.agents.tools.registry import TOOLS, tool_json
from backend.models.projects import ProjectError


def request_budget(messages, allow_tools, remaining, *, tools=None, max_output=4000):
    # UTF-8 bytes deliberately overestimate byte-tokenized input, including tool schemas.
    request = {
        "messages": messages,
        "tools": (TOOLS if tools is None else tools) if allow_tools else [],
    }
    input_reserve = len(tool_json(request).encode("utf-8")) + 1024
    output_allowance = min(max_output, remaining - input_reserve)
    if output_allowance < 256:
        raise ProjectError("TOKEN_BUDGET", "剩余用量不足以处理本轮资料，请缩小任务后重试", 503)
    return input_reserve, output_allowance


def measured_tokens(usage, fallback):
    total = int(usage.get("total_tokens") or 0)
    parts = int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
    return max(total, parts) or fallback
