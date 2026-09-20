from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AFTERWORD_", extra="ignore")
    db: str = "/data/afterword.db"
    embedding_backend: str = "local"
    embedding_model: str = "hashing-768"
    embedding_url: str = "http://ollama:11434"
    embedding_api_key: str = ""
    source_timeout_seconds: float = 15.0
    source_max_bytes: int = 2_000_000
    source_max_items: int = 250
    source_sync_interval_hours: int = 24
    # Contact address included in identified Open Library requests.  The
    # provider remains usable without one, but Open Library asks applications
    # making regular requests to identify themselves.
    openlibrary_contact: str = ""
    cors_origin: str = "http://localhost:3000"
    # Public origin used in digest links. It is deliberately separate from
    # the internal engine URL so Docker installs can link to the web service.
    public_url: str = "http://localhost:5173"
    librarr_allowed_hosts: str = "librarr"
    models_catalog_url: str = "https://models.dev/api.json?type=all"
    models_catalog_cache: str = "/data/models-dev.json"
    models_catalog_ttl_hours: int = 24
    llm_timeout_seconds: float = 45.0
    llm_shadow_max_reads: int = 300
    # Subscription-backed providers run in isolated subprocesses.  Keep the
    # Codex home stable across invocations so device-code auth survives an
    # engine restart, while each ranking request still uses an ephemeral
    # conversation.
    llm_codex_command: str = "codex"
    llm_codex_home: str = "/data/codex-home"
    llm_claude_command: str = "claude"
    llm_subscription_timeout_seconds: float = 90.0
    # Exploration is deliberately opt-in.  The ranking path and response
    # order remain deterministic until an operator enables this flag.
    exploration_enabled: bool = False
    exploration_epsilon: float = 0.0
    exploration_stable_top_k: int = 4

settings = Settings()
