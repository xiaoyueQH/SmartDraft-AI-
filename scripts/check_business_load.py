import argparse
import asyncio
import getpass
import json
import math
import sys
from collections import Counter
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from urllib.parse import urlparse

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]


async def run_load(
    credentials: list[tuple[str, str]],
    *,
    base_url: str = "http://127.0.0.1:8091",
    concurrency: int = 24,
    requests: int = 240,
) -> dict:
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("仅允许对本机 HTTP 演示服务运行")
    if not credentials or not 1 <= concurrency <= 128 or not concurrency <= requests <= 10000:
        raise ValueError("账号不能为空；并发范围 1..128，总请求数介于并发数和 10000 之间")
    scenarios, sessions, cleanup_errors = [], [], []
    async with AsyncExitStack() as stack:
        try:
            for username, password in credentials:
                client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        base_url=base_url, timeout=10, headers={"Origin": base_url}, trust_env=False
                    )
                )
                response = await client.post(
                    "/api/auth/login", json={"username": username, "password": password}
                )
                if response.status_code != 200:
                    raise RuntimeError(f"验收账号登录失败：HTTP {response.status_code}")
                bundle = response.json()
                client.headers.update(
                    {
                        "Authorization": "Bearer " + bundle["access_token"],
                        "X-Session-ID": bundle["session_id"],
                    }
                )
                sessions.append(client)
                admin = bundle["user"]["role"] == "admin"
                prefix = "/api/admin" if admin else "/api"
                listing = await client.get(prefix + "/orders")
                listing.raise_for_status()
                paths = [prefix + "/orders", prefix + "/returns", "/api/products"]
                if listing.json()["items"]:
                    paths.append(prefix + "/orders/" + listing.json()["items"][0]["id"])
                scenarios.extend((client, path, username, admin) for path in paths)
            timings, outcomes = [], Counter()
            ownership_errors = 0

            async def worker(worker_id):
                nonlocal ownership_errors
                for index in range(worker_id, requests, concurrency):
                    client, path, username, admin = scenarios[index % len(scenarios)]
                    started = perf_counter()
                    try:
                        response = await client.get(path)
                        if response.status_code == 200:
                            data = response.json()
                            if not admin and path != "/api/products":
                                rows = data.get("items", []) if "has_more" in data else [data]
                                ownership_errors += sum(
                                    row.get("customer_username") != username for row in rows
                                )
                        outcomes[str(response.status_code)] += 1
                    except (httpx.HTTPError, ValueError) as exc:
                        outcomes[type(exc).__name__] += 1
                    finally:
                        timings.append((perf_counter() - started) * 1000)

            started = perf_counter()
            await asyncio.gather(*(worker(index) for index in range(concurrency)))
            elapsed = perf_counter() - started
            ordered = sorted(timings)

            def percentile(percent):
                return round(ordered[max(0, math.ceil(len(ordered) * percent) - 1)], 2)

            return {
                "tested_at": datetime.now(UTC).isoformat(),
                "scope": "本机演示 HTTP 读取；不含模型、登录吞吐或真实付款物流",
                "accounts": len(credentials),
                "concurrency": concurrency,
                "requests": requests,
                "elapsed_seconds": round(elapsed, 3),
                "requests_per_second": round(requests / elapsed, 2),
                "p50_ms": percentile(0.5),
                "p95_ms": percentile(0.95),
                "p99_ms": percentile(0.99),
                "outcomes": dict(outcomes),
                "ownership_errors": ownership_errors,
                "cleanup_errors": cleanup_errors,
            }
        finally:
            for client in sessions:
                try:
                    response = await client.post("/api/auth/logout")
                    if response.status_code != 204:
                        cleanup_errors.append(f"HTTP {response.status_code}")
                except httpx.HTTPError as exc:
                    cleanup_errors.append(type(exc).__name__)
            if cleanup_errors:
                print("部分验收会话退出失败，请检查登录会话", file=sys.stderr)


async def main():
    parser = argparse.ArgumentParser(description="本机商品/订单/售后 HTTP 只读并发验收")
    parser.add_argument("--users", nargs="+", default=["admin", "user"])
    parser.add_argument("--concurrency", type=int, default=24)
    parser.add_argument("--requests", type=int, default=240)
    parser.add_argument("--base-url", default="http://127.0.0.1:8091")
    args = parser.parse_args()
    credentials = [(username, getpass.getpass(f"{username} 密码：")) for username in args.users]
    result = await run_load(
        credentials,
        base_url=args.base_url,
        concurrency=args.concurrency,
        requests=args.requests,
    )
    directory = PROJECT_ROOT / "storage/verification"
    directory.mkdir(parents=True, exist_ok=True)
    report = directory / "business-load.json"
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"报告：{report}")
    if (
        result["outcomes"] != {"200": args.requests}
        or result["ownership_errors"]
        or result["cleanup_errors"]
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
