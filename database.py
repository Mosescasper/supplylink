"""
database.py
SupplyLink -- Database setup and SQLAlchemy models.

Mirrors the schema in the SupplyLink Hospital Plan:
Department, User, Supplier, Category, Item, Batch, StockMovement,
PurchaseOrder(+Line), ProcurementPlan, MonitoringRecord,
Requisition(+Line), Patient, Prescription(+Line), DischargeRefund.
"""

from datetime import datetime, date

from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


# ---------------------------------------------------------------------------
# Core reference tables
# ---------------------------------------------------------------------------

class Department(db.Model):
    __tablename__ = "departments"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    # Flags departments that hold and issue stock (Drug Store, Holding, Pharmacy points)
    is_store = db.Column(db.Boolean, default=False, nullable=False)

    users = db.relationship("User", back_populates="department")

    def __repr__(self):
        return f"<Department {self.name}>"


class User(UserMixin, db.Model):
    __tablename__ = "users"

    ROLES = ("admin", "pharmacist", "store_officer", "requisition_officer", "staff")

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(160), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(30), nullable=False, default="staff")
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"))

    department = db.relationship("Department", back_populates="users")

    def set_password(self, raw_password: str) -> None:
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password: str) -> bool:
        return check_password_hash(self.password_hash, raw_password)

    def __repr__(self):
        return f"<User {self.email} ({self.role})>"


class Supplier(db.Model):
    __tablename__ = "suppliers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    contact_person = db.Column(db.String(120))
    phone = db.Column(db.String(40))
    email = db.Column(db.String(160))
    address = db.Column(db.String(255))

    items = db.relationship("Item", back_populates="supplier")

    def __repr__(self):
        return f"<Supplier {self.name}>"


class Category(db.Model):
    __tablename__ = "categories"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)

    items = db.relationship("Item", back_populates="category")


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

