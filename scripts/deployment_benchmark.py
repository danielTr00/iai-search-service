from __future__ import annotations

import json
import os
import statistics
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

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


def probe() -> list[dict]:
    """Exercise both agent-facing routes on realistic queries and source pages."""
    base = ENDPOINT.removesuffix("/v1/research")
    requests = [
        ("short_search", "/search", {
            "query": "EU premium vehicle software defined vehicle regulation 2030",
            "search_depth": "advanced", "max_results": 3,
        }),
        ("agent_search", "/search", {
            "query": (
                "Find authoritative, verifiable sources relevant to measurable developments "
                "in the EU premium passenger vehicle market through 2030, focused on "
                "software-defined vehicles, automotive software, connectivity, automated driving, "
                "and regulatory requirements. Include sources from EU institutions, European "
                "Commission, UNECE, ACEA, BMW, Mercedes-Benz, reputable market research if available. "
                "Return URLs, publication dates and specific measurable claims."
            ),
            "search_depth": "advanced", "max_results": 3,
        }),
        ("known_urls", "/extract", {
            "urls": [
                "https://eur-lex.europa.eu/eli/reg/2024/1689/oj",
                "https://www.bmwgroup.com/en/company/strategy.html",
                "https://docs.searxng.org/",
            ], "extract_depth": "basic", "format": "text",
        }),
    ]
    results = []
    for label, path, payload in requests:
        request = urllib.request.Request(
            base + path, data=json.dumps(payload).encode(), method="POST",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=90) as response:
            value = json.load(response)
        if not value.get("results"):
            raise ValueError(f"{label} returned no results")
        if path == "/search" and not any(
            urlsplit(item.get("url", "")).hostname != "accounts.google.com"
            for item in value["results"]
        ):
            raise ValueError(f"{label} returned no usable sources")
        results.append({
            "probe": label,
            "results": len(value.get("results", [])),
            "hosts": [urlsplit(item.get("url", "")).hostname for item in value.get("results", [])],
            "failed_results": [item["error"] for item in value.get("failed_results", [])],
        })
    return results


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
    for result in probe():
        print(json.dumps({"phase": "probe", **result}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(json.dumps({"phase": "failed", "error": type(exc).__name__}), flush=True)
        raise
