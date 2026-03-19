"""
Tests unitarios para agents/planner/grouper.py.

Verifica la lógica de agrupación de pasos del plan en batches paralelos:
  - Pasos consecutivos no-REASONING → un batch paralelo
  - Pasos REASONING → siempre solos, nunca en batch compartido
  - Casos extremos: plan vacío, plan de un solo paso, todos REASONING
"""
from agents.planner.grouper import group_plan_into_batches

# ── Casos básicos ─────────────────────────────────────────────────────────────

class TestGroupPlanIntoBatchesBasic:
    def test_empty_plan_returns_empty_batches(self):
        result = group_plan_into_batches([])
        assert result == []

    def test_single_tool_step_single_batch(self):
        plan = ["K8S_TOOL: get pods"]
        result = group_plan_into_batches(plan)
        assert result == [["K8S_TOOL: get pods"]]

    def test_single_reasoning_step_single_batch(self):
        plan = ["REASONING: analiza los datos"]
        result = group_plan_into_batches(plan)
        assert result == [["REASONING: analiza los datos"]]

    def test_two_tools_grouped_in_one_batch(self):
        plan = ["K8S_TOOL: pods", "RAG_RETRIEVAL: docs"]
        result = group_plan_into_batches(plan)
        assert len(result) == 1
        assert len(result[0]) == 2
        assert "K8S_TOOL: pods" in result[0]
        assert "RAG_RETRIEVAL: docs" in result[0]

    def test_reasoning_after_tool_creates_two_batches(self):
        plan = ["K8S_TOOL: get pods", "REASONING: analiza"]
        result = group_plan_into_batches(plan)
        assert len(result) == 2
        assert result[0] == ["K8S_TOOL: get pods"]
        assert result[1] == ["REASONING: analiza"]


# ── Separación por REASONING ──────────────────────────────────────────────────

class TestReasoningSeparation:
    def test_tool_reasoning_tool_creates_three_batches(self):
        """K8S + REASONING + K8S → [K8S], [REASONING], [K8S]"""
        plan = ["K8S_TOOL: A", "REASONING: B", "K8S_TOOL: C"]
        result = group_plan_into_batches(plan)
        assert len(result) == 3
        assert result[0] == ["K8S_TOOL: A"]
        assert result[1] == ["REASONING: B"]
        assert result[2] == ["K8S_TOOL: C"]

    def test_two_tools_then_reasoning_then_two_tools(self):
        """[K8S, RAG], [REASONING], [PROD, WEB]"""
        plan = [
            "K8S_TOOL: pods",
            "RAG_RETRIEVAL: docs",
            "REASONING: sintetizar",
            "PRODUCTIVITY_TOOL: calendar",
            "WEB_SEARCH: noticias",
        ]
        result = group_plan_into_batches(plan)
        assert len(result) == 3
        assert len(result[0]) == 2      # K8S + RAG en paralelo
        assert result[1] == ["REASONING: sintetizar"]
        assert len(result[2]) == 2      # PROD + WEB en paralelo

    def test_all_reasoning_steps_each_is_separate(self):
        plan = ["REASONING: A", "REASONING: B", "REASONING: C"]
        result = group_plan_into_batches(plan)
        assert len(result) == 3
        assert all(len(b) == 1 for b in result)
        assert result[0] == ["REASONING: A"]
        assert result[1] == ["REASONING: B"]
        assert result[2] == ["REASONING: C"]

    def test_reasoning_at_start_then_tools(self):
        """REASONING primero → [REASONING], [K8S, RAG]"""
        plan = ["REASONING: intro", "K8S_TOOL: pods", "RAG_RETRIEVAL: docs"]
        result = group_plan_into_batches(plan)
        assert len(result) == 2
        assert result[0] == ["REASONING: intro"]
        assert len(result[1]) == 2

    def test_alternating_tool_reasoning(self):
        """K8S, REASONING, RAG, REASONING, WEB → 5 batches de tamaño 1"""
        plan = [
            "K8S_TOOL: A",
            "REASONING: B",
            "RAG_RETRIEVAL: C",
            "REASONING: D",
            "WEB_SEARCH: E",
        ]
        result = group_plan_into_batches(plan)
        assert len(result) == 5
        assert all(len(b) == 1 for b in result)


