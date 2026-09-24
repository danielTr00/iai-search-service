# iAi Search Service

Self-hosted web research for the iAi Agent Platform. The service combines SearXNG discovery with Trafilatura extraction and returns sources and partial failures as JSON. It has no LLM dependency and no Tavily fallback.

## API

```http
GET /health
POST /search
POST /extract
POST /v1/research
Authorization: Bearer ***
Content-Type: application/json
```

`POST /search` follows the core Tavily Search request and response vocabulary while remaining fully self-hosted:

```json
{
  "query": "open source metasearch",
  "search_depth": "basic",
  "topic": "general",
  "time_range": "month",
  "max_results": 5,
  "include_domains": [],
  "exclude_domains": [],
  "include_raw_content": false
}
```

Supported search depths are `ultra-fast`, `fast`, `basic` and `advanced`. The two fast modes return SearXNG results and snippets without fetching each page; `basic` and `advanced` also fetch and extract page content. The response uses `query`, `results`, `response_time` and `request_id`, with `title`, `url`, `content`, `score` and optional `raw_content` per result.

`include_raw_content` currently accepts a boolean; hosted format selectors such as `"markdown"` or `"text"` are rejected rather than silently approximated. `POST /v1/research` remains available for direct URL extraction, partial-error details and backwards compatibility. This is a compatible core subset, not a promise of complete behavioral parity with Tavily's hosted ranking, answer generation or image search.

`POST /extract` follows Tavily's core URL-extraction contract (Bearer authentication, a single URL or 1–8 URLs, `extract_depth` of `basic` or `advanced`, and `format` of `markdown` or `text`). It returns `results` with `url` and `raw_content`, `failed_results` with per-URL errors, `response_time`, and `request_id`. The default format is `markdown`; advanced extraction also attempts tables and uses recall-focused extraction. Other hosted-only options are rejected with 422 instead of silently ignored. The 8-URL cap keeps resource usage predictable; Tavily's hosted API allows larger batches.

## Cache

Successful extracted sources are cached **per normalized URL (fragment removed), extraction depth, output format and domain policy** for 6 hours by default. This cache is shared by `/search`, `/extract` and `/v1/research`; titles and snippets remain specific to each new query. Search-engine result lists have a separate 5-minute cache keyed by query, topic, freshness, depth and result limit. Concurrent requests for the same key share a single upstream operation. Failures and empty search results are not cached, and every request gets a new request ID.

The caches use bounded in-process memory (source: 256 entries / 16 MB; search: 128 entries / 2 MB), evict least-recently-used entries and expire entries by TTL. `SEARCH_SOURCE_CACHE_{TTL_SECONDS,MAX_ENTRIES,MAX_BYTES}` and `SEARCH_QUERY_CACHE_{TTL_SECONDS,MAX_ENTRIES,MAX_BYTES}` configure the bounds; TTL 0 disables that cache. Caches are **not persistent or shared across replicas**: a service restart starts cold. This avoids an extra Redis/database dependency on the constrained Spark host; persistent/shared caching is a separate scaling step if needed.

## Local development

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest
cp .env.example .env
# set SEARCH_API_TOKEN, then:
docker compose up --build
```

The API is available only on `127.0.0.1:8090` by default.

## Resource profile

Default hard limits are 512 MiB for the API and 768 MiB for SearXNG, with two concurrent research requests. Cache data is included in the API's 512 MiB limit. No browser, Redis, database, vector store or secondary model is required.

## Scope

Included: search, direct URL extraction, source filtering, redirects, partial failures and SSRF-oriented public-target validation. Not included: paywall/CAPTCHA bypass, authenticated browser sessions, a proprietary answer-ranking model or a private web index.
