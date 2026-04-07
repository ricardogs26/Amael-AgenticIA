"""
Tests para discovery dinámico de YAML en Bitbucket.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agents.sre.bug_library import is_known_resource


class TestIsKnownResource:
    def test_known_exact_name(self):
        assert is_known_resource("amael-agentic-deployment") is True

    def test_known_by_prefix(self):
        assert is_known_resource("amael-agentic-deployment-7d9fab12-xk2vp") is True

    def test_unknown_returns_false(self):
        assert is_known_resource("amael-demo-oom") is False

    def test_empty_string_returns_false(self):
        assert is_known_resource("") is False

    def test_podinfo_known(self):
        assert is_known_resource("podinfo") is True


def _make_mock_client(mock_response: MagicMock):
    """Helper: returns a patched AsyncClient context manager yielding mock_response on .get()."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)
    return mock_client


class TestSearchFileInRepo:
    @pytest.mark.asyncio
    async def test_returns_path_when_found(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "values": [
                {
                    "type": "code_search_result",
                    "file": {"path": "k8s/demo/amael-demo-oom.yaml"},
                    "content_matches": [{"lines": [{"line": "  name: amael-demo-oom"}]}],
                }
            ]
        }

        with patch("agents.devops.bitbucket_client._auth", return_value=("user", "token")), \
             patch("agents.devops.bitbucket_client.httpx.AsyncClient", return_value=_make_mock_client(mock_response)):
            from agents.devops.bitbucket_client import search_file_in_repo
            result = await search_file_in_repo("ws", "repo", "amael-demo-oom")

        assert result == "k8s/demo/amael-demo-oom.yaml"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"values": []}

        with patch("agents.devops.bitbucket_client._auth", return_value=("user", "token")), \
             patch("agents.devops.bitbucket_client.httpx.AsyncClient", return_value=_make_mock_client(mock_response)):
            from agents.devops.bitbucket_client import search_file_in_repo
            result = await search_file_in_repo("ws", "repo", "nonexistent-deployment")

        assert result is None

    @pytest.mark.asyncio
    async def test_filters_non_yaml_results(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "values": [
                {
                    "type": "code_search_result",
                    "file": {"path": "docs/README.md"},
                    "content_matches": [],
                },
                {
                    "type": "code_search_result",
                    "file": {"path": "k8s/demo/amael-demo-oom.yaml"},
                    "content_matches": [],
                },
            ]
        }

        with patch("agents.devops.bitbucket_client._auth", return_value=("user", "token")), \
             patch("agents.devops.bitbucket_client.httpx.AsyncClient", return_value=_make_mock_client(mock_response)):
            from agents.devops.bitbucket_client import search_file_in_repo
            result = await search_file_in_repo("ws", "repo", "amael-demo-oom")

        assert result == "k8s/demo/amael-demo-oom.yaml"

    @pytest.mark.asyncio
    async def test_returns_none_on_404(self):
        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch("agents.devops.bitbucket_client._auth", return_value=("user", "token")), \
             patch("agents.devops.bitbucket_client.httpx.AsyncClient", return_value=_make_mock_client(mock_response)):
            from agents.devops.bitbucket_client import search_file_in_repo
            result = await search_file_in_repo("ws", "repo", "any-resource")

        assert result is None

    @pytest.mark.asyncio
    async def test_handles_malformed_file_key(self):
        """API result without 'file' key should be skipped, not raise."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "values": [
                {"type": "code_search_result"},  # no 'file' key
            ]
        }

        with patch("agents.devops.bitbucket_client._auth", return_value=("user", "token")), \
             patch("agents.devops.bitbucket_client.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_cls.return_value = mock_client

            from agents.devops.bitbucket_client import search_file_in_repo
            result = await search_file_in_repo("ws", "repo", "some-deployment")

        assert result is None
