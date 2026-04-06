"""
Tests unitarios para agents/sre/bug_library.py

Cubre:
  - get_fix() con resource_name exacto
  - get_fix() con resource_name fuzzy (sufijo hash de pod)
  - get_fix() con resource_name vacío → fallback sin crash
  - get_fix() con resource_name desconocido → fallback a default
  - get_fix() con issue_type inválido → None
  - BugFixTemplate no tiene campos repo/file_path (no rompe instantiación)
  - APP_MANIFEST_MAP cubre todas las apps del cluster
  - patch_fn no modifica YAML estructuralmente inválido (devuelve mismo texto)
"""
import pytest
from agents.sre.bug_library import (
    APP_MANIFEST_MAP,
    BUG_LIBRARY,
    BugFix,
    BugFixTemplate,
    _DEFAULT_APP,
    get_fix,
)


# ── get_fix — casos básicos ───────────────────────────────────────────────────

class TestGetFixBasic:
    def test_returns_none_for_unknown_issue_type(self):
        assert get_fix("NONEXISTENT_ISSUE", "amael-agentic-backend") is None

    def test_returns_bugfix_for_known_issue(self):
        fix = get_fix("OOM_KILLED", "amael-agentic-backend")
        assert fix is not None
        assert isinstance(fix, BugFix)

    def test_bugfix_has_correct_issue_type(self):
        fix = get_fix("HIGH_MEMORY", "amael-agentic-backend")
        assert fix.issue_type == "HIGH_MEMORY"

    def test_all_issue_types_in_bug_library_resolve(self):
        for issue_type in BUG_LIBRARY:
            fix = get_fix(issue_type, "amael-agentic-backend")
            assert fix is not None, f"get_fix({issue_type!r}) returned None"
            assert fix.repo, f"fix.repo is empty for {issue_type}"
            assert fix.file_path, f"fix.file_path is empty for {issue_type}"
            assert callable(fix.patch_fn)

    def test_bugfix_template_has_no_repo_field(self):
        template = BUG_LIBRARY["OOM_KILLED"]
        assert isinstance(template, BugFixTemplate)
        assert not hasattr(template, "repo")
        assert not hasattr(template, "file_path")


# ── get_fix — resolución de resource_name ────────────────────────────────────

class TestGetFixResourceResolution:
    def test_exact_match_uses_correct_manifest(self):
        fix = get_fix("OOM_KILLED", "k8s-agent")
        assert fix.file_path == "k8s/19.-k8s-agent-deployment.yaml"

    def test_exact_match_deployment_suffix(self):
        fix = get_fix("OOM_KILLED", "k8s-agent-deployment")
        assert fix.file_path == "k8s/19.-k8s-agent-deployment.yaml"

    def test_fuzzy_match_pod_hash_suffix(self):
        # Pod name real incluye sufijos como "k8s-agent-7d9f-xxxxx"
        fix = get_fix("OOM_KILLED", "k8s-agent-7d9fab12-xk2vp")
        assert fix.file_path == "k8s/19.-k8s-agent-deployment.yaml"

    def test_fuzzy_match_whatsapp_bridge(self):
        fix = get_fix("HIGH_MEMORY", "whatsapp-deployment-abc123-xyz")
        assert fix.file_path == "k8s/08.-whatsapp-deployment.yaml"

    def test_frontend_next_resolves(self):
        fix = get_fix("HIGH_CPU", "frontend-next")
        assert fix.file_path == "k8s/27.-frontend-next-deployment.yaml"

    def test_productivity_service_resolves(self):
        fix = get_fix("HIGH_RESTARTS", "productivity-service")
        assert fix.file_path == "k8s/15.-productivity-deployment.yaml"

    def test_unknown_resource_uses_default(self):
        fix = get_fix("OOM_KILLED", "some-unknown-service-xyz")
        assert fix is not None
        assert fix.repo == _DEFAULT_APP.repo
        assert fix.file_path == _DEFAULT_APP.file_path

    def test_empty_resource_name_uses_default_without_crash(self):
        # Bug original: empty string causaba fuzzy match no-determinístico
        fix = get_fix("OOM_KILLED", "")
        assert fix is not None
        assert fix.repo == _DEFAULT_APP.repo

    def test_no_resource_name_arg_uses_default(self):
        # resource_name tiene default=""
        fix = get_fix("OOM_KILLED")
        assert fix is not None
        assert fix.repo == _DEFAULT_APP.repo

    def test_single_char_resource_name_uses_default(self):
        # Prefijo de 1 char no debe triggear fuzzy (umbral 4 chars)
        fix = get_fix("OOM_KILLED", "a")
        assert fix.repo == _DEFAULT_APP.repo

    def test_short_prefix_resource_name_uses_default(self):
        # Prefijo de 3 chars (< umbral 4) → no fuzzy
        fix = get_fix("OOM_KILLED", "abc-deployment")
        assert fix.repo == _DEFAULT_APP.repo


