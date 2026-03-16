# CoderAgent — Roadmap Phase 7

**Estado**: Placeholder — no implementado

## Responsabilidad

Agente especializado en generación, refactorización y análisis de código fuente.

## Capacidades planeadas

- Generación de código a partir de descripción en lenguaje natural
- Refactorización de código existente con explicación de cambios
- Análisis estático (bugs, code smells, complejidad ciclomática)
- Generación de tests unitarios para funciones existentes
- Documentación automática (docstrings, comentarios)
- Traducción entre lenguajes de programación

## Skills requeridas

- `llm` — inferencia LLM con contexto de código
- `filesystem` — leer/escribir archivos del proyecto
- `git` — contexto de cambios, diff, historial

## Tools requeridas

- `github` — leer PRs, issues, código del repositorio

## Input/Output esperado

```python
# Input
{
    "task": "generate" | "refactor" | "analyze" | "test" | "document",
    "language": "python" | "typescript" | "go" | ...,
    "code": "código existente (para refactor/analyze/test/document)",
    "description": "descripción de lo que generar (para generate)",
    "context": "contexto adicional del proyecto"
}

# Output
{
    "code": "código generado o modificado",
    "explanation": "explicación de los cambios",
    "tests": "tests generados (si aplica)",
    "diff": "diff unificado (para refactor)"
}
```

## Implementación de referencia

```python
@AgentRegistry.register
class CoderAgent(BaseAgent):
    name = "coder"
    role = "Generación y análisis de código fuente"
    version = "1.0.0"
    capabilities = ["code_generation", "refactoring", "static_analysis", "test_generation"]
    required_skills = ["llm", "filesystem"]

    async def execute(self, task: Dict[str, Any]) -> AgentResult:
        ...
```

## Fase de implementación

**Phase 7** — posterior a la estabilización de la plataforma base (Phases 1-6).
