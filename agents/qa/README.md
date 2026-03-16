# QAAgent — Roadmap Phase 8

**Estado**: Placeholder — no implementado

## Responsabilidad

Agente especializado en validación de resultados, ejecución de pruebas y verificación de calidad de outputs generados por otros agentes.

## Capacidades planeadas

- Validar outputs del CoderAgent (compilación, linting, type checking)
- Ejecutar suites de pruebas (pytest, jest, go test)
- Análisis de cobertura de código
- Validación de manifiestos Kubernetes (kubeval, helm lint)
- Verificación de contratos de API (schema validation)
- Revisión automática de PRs antes de merge

## Integración en el pipeline

```
PlannerAgent → CoderAgent → QAAgent → SupervisorAgent
                                ↓
                          (si falla) → CoderAgent (retry con feedback)
```

## Skills requeridas

- `filesystem` — leer archivos generados, ejecutar comandos
- `llm` — análisis semántico de calidad
- `kubernetes` — validar manifiestos

## Tools requeridas

- `github` — comentar PRs, crear issues de calidad

## Fase de implementación

**Phase 8** — requiere CoderAgent implementado.
