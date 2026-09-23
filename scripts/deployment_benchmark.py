from __future__ import annotations

import json
import os
import statistics
import time
import urllib.error
import urllib.request

ENDPOINT = os.getenv("SEARCH_BENCHMARK_URL", "http://search-api:8080/v1/research")
TOKEN = os.environ["SEARCH_API_TOKEN"]
QUERIES = (
    "SearXNG official documentation",
    "Python asyncio official documentation",
    "FastAPI security official documentation",
)


def research(query: str) -> dict:
    payload = json.dumps({"task": query, "max_sources": 3}).encode()
    request = urllib.request.Request(
        ENDPOINT,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=45) as response:
        result = json.load(response)
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    sources = result.get("sources", [])
    return {
        "query": query,
        "duration_ms": elapsed_ms,
        "sources": len(sources),
        "extracted": sum(source.get("status") == "extracted" for source in sources),
        "partial_errors": len(result.get("errors", [])),
    }


def main() -> None:
    warmup = research(QUERIES[0])
    print(json.dumps({"phase": "warmup", **warmup}), flush=True)

    results = []
    for index in range(6):
        result = research(QUERIES[index % len(QUERIES)])
        results.append(result)
        print(json.dumps({"phase": "measurement", "run": index + 1, **result}), flush=True)

    durations = sorted(item["duration_ms"] for item in results)
    summary = {
        "phase": "summary",
        "runs": len(results),
        "median_ms": round(statistics.median(durations)),
        "mean_ms": round(statistics.mean(durations)),
        "min_ms": min(durations),
        "max_ms": max(durations),
        "total_sources": sum(item["sources"] for item in results),
        "total_extracted": sum(item["extracted"] for item in results),
        "total_partial_errors": sum(item["partial_errors"] for item in results),
    }
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(json.dumps({"phase": "failed", "error": type(exc).__name__}), flush=True)
        raise
