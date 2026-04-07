"""
Tests para POD_FAILED en _GITOPS_FIXABLE y LLM-only patch path.
"""
import pytest
from agents.sre.healer import _GITOPS_FIXABLE
from agents.devops.camael_analyzer import _ISSUE_DESCRIPTIONS


class TestPodFailedGitopsFixable:
    def test_pod_failed_in_gitops_fixable(self):
        assert "POD_FAILED" in _GITOPS_FIXABLE

    def test_all_expected_issue_types_present(self):
        expected = {
            "OOM_KILLED", "CRASH_LOOP", "DEPLOYMENT_DEGRADED",
            "HIGH_MEMORY", "HIGH_CPU", "HIGH_RESTARTS",
            "MEMORY_LEAK_PREDICTED", "POD_FAILED",
        }
        assert expected.issubset(_GITOPS_FIXABLE)


class TestPodFailedAnalyzerDescription:
    def test_pod_failed_has_description(self):
        assert "POD_FAILED" in _ISSUE_DESCRIPTIONS

    def test_pod_failed_description_mentions_logs(self):
        desc = _ISSUE_DESCRIPTIONS["POD_FAILED"]
        assert "log" in desc.lower() or "logs" in desc.lower()
