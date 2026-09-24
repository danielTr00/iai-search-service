from pathlib import Path

import yaml


def test_general_web_has_independent_search_engines():
    settings = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "searxng" / "settings.yml").read_text()
    )
    removed = set(settings["use_default_settings"]["engines"]["remove"])
    overrides = {engine["name"]: engine for engine in settings.get("engines", [])}
    assert {"brave", "google cse", "wikipedia", "wikidata"} <= removed
    assert {"bing", "qwant", "yahoo"} <= {
        name for name, entry in overrides.items()
        if entry.get("disabled") is False and entry.get("inactive", False) is False
    }
