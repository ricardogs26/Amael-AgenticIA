"""
Jophiel — Agente de generación y análisis de código en memoria.

Angelología: Jophiel (יוֹפִיאֵל) es el ángel de la sabiduría, la comprensión y la belleza.

A diferencia de Gabriel (que hace el ciclo GitHub completo), Jophiel trabaja
en memoria: recibe código, lo transforma y devuelve el resultado.
Ideal para sesiones de coding interactivas y pipelines internos.

Tareas disponibles:
  generate   — genera código desde descripción natural
  refactor   — refactoriza código existente según instrucciones
  analyze    — análisis estático: bugs, code smells, seguridad
  test_gen   — genera tests unitarios para código dado
  document   — agrega docstrings y comentarios
  (default)  — modo conversacional con RAG

Registro: @AgentRegistry.register → disponible como AgentRegistry.get("jophiel", ctx)
"""
from __future__ import annotations

import logging
import re
from typing import Any

from agents.base.agent_registry import AgentRegistry
from agents.base.llm_utils import build_prompt, invoke_llm, retrieve_rag_context
from core.agent_base import AgentResult, BaseAgent

logger = logging.getLogger("agents.jophiel")

# ── System prompts ────────────────────────────────────────────────────────────

_SYSTEM_CONVERSATIONAL = """\
Eres Jophiel, un experto en código limpio, patrones de diseño y mejores prácticas.
Trabajas en Amael-IA, plataforma multi-agente con Python/FastAPI/LangGraph/Next.js.

Directrices:
- Código explícito y sin over-engineering
- Explica la causa raíz de bugs antes de proponer el fix
- Sugiere tests para cada cambio significativo
- Responde siempre en el mismo idioma de la pregunta"""

_SYSTEM_GENERATE = """\
Eres Jophiel, experto en generación de código limpio y bien tipado.
Genera el código solicitado en el lenguaje indicado.

Reglas:
- Código explícito y bien nombrado, sin sobre-abstracciones
- Incluye type hints / tipos donde aplique
- Comenta únicamente donde la lógica no sea obvia
- Sin bloques markdown — responde SOLO con los marcadores indicados

Formato OBLIGATORIO:
---CODE_START---
<código generado>
---CODE_END---
---EXPLANATION_START---
<decisiones de diseño clave (máx 5 líneas)>
---EXPLANATION_END---"""

_SYSTEM_REFACTOR = """\
Eres Jophiel, experto en refactoring de código.
Aplica las instrucciones dadas al código existente.

Reglas:
- Mantén la funcionalidad original intacta
- Explica qué problema concreto resuelve cada cambio
- Sin modificaciones no solicitadas
- Sin bloques markdown — solo marcadores

Formato OBLIGATORIO:
---CODE_START---
<código refactorizado completo>
---CODE_END---
---EXPLANATION_START---
<qué cambiaste y por qué>
---EXPLANATION_END---"""

_SYSTEM_ANALYZE = """\
Eres Jophiel, experto en análisis estático de código.
Revisa el código dado e identifica:
1. Bugs potenciales o errores lógicos
2. Code smells (duplicación, funciones largas, god objects)
3. Problemas de seguridad obvios (injection, secrets hardcodeados, etc.)
4. Sugerencias concretas de mejora

Sé específico: indica línea/sección del problema y cómo resolverlo.
Prioriza por severidad: 🔴 crítico, 🟡 mejora, 🟢 estilo."""

_SYSTEM_TEST_GEN = """\
Eres Jophiel, experto en testing (pytest / jest / go test).
Genera tests unitarios para el código dado.

Reglas:
- Cubre casos felices, casos borde y errores esperados
- Tests independientes entre sí (sin estado compartido)
- Nombres: test_<función>_<escenario>_<resultado_esperado>
- Sin mocks innecesarios — mockea solo dependencias externas reales
- Sin bloques markdown — solo marcadores

Formato OBLIGATORIO:
---CODE_START---
<tests generados>
---CODE_END---"""

_SYSTEM_DOCUMENT = """\
Eres Jophiel, experto en documentación técnica de código.
Agrega docstrings y comentarios al código dado.

Reglas:
- Docstrings estilo Google (Args, Returns, Raises, Example)
- Comenta solo donde la lógica no sea obvia
- No describas lo que el código ya expresa por sí mismo
- Mantén la lógica del código sin cambios
- Sin bloques markdown — solo marcadores

Formato OBLIGATORIO:
---CODE_START---
<código con documentación agregada>
---CODE_END---"""


