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

    # Hospital identity (letterhead) — used on prescription pads, reports,
    # and anywhere else the app needs to print/display the hospital's own
    # details. Override any of these via environment variables / .env
    # without touching code, e.g. if the phone line changes or this
    # deployment is reused for a different facility.
    HOSPITAL_NAME = os.environ.get("HOSPITAL_NAME", "Mwai Kibaki Hospital")
    HOSPITAL_ADDRESS = os.environ.get("HOSPITAL_ADDRESS", "P.O Box 541-10106, Othaya")
    HOSPITAL_PHONE_1 = os.environ.get("HOSPITAL_PHONE_1", "0782 620 353")
    HOSPITAL_PHONE_2 = os.environ.get("HOSPITAL_PHONE_2", "0782 620 345")

    # File uploads (delivery notes, PO scans, etc. if needed later)
    UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
    MAX_CONTENT_LENGTH = 8 * 1024 * 1024  # 8MB