# DevOpsAgent — Roadmap Phase 7

**Estado**: Placeholder — no implementado

## Responsabilidad

Agente especializado en automatización de pipelines CI/CD, gestión de infraestructura como código y operaciones cloud.

## Diferencia con SREAgent

| | DevOpsAgent | SREAgent |
|-|-------------|----------|
| Foco | Entrega y despliegue | Confiabilidad y disponibilidad |
| Triggers | Manual / webhook | Autónomo (APScheduler 60s) |
| Acciones | Build, deploy, pipeline | Restart, rollback, notify |
| Conocimiento | CI/CD, IaC, Docker | K8s, métricas, runbooks |

## Capacidades planeadas

- Ejecutar y monitorear pipelines CI/CD (GitHub Actions, GitLab CI)
- Gestión de manifiestos Kubernetes (apply, rollout, scale)
- Operaciones de imagen Docker (build, push, tag)
- Gestión de Helm charts (install, upgrade, rollback)
- Terraform / IaC operations
- Gestión de secrets y ConfigMaps

## Skills requeridas

- `kubernetes` — operaciones K8s (apply, delete, scale)
- `git` — clonar repos, push, tags
- `llm` — diagnóstico y generación de manifiestos

## Tools requeridas

- `github` — Actions, workflows, releases
- `prometheus` — validar métricas post-deploy

## Fase de implementación

**Phase 7** — requiere `skills/git` implementado primero.