class Item(db.Model):
    __tablename__ = "items"

    id = db.Column(db.Integer, primary_key=True)
    sku = db.Column(db.String(60), unique=True, nullable=False)  # code_no
    name = db.Column(db.String(500), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"))
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"))
    unit_of_issue = db.Column(db.String(40), nullable=False)  # Tablets, Vials, Bottles...
    unit_cost = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    avg_monthly_consumption = db.Column(db.Numeric(12, 2), default=0)
    reorder_level = db.Column(db.Numeric(12, 2), default=0)  # manual override if set

    category = db.relationship("Category", back_populates="items")
    supplier = db.relationship("Supplier", back_populates="items")
    batches = db.relationship("Batch", back_populates="item", cascade="all, delete-orphan")

    @property
    def quantity_on_hand(self) -> float:
        """Current total stock level across all locations (live, not stored)."""
        return sum(float(b.quantity_remaining) for b in self.batches)

    def quantity_at(self, location: str) -> float:
        return sum(
            float(b.quantity_remaining) for b in self.batches if b.location == location
        )

    def suggested_reorder_level(self) -> float:
        """Derived reorder level from average monthly consumption, unless
        a manual override (reorder_level) has been set."""
        if self.reorder_level:
            return float(self.reorder_level)
        return round(float(self.avg_monthly_consumption or 0) * 1.5, 2)

    def __repr__(self):
        return f"<Item {self.sku} {self.name}>"


class Batch(db.Model):
    __tablename__ = "batches"

    LOCATIONS = ("Drug Store", "Holding", "Outpatient Pharmacy", "Inpatient Pharmacy")

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    batch_number = db.Column(db.String(80), nullable=False)
    expiry_date = db.Column(db.Date, nullable=False)
    quantity_received = db.Column(db.Numeric(12, 2), nullable=False)
    quantity_remaining = db.Column(db.Numeric(12, 2), nullable=False)
    location = db.Column(db.String(40), nullable=False, default="Drug Store")
    received_date = db.Column(db.Date, default=date.today)

    item = db.relationship("Item", back_populates="batches")

    def months_to_expiry(self, as_of: date = None) -> float:
        as_of = as_of or date.today()
        return (self.expiry_date - as_of).days / 30.44

    def __repr__(self):
        return f"<Batch {self.batch_number} item={self.item_id} loc={self.location}>"


class StockMovement(db.Model):
    __tablename__ = "stock_movements"

    MOVEMENT_TYPES = ("receipt", "issue", "transfer", "adjustment", "refund_reversal")

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    batch_id = db.Column(db.Integer, db.ForeignKey("batches.id"), nullable=False)
    movement_type = db.Column(db.String(20), nullable=False)
    quantity = db.Column(db.Numeric(12, 2), nullable=False)
    from_location = db.Column(db.String(40))
    to_location = db.Column(db.String(40))
    reference = db.Column(db.String(120))  # S11 no., PO no., prescription no., delivery note
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    item = db.relationship("Item")
    batch = db.relationship("Batch")
    created_by = db.relationship("User")


# ---------------------------------------------------------------------------
# Procurement
# ---------------------------------------------------------------------------

class PurchaseOrder(db.Model):
    __tablename__ = "purchase_orders"

    STATUSES = ("Draft", "Sent", "Received", "Cancelled")

    id = db.Column(db.Integer, primary_key=True)
    po_number = db.Column(db.String(40), unique=True, nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="Draft")
    order_date = db.Column(db.Date, default=date.today)
    expected_date = db.Column(db.Date)

    supplier = db.relationship("Supplier")
    lines = db.relationship(
        "PurchaseOrderLine", back_populates="purchase_order", cascade="all, delete-orphan"
    )


class PurchaseOrderLine(db.Model):
    __tablename__ = "purchase_order_lines"

    id = db.Column(db.Integer, primary_key=True)
    purchase_order_id = db.Column(db.Integer, db.ForeignKey("purchase_orders.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    quantity_ordered = db.Column(db.Numeric(12, 2), nullable=False)
    unit_cost = db.Column(db.Numeric(12, 2), nullable=False)
    batch_number = db.Column(db.String(80))   # filled in on receipt
    expiry_date = db.Column(db.Date)          # filled in on receipt

    purchase_order = db.relationship("PurchaseOrder", back_populates="lines")
    item = db.relationship("Item")


class ProcurementPlan(db.Model):
    __tablename__ = "procurement_plans"

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    planned_quantity = db.Column(db.Numeric(12, 2), nullable=False)
    planned_monthly_breakdown = db.Column(db.JSON)  # {"1": 100, "2": 90, ...}

    item = db.relationship("Item")

    __table_args__ = (db.UniqueConstraint("item_id", "year", name="uq_plan_item_year"),)


class MonitoringRecord(db.Model):
    __tablename__ = "monitoring_records"

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    period = db.Column(db.String(20), nullable=False)  # e.g. "2026-07"
    planned_quantity = db.Column(db.Numeric(12, 2), nullable=False)
    actual_issued = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    item = db.relationship("Item")

    @property
    def variance(self):
        return float(self.planned_quantity) - float(self.actual_issued)


# ---------------------------------------------------------------------------
# Requisitions (S11)
# ---------------------------------------------------------------------------

class Requisition(db.Model):
    __tablename__ = "requisitions"

    STATUSES = ("Pending", "Approved", "Rejected", "Issued")

    id = db.Column(db.Integer, primary_key=True)
    req_number = db.Column(db.String(40), unique=True, nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=False)
    issue_point = db.Column(db.String(40), nullable=False)  # Drug Store / Holding
    requested_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="Pending")
    approved_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    issued_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    received_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    department = db.relationship("Department")
    requested_by = db.relationship("User", foreign_keys=[requested_by_id])
    approved_by = db.relationship("User", foreign_keys=[approved_by_id])
    issued_by = db.relationship("User", foreign_keys=[issued_by_id])
    received_by = db.relationship("User", foreign_keys=[received_by_id])
    lines = db.relationship(
        "RequisitionLine", back_populates="requisition", cascade="all, delete-orphan"
    )


class RequisitionLine(db.Model):
    __tablename__ = "requisition_lines"

    id = db.Column(db.Integer, primary_key=True)
    requisition_id = db.Column(db.Integer, db.ForeignKey("requisitions.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    quantity_required = db.Column(db.Numeric(12, 2), nullable=False)
    quantity_issued = db.Column(db.Numeric(12, 2), default=0)
    remarks = db.Column(db.String(255))

    requisition = db.relationship("Requisition", back_populates="lines")
    item = db.relationship("Item")

    @property
    def value(self):
        return float(self.quantity_issued or 0) * float(self.item.unit_cost)


# ---------------------------------------------------------------------------
# Patients / Prescriptions / Discharge
# ---------------------------------------------------------------------------

class Patient(db.Model):
    __tablename__ = "patients"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    gender = db.Column(db.String(10))
    ip_op_number = db.Column(db.String(40), unique=True, nullable=False)
    patient_type = db.Column(db.String(12), nullable=False)  # Outpatient / Inpatient
    age = db.Column(db.Integer)
    weight = db.Column(db.Numeric(6, 2))
    height = db.Column(db.Numeric(6, 2))
    contact = db.Column(db.String(40))
    clinic_ward_unit = db.Column(db.String(80))
    drug_allergies = db.Column(db.Text)

    prescriptions = db.relationship("Prescription", back_populates="patient")


class Prescription(db.Model):
    __tablename__ = "prescriptions"

    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patients.id"), nullable=False)
    date = db.Column(db.Date, default=date.today)
    prescriber_name = db.Column(db.String(120), nullable=False)
    registration_number = db.Column(db.String(60))  # MPDC / COC no.
    designation = db.Column(db.String(80))

    patient = db.relationship("Patient", back_populates="prescriptions")
    lines = db.relationship(
        "PrescriptionLine", back_populates="prescription", cascade="all, delete-orphan"
    )


class PrescriptionLine(db.Model):
    __tablename__ = "prescription_lines"

    id = db.Column(db.Integer, primary_key=True)
    prescription_id = db.Column(db.Integer, db.ForeignKey("prescriptions.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    dose = db.Column(db.String(60))
    route = db.Column(db.String(40))
    frequency = db.Column(db.String(40))
    duration = db.Column(db.String(40))
    quantity_dispensed = db.Column(db.Numeric(12, 2), default=0)

    prescription = db.relationship("Prescription", back_populates="lines")
    item = db.relationship("Item")


class DischargeRefund(db.Model):
    __tablename__ = "discharge_refunds"

    OUTCOMES = ("Continuation Prescription Issued", "Refund Processed")

    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patients.id"), nullable=False)
    prescription_id = db.Column(db.Integer, db.ForeignKey("prescriptions.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    quantity_issued_today = db.Column(db.Numeric(12, 2), nullable=False)
    quantity_undepleted = db.Column(db.Numeric(12, 2), nullable=False)
    unit_cost = db.Column(db.Numeric(12, 2), nullable=False)
    refund_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    outcome = db.Column(db.String(40), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    patient = db.relationship("Patient")
    prescription = db.relationship("Prescription")
    item = db.relationship("Item")


def init_db(app):
    """Bind SQLAlchemy to the Flask app and create tables if they don't exist."""
    db.init_app(app)
    with app.app_context():
        db.create_all()