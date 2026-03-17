"""Tests unitarios para core/constants.py."""
from core.constants import (
    MAX_PLAN_STEPS,
    ActionType,
    AnomalyType,
    Severity,
    StepType,
    SupervisorDecision,
)


class TestStepType:
    def test_all_expected_values_exist(self):
        expected = {
            "K8S_TOOL", "RAG_RETRIEVAL", "PRODUCTIVITY_TOOL",
            "WEB_SEARCH", "DOCUMENT_TOOL", "TTS_TOOL",
            "CODE_GENERATION", "REASONING",
        }
        actual = {s.value for s in StepType}
        assert expected == actual

    def test_is_string_comparable(self):
        assert StepType.REASONING == "REASONING"
        assert StepType.K8S_TOOL == "K8S_TOOL"


class TestActionType:
    def test_rollout_restart_exists(self):
        assert ActionType.ROLLOUT_RESTART == "ROLLOUT_RESTART"

    def test_notify_human_exists(self):
        assert ActionType.NOTIFY_HUMAN == "NOTIFY_HUMAN"


class TestSeverity:
    def test_order_coverage(self):
        levels = {s.value for s in Severity}
        assert {"LOW", "MEDIUM", "HIGH", "CRITICAL"} == levels


class TestSupervisorDecision:
    def test_accept_and_replan(self):
        assert SupervisorDecision.ACCEPT == "ACCEPT"
        assert SupervisorDecision.REPLAN == "REPLAN"


class TestAnomalyType:
    def test_crash_loop_exists(self):
        assert AnomalyType.CRASH_LOOP == "CRASH_LOOP"

    def test_slo_budget_burning_exists(self):
        assert AnomalyType.SLO_BUDGET_BURNING == "SLO_BUDGET_BURNING"


class TestLimits:
    def test_max_plan_steps_positive(self):
        assert MAX_PLAN_STEPS > 0

    def test_max_plan_steps_reasonable(self):
        # Debe ser mayor que 1 (utilidad mínima) y menor de 50 (razonabilidad)
        assert 1 < MAX_PLAN_STEPS < 50