# ── APP_MANIFEST_MAP ──────────────────────────────────────────────────────────

class TestAppManifestMap:
    def test_map_is_not_empty(self):
        assert len(APP_MANIFEST_MAP) > 0

    def test_all_manifests_have_repo_and_file_path(self):
        for name, manifest in APP_MANIFEST_MAP.items():
            assert manifest.repo, f"APP_MANIFEST_MAP[{name!r}].repo is empty"
            assert manifest.file_path, f"APP_MANIFEST_MAP[{name!r}].file_path is empty"

    def test_amael_agentic_backend_present(self):
        assert "amael-agentic-backend" in APP_MANIFEST_MAP

    def test_k8s_agent_present(self):
        assert "k8s-agent" in APP_MANIFEST_MAP

    def test_whatsapp_present(self):
        assert "whatsapp-bridge" in APP_MANIFEST_MAP

    def test_frontend_next_present(self):
        assert "frontend-next" in APP_MANIFEST_MAP


# ── patch_fn — funciones de parchado ─────────────────────────────────────────

class TestPatchFunctions:
    SAMPLE_YAML = """
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      containers:
      - name: test
        resources:
          requests:
            cpu: "250m"
            memory: "256Mi"
          limits:
            cpu: "1000m"
            memory: "512Mi"
        livenessProbe:
          initialDelaySeconds: 30
          periodSeconds: 10
"""

    def test_oom_killed_patch_increases_memory(self):
        fix = get_fix("OOM_KILLED", "amael-agentic-backend")
        patched = fix.patch_fn(self.SAMPLE_YAML)
        assert "1536Mi" in patched  # 512 * 3 = 1536

    def test_high_memory_patch_doubles_memory(self):
        fix = get_fix("HIGH_MEMORY", "amael-agentic-backend")
        patched = fix.patch_fn(self.SAMPLE_YAML)
        # 512Mi * 2 = 1024Mi = 1Gi (formato normalizado por _format_memory)
        assert "1Gi" in patched

    def test_high_cpu_patch_doubles_cpu(self):
        fix = get_fix("HIGH_CPU", "amael-agentic-backend")
        patched = fix.patch_fn(self.SAMPLE_YAML)
        assert "2000m" in patched  # 1000 * 2 = 2000

    def test_high_restarts_patch_increases_delay(self):
        fix = get_fix("HIGH_RESTARTS", "amael-agentic-backend")
        patched = fix.patch_fn(self.SAMPLE_YAML)
        assert "60" in patched  # 30 + 30 = 60

    def test_patch_invalid_yaml_returns_unchanged(self):
        # Si no encuentra la sección, devuelve el mismo texto (no crash)
        fix = get_fix("OOM_KILLED", "amael-agentic-backend")
        bad_yaml = "this is not a valid deployment yaml"
        patched = fix.patch_fn(bad_yaml)
        assert patched == bad_yaml  # patch_fn no modifica lo que no encuentra

    def test_patch_preserves_other_lines(self):
        fix = get_fix("OOM_KILLED", "amael-agentic-backend")
        patched = fix.patch_fn(self.SAMPLE_YAML)
        assert "apiVersion: apps/v1" in patched
        assert "kind: Deployment" in patched
        assert "cpu: \"250m\"" in patched  # requests no cambian
