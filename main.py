"""
main.py
SupplyLink -- Flask application entry point.

Run with:  python main.py
Requires:  pip install flask flask_sqlalchemy flask_login python-dotenv

Set DATABASE_URL in a .env file, e.g.:
    DATABASE_URL=postgresql://user:password@localhost:5432/supplylink
    SECRET_KEY=change-me
"""

import os
from datetime import date

from flask import Flask, jsonify, request
from flask_login import (
    LoginManager,
    login_user,
    logout_user,
    login_required,
    current_user,
)
from dotenv import load_dotenv

from database import (
    db,
    init_db,
    Department,
    User,
    Supplier,
    Category,
    Item,
    Batch,
    PurchaseOrder,
    PurchaseOrderLine,
    Requisition,
    Patient,
    Prescription,
    PrescriptionLine,
)
import methods

load_dotenv()


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
        "DATABASE_URL", "sqlite:///" + os.path.join(os.path.dirname(os.path.abspath(__file__)), "supplylink.db")
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    init_db(app)

    login_manager = LoginManager()
    login_manager.login_view = "login"
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    register_routes(app)
    return app


def register_routes(app):

    # -----------------------------------------------------------------
    # Auth
    # -----------------------------------------------------------------

    @app.route("/api/register", methods=["POST"])
    def register():
        data = request.get_json()
        if User.query.filter_by(email=data["email"]).first():
            return jsonify({"error": "Email already registered"}), 400

        user = User(
            name=data["name"],
            email=data["email"],
            role=data.get("role", "staff"),
            department_id=data.get("department_id"),
        )
        user.set_password(data["password"])
        db.session.add(user)
        db.session.commit()
        return jsonify({"id": user.id, "email": user.email}), 201

    @app.route("/api/login", methods=["POST"])
    def login():
        data = request.get_json()
        user = User.query.filter_by(email=data.get("email")).first()
        if user is None or not user.check_password(data.get("password", "")):
            return jsonify({"error": "Invalid credentials"}), 401
        login_user(user)
        return jsonify({"id": user.id, "name": user.name, "role": user.role})

    @app.route("/api/logout", methods=["POST"])
    @login_required
    def logout():
        logout_user()
        return jsonify({"ok": True})

    # -----------------------------------------------------------------
    # Dashboard
    # -----------------------------------------------------------------

    @app.route("/api/dashboard", methods=["GET"])
    @login_required
    def dashboard():
        return jsonify(methods.get_dashboard_kpis())

    @app.route("/api/dashboard/expiring", methods=["GET"])
    @login_required
    def dashboard_expiring():
        batches = methods.get_expiring_batches()
        return jsonify([
            {
                "batch_id": b.id,
                "item": b.item.name,
                "batch_number": b.batch_number,
                "expiry_date": b.expiry_date.isoformat(),
                "quantity_remaining": float(b.quantity_remaining),
                "location": b.location,
            }
            for b in batches
        ])

    @app.route("/api/dashboard/low-stock", methods=["GET"])
    @login_required
    def dashboard_low_stock():
        items = methods.get_low_stock_items()
        return jsonify([methods.get_suggested_order_quantity(i) for i in items])

    # -----------------------------------------------------------------
    # Inventory
    # -----------------------------------------------------------------

    @app.route("/api/items", methods=["GET"])
    @login_required
    def list_items():
        items = Item.query.all()
        return jsonify([
            {
                "id": i.id,
                "sku": i.sku,
                "name": i.name,
                "unit_of_issue": i.unit_of_issue,
                "unit_cost": float(i.unit_cost),
                "quantity_on_hand": i.quantity_on_hand,
                "reorder_level": i.suggested_reorder_level(),
            }
            for i in items
        ])

    @app.route("/api/items", methods=["POST"])
    @login_required
    def create_item():
        data = request.get_json()
        item = Item(
            sku=data["sku"],
            name=data["name"],
            category_id=data.get("category_id"),
            supplier_id=data.get("supplier_id"),
            unit_of_issue=data["unit_of_issue"],
            unit_cost=data.get("unit_cost", 0),
            avg_monthly_consumption=data.get("avg_monthly_consumption", 0),
            reorder_level=data.get("reorder_level"),
        )
        db.session.add(item)
        db.session.commit()
        return jsonify({"id": item.id}), 201

    # -----------------------------------------------------------------
    # Purchase orders
    # -----------------------------------------------------------------

    @app.route("/api/purchase-orders", methods=["POST"])
    @login_required
    def create_purchase_order():
        data = request.get_json()
        po = PurchaseOrder(
            po_number=data["po_number"],
            supplier_id=data["supplier_id"],
            status="Draft",
            expected_date=data.get("expected_date"),
        )
        db.session.add(po)
        db.session.flush()

        for line in data.get("lines", []):
            db.session.add(PurchaseOrderLine(
                purchase_order_id=po.id,
                item_id=line["item_id"],
                quantity_ordered=line["quantity_ordered"],
                unit_cost=line["unit_cost"],
            ))

        db.session.commit()
        return jsonify({"id": po.id, "po_number": po.po_number}), 201

    @app.route("/api/purchase-orders/<int:po_id>/receive", methods=["POST"])
    @login_required
    def receive_purchase_order(po_id):
        data = request.get_json()
        try:
            received_lines = [
                {
                    "line_id": rl["line_id"],
                    "batch_number": rl["batch_number"],
                    "expiry_date": date.fromisoformat(rl["expiry_date"]),
                }
                for rl in data["received_lines"]
            ]
            po = methods.receive_purchase_order(
                po_id, received_lines, created_by_id=current_user.id
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"id": po.id, "status": po.status})

    # -----------------------------------------------------------------
    # Requisitions (S11)
    # -----------------------------------------------------------------

    @app.route("/api/requisitions", methods=["POST"])
    @login_required
    def create_requisition():
        data = request.get_json()
        req = methods.submit_requisition(
            req_number=data["req_number"],
            department_id=data["department_id"],
            issue_point=data["issue_point"],
            requested_by_id=current_user.id,
            lines=data["lines"],
        )
        return jsonify({"id": req.id, "req_number": req.req_number}), 201

    @app.route("/api/requisitions/<int:req_id>/approve", methods=["POST"])
    @login_required
    def approve_requisition(req_id):
        try:
            req = methods.approve_requisition(req_id, current_user.id)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"id": req.id, "status": req.status})

    @app.route("/api/requisitions/<int:req_id>/reject", methods=["POST"])
    @login_required
    def reject_requisition(req_id):
        try:
            req = methods.reject_requisition(req_id, current_user.id)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"id": req.id, "status": req.status})

    @app.route("/api/requisitions/<int:req_id>/issue", methods=["POST"])
    @login_required
    def issue_requisition(req_id):
        data = request.get_json(silent=True) or {}
        try:
            req = methods.issue_requisition(
                req_id, current_user.id, received_by_id=data.get("received_by_id")
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"id": req.id, "status": req.status})

    # -----------------------------------------------------------------
    # Outpatient dispensing
    # -----------------------------------------------------------------

    @app.route("/api/prescriptions", methods=["POST"])
    @login_required
    def create_prescription():
        data = request.get_json()
        patient = Patient.query.get(data["patient_id"])
        if patient is None:
            return jsonify({"error": "Patient not registered in hospital system"}), 400

        prescription = Prescription(
            patient_id=patient.id,
            prescriber_name=data["prescriber_name"],
            registration_number=data.get("registration_number"),
            designation=data.get("designation"),
        )
        db.session.add(prescription)
        db.session.flush()

        for line in data.get("lines", []):
            db.session.add(PrescriptionLine(
                prescription_id=prescription.id,
                item_id=line["item_id"],
                dose=line.get("dose"),
                route=line.get("route"),
                frequency=line.get("frequency"),
                duration=line.get("duration"),
            ))

        db.session.commit()
        return jsonify({"id": prescription.id}), 201

    @app.route("/api/prescription-lines/<int:line_id>/dispense", methods=["POST"])
    @login_required
    def dispense_line(line_id):
        try:
            line = methods.dispense_prescription_line(line_id, dispensed_by_id=current_user.id)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"id": line.id, "quantity_dispensed": float(line.quantity_dispensed)})

    # -----------------------------------------------------------------
    # Discharge & refunds
    # -----------------------------------------------------------------

    @app.route("/api/discharge", methods=["POST"])
    @login_required
    def discharge_patient():
        data = request.get_json()
        try:
            refunds = methods.process_discharge(
                patient_id=data["patient_id"],
                prescription_id=data["prescription_id"],
                created_by_id=current_user.id,
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        return jsonify([
            {
                "item": r.item.name,
                "quantity_undepleted": float(r.quantity_undepleted),
                "refund_amount": float(r.refund_amount),
                "outcome": r.outcome,
            }
            for r in refunds
        ])


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)