# ── Ejemplo del docstring ─────────────────────────────────────────────────────

class TestDocstringExamples:
    def test_docstring_example_one(self):
        """["K8S_TOOL: A", "RAG_RETRIEVAL: B", "REASONING: C"]
        → [["K8S_TOOL: A", "RAG_RETRIEVAL: B"], ["REASONING: C"]]"""
        plan = ["K8S_TOOL: A", "RAG_RETRIEVAL: B", "REASONING: C"]
        result = group_plan_into_batches(plan)
        assert len(result) == 2
        assert set(result[0]) == {"K8S_TOOL: A", "RAG_RETRIEVAL: B"}
        assert result[1] == ["REASONING: C"]

    def test_docstring_example_two(self):
        """["K8S_TOOL: A", "REASONING: B", "K8S_TOOL: C", "REASONING: D"]
        → [["K8S_TOOL: A"], ["REASONING: B"], ["K8S_TOOL: C"], ["REASONING: D"]]"""
        plan = ["K8S_TOOL: A", "REASONING: B", "K8S_TOOL: C", "REASONING: D"]
        result = group_plan_into_batches(plan)
        assert result == [
            ["K8S_TOOL: A"],
            ["REASONING: B"],
            ["K8S_TOOL: C"],
            ["REASONING: D"],
        ]


# ── Case insensitivity de REASONING ──────────────────────────────────────────

class TestReasoningCaseInsensitivity:
    def test_reasoning_lowercase_is_treated_as_reasoning(self):
        """El código hace step.upper().startswith('REASONING:'), así que minúsculas funciona."""
        plan = ["K8S_TOOL: A", "reasoning: B", "K8S_TOOL: C"]
        result = group_plan_into_batches(plan)
        assert len(result) == 3

    def test_reasoning_mixed_case_is_separated(self):
        plan = ["RAG_RETRIEVAL: docs", "Reasoning: sintetiza"]
        result = group_plan_into_batches(plan)
        assert len(result) == 2
        assert result[0] == ["RAG_RETRIEVAL: docs"]
        assert result[1] == ["Reasoning: sintetiza"]


# ── Preservación del orden ────────────────────────────────────────────────────

class TestBatchOrdering:
    def test_steps_preserve_insertion_order_within_batch(self):
        plan = ["K8S_TOOL: first", "RAG_RETRIEVAL: second", "WEB_SEARCH: third"]
        result = group_plan_into_batches(plan)
        assert len(result) == 1
        assert result[0] == ["K8S_TOOL: first", "RAG_RETRIEVAL: second", "WEB_SEARCH: third"]

    def test_batch_order_matches_plan_order(self):
        plan = [
            "K8S_TOOL: alpha",
            "REASONING: beta",
            "RAG_RETRIEVAL: gamma",
        ]
        result = group_plan_into_batches(plan)
        # Primer batch tiene el primer paso, último batch tiene el último
        assert result[0][0] == "K8S_TOOL: alpha"
        assert result[-1][0] == "RAG_RETRIEVAL: gamma"


# ── Estructura de retorno ─────────────────────────────────────────────────────

class TestReturnStructure:
    def test_returns_list_of_lists(self):
        result = group_plan_into_batches(["K8S_TOOL: test"])
        assert isinstance(result, list)
        assert all(isinstance(b, list) for b in result)

    def test_all_steps_are_strings(self):
        plan = ["K8S_TOOL: A", "REASONING: B", "RAG_RETRIEVAL: C"]
        result = group_plan_into_batches(plan)
        for batch in result:
            for step in batch:
                assert isinstance(step, str)

    def test_total_steps_preserved(self):
        plan = ["K8S_TOOL: A", "RAG_RETRIEVAL: B", "REASONING: C", "WEB_SEARCH: D"]
        result = group_plan_into_batches(plan)
        total = sum(len(b) for b in result)
        assert total == len(plan)
