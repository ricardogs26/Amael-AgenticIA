# Recepcionista de WhatsApp — contacto desde richardx.dev

**Fecha:** 2026-09-09 · **Estado:** aprobado por Ricardo (diseño), pendiente de plan

## Problema

La sección «Get in touch» de richardx.dev debe ofrecer un link de WhatsApp que
lleve a Amael, para que un visitante desconocido pueda contactar a Ricardo.
Hoy el bridge cierra la puerta a todo número no registrado: contesta «asistente
de uso privado» o, con `allow_access_requests=true`, ofrece `/solicitar`. El
único otro camino, el código `AMAEL-XXXXXXXX`, otorga acceso pleno (rol `user`,
memoria, RAG, Cassiel). Ninguno sirve como recepción.

## Decisiones tomadas

1. **Modo recepcionista** para números desconocidos (no formulario web, no
   `/solicitar` a secas).
2. El visitante **puede seguir platicando en modo limitado** hasta que Ricardo
   decida.
3. El recepcionista responde **solo con el perfil público** de Ricardo (texto
   fijo versionado). La colección Qdrant pública queda como fase 2.
4. Ricardo dispone por WhatsApp de **aprobar, rechazar y responder** a través de
   Amael, sin exponer su número personal.

## Alcance

### Fuera de alcance

- Colección Qdrant «public» (fase 2).
- Panel web de leads en frontend-next.
- Segundo número de WhatsApp para separar recepción de alertas SRE.

## 1. Flujo del visitante (whatsapp-bridge)

Link en el sitio:
`https://wa.me/<número del bridge>?text=Hola%20Amael,%20vengo%20de%20richardx.dev`

En `client.on('message')`, rama `!access.allowed`, el orden pasa a ser:

1. Código de emparejamiento `AMAEL-XXXXXXXX` → sin cambios.
2. `/solicitar <nombre>` → sin cambios (legacy).
3. **Todo lo demás** → `POST {AMAEL_BASE_URL}/api/reception/message` con
   `{phone, text, has_media}` y cabeceras `internalHeaders()`; el bridge
   responde `reply` si no es `null`. Timeout 60 s. Si el backend falla, el
   bridge contesta un texto fijo («Ahora no puedo atenderte, intenta más
   tarde») y loguea.

Adjuntos y notas de voz de desconocidos **no se descargan**: se manda
`has_media=true` con `text=""` y el backend contesta que solo atiende texto.

`/lead …` desde un número registrado se rutea a `POST /api/reception/command`
con `{command, phone}` igual que `/sre` (línea ~606 de `index.js`).

Es cambio solo en el ConfigMap `whatsapp-bridge-code` (imagen sin tocar).

## 2. Backend

### 2.1 Tablas (en `_ensure_schema()`)

```sql
CREATE TABLE IF NOT EXISTS leads (
  id            SERIAL PRIMARY KEY,
  phone         TEXT UNIQUE NOT NULL,
  name          TEXT,
  company       TEXT,
  reason        TEXT,
  status        TEXT NOT NULL DEFAULT 'open',   -- open | approved | rejected
  message_count INT  NOT NULL DEFAULT 0,
  notified_at   TIMESTAMPTZ,                    -- último aviso a Ricardo
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS lead_messages (
  id       SERIAL PRIMARY KEY,
  lead_id  INT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  role     TEXT NOT NULL,   -- visitor | amael | ricardo
  content  TEXT NOT NULL,
  ts       TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`lead_messages` es el contexto de la conversación. **No** se escribe en la
memoria episódica de Zaphkiel ni en `messages`/`conversations`.

### 2.2 Módulo `agents/reception/`

- `profile_public.md` — perfil público de Ricardo (extraído de richardx.dev:
  rol, experiencia, stack, proyectos, disponibilidad). Versionado; es la única
  fuente del recepcionista.
- `prompts.py` — system prompt fijo: Amael es el asistente de Ricardo, responde
  preguntas sobre su perfil con el bloque anterior, va pidiendo nombre, empresa
  y motivo del contacto, y remite todo lo demás a «Ricardo te contactará». Una
  cláusula de límites redactada en positivo (qué sí hace), **sin citar lo
  prohibido** (lección 1.17.2). Idioma: el del visitante.
- `receptionist.py` — `handle_message(phone, text) -> str | None`:
  1. Carga o crea el lead. `status=rejected` o clave Redis
     `reception:silenced:{phone}` → devuelve `None` (sin respuesta, sin LLM).
  2. Guardarraíles (§4). Si se excede, devuelve texto fijo sin LLM.
  3. Construye el prompt con los últimos **8 turnos** de `lead_messages`.
  4. Llama al LLM con `LLM_MODEL_FAST`, `reasoning=False`, `format="json"`,
     timeout 45 s. Esquema: `{"reply": str, "name": str|null,
     "company": str|null, "reason": str|null}`.
  5. Validación en código: `reply` no vacío y ≤ 1000 chars (si no, texto de
     respaldo); los campos se guardan **solo si vienen no vacíos y el lead aún
     no los tiene** (el LLM no sobreescribe datos ya capturados).
  6. Persiste ambos mensajes, incrementa `message_count`.
  7. Aviso a Ricardo (§2.3).
- `commands.py` — dispatcher de `/lead` (§3).
- `notify.py` — envío por el bridge al número admin (reutiliza el helper que
  ya usa Cassiel para entregar por `identity_type='whatsapp'`).

### 2.3 Avisos a Ricardo

- **Primer aviso** cuando `name`, `company` y `reason` quedan completos por
  primera vez (`notified_at IS NULL`):
  `📩 Nuevo contacto #7: Juan · Acme · «cotización de RAG». /lead 7 para verlo`.
