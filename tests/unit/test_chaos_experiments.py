"""
Tests unitarios para la suite de experimentos Chaos Mesh.

Valida:
  - Estructura YAML de cada experimento (campos requeridos presentes)
  - Seguridad: ningún experimento apunta a servicios críticos (k8s-agent, postgres, redis, qdrant, ollama)
  - Duración acotada: ningún experimento corre más de 15 minutos
  - Cobertura: los 7 issue_type del SRE agent tienen al menos un experimento
  - Especificidad: todos los selectores incluyen labelSelectors (no sólo namespaces)
  - Schedules: intervalos no más frecuentes que cada 30 minutos
  - Schedules: todos tienen concurrencyPolicy: Forbid
  - Schedules: historyLimit definido
  - Mapeo completo: cada experimento tiene su label issue-type alineado con BUG_LIBRARY
"""
import re
from pathlib import Path
import pytest
import yaml

# ── Paths ─────────────────────────────────────────────────────────────────────

EXPERIMENTS_DIR = Path(__file__).parents[3] / "GitOps-Infra" / "chaos-mesh" / "experiments"

# Archivos de la suite SRE (11-17 = experimentos individuales, 31 = schedules)
SRE_EXPERIMENT_FILES = sorted(EXPERIMENTS_DIR.glob("1[1-7]-*.yaml"))
SCHEDULE_FILE = EXPERIMENTS_DIR / "31-sre-training-schedule.yaml"

# Servicios críticos que NO deben ser target de ningún experimento
CRITICAL_APPS = {
    "amael-agentic-backend",
    "k8s-agent",
    "postgres",
    "redis",
    "qdrant",
    "minio",
    "ollama",
}

# Los 7 issue_types que el SRE agent puede detectar y que cubren los experimentos
EXPECTED_ISSUE_TYPES = {
    "OOM_KILLED",
    "HIGH_MEMORY",
    "HIGH_CPU",
    "HIGH_RESTARTS",
    "POD_FAILED",
    "HIGH_ERROR_RATE",
    "MEMORY_LEAK_PREDICTED",
}

# Duraciones: convierte "3m", "10m", "1h" → segundos
def _parse_duration(d: str) -> int:
    m = re.match(r"^(\d+)(s|m|h)$", d)
    assert m, f"Formato de duración inesperado: {d!r}"
    value, unit = int(m.group(1)), m.group(2)
    return value * {"s": 1, "m": 60, "h": 3600}[unit]


def _load_experiment_docs(path: Path) -> list[dict]:
    """Carga todos los documentos YAML de un archivo (puede ser multi-doc)."""
    with path.open() as f:
        return [doc for doc in yaml.safe_load_all(f) if doc]


def _all_experiment_docs() -> list[tuple[str, dict]]:
    """Devuelve (filename, doc) para cada documento de experimento individual."""
    result = []
    for path in SRE_EXPERIMENT_FILES:
        for doc in _load_experiment_docs(path):
            result.append((path.name, doc))
    return result


def _schedule_docs() -> list[tuple[str, dict]]:
    """Devuelve (filename, doc) para cada Schedule en el archivo 31."""
    result = []
    for doc in _load_experiment_docs(SCHEDULE_FILE):
        result.append((SCHEDULE_FILE.name, doc))
    return result


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def experiment_docs():
    return _all_experiment_docs()


@pytest.fixture(scope="module")
def schedule_docs():
    return _schedule_docs()


# ── Tests de estructura general ───────────────────────────────────────────────

class TestExperimentFilesExist:
    def test_experiment_files_count(self):
        """Deben existir exactamente 7 archivos de experimentos individuales."""
        assert len(SRE_EXPERIMENT_FILES) == 7, (
            f"Se esperaban 7 archivos, hay {len(SRE_EXPERIMENT_FILES)}: {[f.name for f in SRE_EXPERIMENT_FILES]}"
        )

    def test_schedule_file_exists(self):
        assert SCHEDULE_FILE.exists(), f"No existe {SCHEDULE_FILE}"

    def test_all_experiment_files_are_valid_yaml(self):
        for path in SRE_EXPERIMENT_FILES:
            docs = _load_experiment_docs(path)
            assert len(docs) >= 1, f"{path.name} está vacío"

    def test_schedule_file_has_7_documents(self):
        docs = _load_experiment_docs(SCHEDULE_FILE)
        assert len(docs) == 7, (
            f"31-sre-training-schedule.yaml debe tener 7 Schedules (uno por día), tiene {len(docs)}"
        )


