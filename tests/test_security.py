import pytest

from iai_search_service.security import UnsafeUrlError, validate_public_url


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin",
    "http://169.254.169.254/latest/meta-data",
    "http://10.0.0.5/internal",
    "file:///etc/passwd",
])
async def test_private_and_non_http_targets_are_rejected(url):
    with pytest.raises(UnsafeUrlError):
        await validate_public_url(url)


async def test_public_url_is_accepted_without_returning_credentials():
    async def resolver(host, port):
        return ["93.184.216.34"]

    assert await validate_public_url("https://example.com/page", resolver=resolver) == "https://example.com/page"


async def test_url_credentials_are_rejected():
    with pytest.raises(UnsafeUrlError):
        await validate_public_url("https://user:pass@example.com")


async def test_mixed_public_and_private_dns_answers_are_rejected():
    async def resolver(host, port):
        return ["93.184.216.34", "127.0.0.1"]

    with pytest.raises(UnsafeUrlError):
        await validate_public_url("https://mixed.example", resolver=resolver)
