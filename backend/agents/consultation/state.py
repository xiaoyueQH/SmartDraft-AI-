import hashlib
import re
from typing import Literal, TypedDict

from pydantic import BaseModel, Field

from backend.agents.common import supported_technical_fact, technology_has_source
from backend.models.consultation import ConsultationState, Extraction
from backend.models.drafting import DraftDecision
from backend.models.handoff import DocumentAction
from backend.models.projects import DiscoveryNotes


class GraphState(TypedDict):
    route: str
    payload: dict


class FactState(TypedDict):
    patch: dict
    sources: list[dict]
    saved_version: int


class ConsultationPayload(BaseModel):
    """One turn's serializable data; runtime clients, identity and snapshots stay outside."""

    run_id: str
    prompt: str
    options: dict = Field(default_factory=dict)
    notes: dict = Field(default_factory=dict)
    state: ConsultationState = Field(default_factory=ConsultationState)
    patches: list[dict] = Field(default_factory=list)
    drafts: list[dict] = Field(default_factory=list)
    replacement: dict | None = None
    document_action: DocumentAction | None = None
    handoff_outcome: Literal["completed", "partial", "no_change"] = "completed"
    rounds: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    calls: int = Field(default=0, ge=0)
    record_retried: bool = False
    extraction: Extraction | None = None
    answer: str = ""
    decision: DraftDecision = Field(default_factory=DraftDecision)
    discussion_saved: bool = False
    expected_version: int = Field(ge=1)


def valid_draft_request(quote, prompt):
    """A model's control quote must express a current document request, not just be verbatim."""
    if not quote or quote not in prompt:
        return False
    document = r"(?:需求书|需求文档|需求说明|开发文档|设计文档|草案|方案|PRD)"
    action = r"(?:生成|起草|编写|撰写|整理|补齐|补全|补写|更新|修改|完善|写|出)"
    expression = re.compile(
        rf"{action}.{{0,24}}?{document}|{document}.{{0,24}}?{action}"
        rf"|给我.{{0,12}}?{document}",
        re.I,
    )

    def directive(text):
        result = None
        for clause in re.split(r"[，,。；;！!？?\n]", text):
            clause = clause.strip()
            if result and re.fullmatch(
                r"(?:等等|算了|不用了|先别|先不要|暂时不用|取消){1,3}"
                r"(?:吧|了|先继续聊)?",
                clause,
            ):
                result = False
                continue
            match = expression.search(clause)
            bare = re.fullmatch(
                r"(?:请|麻烦)?(?:你|帮我|给我)?(?:现在|直接|先|重新|开始)?"
                r"(?:不要|先别|别|暂不|先不|不用|不必)?"
                r"(?:生成|起草)(?:一下|一份|吧|即可|就行|谢谢)*",
                clause,
            )
            if not match and not bare:
                continue
            before = clause[: match.start()] if match else ""
            control = clause[: match.end()] if match else clause
            if re.search(r"如果|假如|假设|要是|比如|例如|他说|有人说|客户说|用户说", control):
                result = False
            elif re.search(
                r"不要|先别|暂不|暂时不|先不|不必|不用|不需要|不想|别再|不能|不是让|并非|没让"
                r"|(?:别|不)(?:生成|起草)",
                control,
            ):
                result = False
            elif re.search(
                r"怎么|如何|是什么意思|什么叫|什么时候|为什么|是否已经|已经生成", clause
            ):
                result = False
            elif (
                bare
                or not before
                or re.search(
                    r"请|麻烦|帮我|给我|直接|现在|先|重新|开始|把|将|能不能|能否|可以|只|仅", before
                )
            ):
                result = True
        return result is True

    # Check the full turn as well: a clipped quote cannot bypass negation or a later cancellation.
    return directive(quote) and directive(prompt)


def document_action(prompt, *, has_draft=False):
    """Classify only after current-turn authorization; this function grants no permission."""
    clauses = [
        clause
        for clause in re.split(r"[，,。；;！!？?\n]", prompt)
        if valid_draft_request(clause, clause)
    ]
    directive = clauses[-1] if clauses else prompt
    if re.search(r"补齐|补全|补写", directive):
        return "fill"
    if re.search(r"修改|更新|完善", directive) and not re.search(
        r"整篇|整份|全文|全部|完全重写", directive
    ):
        return "revise"
    if re.search(r"重新生成|重写|整篇|整份|全文|全部", directive):
        return "rewrite_all" if has_draft else "create"
    return "revise" if has_draft else "create"


def source_catalog(prompt, run_id, history=()):
    catalog = []
    entries = [("current", prompt, str(run_id), "")]
    entries += [
        (
            f"run:{row['run_id']}" if row.get("run_id") else row["id"],
            row["content"],
            row.get("run_id") or "",
            "" if row.get("run_id") else row["id"],
        )
        for row in reversed(history)
        if row["role"] == "user"
        and row.get("sender_type", "customer") in {"customer", "legacy"}
        and row.get("run_id") != str(run_id)
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
                    "message_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "quote_start": start,
                }
            )
            total += len(quote)
    return catalog