# ── Agent ─────────────────────────────────────────────────────────────────────

@AgentRegistry.register
class JophielAgent(BaseAgent):
    """
    Jophiel — Agente de generación y análisis de código en memoria.

    task dict para modo conversacional (default):
        {"query": str, "user_id": str}

    task dict para tareas específicas:
        {
            "task":        "generate" | "refactor" | "analyze" | "test_gen" | "document",
            "code":        str,          # código existente (para refactor/analyze/test_gen/document)
            "description": str,          # descripción (para generate)
            "instructions": str,         # instrucciones de refactor (para refactor)
            "language":    str,          # lenguaje de programación (default: "python")
            "context":     str,          # contexto adicional (opcional)
            "user_id":     str,
        }
    """

    name         = "jophiel"
    role         = "Generación, refactoring y análisis de código en memoria"
    version      = "1.0.0"
    capabilities = [
        "code_generation",
        "refactoring",
        "static_analysis",
        "test_generation",
        "documentation",
        "rag_retrieval",
    ]

    async def execute(self, task: dict[str, Any]) -> AgentResult:
        task_type = task.get("task", "").lower()
        if task_type == "generate":
            return await self._generate(task)
        if task_type == "refactor":
            return await self._refactor(task)
        if task_type == "analyze":
            return await self._analyze(task)
        if task_type == "test_gen":
            return await self._test_gen(task)
        if task_type == "document":
            return await self._document(task)
        return await self._conversational(task)

    # ── Modo conversacional ───────────────────────────────────────────────────

    async def _conversational(self, task: dict[str, Any]) -> AgentResult:
        query      = task.get("query", "").strip()
        user_email = task.get("user_id", "")

        if not query:
            return AgentResult(success=False, output=None, agent_name=self.name,
                               error="query vacía")

        rag_ctx = await retrieve_rag_context(user_email, query, k=3, agent_name=self.name)
        prompt  = build_prompt(_SYSTEM_CONVERSATIONAL, query, rag_ctx,
                               context_header="## Contexto del proyecto",
                               question_header="## Pregunta de desarrollo")
        try:
            response = await invoke_llm(prompt, self.context, self.name)
            return AgentResult(
                success=True,
                output={"response": response, "source": "jophiel"},
                agent_name=self.name,
                metadata={"rag_used": bool(rag_ctx)},
            )
        except Exception as exc:
            logger.error(f"[jophiel] LLM error: {exc}")
            return AgentResult(success=False, output=None, agent_name=self.name, error=str(exc))

    # ── generate ──────────────────────────────────────────────────────────────

    async def _generate(self, task: dict[str, Any]) -> AgentResult:
        description = task.get("description", "").strip()
        language    = task.get("language", "python")
        context     = task.get("context", "")
        user_email  = task.get("user_id", "")

        if not description:
            return AgentResult(success=False, output=None, agent_name=self.name,
                               error="'description' es requerida para generate")

        rag_ctx = await retrieve_rag_context(user_email, description, k=3, agent_name=self.name)
        body    = f"Lenguaje: {language}\n\nDescripción:\n{description}"
        if context:
            body += f"\n\nContexto adicional:\n{context}"
        prompt = build_prompt(_SYSTEM_GENERATE, body, rag_ctx,
                              context_header="## Contexto del proyecto",
                              question_header="## Tarea")
        try:
            raw  = await invoke_llm(prompt, self.context, self.name)
            code = _extract_block(raw, "CODE")
            expl = _extract_block(raw, "EXPLANATION")
            return AgentResult(
                success=True,
                output={"code": code or raw.strip(), "explanation": expl,
                        "language": language, "task": "generate"},
                agent_name=self.name,
            )
        except Exception as exc:
            logger.error(f"[jophiel] generate error: {exc}")
            return AgentResult(success=False, output=None, agent_name=self.name, error=str(exc))

    # ── refactor ──────────────────────────────────────────────────────────────

    async def _refactor(self, task: dict[str, Any]) -> AgentResult:
        code         = task.get("code", "").strip()
        instructions = task.get("instructions", "").strip()
        language     = task.get("language", "python")

        if not code:
            return AgentResult(success=False, output=None, agent_name=self.name,
                               error="'code' es requerido para refactor")

        body   = f"Lenguaje: {language}\n\nInstrucciones:\n{instructions or 'Mejora la calidad del código'}\n\n## Código actual\n{code}"
        prompt = build_prompt(_SYSTEM_REFACTOR, body, question_header="## Tarea")
        try:
            raw           = await invoke_llm(prompt, self.context, self.name)
            refactored    = _extract_block(raw, "CODE")
            explanation   = _extract_block(raw, "EXPLANATION")
            diff          = _unified_diff(code, refactored or raw.strip(), language)
            return AgentResult(
                success=True,
                output={"code": refactored or raw.strip(), "explanation": explanation,
                        "diff": diff, "language": language, "task": "refactor"},
                agent_name=self.name,
            )
        except Exception as exc:
            logger.error(f"[jophiel] refactor error: {exc}")
            return AgentResult(success=False, output=None, agent_name=self.name, error=str(exc))

    # ── analyze ───────────────────────────────────────────────────────────────

    async def _analyze(self, task: dict[str, Any]) -> AgentResult:
        code     = task.get("code", "").strip()
        language = task.get("language", "python")

        if not code:
            return AgentResult(success=False, output=None, agent_name=self.name,
                               error="'code' es requerido para analyze")

        body   = f"Lenguaje: {language}\n\n## Código a analizar\n{code}"
        prompt = build_prompt(_SYSTEM_ANALYZE, body, question_header="## Análisis requerido")
        try:
            analysis = await invoke_llm(prompt, self.context, self.name)
            return AgentResult(
                success=True,
                output={"analysis": analysis, "language": language, "task": "analyze"},
                agent_name=self.name,
            )
        except Exception as exc:
            logger.error(f"[jophiel] analyze error: {exc}")
            return AgentResult(success=False, output=None, agent_name=self.name, error=str(exc))

    # ── test_gen ──────────────────────────────────────────────────────────────

    async def _test_gen(self, task: dict[str, Any]) -> AgentResult:
        code          = task.get("code", "").strip()
        language      = task.get("language", "python")
        function_name = task.get("function_name", "")

        if not code:
            return AgentResult(success=False, output=None, agent_name=self.name,
                               error="'code' es requerido para test_gen")

        focus  = f"\nFunción a testear: `{function_name}`" if function_name else ""
        body   = f"Lenguaje: {language}{focus}\n\n## Código fuente\n{code}"
        prompt = build_prompt(_SYSTEM_TEST_GEN, body, question_header="## Tests requeridos")
        try:
            raw   = await invoke_llm(prompt, self.context, self.name)
            tests = _extract_block(raw, "CODE")
            return AgentResult(
                success=True,
                output={"tests": tests or raw.strip(), "language": language, "task": "test_gen"},
                agent_name=self.name,
            )
        except Exception as exc:
            logger.error(f"[jophiel] test_gen error: {exc}")
            return AgentResult(success=False, output=None, agent_name=self.name, error=str(exc))

    # ── document ──────────────────────────────────────────────────────────────

    async def _document(self, task: dict[str, Any]) -> AgentResult:
        code     = task.get("code", "").strip()
        language = task.get("language", "python")

        if not code:
            return AgentResult(success=False, output=None, agent_name=self.name,
                               error="'code' es requerido para document")

        body   = f"Lenguaje: {language}\n\n## Código a documentar\n{code}"
        prompt = build_prompt(_SYSTEM_DOCUMENT, body, question_header="## Documentación requerida")
        try:
            raw       = await invoke_llm(prompt, self.context, self.name)
            documented = _extract_block(raw, "CODE")
            return AgentResult(
                success=True,
                output={"code": documented or raw.strip(), "language": language, "task": "document"},
                agent_name=self.name,
            )
        except Exception as exc:
            logger.error(f"[jophiel] document error: {exc}")
            return AgentResult(success=False, output=None, agent_name=self.name, error=str(exc))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_block(text: str, tag: str) -> str | None:
    """Extrae contenido entre ---{tag}_START--- y ---{tag}_END---."""
    m = re.search(rf'---{tag}_START---\s*([\s\S]+?)\s*---{tag}_END---', text)
    return m.group(1).strip() if m else None


def _unified_diff(original: str, modified: str, language: str) -> str:
    """Genera un diff unificado simple entre original y modificado."""
    import difflib
    ext_map = {"python": "py", "typescript": "ts", "javascript": "js",
               "go": "go", "rust": "rs", "java": "java"}
    ext = ext_map.get(language, "txt")
    lines = list(difflib.unified_diff(
        original.splitlines(keepends=True),
        modified.splitlines(keepends=True),
        fromfile=f"original.{ext}",
        tofile=f"refactored.{ext}",
        lineterm="",
    ))
    return "".join(lines) if lines else "(sin cambios)"
