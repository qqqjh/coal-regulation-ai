import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "Coal Regulation AI Backend"
    API_V1_STR: str = "/api"

    # AI 配置（后端审查/对话用，OpenAI 兼容接口）
    OPENAI_API_KEY: str = "YOUR_OPENAI_API_KEY"
    OPENAI_BASE_URL: str = "https://chat.cloudapi.vip/v1"
    # v9 引擎与主智能体用的 DashScope（qwen）配置
    DASHSCOPE_API_KEY: str = ""
    DASHSCOPE_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    # 路径配置
    BACKEND_DIR: Path = Path(__file__).resolve().parent.parent.parent          # backend/
    PROJECT_ROOT: Path = BACKEND_DIR.parent                                    # 仓库根（v9 引擎所在）
    DATA_DIR: Path = BACKEND_DIR / "data"
    CHROMA_DB_DIR: Path = DATA_DIR / "chroma_db"
    UPLOAD_DIR: Path = DATA_DIR / "uploads"

    # v9 集成：与 worker 共享的队列/任务库（位于仓库根 data/ 下）
    V9_QUEUE_DB: Path = PROJECT_ROOT / "data" / "review_queue_v9.db"

    # MinerU：规则文档上传时推荐连接本机常驻 mineru-api
    MINERU_API_URL: str = "http://127.0.0.1:51071"

    # 数据库
    SQLITE_URL: str = f"sqlite+aiosqlite:///{(BACKEND_DIR / 'data' / 'app.db')}"

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parent.parent.parent / ".env"),
        extra="ignore",
    )


settings = Settings()

# 确保目录存在
settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
settings.CHROMA_DB_DIR.mkdir(parents=True, exist_ok=True)
settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
settings.V9_QUEUE_DB.parent.mkdir(parents=True, exist_ok=True)
