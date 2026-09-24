"""The deployed API is reachable by the agent without exposing the research token."""
from pathlib import Path


def test_search_service_has_stable_private_network():
    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()
    assert "searchnet:\n    name: iai-research-net" in compose
    assert "networks: [searchnet]" in compose
    assert '"127.0.0.1:${SEARCH_PORT:-8090}:8080"' in compose
