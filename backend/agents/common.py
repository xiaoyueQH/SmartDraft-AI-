import json
import re

from backend.models.projects import ProjectError


def draft_revision_requested(text):
    if re.search(r"(?:不要|暂不|不必).{0,8}(?:补齐|补全|补写|修改|更新|完善)", text):
        return False
    return bool(
        re.search(
            r"(?:请|帮我|麻烦).{0,25}(?:补齐|补全|补写|更新|修改|完善).{0,30}(?:草案|章节|那一章|部分)"
            r"|(?:草案|章节|那一章).{0,20}(?:补齐|补全|补写|更新|修改|完善)",
            text,
        )
    )


def technology_has_source(value, quotes, *, allow_excluded=False):
    components = re.findall(r"[A-Za-z][A-Za-z0-9_.-]*", value)
    if not components:
        return any(
            value in quote and not re.search(r"不懂|举例|待确认|未选|没选|推荐|未指定", quote)
            for quote in quotes
        )
    for part in components:
        pattern = r"(?<![A-Za-z0-9_.-])" + re.escape(part) + r"(?![A-Za-z0-9_.-])"
        latest = next((quote for quote in reversed(quotes) if re.search(pattern, quote, re.I)), "")
        evidence = [
            clause
            for clause in re.split(r"[，,。；;！!？?\n]", latest)
            if not re.search(
                r"不懂|不了解|不熟悉|不知道|举例|例子|例如|比如|不是.{0,8}(?:选|要求)|交给.{0,8}推荐",
                clause,
            )
        ]
        exclusion = (
            r"不要|不用|不使用|禁止|不能|不再(?:用|使用)|排除|不接受|不采用|不选|不是|不允许"
        )
        negative_value = bool(re.search(exclusion, value))
        evidence = [
            clause for clause in evidence if bool(re.search(exclusion, clause)) == negative_value
        ]
        if negative_value and not allow_excluded:
            return False
        if not any(re.search(pattern, clause, re.I) for clause in evidence):
            return False
    return True


def supported_technical_fact(value, quotes, *, allow_excluded=False):
    """A mention in a neutral example is not evidence of a technical decision."""
    if re.search(r"[A-Za-z][A-Za-z0-9_.-]*", value) and not technology_has_source(
        value, quotes, allow_excluded=allow_excluded
    ):
        return False
    return all(
        term not in value or any(term in quote for quote in quotes) for term in ("低代码", "零代码")
    )


def tool_json(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def request_budget(messages, allow_tools, remaining, *, tools=None, max_output=4000):
    # UTF-8 bytes deliberately overestimate byte-tokenized input, including tool schemas.
    request = {
        "messages": messages,
        "tools": (tools or []) if allow_tools else [],
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
