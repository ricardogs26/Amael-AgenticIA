# ApiCallSkill — Roadmap Phase 7

**Estado**: Placeholder — no implementado

## Responsabilidad

Skill reutilizable para realizar llamadas HTTP a APIs externas con autenticación, retry, timeout y parsing de respuesta.

## Capacidades planeadas

```python
class ApiCallSkill(BaseSkill):
    name = "api_call"

    def get(self, url: str, headers: dict = None, params: dict = None,
            timeout: int = 30) -> dict: ...

    def post(self, url: str, body: dict, headers: dict = None,
             timeout: int = 30) -> dict: ...

    def with_auth(self, auth_type: str, credentials: dict) -> "ApiCallSkill":
        # auth_type: "bearer" | "basic" | "api_key" | "oauth2"
        ...

    def with_retry(self, max_retries: int = 3, backoff: float = 1.0) -> "ApiCallSkill":
        ...
```

## Diferencia con tools existentes

Las `tools/` (prometheus, grafana, whatsapp, github) son integraciones de propósito específico
con lógica de dominio. `ApiCallSkill` es el componente HTTP genérico subyacente que
eventualmente todas podrían usar internamente.

## Dependencia

```
# Ya disponible en el proyecto vía requirements:
httpx  # cliente HTTP async/sync
```
