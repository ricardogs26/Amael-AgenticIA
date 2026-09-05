"""El aviso de una skill propuesta tiene que LLEGAR.

Del 7 al 12-ago-2026 el consolidador propuso 10 skills y ninguna notificación
salió: `_notify_proposal` mandaba severity="LOW" y `notify_whatsapp_sre`
filtra por debajo de SRE_MIN_NOTIFY_SEVERITY (default HIGH). Ricardo supo de
las propuestas el 4-sep, por `/sre skills`. Regla: un aviso que pide una
decisión humana no puede ser de una severidad que el filtro descarta.
"""
import agents.sre.briefing as briefing
import skills.procedural.manage as manage


def test_el_aviso_de_propuesta_sale_con_severidad_que_pasa_el_filtro(monkeypatch):
    seen = {}

    def _fake_notify(message, severity="HIGH"):
        seen["severity"] = severity
        seen["message"] = message
        return True

    monkeypatch.setattr("agents.sre.reporter.notify_whatsapp_sre", _fake_notify)

    manage._notify_proposal("sre-oom-killed", "raphael", "destilada del runbook consolidado")

    from agents.sre.reporter import _SEVERITY_RANK
    assert _SEVERITY_RANK[seen["severity"]] >= _SEVERITY_RANK["HIGH"]
    assert "/sre skill approve sre-oom-killed" in seen["message"]


def test_briefing_lista_skills_pendientes(monkeypatch):
    monkeypatch.setattr(
        "skills.procedural.store.list_skills",
        lambda status=None, scope=None: (
            [{"name": "sre-oom-killed"}, {"name": "sre-high-memory"}, {"name": "sre-pod-failed"}]
            if status == "proposed" else []
        ),
    )
    texto = briefing._pending_skills_section()
    assert "3" in texto
    assert "sre-oom-killed" in texto
    assert "/sre skills" in texto


def test_briefing_sin_pendientes_no_agrega_nada(monkeypatch):
    monkeypatch.setattr("skills.procedural.store.list_skills",
                        lambda status=None, scope=None: [])
    assert briefing._pending_skills_section() == ""


def test_briefing_no_se_cae_si_qdrant_falla(monkeypatch):
    def _boom(status=None, scope=None):
        raise RuntimeError("qdrant down")
    monkeypatch.setattr("skills.procedural.store.list_skills", _boom)
    assert briefing._pending_skills_section() == ""
