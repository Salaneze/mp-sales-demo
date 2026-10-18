import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    wb_token: str = os.getenv("WB_TOKEN", "")
    # Ниже этого остатка (сумма по всем складам) приходит «заканчивается»
    low_stock: int = int(os.getenv("LOW_STOCK", "5"))
    db_path: Path = Path(os.getenv("DB_PATH", "data/sales.db"))
    xlsx_path: Path = Path(os.getenv("XLSX_PATH", "data/sales.xlsx"))
    tg_token: str = os.getenv("TG_TOKEN", "")
    tg_chat_id: str = os.getenv("TG_CHAT_ID", "")
    gsheet_id: str = os.getenv("GSHEET_ID", "")
    gsheet_credentials: str = os.getenv("GSHEET_CREDENTIALS", "")


settings = Settings()
