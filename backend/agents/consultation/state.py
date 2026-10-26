from typing import TypedDict

from backend.models.projects import DiscoveryNotes


class GraphState(TypedDict):
    route: str
    payload: dict


def source_catalog(prompt, run_id, history=()):
    catalog = []
    entries = [("current", prompt, str(run_id), "")]
    entries += [
        (row["id"], row["content"], row.get("run_id") or "", row["id"])
        for row in reversed(history)
        if row["role"] == "user"
    ]
    total = 0
    for prefix, content, source_run, message_id in entries:
        for index, start in enumerate(range(0, len(content), 500)):
            quote = content[start : start + 500]
            if total + len(quote) > 12000:
                return catalog
            catalog.append(
                {
                    "id": f"{prefix}:{index}",
                    "text": quote,
                    "run_id": source_run,
                    "message_id": message_id,
                }
            )
            total += len(quote)
    return catalog


def verified_patch(extraction, prompt, run_id, existing, history=()):
    patch = extraction.notes.model_dump(exclude_unset=True)
    sources = []
    catalog = {item["id"]: item for item in source_catalog(prompt, run_id, history)}
    normalized = []
    for source in extraction.sources:
        values = patch.get(source.field)
        if (
            isinstance(values, list)
            and len(values) > 1
            and source.value in {delimiter.join(values) for delimiter in (", ", ",", "，", "、")}
        ):
            normalized.extend(source.model_copy(update={"value": value}) for value in values)
        else:
            normalized.append(source)
    for source in normalized:
        if source.source_id:
            item = catalog.get(source.source_id)
            if item is None:
                raise ValueError(f"字段 {source.field} 的source_id不存在，请从source_catalog复制id")
            source = source.model_copy(
                update={
                    "quote": item["text"],
                    "run_id": item["run_id"],
                    "message_id": item["message_id"],
                }
            )
            sources.append(source)
            continue
        if not source.quote:
            raise ValueError(f"字段 {source.field} 必须有source_id或连续原文quote")
        origin = next(
            (
                row
                for row in reversed(history)
                if row["role"] == "user" and source.quote in row["content"]
            ),
            None,
        )
        if source.quote not in prompt and origin is None:
            raise ValueError(
                f"字段 {source.field} 的quote不在本轮输入中，"
                "请直接复制连续原文，不能使用省略号或拼接片段"
            )
        attribution = (
            {"run_id": str(run_id), "message_id": ""}
            if source.quote in prompt
            else {"run_id": origin.get("run_id") or "", "message_id": origin["id"]}
        )
        sources.append(source.model_copy(update=attribution))
    for field, value in list(patch.items()):
        if field == "open_questions":
            continue
        if not value:
            patch.pop(field)
            continue
        if (
            isinstance(value, str)
            and value in {"待确认", "未知", "未说明", "不确定"}
            and not any(source.field == field and source.value == value for source in sources)
        ):
            patch.pop(field)
            continue
        values = [(field, item) for item in value] if isinstance(value, list) else [(field, value)]
        if field == "remove_facts":
            values = [(key, item) for key, items in value.items() for item in items]
        for key, item in values:
            old = existing.get(key)
            if (
                key in DiscoveryNotes.model_fields
                and (old == item or isinstance(old, list) and item in old)
                and field != "remove_facts"
            ):
                continue
            if not any(source.field == key and source.value == item for source in sources):
                if key == "technology_preference":
                    selected = [source for source in sources if source.field == key]
                    current = [
                        source
                        for source in selected
                        if source.run_id == str(run_id) and not source.message_id
                    ]
                    selected = current or selected
                    parts = list(dict.fromkeys(source.value for source in selected))
                    origins = {(source.run_id, source.message_id) for source in selected}
                    if parts and len(origins) == 1 and item == "；".join(parts):
                        continue
                raise ValueError(
                    f"字段 {key} 的新增值缺少匹配的sources条目，field和value必须逐字对应notes"
                )
    for quote in (extraction.draft_request_quote, extraction.cancel_draft_quote):
        if quote and quote not in prompt:
            raise ValueError("整理意愿缺少本轮原话")
    return patch, sources