class TestRequiredFields:
    """Cada experimento debe tener los campos requeridos por Chaos Mesh."""

    def test_has_apiVersion(self, experiment_docs):
        for fname, doc in experiment_docs:
            assert "apiVersion" in doc, f"{fname}: falta apiVersion"
            assert doc["apiVersion"] == "chaos-mesh.org/v1alpha1", f"{fname}: apiVersion incorrecto"

    def test_has_kind(self, experiment_docs):
        valid_kinds = {"StressChaos", "PodChaos", "HTTPChaos", "NetworkChaos", "DNSChaos"}
        for fname, doc in experiment_docs:
            assert "kind" in doc, f"{fname}: falta kind"
            assert doc["kind"] in valid_kinds, f"{fname}: kind={doc['kind']!r} no es un tipo chaos válido"

    def test_has_metadata_name(self, experiment_docs):
        for fname, doc in experiment_docs:
            assert doc.get("metadata", {}).get("name"), f"{fname}: falta metadata.name"

    def test_has_metadata_namespace_chaos_testing(self, experiment_docs):
        for fname, doc in experiment_docs:
            ns = doc.get("metadata", {}).get("namespace")
            assert ns == "chaos-testing", f"{fname}: namespace debe ser 'chaos-testing', es {ns!r}"

    def test_has_spec(self, experiment_docs):
        for fname, doc in experiment_docs:
            assert "spec" in doc, f"{fname}: falta spec"

    def test_has_spec_duration(self, experiment_docs):
        for fname, doc in experiment_docs:
            assert "duration" in doc["spec"], f"{fname}: falta spec.duration"

    def test_has_spec_mode(self, experiment_docs):
        for fname, doc in experiment_docs:
            assert "mode" in doc["spec"], f"{fname}: falta spec.mode"

    def test_has_spec_selector(self, experiment_docs):
        for fname, doc in experiment_docs:
            assert "selector" in doc["spec"], f"{fname}: falta spec.selector"


# ── Tests de seguridad ────────────────────────────────────────────────────────

class TestSafetyNoCriticalTargets:
    """Ningún experimento debe apuntar a servicios críticos."""

    def test_no_critical_app_in_label_selectors(self, experiment_docs):
        for fname, doc in experiment_docs:
            label_sel = doc["spec"].get("selector", {}).get("labelSelectors", {})
            app_target = label_sel.get("app", "")
            assert app_target not in CRITICAL_APPS, (
                f"{fname}: apunta a servicio crítico '{app_target}'"
            )

    def test_no_critical_app_in_expression_selectors(self, experiment_docs):
        for fname, doc in experiment_docs:
            exprs = doc["spec"].get("selector", {}).get("expressionSelectors", []) or []
            for expr in exprs:
                if expr.get("key") == "app":
                    for val in expr.get("values", []):
                        assert val not in CRITICAL_APPS, (
                            f"{fname}: expressionSelector apunta a servicio crítico '{val}'"
                        )

    def test_namespace_is_amael_ia_only(self, experiment_docs):
        """Los experimentos SRE solo deben afectar amael-ia, no observability ni vault."""
        for fname, doc in experiment_docs:
            namespaces = doc["spec"].get("selector", {}).get("namespaces", [])
            for ns in namespaces:
                assert ns == "amael-ia", (
                    f"{fname}: namespace '{ns}' fuera de amael-ia — riesgo de afectar infra"
                )


# ── Tests de duración ────────────────────────────────────────────────────────

class TestDurationBounds:
    MAX_DURATION_SECONDS = 15 * 60  # 15 minutos máximo
    MIN_DURATION_SECONDS = 2 * 60   # 2 minutos mínimo (necesita ≥2 ciclos SRE a 60s)

    def test_duration_within_bounds(self, experiment_docs):
        for fname, doc in experiment_docs:
            dur_str = doc["spec"].get("duration", "0s")
            dur_sec = _parse_duration(dur_str)
            assert dur_sec <= self.MAX_DURATION_SECONDS, (
                f"{fname}: duración {dur_str} excede máximo de 15min"
            )
            assert dur_sec >= self.MIN_DURATION_SECONDS, (
                f"{fname}: duración {dur_str} es menor que 2min — SRE no tendría tiempo de detectar"
            )


# ── Tests de especificidad del selector ──────────────────────────────────────

class TestSelectorSpecificity:
    """Cada experimento debe apuntar a una app específica, no a todo un namespace."""

    def test_has_label_selector_with_app(self, experiment_docs):
        for fname, doc in experiment_docs:
            label_sel = doc["spec"].get("selector", {}).get("labelSelectors", {})
            assert "app" in label_sel, (
                f"{fname}: el selector no tiene 'app' en labelSelectors — demasiado amplio"
            )

    def test_label_selector_app_not_empty(self, experiment_docs):
        for fname, doc in experiment_docs:
            app = doc["spec"].get("selector", {}).get("labelSelectors", {}).get("app", "")
            assert app, f"{fname}: labelSelectors.app está vacío"


