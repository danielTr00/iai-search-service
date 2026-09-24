"""Internal DNS aliases stay stable across Dokploy Compose redeployments."""
from pathlib import Path


def test_research_services_have_explicit_dns_aliases():
    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()
    search, remainder = compose.split("\n  searxng:", 1)
    searx = remainder.split("\n  deployment-benchmark:", 1)[0]
    assert "networks:\n      searchnet:\n        aliases: [search-api]" in search
    assert "networks:\n      searchnet:\n        aliases: [searxng]" in searx
