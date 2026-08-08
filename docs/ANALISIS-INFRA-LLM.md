# Análisis de infraestructura LLM — Ollama sobre RTX 5070

**Fecha**: 7-ago-2026 · **Método**: todo medido en el cluster, nada de valores de catálogo.

---

## 1. Inventario medido

### Hardware
| | |
|---|---|
| GPU | RTX 5070, **12 227 MiB** VRAM |
| En uso | 9 410 MiB (ollama) + ~1 300 MiB (overhead CUDA/driver, no reclamable) |
| **Libre real** | **~1 000 MiB** — este es el presupuesto de todas las decisiones |

### Dos servidores Ollama (0.32.5)

| | `ollama-deployment` (GPU) | `ollama-cpu-deployment` (tier profundo) |
|---|---|---|
| Env | `NUM_PARALLEL=1`, `CONTEXT_LENGTH=4096`, `KEEP_ALIVE=24h` | `NUM_PARALLEL=1`, `CONTEXT_LENGTH=8192` |
| Recursos | 1 GPU, 4 CPU, 16 Gi | 10 CPU, 24 Gi |
| Rol | interactivo: chat, planner, trader, Cassiel, visión | nocturno: postmortems, consolidadores |

### Modelos en disco (GPU)

| Modelo | Tamaño | Cuant. | Uso real |
|---|---|---|---|
| `qwen3:14b` | 9.3 GB | Q4_K_M | principal — residente 24/7 |
| `qwen3:1.7b` | 1.4 GB | Q4_K_M | **retirado** — la ruta rápida usa el 14b sin thinking (`LLM_MODEL_FAST=qwen3:14b`); solo ocupa disco |
| `qwen2.5vl:3b` | 3.2 GB | Q4_K_M | visión (/grafana) |
| `nomic-embed-text` | 0.3 GB | F16 | embeddings — **ya corre 100 % en CPU** |
| `qwen2.5:14b` | 9.0 GB | Q4_K_M | **legacy, sin consumidores** |
| `glm4` | 5.5 GB | Q4_0 | **legacy, sin consumidores** |

En CPU: `qwen3:30b-a3b` (MoE 30B/3B activos) — 18.6 GB, para el tier profundo.

---

## 2. Hallazgos (los tres importan más que cualquier tuning)

### H1 — La ruleta del runner: el contexto efectivo lo decide *quien carga primero*

El `num_ctx` **no es del servidor ni del modelo: es del runner cargado**. La
cadena verificada hoy:

1. `OLLAMA_CONTEXT_LENGTH=4096` está en el deployment ✓
2. Pero a las 15:21 el servidor truncaba en `limit=2050` — un runner de 2048
   cargado temprano quedó **clavado por keep_alive** y todas las peticiones
   sin `num_ctx` explícito lo heredaron el día entero.
3. Mis pruebas con `num_ctx=8192/6144` **recargaron** el runner; al volver a
   cargar limpio tomó el 4096 del env y el trader "sanó solo".

Corolario crítico: **si un consumidor pide un `num_ctx` distinto al de los
demás, cada alternancia recarga el modelo** (~10-20 s de reload en cada
switch). Un `num_ctx` por-request en el trader alternando con el chat a 4096
sería thrashing de recargas cada 5 minutos. La configuración correcta es UN
contexto uniforme a nivel servidor, y ningún cliente mandando el suyo.

### H2 — El presupuesto de VRAM, medido

KV cache fp16 de qwen3:14b ≈ 160 KB/token:

| `num_ctx` | KV extra vs 2048 | Total | ¿Cabe en el GB libre? |
|---|---|---|---|
| 4096 (actual) | +0.33 GB | 9.6 GB | ✓ (medido: 9.6/9.6, 100 % GPU) |
| 6144 fp16 | +0.9 GB | 10.5 GB | ✗ (medido: derrama a CPU) |
| 8192 fp16 | +1.2 GB | 10.8 GB | ✗ (medido: derrama a CPU) |
| **6144 + KV q8_0** | **+0.45 GB** | **~10.0 GB** | **✓ estimado — verificar al aplicar** |
| 8192 + KV q8_0 | +0.65 GB | ~10.2 GB | límite; probable pero justo |

Corrección (8-ago): el 1.7b está RETIRADO (`LLM_MODEL_FAST=qwen3:14b`) — la
ruta rápida y el pipeline comparten el mismo runner del 14b, así que el
desalojo mutuo que preocupaba aquí no existe. La visión (3.2 GB) sí desaloja
al 14b cuando se usa /grafana.

### H3 — El estado actual funciona *por suerte*

El prompt equity mide 4 336 tokens contra 4 096 de ventana: el recorte actual
(~240 tokens) solo muerde el inicio del system prompt y el modelo sobrevive.
Cualquier símbolo, posición o trade adicional profundiza el corte hasta volver
a producir respuestas degeneradas. **No es un estado estable.**

