import os

API_TOKEN = os.environ.get("API_TOKEN", "")
DB_URL = os.environ.get("DB_URL", "postgres://localhost:5432/demo")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "info")
