"""Lossless Markdown sections and a version-bound index without a second body copy."""

import hashlib
import json
import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Literal
from uuid import UUID, uuid4

from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import JSON, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.orm import Base

PARSER_VERSION = 1
MAX_INDEX_BLOCKS = 512
MAX_UNDO_BYTES = 65536


def content_hash(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _payload_hash(block):
    if "body_hash" in block:
        return block["body_hash"]
    body = block.get("content", "")
    tokens = MarkdownIt("commonmark").enable("table").parse(body)
    heading = next((t for t in tokens if t.type == "heading_open" and t.level == 0), None)
    lines = [line for line in re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", body) if line]
    rest = "".join(lines[heading.map[1] :] if heading and heading.map[0] == 0 else lines)
    return content_hash(rest) if rest.strip() else ""


def split_markdown(content, previous=()):
    """One parser, source-coordinate slices; never use position or a leftover pair as identity."""
    lines = [line for line in re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", content) if line]
    tokens = MarkdownIt("commonmark").enable("table").parse(content)
    headings = []
    for index, token in enumerate(tokens):
        if token.type == "heading_open" and token.level == 0 and token.map:
            level = int(token.tag[1])
            if level <= 3:
                headings.append((token.map[0], level, tokens[index + 1].content, token.map[1]))
    primary = [row for row in headings if row[1] <= 2] or headings
    boundaries = {0: (0, "文档说明", 0)}
    for position, (start, level, title, heading_end) in enumerate(primary):
        boundaries[start] = (level, title, heading_end)
        end = primary[position + 1][0] if position + 1 < len(primary) else len(lines)
        if sum(len(line) for line in lines[start:end]) > 6000:
            for sub_start, sub_level, sub_title, sub_end in headings:
                if start < sub_start < end:
                    boundaries[sub_start] = (sub_level, sub_title, sub_end)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    sections = sorted(boundaries)
    atomic_starts = sorted({token.map[0] for token in tokens if token.level == 0 and token.map})
    for position, start in enumerate(sections):
        end = sections[position + 1] if position + 1 < len(sections) else len(lines)
        if offsets[end] - offsets[start] <= 12000:
            continue
        anchor = start
        level, title, _ = boundaries[start]
        for boundary in atomic_starts:
            if anchor < boundary < end and offsets[boundary] - offsets[anchor] >= 6000:
                boundaries[boundary] = (level, title + "（续）", boundary)
                anchor = boundary
    blocks = []
    starts = sorted(boundaries)
    for ordinal, start in enumerate(starts):
        end = starts[ordinal + 1] if ordinal + 1 < len(starts) else len(lines)
        body = "".join(lines[start:end])
        if not body:
            continue
        level, title, heading_end = boundaries[start]
        payload = "".join(lines[heading_end:end])
        blocks.append(
            {
                "id": "",
                "title": title,
                "level": level,
                "content": body,
                "hash": content_hash(body),
                "position": len(blocks),
                "start": offsets[start],
                "end": offsets[end],
                "body_hash": content_hash(payload) if payload.strip() else "",
                "kind": "preamble"
                if level == 0
                else "continuation"
                if heading_end == start
                else "section",
                "parent_id": None,
                "oversized": len(body) > 12000,
            }
        )
    old = [dict(row) for row in previous]
    for row in old:
        row["body_hash"] = _payload_hash(row)
    used = set()
    if (
        len(old) == len(blocks)
        and len({row["id"] for row in old}) == len(old)
        and all(a["hash"] == b["hash"] for a, b in zip(old, blocks, strict=True))
    ):
        # An identical complete partition is an unchanged document, even with duplicates.
        for block, row in zip(blocks, old, strict=True):
            block["id"] = row["id"]
            used.add(row["id"])
    # Each identity key must be unique on BOTH complete sides, not merely among leftovers.
    for key in (
        lambda row: ("hash", row["hash"]),
        lambda row: ("title", row["level"], row["title"]),
        lambda row: ("body", row["body_hash"]) if row["body_hash"] else None,
    ):
        before, after = Counter(key(row) for row in old), Counter(key(row) for row in blocks)
        lookup = {key(row): row for row in old if before[key(row)] == 1}
        for block in blocks:
            identity = key(block)
            candidate = lookup.get(identity)
            if (
                not block["id"]
                and identity is not None
                and after[identity] == 1
                and candidate
                and candidate["id"] not in used
            ):
                block["id"] = candidate["id"]
                used.add(candidate["id"])
    stack = []
    for block in blocks:
        block["id"] = block["id"] or str(uuid4())
        while stack and stack[-1]["level"] >= block["level"]:
            stack.pop()
        block["parent_id"] = stack[-1]["id"] if stack else None
        if block["level"]:
            stack.append(block)
    return blocks


class BlockInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1, max_length=40)
    title: str = Field(max_length=120000)
    level: int = Field(ge=0, le=3)
    kind: Literal["preamble", "section", "continuation"]
    position: int = Field(ge=0)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    body_hash: str = Field(pattern=r"^(?:[0-9a-f]{64})?$")
    parent_id: str | None = None
    oversized: bool = False


class DocumentIndex(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    parser_version: Literal[1] = PARSER_VERSION
    item_id: UUID
    content_version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_length: int = Field(ge=0)
    status: Literal["ready", "too_large"] = "ready"
    mapping_status: Literal["initial", "matched", "invalidated"] = "initial"
    blocks: tuple[BlockInfo, ...] = Field(default=(), max_length=MAX_INDEX_BLOCKS)
    invalidated_ids: tuple[str, ...] = Field(default=(), max_length=MAX_INDEX_BLOCKS)

    @model_validator(mode="after")
    def valid_partition(self):
        if self.status == "too_large":
            if self.blocks:
                raise ValueError("超限目录不能包含部分块冒充完整目录")
            return self
        seen, offset, stack = set(), 0, []
        for position, block in enumerate(self.blocks):
            if (
                block.id in seen
                or block.start != offset
                or block.end <= block.start
                or block.position != position
                or block.parent_id
                and block.parent_id not in seen
            ):
                raise ValueError("目录区间或身份不完整")
            offset = block.end
            seen.add(block.id)
            while stack and stack[-1].level >= block.level:
                stack.pop()
            if block.parent_id != (stack[-1].id if stack else None):
                raise ValueError("目录父级与章节层级不一致")
            if block.level:
                stack.append(block)
        if offset != self.content_length or seen.intersection(self.invalidated_ids):
            raise ValueError("目录没有覆盖完整正文或复用已失效编号")
        return self

    def read_blocks(self, content):
        if self.content_hash != content_hash(content) or self.content_length != len(content):
            raise ValueError("目录与正文不匹配")
        if self.status != "ready":
            raise ValueError("正文结构数量超出本次目录容量")
        parsed = split_markdown(content)
        keys = ("title", "level", "kind", "start", "end", "hash", "body_hash", "oversized")
        if len(parsed) != len(self.blocks) or any(
            any(getattr(block, key) != raw[key] for key in keys)
            for block, raw in zip(self.blocks, parsed, strict=True)
        ):
            raise ValueError("目录结构与正文不匹配")
        result = []
        for block in self.blocks:
            body = content[block.start : block.end]
            if content_hash(body) != block.hash:
                raise ValueError("目录块校验失败")
            result.append({**block.model_dump(), "content": body})
        return result


def build_document_index(item_id, version, content, previous=None):
    if previous and (previous.item_id != UUID(str(item_id)) or version < previous.content_version):
        raise ValueError("不能复用其他文档或未来版本的目录")
    old = [b.model_dump() for b in previous.blocks] if previous else []
    blocks = split_markdown(content, old)
    status = "ready" if len(blocks) <= MAX_INDEX_BLOCKS else "too_large"
    known = {row["id"] for row in blocks} if status == "ready" else set()
    invalidated = tuple(row["id"] for row in old if row["id"] not in known)
    return DocumentIndex(
        item_id=item_id,
        content_version=version,
        content_hash=content_hash(content),
        content_length=len(content),
        status=status,
        blocks=tuple(
            BlockInfo.model_validate({k: v for k, v in row.items() if k != "content"})
            for row in blocks
        )
        if status == "ready"
        else (),
        invalidated_ids=invalidated,
        mapping_status="invalidated" if invalidated else "matched" if previous else "initial",
    )


class DocumentMetadata(Base):
    __tablename__ = "document_metadata"
    item_id: Mapped[UUID] = mapped_column(
        ForeignKey("project_items.id", ondelete="CASCADE"), primary_key=True
    )
    project_id: Mapped[UUID] = mapped_column(ForeignKey("delivery_projects.id"), index=True)
    content_version: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    parser_version: Mapped[int] = mapped_column(Integer)
    manifest: Mapped[dict] = mapped_column(JSON)


def revision_edits(original_blocks, replacements):
    """Bounded line diff plus common-edge trimming; offsets refer to exact Python strings."""
    edits = []
    shift = 0
    for block in original_blocks:
        old = block["content"]
        new = replacements.get(block["id"], old)
        old_lines, new_lines = old.splitlines(keepends=True), new.splitlines(keepends=True)
        a, b = [0], [0]
        for line in old_lines:
            a.append(a[-1] + len(line))
        for line in new_lines:
            b.append(b[-1] + len(line))
        operations = (
            SequenceMatcher(None, old_lines, new_lines, autojunk=True).get_opcodes()
            if max(len(old_lines), len(new_lines)) <= 5000
            else [("replace", 0, len(old_lines), 0, len(new_lines))]
        )
        for tag, i, j, x, y in operations:
            if tag == "equal":
                continue
            left, right = old[a[i] : a[j]], new[b[x] : b[y]]
            prefix = 0
            while prefix < min(len(left), len(right)) and left[prefix] == right[prefix]:
                prefix += 1
            suffix = 0
            while (
                suffix < min(len(left), len(right)) - prefix
                and left[len(left) - suffix - 1] == right[len(right) - suffix - 1]
            ):
                suffix += 1
            old_text = left[prefix : len(left) - suffix if suffix else len(left)]
            new_text = right[prefix : len(right) - suffix if suffix else len(right)]
            if old_text != new_text:
                edits.append(
                    {
                        "block_id": block["id"],
                        "title": block["title"],
                        "old_start": block["start"] + a[i] + prefix,
                        "new_start": block["start"] + shift + b[x] + prefix,
                        "old_text": old_text,
                        "new_text": new_text,
                    }
                )
        shift += len(new) - len(old)
    return edits


def validate_revision_markdown(content):
    for token in MarkdownIt("commonmark").enable("table").parse(content):
        if token.type == "fence" and token.map:
            # The parser counts both marker lines only for a real closing fence. Comparing
            # source text with strip() would accept an indented fake close and reject quotes.
            body_lines = token.content.count("\n") + bool(
                token.content and not token.content.endswith("\n")
            )
            if token.map[1] - token.map[0] != body_lines + 2:
                raise ValueError("修改块包含未闭合的代码围栏")


def apply_inverse(content, edits):
    result, end = [], 0
    for edit in edits:
        start = edit["new_start"]
        stop = start + len(edit["new_text"])
        if start < end or content[start:stop] != edit["new_text"]:
            raise ValueError("最近差异与当前正文不匹配")
        result.extend((content[end:start], edit["old_text"]))
        end = stop
    return "".join((*result, content[end:]))


def bounded_revision(edits, content, extra=None):
    size = len(json.dumps({"edits": edits, "extra": extra}, ensure_ascii=False).encode())
    removed = sum(len(e["old_text"]) for e in edits)
    if size > MAX_UNDO_BYTES or removed > max(256, len(content) * 0.8):
        raise ValueError("本次修改过大，无法提供有界撤销；请分次修改或明确重新生成整份")


def restore_block_ids(index, previous_blocks):
    wanted = {(b["start"], b["end"], b["hash"]): b["id"] for b in previous_blocks}
    remap = {b.id: wanted.get((b.start, b.end, b.hash), b.id) for b in index.blocks}
    return DocumentIndex.model_validate(
        index.model_dump()
        | {
            "blocks": [
                b.model_dump()
                | {"id": remap[b.id], "parent_id": remap.get(b.parent_id, b.parent_id)}
                for b in index.blocks
            ],
            "invalidated_ids": tuple(i for i in index.invalidated_ids if i not in remap.values()),
        }
    )


def revision_block_ids(index, blocks, replacements):
    """A version-checked explicit edit supplies identity evidence unavailable to free reparse."""
    hints, shift = [], 0
    parsed = {(b.start, b.end, b.hash) for b in index.blocks}
    for block in blocks:
        body = replacements.get(block["id"], block["content"])
        start = block["start"] + shift
        key = (start, start + len(body), content_hash(body))
        if key in parsed:
            hints.append({"id": block["id"], "start": key[0], "end": key[1], "hash": key[2]})
        elif block["id"] not in replacements:
            raise ValueError("修改破坏了未改块的结构边界")
        shift += len(body) - len(block["content"])
    return restore_block_ids(index, hints)


def unchanged_inverse_blocks(blocks, edits):
    hints = []
    for block in blocks:
        if any(
            block.start <= e["new_start"] < block.end
            or e["new_start"] < block.start < e["new_start"] + len(e["new_text"])
            for e in edits
        ):
            continue
        shift = sum(
            len(e["old_text"]) - len(e["new_text"]) for e in edits if e["new_start"] < block.start
        )
        hints.append(block.model_dump() | {"start": block.start + shift, "end": block.end + shift})
    return hints


class DocumentUndo(Base):
    __tablename__ = "document_undo"
    item_id: Mapped[UUID] = mapped_column(
        ForeignKey("project_items.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("delivery_projects.id"), index=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("project_runs.id"))
    before_version: Mapped[int] = mapped_column(Integer)
    after_version: Mapped[int] = mapped_column(Integer)
    before_hash: Mapped[str] = mapped_column(String(64))
    after_hash: Mapped[str] = mapped_column(String(64))
    edits: Mapped[list] = mapped_column(JSON)
    before_questions: Mapped[list] = mapped_column(JSON)
    after_questions: Mapped[list] = mapped_column(JSON)
    before_blocks: Mapped[list] = mapped_column(JSON)
