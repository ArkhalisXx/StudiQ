import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTANCE = ROOT / 'instance'
DATABASE_URL = os.getenv('DATABASE_URL', f'sqlite:///{INSTANCE / "coworking.db"}')
