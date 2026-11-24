import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # Ollama (Local Qwen)
    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen3.5:4b")

    # Gemini Cloud
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

    # MongoDB Atlas
    MONGODB_URI: str = os.getenv("MONGODB_URI", "")
    MONGODB_DB_NAME: str = os.getenv("MONGODB_DB_NAME", "zapit_db")

    # SQLite Fallback
    SQLITE_DB_PATH: str = os.getenv("SQLITE_DB_PATH", "data/zapit.db")


settings = Settings()