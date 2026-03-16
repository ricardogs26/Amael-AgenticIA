# Embeddings Cache — Roadmap Phase 8

**Estado**: Placeholder — no implementado

## Responsabilidad

Caché de embeddings generados para evitar llamadas repetidas a Ollama/OpenAI
cuando el mismo texto ya fue embebido previamente.

## Implementación planeada

```python
class EmbeddingsCache:
    """
    Caché Redis + generación lazy de embeddings.
    Reduce llamadas a Ollama para textos repetidos (queries frecuentes, filenames).
    """

    def __init__(self, provider: str = "ollama", ttl_seconds: int = 3600):
        self._redis = get_redis_client()
        self._provider = provider
        self._ttl = ttl_seconds

    def embed(self, text: str) -> List[float]:
        cache_key = f"embed:{hashlib.md5(text.encode()).hexdigest()}"
        cached = self._redis.get(cache_key)
        if cached:
            return json.loads(cached)

        vector = self._generate(text)
        self._redis.setex(cache_key, self._ttl, json.dumps(vector))
        return vector

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        # Check cache para cada texto, generar solo los que faltan
        ...
```

## Impacto esperado

Para queries frecuentes (mismos filenames, saludos, preguntas comunes),
el caché elimina la latencia de embedding (~200-500ms en Ollama).
