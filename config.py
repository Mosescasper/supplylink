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
    REQUISITION_SLA_HOURS = 48
    REQUISITION_MAX_MULTIPLIER = 1.5   # can't request more than 1.5x this dept's avg monthly usage of an item
    REQUISITION_CONSUMPTION_MONTHS = 3  # lookback window used to calculate that average

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

    # Only this email can create user accounts (register/ward-account routes)
    SUPER_ADMIN_EMAIL = os.environ.get("SUPER_ADMIN_EMAIL", "mm5134065@gmail.com")

    # Only this email can create user accounts (register/ward-account routes)
    SUPER_ADMIN_EMAIL = os.environ.get("SUPER_ADMIN_EMAIL", "mm5134065@gmail.com")
    # Additional email(s) also allowed to create accounts, without having
    # the super admin's special protections (they CAN be deleted or
    # force-logged-out normally, unlike SUPER_ADMIN_EMAIL). Comma-separate
    # for more than one, e.g. "person1@x.com,person2@x.com".
    ACCOUNT_CREATOR_EMAILS = [
        e.strip().lower()
        for e in os.environ.get("ACCOUNT_CREATOR_EMAILS", "").split(",")
        if e.strip()
    ]

    # Cloudflare R2 / Backblaze B2 (S3-compatible) storage for patient documents
    R2_ENDPOINT_URL = os.environ.get("R2_ENDPOINT_URL")
    R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID")
    R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
    R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "supplylink-documents")
    R2_REGION = os.environ.get("R2_REGION", "us-east-005")