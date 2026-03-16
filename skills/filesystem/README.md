# FilesystemSkill — Roadmap Phase 7

**Estado**: Placeholder — no implementado

## Responsabilidad

Skill reutilizable para operaciones seguras sobre el sistema de archivos: leer, escribir, buscar y ejecutar comandos en directorios autorizados.

## Capacidades planeadas

```python
class FilesystemSkill(BaseSkill):
    name = "filesystem"

    def read_file(self, path: str) -> str: ...
    def write_file(self, path: str, content: str) -> bool: ...
    def list_directory(self, path: str, pattern: str = "*") -> List[str]: ...
    def search_content(self, path: str, query: str, file_type: str = None) -> List[dict]: ...
    def run_command(self, cmd: List[str], cwd: str, timeout: int = 30) -> dict: ...
    def file_exists(self, path: str) -> bool: ...
    def delete_file(self, path: str) -> bool: ...
```

## Seguridad

Debe implementar lista blanca de directorios permitidos (`ALLOWED_PATHS`).
Nunca permitir paths con `..` (path traversal).
`run_command` debe usar lista de comandos permitidos (no shell=True).

## Agentes que la usarán

- `CoderAgent` — leer/escribir archivos de código
- `QAAgent` — ejecutar tests, linting
- `DevOpsAgent` — gestionar configs y manifiestos locales
