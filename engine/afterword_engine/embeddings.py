import hashlib
import os
import time
import asyncio
import httpx
import numpy as np
from sklearn.feature_extraction.text import HashingVectorizer
from .config import settings

class Embedder:
    name = "base"
    model = ""
    async def embed(self, texts: list[str]) -> list[list[float]]: raise NotImplementedError
    async def health(self):
        started = time.perf_counter()
        try:
            vectors = await self.embed(["Bookward embedding health check"])
            dimensions = len(vectors[0]) if vectors else 0
            if not dimensions: raise ValueError("Provider returned an empty vector")
            return {"ok": True, "backend": self.name, "model": self.model, "dimensions":dimensions, "latency_ms":round((time.perf_counter()-started)*1000)}
        except Exception as exc:
            return {"ok":False,"backend":self.name,"model":self.model,"error":str(exc)[:500]}

class LocalHashingEmbedder(Embedder):
    name = "local"
    model = "hashing-768"
    def __init__(self): self.vectorizer = HashingVectorizer(n_features=768, alternate_sign=False, norm="l2", ngram_range=(1, 2), stop_words="english")
    async def embed(self, texts): return self.vectorizer.transform(texts).toarray().astype("float32").tolist()

class FastEmbedder(Embedder):
    name = "fastembed"
    def __init__(self, model):
        from fastembed import TextEmbedding
        self.model = model or "BAAI/bge-small-en-v1.5"
        self.client = TextEmbedding(model_name=self.model, cache_dir=os.getenv("AFTERWORD_MODEL_CACHE", "/data/models"))
    async def embed(self, texts): return await asyncio.to_thread(lambda: [np.asarray(vector, dtype=np.float32).tolist() for vector in self.client.embed(texts)])

class SentenceTransformerEmbedder(Embedder):
    name = "sentence-transformers"
    def __init__(self, model):
        from sentence_transformers import SentenceTransformer
        self.model = model
        self.client = SentenceTransformer(model)
    async def embed(self, texts): return await asyncio.to_thread(lambda: self.client.encode(texts, normalize_embeddings=True).astype("float32").tolist())

class OllamaEmbedder(Embedder):
    name = "ollama"
    def __init__(self, url, model): self.url, self.model = url.rstrip('/'), model
    async def embed(self, texts):
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(f"{self.url}/api/embed", json={"model": self.model, "input": texts})
            response.raise_for_status(); return response.json()["embeddings"]


def _ordered_openai_embeddings(payload, expected_count: int) -> list[list[float]]:
    """Return vectors in request order, rejecting ambiguous provider indexes."""

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Embedding provider response must contain a data list")
    data = payload["data"]
    if len(data) != expected_count:
        raise ValueError("Embedding provider returned the wrong number of vectors")
    if any(not isinstance(item, dict) for item in data):
        raise ValueError("Embedding provider returned malformed vector entries")
    has_indexes = ["index" in item for item in data]
    if not all(has_indexes):
        if not any(has_indexes):
            raise ValueError("Embedding provider response omitted vector indexes")
        raise ValueError("Embedding provider returned incomplete vector indexes")
    if all(has_indexes):
        indexes = [item["index"] for item in data]
        if (
            any(isinstance(index, bool) or not isinstance(index, int) for index in indexes)
            or sorted(indexes) != list(range(expected_count))
        ):
            raise ValueError("Embedding provider returned invalid vector indexes")
        data = sorted(data, key=lambda item: item["index"])
    vectors = [item.get("embedding") for item in data]
    if any(not isinstance(vector, list) or not vector for vector in vectors):
        raise ValueError("Embedding provider returned malformed vectors")
    return vectors

class OpenAICompatibleEmbedder(Embedder):
    name = "openai-compatible"
    def __init__(self, url, model, key): self.url, self.model, self.key = url.rstrip('/'), model, key
    async def embed(self, texts):
        headers = {"authorization": f"Bearer {self.key}"} if self.key else {}
        async with httpx.AsyncClient(timeout=120) as client:
            suffix = "/embeddings" if self.url.endswith("/v1") else "/v1/embeddings"
            response = await client.post(f"{self.url}{suffix}", headers=headers, json={"model": self.model, "input": texts})
            response.raise_for_status()
            return _ordered_openai_embeddings(response.json(), len(texts))

def get_embedder(backend=None, model=None, url=None, api_key=None):
    backend = backend or settings.embedding_backend
    defaults = {"local":"hashing-768","fastembed":"BAAI/bge-small-en-v1.5","sentence-transformers":"sentence-transformers/all-MiniLM-L6-v2","ollama":"qwen3-embedding:0.6b"}
    model = defaults.get(backend, "") if not model or model == "hashing-768" and backend != "local" else model
    if backend == "local": return LocalHashingEmbedder()
    if backend == "fastembed": return FastEmbedder(model or "BAAI/bge-small-en-v1.5")
    if backend == "sentence-transformers": return SentenceTransformerEmbedder(model or "sentence-transformers/all-MiniLM-L6-v2")
    if backend == "ollama": return OllamaEmbedder(url or settings.embedding_url, model or "qwen3-embedding:0.6b")
    if backend == "openai-compatible":
        if not url: raise ValueError("OpenAI-compatible providers require an endpoint URL")
        return OpenAICompatibleEmbedder(url, model, api_key or settings.embedding_api_key)
    raise ValueError(f"Unsupported embedding backend: {backend}")

def cosine(left, right):
    try:
        with np.errstate(over="ignore", invalid="ignore"):
            a = np.asarray(left, dtype=np.float64)
            b = np.asarray(right, dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Embedding vectors must contain numeric values") from exc
    if a.ndim != 1 or b.ndim != 1 or a.shape != b.shape or not a.size:
        raise ValueError("Embedding vectors must be non-empty rows with matching dimensions")
    if not np.isfinite(a).all() or not np.isfinite(b).all():
        raise ValueError("Embedding vectors must contain only finite values")
    a_scale, b_scale = np.max(np.abs(a)), np.max(np.abs(b))
    if not a_scale or not b_scale:
        return 0.0
    a = a / a_scale
    b = b / b_scale
    a_norm, b_norm = np.linalg.norm(a), np.linalg.norm(b)
    return float(np.dot(a, b) / (a_norm * b_norm))

def vector_blob(vector): return np.asarray(vector, dtype=np.float32).tobytes()
def blob_vector(blob): return np.frombuffer(blob, dtype=np.float32).tolist()
def content_hash(text): return hashlib.sha256(text.encode()).hexdigest()
