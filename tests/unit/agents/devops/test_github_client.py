"""
Pruebas del cliente GitHub que sustituyó a Bitbucket tras retirar la POC.

El foco está en las diferencias reales entre ambas APIs — que es donde puede
romperse el flujo GitOps sin que nadie lo note:
  - GitHub exige el `sha` del blob para actualizar un archivo; Bitbucket no.
  - Los nombres de estrategia de merge son distintos.
  - agent.py consume `pr["id"]` y `pr["links"]["html"]["href"]` (forma Bitbucket).
"""
import base64
import json

import httpx
import pytest
import respx

from agents.devops import github_client as gh

GH = "https://api.github.com"


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")


@respx.mock
async def test_read_file_decodifica_base64():
    contenido = "apiVersion: apps/v1\nkind: Deployment\n"
    respx.get(f"{GH}/repos/ricardogs26/Amael-AgenticIA/contents/k8s/app.yaml").mock(
        return_value=httpx.Response(
            200,
            json={
                "content": base64.b64encode(contenido.encode()).decode(),
                "encoding": "base64",
                "sha": "abc123",
            },
        )
    )

    assert await gh.read_file("ricardogs26", "Amael-AgenticIA", "k8s/app.yaml") == contenido


@respx.mock
async def test_commit_file_incluye_sha_cuando_el_archivo_existe():
    """Sin el sha, GitHub rechaza la actualización con 409."""
    respx.get(f"{GH}/repos/o/r/contents/k8s/app.yaml").mock(
        return_value=httpx.Response(200, json={"sha": "sha-viejo", "encoding": "base64", "content": ""})
    )
    put = respx.put(f"{GH}/repos/o/r/contents/k8s/app.yaml").mock(
        return_value=httpx.Response(200, json={"commit": {"sha": "nuevo"}})
    )

    await gh.commit_file("o", "r", "k8s/app.yaml", "contenido", "fix/rama", "msg")

    body = json.loads(put.calls[0].request.read())
    assert body["sha"] == "sha-viejo"
    assert body["branch"] == "fix/rama"


@respx.mock
async def test_commit_file_omite_sha_cuando_el_archivo_es_nuevo():
    """Mandar un sha inexistente en un archivo nuevo también falla."""
    respx.get(f"{GH}/repos/o/r/contents/nuevo.yaml").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    put = respx.put(f"{GH}/repos/o/r/contents/nuevo.yaml").mock(
        return_value=httpx.Response(201, json={"commit": {"sha": "x"}})
    )

    await gh.commit_file("o", "r", "nuevo.yaml", "contenido", "main", "msg")

    assert "sha" not in json.loads(put.calls[0].request.read())


@respx.mock
async def test_create_pr_expone_la_forma_que_consume_agent_py():
    respx.post(f"{GH}/repos/o/r/pulls").mock(
        return_value=httpx.Response(
            201, json={"number": 42, "html_url": "https://github.com/o/r/pull/42"}
        )
    )

    pr = await gh.create_pr("o", "r", "titulo", "cuerpo", "fix/rama")

    # agent.py lee exactamente estas dos rutas
    assert pr["id"] == 42
    assert pr["links"]["html"]["href"] == "https://github.com/o/r/pull/42"


@respx.mock
async def test_merge_pr_traduce_estrategias_y_expone_merge_commit_hash():
    ruta = respx.put(f"{GH}/repos/o/r/pulls/7/merge").mock(
        return_value=httpx.Response(200, json={"sha": "deadbeef", "merged": True})
    )

    result = await gh.merge_pr("o", "r", 7, message="m", merge_strategy="merge_commit")

    assert json.loads(ruta.calls[0].request.read())["merge_method"] == "merge"
    # agent.py lee merge_result["merge_commit"]["hash"]
    assert result["merge_commit"]["hash"] == "deadbeef"


@respx.mock
async def test_merge_pr_mapea_squash_y_fast_forward():
    respx.put(f"{GH}/repos/o/r/pulls/1/merge").mock(
        return_value=httpx.Response(200, json={"sha": "a"})
    )
    await gh.merge_pr("o", "r", 1, merge_strategy="squash")
    assert json.loads(respx.calls[-1].request.read())["merge_method"] == "squash"

    await gh.merge_pr("o", "r", 1, merge_strategy="fast_forward")
    assert json.loads(respx.calls[-1].request.read())["merge_method"] == "rebase"


