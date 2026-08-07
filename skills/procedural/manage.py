"""
skill_manage — la interfaz con la que un AGENTE escribe skills.

Semántica de Hermes (create/patch), con una diferencia deliberada e
innegociable: TODA escritura produce una propuesta que espera aprobación por
WhatsApp. Este módulo no sabe activar skills — esa función ni siquiera se
importa aquí. Un agente que se auto-modifica sin revisión, en un cluster donde
el trader opera dinero real, no es un escenario a mitigar: es uno a hacer
imposible por construcción.
"""
from __future__ import annotations

import logging

from skills.procedural import store

logger = logging.getLogger("skills.manage")

_NOTIFY_MAX = 500   # el aviso de WhatsApp es un resumen, no la skill entera


def skill_manage(
    action: str,
    owner_agent: str,
    skill_md: str = "",
    name: str = "",
    old: str = "",
    new: str = "",
    reason: str = "",
    notify: bool = True,
) -> dict:
    """
    Acciones:
      create — propone una skill nueva (skill_md completo)
      patch  — propone una edición de una skill ACTIVA (old/new exactos y únicos)

    Devuelve {"ok": bool, "detail": str legible para el agente}. Los errores de
    formato NO lanzan: el texto del error es la retroalimentación con la que el
    agente corrige e intenta de nuevo.
    """
    try:
        if action == "create":
            r = store.propose(skill_md, owner_agent=owner_agent, reason=reason)
            detail = (f"Propuesta '{r['name']}' registrada — pendiente de "
                      f"aprobación humana (/sre skill approve {r['name']}).")
        elif action == "patch":
            activa = store.get_skill(name, status="active")
            if not activa:
                return {"ok": False,
                        "detail": f"No hay skill activa llamada {name!r} que parchear."}
            parcheado = store.patch_text(activa["text"], old, new)
            r = store.propose(parcheado, owner_agent=owner_agent,
                              reason=reason or f"patch de {name}")
            detail = (f"Patch de '{name}' propuesto — la versión activa sigue "
                      f"vigente hasta que se apruebe.")
        else:
            return {"ok": False,
                    "detail": f"Acción desconocida: {action!r}. Usa create | patch."}
    except store.SkillFormatError as exc:
        return {"ok": False, "detail": str(exc)}
    except Exception as exc:
        logger.error(f"[skill_manage] {action} falló: {exc}")
        return {"ok": False, "detail": f"Error interno: {exc}"}

    if notify:
        _notify_proposal(r["name"], owner_agent, reason)
    return {"ok": True, "detail": detail, "name": r["name"]}


def _notify_proposal(name: str, owner_agent: str, reason: str) -> None:
    """Aviso por WhatsApp al admin. Best-effort: la propuesta ya está guardada
    y aparece en /sre skills aunque el aviso no salga."""
    try:
        from agents.sre.reporter import notify_whatsapp_sre
        razon = f"\nMotivo: {reason[:_NOTIFY_MAX]}" if reason else ""
        notify_whatsapp_sre(
            f"🧠 *Nueva skill propuesta por {owner_agent}*: `{name}`{razon}\n\n"
            f"Ver:      /sre skill show {name}\n"
            f"Aprobar:  /sre skill approve {name}\n"
            f"Rechazar: /sre skill reject {name}",
            severity="LOW",
        )
    except Exception as exc:
        logger.warning(f"[skill_manage] aviso WhatsApp no salió: {exc}")
