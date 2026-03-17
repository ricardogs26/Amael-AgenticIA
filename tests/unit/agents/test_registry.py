"""Tests unitarios para agents/base/agent_registry.py."""
import pytest

from agents.base.agent_registry import AgentRegistry
from core.agent_base import AgentContext, AgentResult, BaseAgent
from core.exceptions import AgentNotFoundError


# ── Agente stub para tests ────────────────────────────────────────────────────

class _DummyAgent(BaseAgent):
    name         = "_test_dummy_agent"
    role         = "Agente de prueba"
    version      = "0.0.1"
    capabilities = ["testing"]

    async def execute(self, task):
        return AgentResult(success=True, output={"echo": task}, agent_name=self.name)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestAgentRegistry:
    def setup_method(self):
        """Limpiar el registry antes de cada test para aislamiento."""
        AgentRegistry.unregister("_test_dummy_agent")

    def teardown_method(self):
        AgentRegistry.unregister("_test_dummy_agent")

    def test_register_and_is_registered(self):
        AgentRegistry.register(_DummyAgent)
        assert AgentRegistry.is_registered("_test_dummy_agent")

    def test_register_via_decorator(self):
        @AgentRegistry.register
        class _TmpAgent(BaseAgent):
            name = "_test_dummy_agent"
            role = "tmp"
            version = "0.0.1"
            capabilities = []
            async def execute(self, task):
                return AgentResult(success=True, output=None, agent_name=self.name)

        assert AgentRegistry.is_registered("_test_dummy_agent")
        AgentRegistry.unregister("_test_dummy_agent")

    def test_get_instantiates_agent(self):
        AgentRegistry.register(_DummyAgent)
        ctx   = AgentContext(request_id="test-001", user_id="test@example.com", conversation_id="conv-001", llm=None)
        agent = AgentRegistry.get("_test_dummy_agent", ctx)
        assert isinstance(agent, _DummyAgent)

    def test_get_unknown_raises(self):
        with pytest.raises(AgentNotFoundError):
            ctx = AgentContext(request_id="test-002", user_id="test@example.com", conversation_id="conv-002", llm=None)
            AgentRegistry.get("__nonexistent_agent__", ctx)

    def test_names_returns_sorted_list(self):
        AgentRegistry.register(_DummyAgent)
        names = AgentRegistry.names()
        assert "_test_dummy_agent" in names
        assert names == sorted(names)

    def test_count_increases_after_register(self):
        before = AgentRegistry.count()
        AgentRegistry.register(_DummyAgent)
        assert AgentRegistry.count() == before + 1

    def test_unregister_removes_agent(self):
        AgentRegistry.register(_DummyAgent)
        AgentRegistry.unregister("_test_dummy_agent")
        assert not AgentRegistry.is_registered("_test_dummy_agent")

    def test_register_without_name_raises(self):
        class _NoName(BaseAgent):
            name = ""
            role = "sin nombre"
            version = "0.0.1"
            capabilities = []
            async def execute(self, task):
                return AgentResult(success=True, output=None, agent_name="")

        with pytest.raises(ValueError):
            AgentRegistry.register(_NoName)

    def test_list_agents_includes_registered(self):
        AgentRegistry.register(_DummyAgent)
        listing = AgentRegistry.list_agents()
        names   = [a["name"] for a in listing]
        assert "_test_dummy_agent" in names
