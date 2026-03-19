"""Tests unitarios para la heurística de importancia de ZaphkielAgent."""
from agents.memory_agent.agent import _compute_importance, _detect_episode_type


class TestComputeImportance:
    def test_base_score_above_threshold(self):
        score = _compute_importance("hola", "hola")
        assert score >= 0.3

    def test_preference_marker_increases_score(self):
        score = _compute_importance("prefiero respuestas cortas", "ok")
        assert score >= 0.7

    def test_long_message_increases_score(self):
        long_msg = "a" * 250
        score = _compute_importance(long_msg, "respuesta")
        assert score >= 0.5

    def test_error_keyword_increases_score(self):
        score = _compute_importance("hay un error en el deploy", "revisando")
        assert score >= 0.55

    def test_empty_messages_return_base(self):
        score = _compute_importance("", "")
        assert score == 0.3


class TestDetectEpisodeType:
    def test_preference_detected(self):
        ep_type = _detect_episode_type("prefiero que respondas en español")
        assert ep_type == "preference"

    def test_error_detected_as_fact(self):
        ep_type = _detect_episode_type("hay un bug en el módulo de auth")
        assert ep_type == "fact"

    def test_generic_question_is_conversation(self):
        ep_type = _detect_episode_type("¿qué hace el PlannerAgent?")
        assert ep_type == "conversation"