---

## 3. Parámetros de Ollama, uno por uno

Los que importan en esta infraestructura, con su significado y mi lectura:

| Parámetro | Qué controla | Valor hoy | Lectura |
|---|---|---|---|
| `OLLAMA_CONTEXT_LENGTH` | ventana default de los runners (tokens que el modelo "tiene en mente") | 4096 GPU / 8192 CPU | GPU quedó corto para el prompt real del trader (4.3k) |
| `OLLAMA_KV_CACHE_TYPE` | precisión de la caché de atención: `f16` (default), `q8_0` (½ memoria), `q4_0` (¼, con pérdida visible) | f16 | la palanca barata: q8_0 es prácticamente sin pérdida |
| `OLLAMA_FLASH_ATTENTION` | kernel de atención optimizado; **prerequisito** para KV cuantizada | off | además suele *mejorar* latencia en generación |
| `OLLAMA_NUM_PARALLEL` | slots concurrentes por modelo; la VRAM de contexto se multiplica por N | 1 | correcto para 12 GB — no tocar |
| `OLLAMA_KEEP_ALIVE` | cuánto vive un modelo cargado sin uso | 24h | razonable; el `-1` que ancla al 14b viene de un cliente, no del servidor |
| `OLLAMA_MAX_LOADED_MODELS` | tope de modelos simultáneos | default (3×GPU) | irrelevante: la VRAM limita antes |
| `num_predict` (cliente) | tope de tokens de respuesta | 512 fast / libre resto | sano |
| Cuantización de pesos | Q4_K_M en todos los qwen | Q4_K_M | el sweet spot calidad/VRAM; subir a Q5/Q6 no cabe |

---

## 4. Opciones de decisión

### Opción A — KV q8_0 + contexto uniforme 6144 (mi recomendación)

En `ollama-deployment`:
```yaml
- {name: OLLAMA_CONTEXT_LENGTH, value: "6144"}
- {name: OLLAMA_FLASH_ATTENTION, value: "1"}
- {name: OLLAMA_KV_CACHE_TYPE,  value: "q8_0"}
```
Y **ningún cliente manda `num_ctx`** (incluido el trader — se retira el
por-request para no provocar la ruleta de H1).

- ✅ El prompt del trader cabe con ~40 % de margen de crecimiento
- ✅ Un solo runner uniforme: cero thrashing de recargas
- ✅ Latencia esperada igual o mejor (flash attention); costo q8: ±1-3 %
- ⚠️ Toca el Ollama compartido; reinicio ~1-2 min (procedimiento pod delete)
- Verificación: residencia 100 % GPU, sonda de 5k tokens sin truncar, latencia
  fast path ±10 % del baseline (0.6-0.8 s), ciclo equity real sano
- Rollback: quitar 2 env + reinicio (2 min)

### Opción B — Dieta de prompt del trader (para caber en 4096)

Recortar contexto del analyzer: trades 10→5, señales con 2 decimales, system
más compacto (~4.3k → ~3.4k tokens).

- ✅ No toca infra compartida
- ❌ Recorta los insumos de decisión (producto de trading)
- ❌ El margen muere con el próximo símbolo — vuelve a ser mecha corta

### Opción C — Limpieza de VRAM complementaria (compatible con A o B)

- Borrar `qwen2.5:14b` y `glm4` del disco (14.5 GB de disco, no afectan VRAM
  pero ensucian inventario) — sin consumidores conocidos; **confirmar antes**.
- Aceptar el desalojo mutuo 14b↔1.7b como está (ya sucede), o evaluar si la
  ruta rápida justifica su modelo aparte contra el costo de recargas.

### Lo que NO recomiendo

- `num_ctx` por-request divergente (thrashing de H1)
- `q4_0` en KV (pérdida real de calidad)
- Analyzer al tier CPU (60-140 s solo de lectura del prompt — descartado por latencia)
- Fine-tuning de pesos (el skill auditado): no resuelve nada de esto; es otra
  conversación para cuando quieras especializar el modelo con tus datos

---

## 5. Registro de la sesión de diagnóstico

Cadena causal completa del incidente del trader (7-ago):

```
1.0.29–31 (6-ago) engordan el prompt equity  →  4 336 tokens
   + runner clavado en 2048 por un cargador temprano (H1)
   + Ollama trunca POR EL FRENTE conservando 4 tokens
   = system prompt muerto → el LLM responde {"error": "entrada no válida"}
   = registrado como hold conf=0.00 → «bajas cada ~6 min» en Grafana
```

Fixes ya desplegados (trader 1.0.33): respuesta degenerada → camino de error
con diagnóstico completo, nunca una "decisión". Pendiente de decisión: A/B/C.
