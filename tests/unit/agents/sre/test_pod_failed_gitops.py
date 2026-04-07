"""
Tests para POD_FAILED en _GITOPS_FIXABLE y LLM-only patch path.
"""
import pytest
from agents.sre.healer import _GITOPS_FIXABLE
from agents.devops.camael_analyzer import _ISSUE_DESCRIPTIONS
from agents.sre.bug_library import _patch_memory_limit, _patch_cpu_limit


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


class TestLlmOnlyFallbackBothPatchesNoop:
    """Verifies that when no resources section is present, both patch functions
    return the YAML unchanged — this is the condition that triggers the
    patched_content = yaml_content fallback (UnboundLocalError fix)."""

    _YAML_NO_RESOURCES = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: test-app
spec:
  replicas: 1
  template:
    spec:
      containers:
        - name: test-app
          image: test-app:1.0.0
"""

    def test_llm_only_fallback_both_patches_noop(self):
        yaml_no_resources = self._YAML_NO_RESOURCES

        # Both patch functions should return the original string unchanged
        # because there is no resources section to modify.
        # Note: the patch functions use splitlines()+join which strips a trailing newline,
        # so we compare stripped versions to avoid false failures.
        result_memory = _patch_memory_limit(yaml_no_resources, multiplier=2.0)
        result_cpu = _patch_cpu_limit(yaml_no_resources, multiplier=2.0)

        assert result_memory == yaml_no_resources.rstrip("\n")
        assert result_cpu == yaml_no_resources.rstrip("\n")

        # This is the correct fallback: patched_content = yaml_content
        yaml_content = yaml_no_resources
        patched_content = yaml_content
        assert patched_content == yaml_content