- **Resúmenes**: después del primer aviso, cada 5 mensajes nuevos del visitante
  un resumen de una línea (los últimos 5 mensajes truncados, sin LLM).
- Si a los 10 mensajes el lead sigue incompleto, se avisa igual con lo que haya
  (evita que un visitante que nunca da empresa quede invisible).

### 2.4 Router `interfaces/api/routers/reception.py`

| Endpoint | Auth | Body | Respuesta |
|---|---|---|---|
| `POST /api/reception/message` | `X-Internal-Secret` | `{phone, text, has_media}` | `{reply: str\|null}` |
| `POST /api/reception/command` | `X-Internal-Secret` | `{command, phone}` | `{reply: str}` |

`command` verifica que `phone` resuelva a un `user_profile` con `role='admin'`
y `status='active'`; si no, responde «Comando no disponible» sin más detalle.

## 3. Comandos de Ricardo

| Comando | Efecto |
|---|---|
| `/lead` | Lista leads `open` (id, nombre, empresa, mensajes, antigüedad), máx. 10 |
| `/lead <n>` | Ficha + últimos 10 mensajes |
| `/lead <n> aprobar` | Da de alta el número reutilizando la lógica de `redeem_pair_code` (sin código): `user_profile` rol `user` + `user_identity` whatsapp. `status=approved`. Avisa al visitante: «✅ Ricardo te dio acceso a Amael…». Desde ese mensaje el número entra al flujo normal. |
| `/lead <n> rechazar` | `status=rejected` + `reception:silenced:{phone}` TTL 30 d. Sin aviso al visitante. |
| `/lead <n> responder <texto>` | Envía al visitante por el bridge con prefijo `Ricardo:` y guarda `role=ricardo`. El lead sigue `open`. |
| `/lead ayuda` | Lista de comandos |

Errores (id inexistente, sin texto en `responder`) devuelven mensaje corto. Ningún
comando pide confirmación extra: escribirlo por WhatsApp es la confirmación.

## 3.1 Activación (añadido tras la prueba de Ricardo, 2026-09-09)

Un número sin lead solo abre conversación si su mensaje contiene «hola amael»
y «richardx.dev» tras normalizar (minúsculas, sin acentos ni puntuación).
Cualquier otro mensaje de un desconocido se ignora en silencio: sin respuesta
no hay señal para spam. Con lead existente todo mensaje pasa.

## 4. Guardarraíles (todos en código)

| Límite | Valor | Mecanismo | Respuesta al exceder |
|---|---|---|---|
| Mensajes por número / 24 h | 15 (`RECEPTION_MAX_PER_PHONE`) | Redis `reception:rate:{phone}` INCR + EXPIRE 24 h | «Ya tengo tus datos, Ricardo te contactará» (sin LLM) |
| Mensajes globales / día | 100 (`RECEPTION_MAX_PER_DAY`) | Redis `reception:rate:global:{YYYY-MM-DD}` | Igual, sin LLM |
| Longitud del texto | 500 chars | truncado antes del prompt | — |
| Media | — | `has_media` | «Por aquí solo atiendo texto» |
| Contexto | 8 turnos | `lead_messages` LIMIT | — |
| Respuesta del LLM | ≤ 1000 chars, JSON válido | validación | texto de respaldo |
| Aviso a Ricardo | máx. 1 por lead cada 5 mensajes | `notified_at` + `message_count` | — |

Métrica Prometheus: `amael_reception_messages_total{result=replied|rate_limited|silenced|media|error}`
y `amael_reception_leads_total{event=created|completed|approved|rejected}`.

El recepcionista **no** tiene herramientas, RAG, memoria episódica ni acceso a
Cassiel, así que una inyección de prompt solo puede afectar el texto de
respuesta al propio visitante.

## 5. Sitio (GitOps-Infra/profile-site)

Botón «WhatsApp» junto a LinkedIn · GitHub en «Get in touch» (`index.html`
~línea 212) con el link de §1. Push a `develop` publica en Pages. El CSP de
`_headers` no necesita cambio (es un enlace, no un recurso cargado).

## 6. Pruebas

Unitarias (sin LLM real, `ChatOllama` mockeado):

- Extracción JSON: campos parciales, campos vacíos no sobreescriben, JSON
  inválido → respaldo, `reply` >1000 → respaldo.
- Tope por número y global; el mensaje 16 no llama al LLM.
- `rejected`/silenciado → `None` y sin LLM.
- Primer aviso solo al completar los tres campos; resumen cada 5; aviso forzado
  a los 10.
- `command` con `phone` no admin → «Comando no disponible».
- `aprobar` crea `user_profile` + `user_identity` y `check_access` devuelve
  `allowed=True` después.
- `responder` guarda `role=ricardo` y llama al bridge.

E2E manual: un número de Ricardo no registrado escribe desde el link, completa
datos, Ricardo ve `/lead`, responde, aprueba, y el número entra al flujo normal.

## 7. Despliegue

1. Backend: bump de manifest `05-backend-deployment.yaml` (el CI construye y
   despliega por push a main). Nuevas env en el ConfigMap: `RECEPTION_MAX_PER_PHONE`,
   `RECEPTION_MAX_PER_DAY` (con defaults en `settings.py`).
2. Bridge: actualizar ConfigMap `whatsapp-bridge-code` + `rollout restart`.
3. Sitio: push a `develop` de GitOps-Infra.

## Fase 2 (futuro, no en este spec)

Colección Qdrant `public` alimentada por Ricardo (CV largo, casos, tarifas) que
el recepcionista consulte con `retrieve_documents` acotado a esa colección.
