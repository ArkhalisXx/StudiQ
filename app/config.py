import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env', override=False)
INSTANCE = ROOT / 'instance'
DATABASE_URL = os.getenv('DATABASE_URL', f'sqlite:///{INSTANCE / "coworking.db"}')
