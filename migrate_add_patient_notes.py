"""
migrate_add_patient_notes.py
SupplyLink -- One-time migration to add the patient_notes table.

This only creates a brand new table (via db.create_all(), which never
touches existing tables) -- nothing else changes, no existing data affected.

Usage:
    python migrate_add_patient_notes.py
"""

from app import app
from extensions import db

with app.app_context():
    print("Creating patient_notes table if it doesn't exist...")
    db.create_all()
    print("Done.")