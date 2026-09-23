# iAi Search Service

Self-hosted web research for the iAi Agent Platform. The service combines SearXNG discovery with Trafilatura extraction and returns sources and partial failures as JSON. It has no LLM dependency and no Tavily fallback.

## API

```http
GET /health
POST /v1/research
Authorization: Bearer $SEARCH_API_TOKEN
Content-Type: application/json
```

```json
{
  "task": "open source metasearch",
  "urls": [],
  "depth": "standard",
  "max_sources": 3,
  "freshness": "any",
  "allowed_domains": []
}
```

The response identifies the strategy, extracted sources, per-source status, partial errors, duration and a request ID. The calling agent remains responsible for evaluating and citing the sources.

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

Default hard limits are 512 MiB for the API and 768 MiB for SearXNG, with two concurrent research requests. No browser, Redis, database, vector store or secondary model is required.

## Scope

Included: search, direct URL extraction, source filtering, redirects, partial failures and SSRF-oriented public-target validation. Not included: paywall/CAPTCHA bypass, authenticated browser sessions, a proprietary answer-ranking model or a private web index.