@respx.mock
async def test_approve_pr_explica_el_422_de_auto_aprobacion():
    respx.post(f"{GH}/repos/o/r/pulls/9/reviews").mock(
        return_value=httpx.Response(422, json={"message": "Can not approve your own pull request"})
    )

    with pytest.raises(RuntimeError, match="mismo token que lo creó"):
        await gh.approve_pr("o", "r", 9)


@respx.mock
async def test_close_pr_devuelve_el_codigo_http():
    respx.patch(f"{GH}/repos/o/r/pulls/3").mock(return_value=httpx.Response(200, json={}))
    assert await gh.close_pr("o", "r", 3) == 200

    respx.patch(f"{GH}/repos/o/r/pulls/4").mock(return_value=httpx.Response(404, json={}))
    assert await gh.close_pr("o", "r", 4) == 404


@respx.mock
async def test_get_branch_head():
    respx.get(f"{GH}/repos/o/r/git/ref/heads/main").mock(
        return_value=httpx.Response(200, json={"object": {"sha": "cafe1234"}})
    )
    assert await gh.get_branch_head("o", "r") == "cafe1234"


@respx.mock
async def test_search_file_in_repo_confirma_por_contenido():
    """Dos rutas plausibles: gana la que realmente declara el recurso."""
    respx.get(f"{GH}/repos/o/r/git/trees/main").mock(
        return_value=httpx.Response(200, json={"truncated": False, "tree": [
            {"type": "blob", "path": "README.md"},
            {"type": "blob", "path": "k8s/agents/06-demo-oom.yaml"},
            {"type": "blob", "path": "k8s/demo/amael-demo-oom.yaml"},
        ]})
    )

    def _yaml(nombre):
        cuerpo = f"kind: Deployment\nmetadata:\n  name: {nombre}\n"
        return httpx.Response(200, json={
            "content": base64.b64encode(cuerpo.encode()).decode(),
            "encoding": "base64", "sha": "s",
        })

    respx.get(f"{GH}/repos/o/r/contents/k8s/demo/amael-demo-oom.yaml").mock(
        return_value=_yaml("otro-nombre")
    )
    respx.get(f"{GH}/repos/o/r/contents/k8s/agents/06-demo-oom.yaml").mock(
        return_value=_yaml("amael-demo-oom")
    )

    found = await gh.search_file_in_repo("o", "r", "amael-demo-oom")
    assert found == "k8s/agents/06-demo-oom.yaml"


@respx.mock
async def test_search_file_in_repo_cae_a_coincidencia_por_ruta():
    respx.get(f"{GH}/repos/o/r/git/trees/main").mock(
        return_value=httpx.Response(200, json={"truncated": False, "tree": [
            {"type": "blob", "path": "k8s/amael-demo-oom.yaml"},
        ]})
    )
    respx.get(f"{GH}/repos/o/r/contents/k8s/amael-demo-oom.yaml").mock(
        return_value=httpx.Response(200, json={
            "content": base64.b64encode(b"kind: Deployment\n").decode(),
            "encoding": "base64", "sha": "s",
        })
    )

    assert await gh.search_file_in_repo("o", "r", "amael-demo-oom") == "k8s/amael-demo-oom.yaml"


@respx.mock
async def test_search_file_in_repo_sin_yamls_devuelve_none():
    respx.get(f"{GH}/repos/o/r/git/trees/main").mock(
        return_value=httpx.Response(200, json={"truncated": False, "tree": [
            {"type": "blob", "path": "README.md"},
        ]})
    )
    assert await gh.search_file_in_repo("o", "r", "amael-demo-oom") is None


async def test_search_file_in_repo_rechaza_nombres_invalidos():
    """No mandar a la API de búsqueda algo que no es un nombre de recurso K8s."""
    assert await gh.search_file_in_repo("o", "r", "../../etc/passwd") is None
    assert await gh.search_file_in_repo("o", "r", "Nombre Con Espacios") is None


def test_falta_de_token_falla_claro(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="GITHUB_TOKEN no configurado"):
        gh._headers()
