"""
Seeds the Department records that SupplyLink's role-based logic depends on
by name (pharmacist scoping, registry HIS auto-select, restock requisition
source/destination lookups, etc.).

Safe to re-run -- skips any department name that already exists.

USAGE (from inside your project folder, with DATABASE_URL already set to
whichever database you want to seed):

    python seed_departments.py
"""

from app import app
from extensions import db
from models import Department

# Add or edit this list to match your hospital's actual department
# structure. The names marked REQUIRED are checked by exact string match
# elsewhere in the codebase -- renaming them will break that logic unless
# you also update the corresponding code.
DEPARTMENTS = [
    ("Drug Store", True),              # REQUIRED -- store_officer scope, restock source
    ("Holding", True),                 # REQUIRED -- pharmacist scope, restock destination
    ("Outpatient Pharmacy", True),     # REQUIRED -- pharmacist scope (exact match)
    ("Inpatient Pharmacy", True),      # REQUIRED -- pharmacist scope (exact match)
    ("Supply Chain Store", True),      # REQUIRED -- supply_chain scope, restock source
    ("HIS (Health Information System)", True),
    ("ICU", True),
    ("Accident & Emergency", True),
    ("Theatre", True),
    ("Labs", True),
    ("Oncology", True),
    # REQUIRED -- must contain "HIS", registry auto-select
    # Add real clinical departments/wards below as needed, e.g.:
    # ("Maternity Ward", False),
    # ("General Outpatient Clinic", False),
]


def main():
    with app.app_context():
        created = []
        skipped = []

        for name, is_store in DEPARTMENTS:
            existing = Department.query.filter_by(name=name).first()
            if existing:
                skipped.append(name)
                continue

            dept = Department(name=name, is_store=is_store)
            db.session.add(dept)
            created.append(name)

        db.session.commit()

        print(f"Created {len(created)} department(s):")
        for name in created:
            print(f"  + {name}")

        if skipped:
            print(f"\nAlready existed, skipped {len(skipped)}:")
            for name in skipped:
                print(f"  = {name}")


if __name__ == "__main__":
    main()