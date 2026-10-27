from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PW_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://pocketwatcher:pocketwatcher@localhost:5432/pocketwatcher"
    # Fernet key for secrets at rest. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    fernet_key: str = ""
    app_base_url: str = "http://localhost:8000"
    # Signs the session cookie. Any long random string; rotating it signs everyone out.
    session_secret: str = ""
    secure_cookies: bool = True  # set False only for local http development

    workos_api_key: str = ""
    workos_client_id: str = ""

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "PocketWatcher <alerts@pocketwatcher.dev>"
    smtp_starttls: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
