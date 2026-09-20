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

class OpenAICompatibleEmbedder(Embedder):
    name = "openai-compatible"
    def __init__(self, url, model, key): self.url, self.model, self.key = url.rstrip('/'), model, key
    async def embed(self, texts):
        headers = {"authorization": f"Bearer {self.key}"} if self.key else {}
        async with httpx.AsyncClient(timeout=120) as client:
            suffix = "/embeddings" if self.url.endswith("/v1") else "/v1/embeddings"
            response = await client.post(f"{self.url}{suffix}", headers=headers, json={"model": self.model, "input": texts})
            response.raise_for_status(); return [item["embedding"] for item in sorted(response.json()["data"], key=lambda item:item.get("index",0))]

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
    a, b = np.asarray(left, dtype=np.float32), np.asarray(right, dtype=np.float32)
    denominator = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.dot(a, b) / denominator) if denominator else 0.0

def vector_blob(vector): return np.asarray(vector, dtype=np.float32).tobytes()
def blob_vector(blob): return np.frombuffer(blob, dtype=np.float32).tolist()
def content_hash(text): return hashlib.sha256(text.encode()).hexdigest()
