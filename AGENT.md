# iAi Search Service — Agent Notes

## Stack
Python 3.13, FastAPI, HTTPX, Trafilatura and SearXNG. Docker Compose is the deployment unit.

## Purpose
External, self-hosted web-research tool for the iAi Agent Platform. It searches through SearXNG, retrieves public HTTP(S) pages, extracts readable text and returns structured sources. It never asks an LLM to synthesize an answer.

## Key Files
- `src/iai_search_service/main.py`: HTTP boundary and bearer authentication.
- `src/iai_search_service/service.py`: search, fetch, extraction, limits and partial errors.
- `src/iai_search_service/security.py`: public-address validation and redirect checks.
- `docker-compose.yml`: Spark/Dokploy deployment.
- `searxng/settings.yml`: internal SearXNG JSON API configuration.

## Deployment
Dokploy Compose on Spark, Git source from `danielTr00/iai-search-service`, branch `main`. Only `127.0.0.1:8090` is published. The iAi backend calls `http://host.docker.internal:8090` with the shared bearer token. No public router or domain is required.

## Security Status
Bearer token required for `/v1/research`; `/health` is public. URL schemes, credentials, resolved addresses and redirects are checked before retrieval. Response size, extracted content, parallel requests and container resources are bounded. Remaining limitation: DNS rebinding cannot be completely excluded by application-level pre-resolution alone; deployment egress rules are the stronger future control.

## Health Endpoint
`GET /health` returns `200 {"status":"ok"}` without authentication.

## Decisions & Rationale
The service owns discovery and extraction but not answer synthesis. SearXNG and Trafilatura stay behind one stable API so engines or a future isolated browser worker can change without modifying the agent application. Direct HTTP is used before MCP because the consumer is one known backend and the contract is smaller.

## Next Steps / Open Items
- Add an isolated browser worker only when measured extraction failures justify its memory cost.
- Add egress firewall enforcement for defense in depth against DNS rebinding.
- Add a bounded TTL cache after real repeated-query measurements.
