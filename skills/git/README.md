# GitSkill — Roadmap Phase 7

**Estado**: Placeholder — no implementado

## Responsabilidad

Skill reutilizable para operaciones Git: clonar repositorios, leer historial, crear commits, gestionar branches y tags.

## Capacidades planeadas

```python
class GitSkill(BaseSkill):
    name = "git"

    def clone(self, repo_url: str, target_dir: str) -> str: ...
    def diff(self, path: str, ref1: str = "HEAD", ref2: str = None) -> str: ...
    def log(self, path: str, n: int = 20) -> List[dict]: ...
    def status(self, path: str) -> dict: ...
    def commit(self, path: str, message: str, files: List[str]) -> str: ...
    def push(self, path: str, remote: str = "origin", branch: str = "main") -> bool: ...
    def create_branch(self, path: str, name: str) -> bool: ...
    def checkout(self, path: str, ref: str) -> bool: ...
```

## Agentes que la usarán

- `CoderAgent` — leer contexto del repo antes de generar código
- `DevOpsAgent` — operations de release y tagging

## Nota

La `GitHubTool` en `tools/github/` cubre operaciones vía GitHub API REST/GraphQL.
Esta `GitSkill` cubre operaciones Git locales vía `gitpython` o subprocess.
Son complementarias, no redundantes.

## Dependencia

```
pip install gitpython
```