# ── Tests de cobertura de issue_types ────────────────────────────────────────

class TestIssueTypeCoverage:
    """Los 7 issue_type del SRE deben tener al menos un experimento con su label."""

    def test_all_issue_types_covered_by_labels(self, experiment_docs):
        covered = set()
        for fname, doc in experiment_docs:
            issue_type = doc.get("metadata", {}).get("labels", {}).get("issue-type", "")
            if issue_type:
                covered.add(issue_type)
        missing = EXPECTED_ISSUE_TYPES - covered
        assert not missing, (
            f"Issue types sin experimento: {missing}. "
            f"Cubiertos: {covered}"
        )

    def test_no_unknown_issue_types(self, experiment_docs):
        known = EXPECTED_ISSUE_TYPES | {"CRASH_LOOP"}  # HIGH_RESTARTS cubre CRASH_LOOP también
        for fname, doc in experiment_docs:
            issue_type = doc.get("metadata", {}).get("labels", {}).get("issue-type", "")
            if issue_type:
                assert issue_type in known, (
                    f"{fname}: issue-type={issue_type!r} no es un tipo conocido del SRE"
                )

    def test_each_experiment_has_issue_type_label(self, experiment_docs):
        for fname, doc in experiment_docs:
            issue_type = doc.get("metadata", {}).get("labels", {}).get("issue-type", "")
            assert issue_type, f"{fname}: falta label issue-type — no se puede rastrear en Grafana"

    def test_each_experiment_has_sre_training_label(self, experiment_docs):
        for fname, doc in experiment_docs:
            sre_label = doc.get("metadata", {}).get("labels", {}).get("sre-training", "")
            assert sre_label == "true", f"{fname}: falta label sre-training=true"


# ── Tests de Schedule CRDs ────────────────────────────────────────────────────

