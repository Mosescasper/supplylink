from app import app
from models import Prescriber, Prescription, PrescriptionLine

with app.app_context():
    print("Prescriber has user_id column:", hasattr(Prescriber(), "user_id"))

    p = Prescription.query.first()
    if p:
        print("Sample prescription status:", p.status)
    else:
        print("No prescriptions yet")

    line = PrescriptionLine.query.first()
    if line:
        print("Sample line - prescribed:", line.quantity_prescribed, "dispensed:", line.quantity_dispensed)
    else:
        print("No prescription lines yet (fine if you haven't dispensed anything)")