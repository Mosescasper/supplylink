"""
One-off maintenance script: replace the Department table contents with
just the three departments SupplyLink actually uses.

Safe to run because 'Pharmacy' (and the other old rows) aren't referenced
by any existing User.department_id yet. If that's no longer true by the
time you run this, the delete will fail on the foreign key and nothing
will be committed — you'd need to reassign those users first.

Usage:
    python reset_departments.py
(run from the same directory as app.py, with your venv active)
"""

from app import app
from extensions import db
from models import Department

NEW_DEPARTMENTS = ["Drug Store", "Inpatient Pharmacy", "Outpatient Pharmacy"]

with app.app_context():
    existing = Department.query.all()
    print(f"Found {len(existing)} existing department(s): {[d.name for d in existing]}")

    for dept in existing:
        db.session.delete(dept)

    for name in NEW_DEPARTMENTS:
        db.session.add(Department(name=name))

    db.session.commit()

    result = Department.query.order_by(Department.name).all()
    print(f"Done. Departments are now: {[d.name for d in result]}")