class TestScheduleCRDs:
    """Valida los 7 Schedule CRDs del archivo 31."""

    def test_all_are_schedule_kind(self, schedule_docs):
        for fname, doc in schedule_docs:
            assert doc["kind"] == "Schedule", (
                f"{fname}: documento no es Schedule, es {doc['kind']!r}"
            )

    def test_all_have_concurrency_forbid(self, schedule_docs):
        for fname, doc in schedule_docs:
            policy = doc["spec"].get("concurrencyPolicy")
            assert policy == "Forbid", (
                f"{doc['metadata']['name']}: concurrencyPolicy debe ser Forbid, es {policy!r}"
            )

    def test_all_have_history_limit(self, schedule_docs):
        for fname, doc in schedule_docs:
            limit = doc["spec"].get("historyLimit")
            assert limit is not None, f"{doc['metadata']['name']}: falta historyLimit"
            assert limit >= 1, f"{doc['metadata']['name']}: historyLimit debe ser ≥ 1"

    def test_all_have_starting_deadline(self, schedule_docs):
        for fname, doc in schedule_docs:
            deadline = doc["spec"].get("startingDeadlineSeconds")
            assert deadline is not None, (
                f"{doc['metadata']['name']}: falta startingDeadlineSeconds — "
                "si el controller estuvo caído puede acumular runs"
            )

    def test_no_schedule_more_frequent_than_hourly(self, schedule_docs):
        """Ningún schedule debe dispararse más de una vez por hora."""
        for fname, doc in schedule_docs:
            schedule_expr = doc["spec"].get("schedule", "")
            # Detectar patterns sospechosos: @every Xm con X<60, o minutos con */N donde N<60
            if schedule_expr.startswith("@every"):
                m = re.match(r"@every (\d+)(s|m|h)", schedule_expr)
                if m:
                    value, unit = int(m.group(1)), m.group(2)
                    total_seconds = value * {"s": 1, "m": 60, "h": 3600}[unit]
                    assert total_seconds >= 1800, (  # mínimo 30 minutos
                        f"{doc['metadata']['name']}: schedule '{schedule_expr}' es más frecuente que cada 30 min"
                    )
            elif re.match(r"\*/(\d+)\s", schedule_expr):
                interval_min = int(re.match(r"\*/(\d+)", schedule_expr).group(1))
                assert interval_min >= 30, (
                    f"{doc['metadata']['name']}: schedule '{schedule_expr}' es más frecuente que cada 30 min"
                )

    def test_all_schedules_cover_different_days(self, schedule_docs):
        """Los 7 schedules deben estar en días distintos (no se pisan)."""
        # Extraer el día de la semana del cron (campo 5: 0-7)
        day_fields = []
        for fname, doc in schedule_docs:
            cron = doc["spec"].get("schedule", "")
            parts = cron.split()
            if len(parts) == 5:
                day_fields.append(parts[4])
        # Todos los días deben ser únicos
        assert len(day_fields) == len(set(day_fields)), (
            f"Hay schedules en el mismo día de la semana: {day_fields}"
        )

    def test_schedule_issue_types_cover_all_expected(self, schedule_docs):
        covered = set()
        for fname, doc in schedule_docs:
            issue_type = doc.get("metadata", {}).get("labels", {}).get("issue-type", "")
            if issue_type:
                covered.add(issue_type)
        missing = EXPECTED_ISSUE_TYPES - covered
        assert not missing, f"Schedules no cubren: {missing}"

    def test_schedule_inner_chaos_selector_has_label(self, schedule_docs):
        """El chaos embebido en cada Schedule también debe tener labelSelectors."""
        kind_to_field = {
            "StressChaos": "stressChaos",
            "PodChaos":    "podChaos",
            "HTTPChaos":   "httpChaos",
            "NetworkChaos": "networkChaos",
        }
        for fname, doc in schedule_docs:
            chaos_type = doc["spec"].get("type", "")
            field = kind_to_field.get(chaos_type)
            if not field:
                continue
            chaos_spec = doc["spec"].get(field, {})
            label_sel = chaos_spec.get("selector", {}).get("labelSelectors", {})
            assert "app" in label_sel, (
                f"{doc['metadata']['name']}: el chaos embebido en Schedule no tiene labelSelectors.app"
            )

    def test_schedule_inner_chaos_no_critical_target(self, schedule_docs):
        kind_to_field = {
            "StressChaos": "stressChaos",
            "PodChaos":    "podChaos",
            "HTTPChaos":   "httpChaos",
            "NetworkChaos": "networkChaos",
        }
        for fname, doc in schedule_docs:
            chaos_type = doc["spec"].get("type", "")
            field = kind_to_field.get(chaos_type)
            if not field:
                continue
            chaos_spec = doc["spec"].get(field, {})
            app = chaos_spec.get("selector", {}).get("labelSelectors", {}).get("app", "")
            assert app not in CRITICAL_APPS, (
                f"{doc['metadata']['name']}: Schedule apunta a servicio crítico '{app}'"
            )


# ── Tests de stressors específicos ───────────────────────────────────────────

