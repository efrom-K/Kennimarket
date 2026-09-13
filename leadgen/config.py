import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    lm_studio_base_url: str = os.getenv("LM_STUDIO_BASE_URL", "http://localhost:1234/v1")
    lm_studio_model: str = os.getenv("LM_STUDIO_MODEL", "local-model")

    dgis_api_key: str = os.getenv("DGIS_API_KEY", "")

    target_leads: int = int(os.getenv("TARGET_LEADS", "1000"))
    db_path: str = os.getenv("DB_PATH", "leadgen.db")


settings = Settings()
