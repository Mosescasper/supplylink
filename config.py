import os
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production")

    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        "postgresql://postgres:12345@localhost:5432/supplylink"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Business rules
    EXPIRY_ALERT_MONTHS = 6          # FEFO alert window
    LOW_STOCK_ALERT = True

    # Currency / locale
    CURRENCY = "KES"

    # File uploads (delivery notes, PO scans, etc. if needed later)
    UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
    MAX_CONTENT_LENGTH = 8 * 1024 * 1024  # 8MB