class TestStressorConfiguration:
    """Valida que los stressors tengan valores que efectivamente superen los umbrales."""

    # Umbrales del SRE (del main.py de k8s-agent)
    SRE_MEMORY_THRESHOLD = 0.85
    SRE_MEMORY_LEAK_RATE = 5 * 1024 * 1024  # 5 MB/s

    def _parse_size_mb(self, size_str: str) -> float:
        """Convierte '600MB', '450MB', '3GB' → MB."""
        m = re.match(r"^(\d+(?:\.\d+)?)(MB|GB|KB)$", size_str, re.IGNORECASE)
        assert m, f"Formato de size inesperado: {size_str!r}"
        value, unit = float(m.group(1)), m.group(2).upper()
        return value * {"KB": 1/1024, "MB": 1, "GB": 1024}[unit]

    def test_oom_experiment_size_exceeds_frontend_limit(self):
        """OOM stress debe superar el límite de 512Mi de frontend-next."""
        path = EXPERIMENTS_DIR / "11-oom-killed-frontend.yaml"
        docs = _load_experiment_docs(path)
        doc = docs[0]
        size_str = doc["spec"]["stressors"]["memory"]["size"]
        size_mb = self._parse_size_mb(size_str)
        limit_mb = 512  # frontend-next límite
        assert size_mb > limit_mb, (
            f"OOM stress ({size_mb}MB) debe superar el límite de frontend-next ({limit_mb}MB)"
        )

    def test_high_memory_size_exceeds_threshold_without_oom(self):
        """HIGH_MEMORY stress debe superar 85% del límite pero NO causar OOM."""
        path = EXPERIMENTS_DIR / "12-high-memory-llm-adapter.yaml"
        docs = _load_experiment_docs(path)
        doc = docs[0]
        size_str = doc["spec"]["stressors"]["memory"]["size"]
        size_mb = self._parse_size_mb(size_str)
        limit_mb = 512  # llm-adapter límite
        threshold_mb = limit_mb * self.SRE_MEMORY_THRESHOLD
        assert size_mb > threshold_mb, (
            f"HIGH_MEMORY stress ({size_mb}MB) debe superar umbral {threshold_mb:.0f}MB (85% de {limit_mb}MB)"
        )
        assert size_mb < limit_mb, (
            f"HIGH_MEMORY stress ({size_mb}MB) debe ser < límite ({limit_mb}MB) para no causar OOM"
        )

    def test_memory_leak_size_is_safe_for_cosyvoice(self):
        """MEMORY_LEAK stress no debe superar el 50% del límite de cosyvoice."""
        path = EXPERIMENTS_DIR / "16-memory-leak-cosyvoice.yaml"
        docs = _load_experiment_docs(path)
        doc = docs[0]
        size_str = doc["spec"]["stressors"]["memory"]["size"]
        size_mb = self._parse_size_mb(size_str)
        limit_mb = 14 * 1024  # cosyvoice: 14Gi
        safe_threshold = limit_mb * 0.50
        assert size_mb < safe_threshold, (
            f"MEMORY_LEAK stress ({size_mb}MB) supera el 50% del límite de cosyvoice ({safe_threshold}MB)"
        )

    def test_high_cpu_workers_positive(self):
        path = EXPERIMENTS_DIR / "13-high-cpu-productivity.yaml"
        docs = _load_experiment_docs(path)
        doc = docs[0]
        workers = doc["spec"]["stressors"]["cpu"]["workers"]
        load = doc["spec"]["stressors"]["cpu"]["load"]
        assert workers >= 1, "HIGH_CPU debe tener al menos 1 worker"
        assert 50 <= load <= 100, f"CPU load {load} fuera de rango útil [50, 100]"

    def test_http_chaos_abort_configured(self):
        path = EXPERIMENTS_DIR / "15-high-error-rate-llm-adapter.yaml"
        docs = _load_experiment_docs(path)
        doc = docs[0]
        assert doc["spec"].get("abort") is True, "HTTPChaos debe tener abort: true"
        percentage = doc["spec"].get("percentage", 100)
        assert percentage >= 50, (
            f"HTTPChaos abort percentage ({percentage}%) debe ser ≥50% para superar umbral SRE"
        )

    def test_container_kill_has_container_names(self):
        path = EXPERIMENTS_DIR / "14-crash-loop-whatsapp.yaml"
        docs = _load_experiment_docs(path)
        doc = docs[0]
        containers = doc["spec"].get("containerNames", [])
        assert containers, "container-kill debe especificar containerNames para evitar matar sidecars"
        assert "whatsapp-bridge" in containers


# ── Tests de alineación con SRE BUG_LIBRARY ──────────────────────────────────

class TestSreBugLibraryAlignment:
    """Valida que el issue-type de cada experimento exista en BUG_LIBRARY."""

    def test_autopatch_issue_types_in_bug_library(self, experiment_docs):
        """
        Solo los issue_types con acción ROLLOUT_RESTART/YAML-patch necesitan entrada en BUG_LIBRARY.
        HIGH_ERROR_RATE y POD_FAILED son NOTIFY_HUMAN — no tienen patch YAML, y está bien.
        """
        try:
            from agents.sre.bug_library import BUG_LIBRARY
        except ImportError:
            pytest.skip("agents.sre.bug_library no disponible en este entorno")

        # Estos issue_types resultan en NOTIFY_HUMAN — no requieren entrada en BUG_LIBRARY
        notify_human_types = {"HIGH_ERROR_RATE", "POD_FAILED"}

        for fname, doc in experiment_docs:
            issue_type = doc.get("metadata", {}).get("labels", {}).get("issue-type", "")
            if not issue_type or issue_type in notify_human_types:
                continue
            assert issue_type in BUG_LIBRARY, (
                f"{fname}: issue-type={issue_type!r} no existe en BUG_LIBRARY. "
                f"Tipos con patch automático: {list(BUG_LIBRARY.keys())}"
            )

    def test_notify_human_types_not_in_bug_library(self, experiment_docs):
        """Confirma que HIGH_ERROR_RATE y POD_FAILED NO tienen patch YAML (diseño intencional)."""
        try:
            from agents.sre.bug_library import BUG_LIBRARY
        except ImportError:
            pytest.skip("agents.sre.bug_library no disponible en este entorno")

        notify_human_types = {"HIGH_ERROR_RATE", "POD_FAILED"}
        for issue_type in notify_human_types:
            assert issue_type not in BUG_LIBRARY, (
                f"{issue_type} aparece en BUG_LIBRARY — debería ser solo NOTIFY_HUMAN sin patch YAML"
            )
