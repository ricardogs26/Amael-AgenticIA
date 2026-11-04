# llama-swap (llama.cpp) en lab-home: archivado

Rama `experimento/llama-swap`, **sin merge a main**. Probado del 3 al 4 de noviembre de 2026 (A45 + A51) y retirado del cluster el mismo 4-nov. El cluster sigue con Ollama y `gemma4-uncensored-amael` (Gemma 4 12B, 2×64K).

Veredicto y números: `~/obsidian-vault/amael/2026-11-04-llama-server-vs-ollama.md`. Reporte HTML y datos crudos: `~/k8s-lab/docs/analisis/2026-11-03-llama-server/`.

## Por qué no se quedó

- Con Gemma 12B, llama-server da solo +16 % con 2 clientes, y no tiene la API de Ollama (`/api/chat`) que usa Amael; solo `/v1`.
- Los MoE (Gemma 4 26B-A4B y Qwen3.6-35B-A3B) son más listos, pero con 12 GiB dan 21–27 t/s con 2 clientes y 85–144 s de TTFT a 60K.
- Qwen3.8-27B denso corre a 5 t/s.

## Archivos

| Archivo | Qué es |
|---|---|
| `k8s/infrastructure/10-llama-swap.yaml` | Deployment (replicas 0, Recreate, 1 GPU), Service `:8080` y montajes: hostPath `/home/richardx/llm-modelos` y PVC de Ollama de solo lectura |
| `docs/llama-swap/genera_config.py` | Genera el ConfigMap `llama-swap-config` (19 entradas: `--n-cpu-moe` por modelo, 2×64K, KV q8) |
| `docs/llama-swap/config.yaml` | La configuración que se usó |
| `docs/llama-swap/descarga.sh`, `descarga-densos.sh` | Descarga de los GGUF con `curl -C -` |

## Para volver a usarlo

1. Los GGUF se borraron el 4-nov (A52–A54). Hay que bajarlos de nuevo con `descarga.sh` (~40 min por modelo).
2. En `genera_config.py`, las entradas `-auto` no deben llevar `-ngl`: con `-fit` llama-server aborta. El generador ya está corregido; `config.yaml` todavía no.
3. Aplicar el ConfigMap y el manifest. Para usar la GPU, bajar Ollama a 0 y subir llama-swap a 1, porque no caben los dos.
4. Portal: `/ui`. Chat por modelo: `/upstream/<modelo>/`.
