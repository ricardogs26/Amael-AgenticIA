"""
clients — Abstracción HTTP vs in-process para servicios extraídos del backend.

Estos módulos permiten que el código consumidor (routers, healer, etc.)
no dependa directamente de `agents/sre/` o `agents/devops/`. En su lugar,
importan desde `clients.raphael_client` / `clients.camael_client`, y el
feature flag `AGENTS_MODE` decide si la llamada se resuelve localmente
(in-process) o vía HTTP a los servicios extraídos.

Fase 1 (actual): los clientes existen pero AGENTS_MODE=inprocess por default,
                 así que el comportamiento es idéntico al backend monolítico.
Fase 2:          se rewire el router `interfaces/api/routers/sre.py` para
                 importar desde `clients.raphael_client` en vez de `agents.sre`.
Fase 3:          se rewire `agents/sre/scheduler.py:420` y `healer.py:807`
                 para usar `clients.camael_client`.
"""
