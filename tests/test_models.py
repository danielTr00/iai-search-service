import pytest
from pydantic import ValidationError

from iai_search_service.models import ResearchRequest, SearchRequest


@pytest.mark.parametrize(
    "model,field,required",
    [
        (ResearchRequest, "allowed_domains", {"task": "research"}),
        (ResearchRequest, "excluded_domains", {"task": "research"}),
        (SearchRequest, "include_domains", {"query": "research"}),
        (SearchRequest, "exclude_domains", {"query": "research"}),
    ],
)
@pytest.mark.parametrize("domain", ["docs.python.org OR example.org", "docs.python.org\nexample.org"])
def test_domain_lists_reject_operator_injection(model, field, required, domain):
    with pytest.raises(ValidationError):
        model(**required, **{field: [domain]})


@pytest.mark.parametrize(
    "model,field,required",
    [
        (ResearchRequest, "allowed_domains", {"task": "research"}),
        (ResearchRequest, "excluded_domains", {"task": "research"}),
        (SearchRequest, "include_domains", {"query": "research"}),
        (SearchRequest, "exclude_domains", {"query": "research"}),
    ],
)
def test_domain_lists_normalize_valid_hosts(model, field, required):
    result = model(**required, **{field: ["Docs.Python.org.", "sub.example.org"]})
    assert getattr(result, field) == ["docs.python.org", "sub.example.org"]


@pytest.mark.parametrize(
    "model,field,required",
    [
        (ResearchRequest, "allowed_domains", {"task": "research"}),
        (ResearchRequest, "excluded_domains", {"task": "research"}),
        (SearchRequest, "include_domains", {"query": "research"}),
        (SearchRequest, "exclude_domains", {"query": "research"}),
    ],
)
def test_domain_lists_idna_normalize(model, field, required):
    result = model(**required, **{field: ["bücher.de"]})
    assert getattr(result, field) == ["xn--bcher-kva.de"]


@pytest.mark.parametrize(
    "domain",
    ["-bad.example", "bad-.example", "bad..example", "example.org:443", "https://example.org", "example.org/path", "bad host.example", "bad\thost.example"],
)
def test_domain_lists_reject_invalid_host_syntax(domain):
    with pytest.raises(ValidationError):
        ResearchRequest(task="research", allowed_domains=[domain])
    with pytest.raises(ValidationError):
        SearchRequest(query="research", include_domains=[domain])
