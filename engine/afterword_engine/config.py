from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AFTERWORD_", extra="ignore")
    db: str = "/data/afterword.db"
    backup_dir: str = ""
    backup_interval_hours: int = 24
    backup_retention_count: int = 7
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
    # Exploration is deliberately opt-in.  The ranking path and response
    # order remain deterministic until an operator enables this flag.
    exploration_enabled: bool = False
    exploration_epsilon: float = 0.0
    exploration_stable_top_k: int = 4

settings = Settings()
