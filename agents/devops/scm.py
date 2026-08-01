"""
agents.devops.scm — selector del proveedor de control de versiones de Camael.

Bitbucket se retiró tras la POC (ago-2026); el flujo GitOps ahora corre sobre
GitHub. Este módulo existe para que el cambio sea una variable de entorno y no
un refactor: `agent.py` importa siempre `scm`, nunca un proveedor concreto.

    SCM_PROVIDER=github     (default)
    SCM_PROVIDER=bitbucket  (legacy — requiere BITBUCKET_TOKEN/USERNAME válidos)

Ambos clientes exponen la misma API, así que cambiar de proveedor no altera el
comportamiento de Camael.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("agents.camael.scm")

PROVIDER = os.environ.get("SCM_PROVIDER", "github").strip().lower()

if PROVIDER == "bitbucket":
    from agents.devops import bitbucket_client as _impl
    logger.info("[scm] Proveedor: Bitbucket (legacy)")
else:
    if PROVIDER != "github":
        logger.warning(f"[scm] SCM_PROVIDER='{PROVIDER}' desconocido — usando github.")
        PROVIDER = "github"
    from agents.devops import github_client as _impl
    logger.info("[scm] Proveedor: GitHub")

# API pública — idéntica en ambos clientes.
get_branch_head    = _impl.get_branch_head
create_branch      = _impl.create_branch
read_file          = _impl.read_file
commit_file        = _impl.commit_file
create_pr          = _impl.create_pr
approve_pr         = _impl.approve_pr
merge_pr           = _impl.merge_pr
get_pr             = _impl.get_pr
close_pr           = _impl.close_pr
trigger_pipeline   = _impl.trigger_pipeline
get_pipeline       = _impl.get_pipeline
list_pipelines     = _impl.list_pipelines
search_file_in_repo = _impl.search_file_in_repo

__all__ = [
    "PROVIDER",
    "approve_pr",
    "commit_file",
    "create_branch",
    "close_pr",
    "create_pr",
    "get_branch_head",
    "get_pipeline",
    "get_pr",
    "list_pipelines",
    "merge_pr",
    "read_file",
    "search_file_in_repo",
    "trigger_pipeline",
]
