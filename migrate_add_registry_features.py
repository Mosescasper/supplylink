"""
migrate_add_registry_features.py
SupplyLink -- One-time migration adding registry-department features:
patient_movements (admission/transfer/discharge log), patient_files (file
tracking), and appointments (scheduling).

All three are brand new tables -- created via db.create_all(), which only
creates tables that don't already exist and never touches existing ones.
No existing data is affected.

Usage:
    python migrate_add_registry_features.py
"""

from app import app
from extensions import db

with app.app_context():
    print("Creating patient_movements, patient_files, appointments tables if missing...")
    db.create_all()
    print("Done.")