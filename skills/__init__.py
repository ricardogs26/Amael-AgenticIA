"""
skills — Sistema de Skills de Amael-AgenticIA.

Skills disponibles (registradas en SkillRegistry via register_all_skills()):

  kubernetes  — KubernetesSkill: list_pods, describe_pod, list_nodes, get_deployment
  rag         — RAGSkill: retrieve, ingest, list_docs, delete_collection
  llm         — LLMSkill: invoke, chat (Ollama singleton)
  vault       — VaultSkill: get/put/delete/has/list secrets (KV v2)
  web         — WebSkill: search (DuckDuckGo), fetch_url

Uso típico desde un agente:
    from skills.registry import SkillRegistry, register_all_skills
    register_all_skills()
    k8s = SkillRegistry.get("kubernetes")
    result = await k8s.list_pods(ListPodsInput(namespace="amael-ia"))

Uso en health endpoint:
    from skills.registry import SkillRegistry
    status = await SkillRegistry.health_check_all()
"""
from skills.kubernetes import KubernetesSkill
from skills.llm import LLMSkill
from skills.rag import RAGSkill
from skills.registry import SkillNotFoundError, SkillRegistry, register_all_skills
from skills.vault import VaultSkill
from skills.web import WebSkill

__all__ = [
    # Registry
    "SkillRegistry",
    "SkillNotFoundError",
    "register_all_skills",
    # Skills
    "KubernetesSkill",
    "RAGSkill",
    "LLMSkill",
    "VaultSkill",
    "WebSkill",
]
