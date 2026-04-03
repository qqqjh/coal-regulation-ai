import os
from pydantic_settings import BaseSettings
from pathlib import Path

class Settings(BaseSettings):
    PROJECT_NAME: str = "Coal Regulation AI Backend"
    API_V1_STR: str = "/api"
    
    # AI 配置
    OPENAI_API_KEY: str = "sk-BQcsFzKuUHQ360c0xUzLwz47G0hJt2AKtFOFmUkcLK67JgH3"  # 替换为您的 Key
    OPENAI_BASE_URL: str = "https://chat.cloudapi.vip/v1"
    
    # 路径配置
    BASE_DIR: Path = Path(__file__).resolve().parent.parent.parent
    DATA_DIR: Path = BASE_DIR / "data"
    CHROMA_DB_DIR: Path = DATA_DIR / "chroma_db"
    UPLOAD_DIR: Path = DATA_DIR / "uploads"
    
    # 数据库
    SQLITE_URL: str = f"sqlite+aiosqlite:///{DATA_DIR}/app.db"

    class Config:
        env_file = ".env"

settings = Settings()

# 确保目录存在
settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
settings.CHROMA_DB_DIR.mkdir(parents=True, exist_ok=True)
settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