def verified_patch(extraction, prompt, run_id, existing, history=(), *, current_message_ids=()):
    patch = extraction.notes.model_dump(exclude_unset=True)
    removals = patch.get("remove_facts", {})
    noop_technology = set()
    if "technology_preference" not in patch:
        noop_technology = {
            value
            for value in removals.get("technology_preference", [])
            if value != existing.get("technology_preference")
        }
        if noop_technology:
            removals["technology_preference"] = [
                value for value in removals["technology_preference"] if value not in noop_technology
            ]
            if not removals["technology_preference"]:
                removals.pop("technology_preference")
    sources = []
    catalog = {item["id"]: item for item in source_catalog(prompt, run_id, history)}
    normalized = []
    for source in extraction.sources:
        if source.field == "technology_preference" and source.value in noop_technology:
            continue
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
                    "quote": source.quote
                    if source.quote and source.quote in item["text"]
                    else item["text"],
                    "run_id": item["run_id"],
                    "message_id": item["message_id"],
                    "message_sha256": item["message_sha256"],
                    "quote_start": item["quote_start"]
                    + (
                        item["text"].index(source.quote)
                        if source.quote and source.quote in item["text"]
                        else 0
                    ),
                }
            )
            source = source.model_copy(
                update={
                    "quote_end": source.quote_start + len(source.quote),
                    "quote_sha256": hashlib.sha256(source.quote.encode("utf-8")).hexdigest(),
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
            else {
                "run_id": origin.get("run_id") or "",
                "message_id": "" if origin.get("run_id") else origin["id"],
            }
        )
        content = prompt if source.quote in prompt else origin["content"]
        start = content.index(source.quote)
        sources.append(
            source.model_copy(
                update={
                    **attribution,
                    "message_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "quote_start": start,
                    "quote_end": start + len(source.quote),
                    "quote_sha256": hashlib.sha256(source.quote.encode("utf-8")).hexdigest(),
                }
            )
        )
    removals = patch.get("remove_facts", {})
    for source in sources:
        if source.value in removals.get(source.field, []):
            current_message = source.message_id in current_message_ids
            if not current_message and (source.message_id or source.run_id != str(run_id)):
                raise ValueError("撤销旧事实必须引用本轮纠正原话")
            if source.field == "technology_preference" and not re.search(
                r"纠正|更正|只是|举例|不是|取消|删除|不用|不要|不再|改", source.quote
            ):
                raise ValueError("撤销技术选择需要明确纠正依据")
    rejected = {
        (source.field, source.value)
        for source in sources
        if source.field in {"technology_preference", "constraints"}
        and source.value not in removals.get(source.field, [])
        and (
            source.intent == "unspecified"
            or source.field == "technology_preference"
            and not technology_has_source(source.value, [source.quote])
            or source.field == "technology_preference"
            and source.intent == "excluded"
            or not supported_technical_fact(
                source.value, [source.quote], allow_excluded=source.field == "constraints"
            )
        )
    }
    for field, value in list(patch.items()):
        if isinstance(value, str) and (field, value) in rejected:
            patch.pop(field)
        elif isinstance(value, list):
            patch[field] = [item for item in value if (field, item) not in rejected]
    sources = [s for s in sources if (s.field, s.value) not in rejected]
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


def question_key(text):
    return re.sub(r"[\W_]+", "", re.sub(r"^\s*(?:[-*]|\d+[.、])\s*", "", text)).lower()


_REPLY_LITERAL = re.compile(
    r"```[\s\S]*?(?:```|\Z)|`[^`\n]*(?:`|\Z)|https?://[^\s<>。！？)\]]+"
    r"|“[^”]*”|「[^」]*」|『[^』]*』"
)


def reply_units(reply):
    """Keep bracketed explanations, quoted endings and code with their sentence."""
    pairs = {"（": "）", "(": ")", "[": "]", "【": "】"}
    units, stack = [], []
    start = index = 0
    while index < len(reply):
        literal = _REPLY_LITERAL.match(reply, index)
        if literal:
            index = literal.end()
            continue
        char = reply[index]
        if char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
        index += 1
        if not stack and char in "。！？!?\n":
            while index < len(reply) and reply[index] in "”’」』":
                index += 1
            for marker in ("**", "__"):
                if reply[start:index].count(marker) % 2 and reply.startswith(marker, index):
                    index += len(marker)
            units.append(reply[start:index])
            start = index
    if start < len(reply):
        units.append(reply[start:])
    # Parenthesized options immediately following a question belong to that question,
    # including a single line break. Removing a repeated question removes its options too.
    combined = []
    for unit in units:
        if (
            combined
            and unit.lstrip().startswith(("（", "("))
            and re.search(r"[?？][”’」』]*(?:\*\*|__)?\s*$", combined[-1])
            and not combined[-1].endswith("\n\n")
        ):
            combined[-1] += unit
        elif unit.isspace() and combined:
            combined[-1] += unit
        else:
            combined.append(unit)
    return combined


def limit_questions(reply, asked=(), *, limit=2):
    """Bound public questions without another model call; preserve declarative sentences."""

    def identity(text):
        return question_key(re.split(r"[?？]", text, maxsplit=1)[0])

    known = {identity(item) for item in asked}
    output, questions = [], []
    used = 0
    for part in reply_units(reply):
        visible = _REPLY_LITERAL.sub("", part).rstrip()
        marks = len(re.findall(r"[?？]", visible))
        questioning = bool(marks) or bool(
            re.search(
                r"(?:请问|请告诉|请介绍|请说明|请描述|请分享|能否|是否|有没有|怎样|如何|什么|哪些)",
                visible,
            )
            and not visible.endswith(("。", "！", "!"))
        )
        if questioning:
            key = identity(visible)
            cost = max(1, marks)
            if used + cost > limit or key in known:
                continue
            known.add(key)
            used += cost
            questions.append(part.strip())
        output.append(part)
    return re.sub(r"\n{3,}", "\n\n", "".join(output)).strip(), questions
