import asyncio
import hashlib
import ipaddress
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import httpx

from backend.models.consultation import Reference

PUBLIC_TOPICS = {
    "data_integrity": "业务系统 数据一致性 库存并发扣减 幂等 设计",
    "payment_callbacks": "支付回调 重复通知 幂等 官方文档",
    "refund_workflow": "售后退款 状态流转 通用系统设计",
    "permissions": "业务系统 角色权限 数据访问控制 设计",
    "data_import": "业务系统 数据导入 校验 重复数据处理",
    "integration": "接口集成 超时重试 幂等 官方文档",
    "accessibility": "Web 表单 无障碍 设计 官方指南",
}
PUBLIC_TOPIC_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "按公开主题查阅通用资料，查询由服务端生成，不传客户资料。",
        "parameters": {
            "type": "object",
            "properties": {"topic": {"type": "string", "enum": list(PUBLIC_TOPICS)}},
            "required": ["topic"],
            "additionalProperties": False,
        },
    },
}

WEB_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "查询公开行业背景或官方接口资料；不得提交客户身份、私密业务资料或凭证。"
            "只返回搜索摘要，不能代客户确定业务事实。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 2, "maxLength": 120}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}


def public_url(value):
    if value.startswith("//"):
        value = "https:" + value
    parsed = urlsplit(value)
    if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
        value = parse_qs(parsed.query).get("uddg", [value])[0]
        parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return None
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return None
    try:
        if not ipaddress.ip_address(host).is_global:
            return None
    except ValueError:
        if "." not in host:
            return None
    return value[:2000]


class ResultsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.current, self.capture = [], None, ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "a" and "result__a" in classes:
            url = public_url(attrs.get("href", ""))
            self.current = {"url": url, "title": "", "snippet": ""} if url else None
            if self.current:
                self.rows.append(self.current)
            self.capture = "title"
        elif "result__snippet" in classes:
            self.capture = "snippet"

    def handle_data(self, data):
        if self.current and self.capture:
            self.current[self.capture] += data

    def handle_endtag(self, tag):
        if tag in {"a", "div", "td"}:
            self.capture = ""


async def search_public(client, query, *, request_seconds=6):
    if not isinstance(query, str) or not 2 <= len(query.strip()) <= 120:
        raise ValueError("查询长度不符合要求")
    if re.search(r"@|\b\d{7,}\b|(?:sk-|tvly-|Bearer\s)", query, re.I):
        raise ValueError("查询可能包含身份或凭证信息")
    chunks, size = [], 0
    async with (
        asyncio.timeout(request_seconds),
        client.stream(
            "GET",
            "https://html.duckduckgo.com/html/",
            params={"q": query.strip()},
            timeout=request_seconds,
            follow_redirects=False,
            headers={"User-Agent": "SupportFlow/1.0"},
        ) as response,
    ):
        response.raise_for_status()
        if response.status_code != 200:
            raise httpx.HTTPStatusError(
                "Search unavailable", request=response.request, response=response
            )
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > 1_000_000:
                raise ValueError("搜索响应超过限制")
            chunks.append(chunk)
    parser = ResultsParser()
    parser.feed(b"".join(chunks).decode("utf-8", errors="replace"))
    result, seen = [], set()
    for row in parser.rows:
        if row["url"] in seen or not row["title"].strip():
            continue
        seen.add(row["url"])
        result.append(
            Reference(
                id="web-" + hashlib.sha256(row["url"].encode()).hexdigest()[:12],
                title=row["title"].strip()[:300],
                url=row["url"],
                snippet=row["snippet"].strip()[:1200],
                fetched_at=datetime.now(UTC).isoformat(),
            ).model_dump()
        )
        if len(result) == 3:
            break
    return result
