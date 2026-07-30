"""One-off script to seed initial hospital departments.
Run once with: python seed_departments.py
Safe to re-run — skips any department that already exists by name.
"""
from app import app
from extensions import db
from models import Department

# is_store=True marks physical stock-holding points used as requisition
# issue_points (Drug Store, Holding, Outpatient Pharmacy, Inpatient
# Pharmacy, Supply Chain). Everything else is a clinical/admin department
# that *requests* stock via requisitions.
#
# Clinical department names match MKRH's actual published services
# (https://mkrh.go.ke/services) so they line up with how the hospital
# already refers to them.
DEPARTMENTS = [
    ("Drug Store", True),
    ("Holding", True),
    ("Outpatient Pharmacy", True),
    ("Inpatient Pharmacy", True),
    ("Supply Chain", True),
    ("HIS", False),                 # Hospital Information System / IT
    ("Emergency Trolley", False),   # required — referenced by name in app.py
    ("MOPC", False),                # Medical Outpatient Clinic
    ("Maternity Unit", False),
    ("Newborn Unit", False),
    ("Dental Unit", False),
    ("Renal Unit", False),
    ("Ophthalmology Unit", False),
]

with app.app_context():
    created = 0
    for name, is_store in DEPARTMENTS:
        existing = Department.query.filter_by(name=name).first()
        if existing:
            print(f"Skipping (already exists): {name}")
            continue
        db.session.add(Department(name=name, is_store=is_store))
        created += 1
        print(f"Created: {name}")

    db.session.commit()
    print(f"\nDone. {created} department(s) created.")