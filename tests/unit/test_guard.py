"""Production guard: fail closed, block default branch, allow verified non-default branch."""

import httpx
import pytest

from app.config import ConfigError
from app.db.guard import assert_not_production, endpoint_id_from_host

URL = "postgresql://u:p@ep-cool-name-123-pooler.us-east-2.aws.neon.tech/neondb"


def _client(branch: dict) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/endpoints"):
            return httpx.Response(
                200, json={"endpoints": [{"id": "ep-cool-name-123", "branch_id": "br-1"}]}
            )
        return httpx.Response(200, json={"branch": branch})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_endpoint_id_strips_pooler():
    assert endpoint_id_from_host("ep-a-1-pooler.x.neon.tech") == "ep-a-1"


def test_fails_closed_without_api_credentials():
    with pytest.raises(ConfigError, match="NEON_API_KEY"):
        assert_not_production(URL, neon_api_key="", neon_project_id="")


@pytest.mark.parametrize(
    "branch", [{"name": "main", "default": True}, {"name": "prod"}, {"name": "x", "primary": True}]
)
def test_blocks_production_branch(branch):
    with pytest.raises(ConfigError, match="Refusing"):
        assert_not_production(URL, neon_api_key="k", neon_project_id="p", client=_client(branch))


def test_allows_dev_branch():
    name = assert_not_production(
        URL,
        neon_api_key="k",
        neon_project_id="p",
        client=_client({"name": "test", "default": False}),
    )
    assert name == "test"


def test_unknown_endpoint_is_refused():
    def handler(req):
        return httpx.Response(200, json={"endpoints": []})

    with pytest.raises(ConfigError, match="not found"):
        assert_not_production(
            URL,
            neon_api_key="k",
            neon_project_id="p",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
