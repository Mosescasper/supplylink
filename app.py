from hashlib import new

from dateutil.relativedelta import relativedelta
import csv
import io
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (
    Flask, render_template, redirect, url_for, request, flash,
    Response, abort, jsonify, send_from_directory, session
)
from flask_login import (
    login_user, logout_user, login_required, current_user
)
from flask_migrate import Migrate
from sqlalchemy import func

import os
import uuid
from werkzeug.utils import secure_filename
import boto3

from config import Config
from extensions import db, login_manager
from models import (
    Department, User, Category, Supplier, Item, Batch, StockMovement,
    PurchaseOrder, PurchaseOrderLine, Delivery, DeliveryLineItem,
    ProcurementPlan, MonitoringRecord,
    Requisition, RequisitionLine, Patient, Prescription, PrescriptionLine,
    DischargeRefund, SupplierItem, LOCATIONS, Hospital, Prescriber,
    PatientNote, PatientMovement, PatientFile, Appointment, PatientDocument, PurchaseRequisition, PurchaseRequisitionLine
)
from apis.admin import admin_bp
from apis.hospital import hospital_bp

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)
login_manager.init_app(app)
s3_client = boto3.client(
    "s3",
    endpoint_url=Config.R2_ENDPOINT_URL,
    aws_access_key_id=Config.R2_ACCESS_KEY_ID,
    aws_secret_access_key=Config.R2_SECRET_ACCESS_KEY,
    region_name=Config.R2_REGION,
)

app.register_blueprint(admin_bp)
app.register_blueprint(hospital_bp)

with app.app_context():
    db.create_all()

@app.before_request
def _enforce_force_logout():
    if current_user.is_authenticated and current_user.force_logout_at:
        login_time_str = session.get("login_time")
        if not login_time_str or datetime.fromisoformat(login_time_str) < current_user.force_logout_at:
            logout_user()
            flash("You have been logged out by an administrator.", "info")
            return redirect(url_for("login"))

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.context_processor
def inject_pharmacist_scope():
    if current_user.is_authenticated and current_user.role == "pharmacist":
        return dict(pharmacist_scope=_pharmacist_scope_locations(current_user))
    return dict(pharmacist_scope=None)

# Inject hospital info into all templates (letterhead/contact)
@app.context_processor
def inject_hospital_info():
    return dict(
        hospital_name=Config.HOSPITAL_NAME,
        hospital_address=Config.HOSPITAL_ADDRESS,
        hospital_phone_1=Config.HOSPITAL_PHONE_1,
        hospital_phone_2=Config.HOSPITAL_PHONE_2,
    )
@app.context_processor
def inject_pending_requisition_count():
    """Powers the notification badge next to 'Requisitions' in the sidebar.
    Meaning differs by role -- see _pending_action_requisitions_count and
    _recently_issued_for_pharmacist_count further down the file."""
    if not current_user.is_authenticated:
        return dict(pending_requisition_count=0)

    role = current_user.role
    if role == "pharmacist":
        count = _recently_issued_for_pharmacist_count(current_user)
    elif role in ("admin", "store_officer", "hod_pharmacy"):
        scope_locations = ROLE_LOCATION_SCOPE.get(role)
        count = _pending_action_requisitions_count(role, scope_locations)
    else:
        count = 0

    return dict(pending_requisition_count=count)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def role_required(*roles):
    """Restrict a route to specific User.role values (admin always allowed)."""
    def decorator(view_fn):
        @wraps(view_fn)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for("login"))
            if current_user.role != "admin" and current_user.role not in roles:
                flash("You don't have permission to do that.", "danger")
                return redirect(url_for("dashboard"))
            return view_fn(*args, **kwargs)
        return wrapped
    return decorator


def next_reference(prefix, model, field):
    """Generate a simple sequential reference like S11-000123 / PO-000045."""
    count = db.session.query(model).count() + 1
    return f"{prefix}-{count:06d}"


def log_movement(item, batch, movement_type, quantity, from_location=None,
                  to_location=None, reference=None):
    movement = StockMovement(
        item_id=item.id,
        batch_id=batch.id,
        movement_type=movement_type,
        quantity=quantity,
        from_location=from_location,
        to_location=to_location,
        reference=reference,
        created_by_id=current_user.id if current_user.is_authenticated else None,
    )
    db.session.add(movement)
    return movement

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def role_required(*roles):
    """Restrict a route to specific User.role values (admin always allowed)."""
    def decorator(view_fn):
        @wraps(view_fn)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for("login"))
            if current_user.role != "admin" and current_user.role not in roles:
                flash("You don't have permission to do that.", "danger")
                return redirect(url_for("dashboard"))
            return view_fn(*args, **kwargs)
        return wrapped
    return decorator


def next_reference(prefix, model, field):
    """Generate a simple sequential reference like S11-000123 / PO-000045."""
    count = db.session.query(model).count() + 1
    return f"{prefix}-{count:06d}"


def log_movement(item, batch, movement_type, quantity, from_location=None,
                  to_location=None, reference=None):
    movement = StockMovement(
        item_id=item.id,
        batch_id=batch.id,
        movement_type=movement_type,
        quantity=quantity,
        from_location=from_location,
        to_location=to_location,
        reference=reference,
        created_by_id=current_user.id if current_user.is_authenticated else None,
    )
    db.session.add(movement)
    return movement


def _get_or_create_own_prescriber(user):
    """A doctor writing their own prescription shouldn't have to retype
    their name/registration number/designation every time. Returns the
    Prescriber record linked to this user's account, creating one on first
    use if it doesn't exist yet (name pre-filled from the account; reg
    number and designation left blank for the doctor to fill in later via
    /prescribers/<id>/edit)."""
    prescriber = Prescriber.query.filter_by(user_id=user.id).first()
    if prescriber:
        return prescriber

    prescriber = Prescriber(name=user.name, user_id=user.id)
    db.session.add(prescriber)
    db.session.flush()
    return prescriber


def _csv_response(filename, header, rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
#
# Only three roles exist in this system: admin, store_officer, pharmacist.
# Registration always validates against User.ROLES, so even a tampered form
# submission can't create a user with any other role.

@app.route("/register", methods=["GET", "POST"])
def register():
    departments = Department.query.order_by(Department.name).all()
    # Only these three roles are ever offered / accepted.
    roles = User.ROLES
    roles_labels = User.ROLE_LABELS  # define once, reuse in every render_template call below

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        role = request.form.get("role", "")
        department_id = request.form.get("department_id") or None

        if not name or not email or not password:
            flash("Name, email, and password are required.", "danger")
            return render_template("register.html", departments=departments, roles=roles, roles_labels=roles_labels)

        if password != confirm_password:
            flash("Passwords do not match.", "danger")
            return render_template("register.html", departments=departments, roles=roles, roles_labels=roles_labels)

        if role not in User.ROLES:
            flash("Please select a valid role (Admin, Store Officer, Pharmacist, or Doctor).", "danger")
            return render_template("register.html", departments=departments, roles=roles, roles_labels=roles_labels)

        if User.query.filter_by(email=email).first():
            flash("An account with that email already exists.", "danger")
            return render_template("register.html", departments=departments, roles=roles, roles_labels=roles_labels)

        user = User(name=name, email=email, role=role, department_id=department_id)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        flash("Account created. Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("register.html", departments=departments, roles=roles, roles_labels=roles_labels)

@app.route("/login", methods=["GET", "POST"])
def login():
    # role comes from the hub page's link (?role=admin, ?role=pharmacist, etc.)
    # Only ever trust it if it's one of the real roles — anything else is ignored.
    role = request.args.get("role", "").strip()
    if role and role not in User.ROLES:
        role = ""
    role_label = User.ROLE_LABELS.get(role) if role else None

    if current_user.is_authenticated:
        # Already logged in, and this hit /login with no specific role
        # attached (e.g. just clicked "Log in" generically) -> go to dashboard.
        if not role:
            return redirect(url_for("dashboard"))

        # Already logged in as the SAME role that was clicked -> no need
        # to log in again, just go straight to the dashboard.
        if current_user.role == role:
            return redirect(url_for("dashboard"))

        # Logged in as a DIFFERENT role than the one just clicked (e.g.
        # logged in as Admin, clicked Pharmacist) -> log them out first so
        # the login form actually shows and asks for Pharmacist credentials,
        # instead of silently bouncing back to the Admin dashboard.
        logout_user()

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        submitted_role = request.form.get("role", "").strip()
        if submitted_role and submitted_role not in User.ROLES:
            submitted_role = ""
        submitted_role_label = User.ROLE_LABELS.get(submitted_role) if submitted_role else None

        user = User.query.filter_by(email=email).first()
        user = User.query.filter_by(email=email).first()

        if not user or not user.check_password(password):
            flash("Invalid email or password.", "danger")
            return render_template(
                "login.html", role=submitted_role, role_label=submitted_role_label
            )

        if submitted_role and user.role != submitted_role:
            flash(
                f"These credentials aren't valid for {submitted_role_label} access.",
                "danger",
            )
            return render_template(
                "login.html", role=submitted_role, role_label=submitted_role_label
            )

        login_user(user)
        session["login_time"] = datetime.utcnow().isoformat()
        flash(f"Welcome back, {user.name}.", "success")
        next_url = request.args.get("next")
        return redirect(next_url or url_for("dashboard"))

    return render_template("login.html", role=role, role_label=role_label)
@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("home"))


# ---------------------------------------------------------------------------
# User management (admin only) — password resets
# ---------------------------------------------------------------------------

@app.route("/users")
@login_required
@role_required("admin")
def user_list():
    users = User.query.order_by(User.name).all()
    return render_template("users/list.html", users=users)
@app.route("/users/<int:user_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def user_delete(user_id):
    if user_id == current_user.id:
        flash("You can't delete your own account.", "danger")
        return redirect(url_for("user_list"))

    user = User.query.get_or_404(user_id)
    db.session.delete(user)
    db.session.commit()
    flash(f"{user.name} has been deleted.", "success")
    return redirect(url_for("user_list"))


@app.route("/users/<int:user_id>/force-logout", methods=["POST"])
@login_required
@role_required("admin")
def user_force_logout(user_id):
    if user_id == current_user.id:
        flash("You can't force-logout your own account.", "danger")
        return redirect(url_for("user_list"))

    user = User.query.get_or_404(user_id)
    user.force_logout_at = datetime.utcnow()
    db.session.commit()
    flash(f"{user.name} will be logged out on their next request.", "success")
    return redirect(url_for("user_list"))

@app.route("/users/<int:user_id>/reset-password", methods=["GET", "POST"])
@login_required
@role_required("admin")
def user_reset_password(user_id):
    user = User.query.get_or_404(user_id)

    if request.method == "POST":
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if len(new_password) < 6:
            flash("Password must be at least 6 characters.", "danger")
            return render_template("users/reset_password.html", user=user)

        if new_password != confirm_password:
            flash("Passwords do not match.", "danger")
            return render_template("users/reset_password.html", user=user)

        user.set_password(new_password)
        db.session.commit()
        flash(f"Password reset for {user.name}. Share the new password with them securely.", "success")
        return redirect(url_for("user_list"))

    return render_template("users/reset_password.html", user=user)

def _scoped_items(items, locations):
    """Items actually tracked (have or had a batch record) in the given
    locations — i.e. what this store actually deals in. locations=None
    means unscoped (every item in the catalog, for admin/doctor)."""
    if locations is None:
        return items
    return [i for i in items if any(b.location in locations for b in i.batches)]
# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

ROLE_LOCATION_SCOPE = {
    "store_officer": ["Drug Store", "Holding"],
    "pharmacist": ["Holding", "Outpatient Pharmacy", "Inpatient Pharmacy"],
    "hod_pharmacy": ["Holding", "Outpatient Pharmacy", "Inpatient Pharmacy"],
    "supply_chain": ["Supply Chain Store"],
    # admin and doctor fall through to None (unscoped)...
    # Doctors don't deal in stock at all — their dashboard branch shows
    # patient/prescription info instead. supply_chain deals in suppliers/
    # POs/procurement rather than any single physical location, so it also
    # needs the unscoped (whole-hospital) stock picture to plan purchasing.
}
def _current_scope_locations():
    """Locations the logged-in user's role is restricted to. None = unscoped (admin/doctor)."""
    if current_user.role == "pharmacist":
        return _pharmacist_scope_locations(current_user)
    return ROLE_LOCATION_SCOPE.get(current_user.role)
def _scoped_quantity(item, locations):
    """On-hand quantity for an item, restricted to a set of locations.
    locations=None means unscoped (all locations)."""
    if locations is None:
        return item.quantity_on_hand
    by_location = item.quantity_on_hand_by_location
    return sum(by_location.get(loc, 0) for loc in locations)


def _scoped_low_stock_items(items, locations):
    result = []
    for i in items:
        qty = _scoped_quantity(i, locations)
        if qty <= (i.reorder_level or 0):
            result.append(i)
    return result


def _scoped_stock_value(items, locations):
    total = 0
    for i in items:
        qty = _scoped_quantity(i, locations)
        total += float(qty) * float(i.unit_cost or 0)
    return total


def _scoped_near_expiry_batches(locations):
    batches = [
        b for b in Batch.query.all()
        if (b.quantity_remaining or 0) > 0 and b.is_near_expiry(Config.EXPIRY_ALERT_MONTHS)
    ]
    if locations:
        batches = [b for b in batches if b.location in locations]
    return sorted(batches, key=lambda b: b.expiry_date)


def _scoped_recent_movements(locations, limit=10):
    q = StockMovement.query
    if locations:
        q = q.filter(
            (StockMovement.from_location.in_(locations))
            | (StockMovement.to_location.in_(locations))
        )
    return q.order_by(StockMovement.created_at.desc()).limit(limit).all()


def _prescribable_items():
    """Items a doctor can actually prescribe right now — must have real
    stock in at least one dispensing location (Outpatient or Inpatient
    Pharmacy), since that's where prescription_dispense() draws from."""
    dispensing_locations = ("Outpatient Pharmacy", "Inpatient Pharmacy")
    all_items = Item.query.order_by(Item.name).all()
    return [
        i for i in all_items
        if any(_scoped_quantity(i, [loc]) > 0 for loc in dispensing_locations)
    ]

def _pharmacist_scope_locations(user):
    """A pharmacist's scope depends on which pharmacy department they're
    assigned to at registration. Holding is always included, since stock
    passes through Holding before reaching either dispensing point.
    No department (or an unrecognized one) assigned -> fall back to both,
    so nothing breaks for legacy/general pharmacist accounts."""
    if user.department and user.department.name == "Outpatient Pharmacy":
        return ["Holding", "Outpatient Pharmacy"]
    if user.department and user.department.name == "Inpatient Pharmacy":
        return ["Holding", "Inpatient Pharmacy"]
    return ["Holding", "Outpatient Pharmacy", "Inpatient Pharmacy"]


def _pharmacy_scope_display(scope_locations):
    """Human-readable label/tag for the dashboard, based on a pharmacist's
    actual scope rather than the old hardcoded 'all three' text."""
    names = [loc for loc in scope_locations if loc != "Holding"]
    if len(scope_locations) == 3:
        return "across Holding, Outpatient & Inpatient Pharmacy", "HOLDING · OUTPATIENT · INPATIENT"
    label = "across Holding & " + " & ".join(names)
    tag = "HOLDING · " + " · ".join(n.split(" ")[0].upper() for n in names)
    return label, tag

@app.route("/")
def home():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return render_template("home.html")
def _department_avg_monthly_consumption(department_id, item_id, months=None):
    """This department's average monthly usage of an item, based on
    quantity_issued from their own past Issued S11 requisitions over the
    last `months` months. Returns None if they have no issue history for
    this item at all (so a first-time request isn't blocked)."""
    months = months or Config.REQUISITION_CONSUMPTION_MONTHS
    since = datetime.utcnow() - timedelta(days=months * 30)

    total_issued = (
        db.session.query(func.sum(RequisitionLine.quantity_issued))
        .join(Requisition, Requisition.id == RequisitionLine.requisition_id)
        .filter(
            Requisition.department_id == department_id,
            Requisition.status == "Issued",
            Requisition.created_at >= since,
            RequisitionLine.item_id == item_id,
        )
        .scalar()
    )

    if not total_issued:
        return None
    return float(total_issued) / months
def _scoped_pending_requisitions_count(role, scope_locations):
    """How many Pending requisitions each role should see on their
    dashboard KPI card:
        admin / supply_chain -> hospital-wide count (unscoped)
        store_officer        -> only Drug Store issue-point requisitions
                                 (Holding-issue-point ones are HOD Pharmacy's)
        hod_pharmacy          -> only Holding issue-point requisitions
                                 (Drug Store -> Holding -> Dispensing flow:
                                 OP/IP requisitioning FROM Holding)
        pharmacist            -> only requisitions THEY personally raised
    """
    query = Requisition.query.filter_by(status="Pending")

    if role == "store_officer":
        query = query.filter(Requisition.issue_point == "Drug Store")
    elif role == "hod_pharmacy":
        query = query.filter(Requisition.issue_point == "Holding")
    elif role == "pharmacist":
        query = query.filter(Requisition.requested_by_id == current_user.id)

    return query.count()
def _pending_action_requisitions_count(role, scope_locations):
    """'Something needs YOUR action' count -- store_officer/hod_pharmacy/admin."""
    query = Requisition.query.filter_by(status="Pending")
    if role == "store_officer":
        query = query.filter(Requisition.issue_point == "Drug Store")
    elif role == "hod_pharmacy":
        query = query.filter(Requisition.issue_point == "Holding")
    return query.count()


def _recently_issued_for_pharmacist_count(user, hours=24):
    """'Something you're waiting on just arrived' count -- pharmacist only.
    Requisitions THEY raised that moved to Issued within the last `hours`."""
    since = datetime.utcnow() - timedelta(hours=hours)
    return Requisition.query.filter(
        Requisition.requested_by_id == user.id,
        Requisition.status == "Issued",
        Requisition.created_at >= since,
    ).count()

def _can_action_requisition(user, req):
    """Who may approve/reject/issue a given requisition depends on WHERE
    it draws stock from, mirroring the physical supply chain:
    Supply Chain Store -> Drug Store -> Holding -> Dispensing area (OP/IP).
        issue_point == "Supply Chain Store" -> Supply Chain (or admin) —
                                                the store_officer restock step
        issue_point == "Drug Store"         -> Store Officer (or admin)
        issue_point == "Holding"            -> HOD Pharmacy (or admin) — this
                                                is the OP/IP-to-Holding step
    """
    if user.role == "admin":
        return True
    if req.issue_point == "Supply Chain Store" and user.role == "supply_chain":
        return True
    if req.issue_point == "Drug Store" and user.role == "store_officer":
        return True
    if req.issue_point == "Holding" and user.role == "hod_pharmacy":
        return True
    return False

    

@app.route("/dashboard")
@login_required
def dashboard():
    role = current_user.role
    scope_locations = _pharmacist_scope_locations(current_user) if role == "pharmacist" else ROLE_LOCATION_SCOPE.get(role)

    items = Item.query.all()
    scoped_items = _scoped_items(items, scope_locations)

    near_expiry_batches = _scoped_near_expiry_batches(scope_locations)
    low_stock_items = _scoped_low_stock_items(scoped_items, scope_locations)
    total_stock_value = _scoped_stock_value(items, scope_locations)

    kpis = {
        "total_items": len(scoped_items),
        "near_expiry_count": len(near_expiry_batches),
        "low_stock_count": len(low_stock_items),
        "open_purchase_orders": PurchaseOrder.query.filter(
            PurchaseOrder.status.in_(["Draft", "Sent"])
        ).count(),
       "pending_requisitions": _scoped_pending_requisitions_count(role, scope_locations),
        "total_stock_value": total_stock_value,
    }

    recent_movements = _scoped_recent_movements(scope_locations, limit=10)
    recent_requisitions = (
        Requisition.query.order_by(Requisition.created_at.desc()).limit(10).all()
    )
    recent_prescriptions = (
        Prescription.query.order_by(Prescription.date.desc()).limit(10).all()
    )
    recent_refunds = (
        DischargeRefund.query.order_by(DischargeRefund.created_at.desc()).limit(10).all()
    )

    context = dict(
        kpis=kpis,
        near_expiry_batches=near_expiry_batches,
        low_stock_items=low_stock_items,
        recent_movements=recent_movements,
        recent_requisitions=recent_requisitions,
        recent_prescriptions=recent_prescriptions,
        recent_refunds=recent_refunds,
        currency=Config.CURRENCY,
        role=role,
        today=date.today(),
        # NEW — comma-joined location list for the dashboard chart JS to
        # scope its API calls with (empty string = unscoped, i.e. admin).
        location_scope_param=",".join(scope_locations) if scope_locations else "",
    )
    # -----------------------------------------------------------------
    # Critical alerts (admin/store_officer only) — a step more urgent
    # than the general "near expiry" / "low stock" panels above.
    # -----------------------------------------------------------------
    if role in ("admin", "store_officer"):
        today_date = date.today()
        critical_expiry_batches = [
            b for b in Batch.query.filter(Batch.quantity_remaining > 0).all()
            if 0 <= (b.expiry_date - today_date).days <= 30
            and (not scope_locations or b.location in scope_locations)
        ]
        critical_low_stock_items = [
            i for i in scoped_items
            if _scoped_quantity(i, scope_locations) <= 0
            or (i.reorder_level and _scoped_quantity(i, scope_locations) <= float(i.reorder_level) * 0.25)
        ]
        context["critical_expiry_count"] = len(critical_expiry_batches)
        context["critical_low_stock_count"] = len(critical_low_stock_items)
    # -----------------------------------------------------------------
    # Role-specific extra panels
    # -----------------------------------------------------------------
    if role == "admin":
        context["location_values"] = {
            loc: _scoped_stock_value(items, [loc]) for loc in LOCATIONS
        }
        context["variance_flags"] = (
            MonitoringRecord.query.order_by(
                func.abs(MonitoringRecord.planned_quantity - MonitoringRecord.actual_issued).desc()
            ).limit(6).all()
        )
        context["recent_pos"] = (
            PurchaseOrder.query.order_by(PurchaseOrder.order_date.desc()).limit(6).all()
        )
        context["po_fulfillment"] = (
            PurchaseOrder.query.filter(PurchaseOrder.status.in_(["Received", "Partially Received"]))
            .order_by(PurchaseOrder.order_date.desc())
            .limit(8)
            .all()
        )
        context["requisition_fulfillment"] = (
            Requisition.query.filter(Requisition.status == "Issued")
            .order_by(Requisition.created_at.desc())
            .limit(8)
            .all()
        )
    elif role == "store_officer":
        context["drug_store_value"] = _scoped_stock_value(items, ["Drug Store"])
        context["holding_value"] = _scoped_stock_value(items, ["Holding"])
        context["incoming_pos"] = (
            PurchaseOrder.query.filter(PurchaseOrder.status.in_(["Draft", "Sent"]))
            .order_by(PurchaseOrder.order_date.desc())
            .limit(6)
            .all()
        )
        # Only Drug Store issue-point requisitions are Store Officer's to
        # action now — Holding issue-point ones go to HOD Pharmacy instead.
        context["pending_requisitions_to_action"] = (
            Requisition.query.filter(
                Requisition.status == "Pending",
                Requisition.issue_point == "Drug Store",
            )
            .order_by(Requisition.created_at.desc())
            .limit(6)
            .all()
        )
        
        context["transfers_to_holding"] = (
            StockMovement.query.filter(
                StockMovement.movement_type == "transfer",
                StockMovement.from_location == "Drug Store",
                StockMovement.to_location == "Holding",
            )
            .order_by(StockMovement.created_at.desc())
            .limit(6)
            .all()
        )
        context["po_fulfillment"] = (
            PurchaseOrder.query.filter(PurchaseOrder.status.in_(["Received", "Partially Received"]))
            .order_by(PurchaseOrder.order_date.desc())
            .limit(8)
            .all()
        )
        context["requisition_fulfillment"] = (
            Requisition.query.filter(
                Requisition.status == "Issued",
                Requisition.issue_point.in_(["Drug Store", "Holding"]),
            )
            .order_by(Requisition.created_at.desc())
            .limit(8)
            .all()
        )
    elif role == "pharmacist":
        pharmacy_location_cards = [(loc, _scoped_stock_value(items, [loc])) for loc in scope_locations]
        context["pharmacy_location_cards"] = pharmacy_location_cards
        context["pharmacy_kpi_row_class"] = "kpi-row--three" if len(scope_locations) == 3 else "kpi-row--two"
        pharmacy_scope_label, pharmacy_scope_tag = _pharmacy_scope_display(scope_locations)
        context["pharmacy_scope_label"] = pharmacy_scope_label
        context["pharmacy_scope_tag"] = pharmacy_scope_tag
        context["holding_value"] = _scoped_stock_value(items, ["Holding"])
        context["my_requisitions"] = (
            Requisition.query.filter_by(requested_by_id=current_user.id)
            .order_by(Requisition.created_at.desc())
            .limit(6)
            .all()
        )
        context["recent_discharge_refunds"] = (
            DischargeRefund.query.order_by(DischargeRefund.created_at.desc()).limit(6).all()
        )
# Prescription Fill Rate — same logic as api_doctor_completion_rate,
        # but hospital-wide rather than scoped to one doctor.
        total_prescriptions = Prescription.query.count()
        dispensed_prescriptions = Prescription.query.filter_by(status="Dispensed").count()
        partial_prescriptions = Prescription.query.filter_by(status="Partially Dispensed").count()

        context["fill_rate"] = (
            round((dispensed_prescriptions / total_prescriptions) * 100, 1)
            if total_prescriptions else 0.0
        )
        context["dispensed_prescriptions"] = dispensed_prescriptions
        context["partial_prescriptions"] = partial_prescriptions
        context["total_prescriptions"] = total_prescriptions

    elif role == "hod_pharmacy":
        context["holding_value"] = _scoped_stock_value(items, ["Holding"])
        context["outpatient_value"] = _scoped_stock_value(items, ["Outpatient Pharmacy"])
        context["inpatient_value"] = _scoped_stock_value(items, ["Inpatient Pharmacy"])

        # Requisitions raised by OP/IP Pharmacy drawing on Holding stock,
        # waiting on HOD Pharmacy sign-off before they can be issued.
        context["requisitions_to_approve"] = (
            Requisition.query.filter(
                Requisition.status == "Pending",
                Requisition.issue_point == "Holding",
            )
            .order_by(Requisition.created_at.asc())
            .all()
        )

        # Requisitions already approved by HOD Pharmacy, waiting to be
        # physically issued from Holding.
        context["approved_awaiting_issue"] = (
            Requisition.query.filter(
                Requisition.status == "Approved",
                Requisition.issue_point == "Holding",
            )
            .order_by(Requisition.created_at.asc())
            .all()
        )

        # Full drug-flow trail across Holding -> Outpatient/Inpatient
        # Pharmacy, so HOD can track movement through both dispensing points.
        context["pharmacy_flow_movements"] = (
            StockMovement.query.filter(
                (StockMovement.from_location.in_(["Holding", "Outpatient Pharmacy", "Inpatient Pharmacy"]))
                | (StockMovement.to_location.in_(["Holding", "Outpatient Pharmacy", "Inpatient Pharmacy"]))
            )
            .order_by(StockMovement.created_at.desc())
            .limit(15)
            .all()
        )

        context["requisition_fulfillment"] = (
            Requisition.query.filter(
                Requisition.status == "Issued",
                Requisition.issue_point == "Holding",
            )
            .order_by(Requisition.created_at.desc())
            .limit(8)
            .all()
        )

    elif role == "supply_chain":
        context["open_pos"] = (
            PurchaseOrder.query.filter(PurchaseOrder.status.in_(["Draft", "Sent"]))
            .order_by(PurchaseOrder.order_date.desc())
            .limit(8)
            .all()
        )
        context["po_fulfillment"] = (
            PurchaseOrder.query.filter(PurchaseOrder.status.in_(["Received", "Partially Received"]))
            .order_by(PurchaseOrder.order_date.desc())
            .limit(8)
            .all()
        )
        context["supplier_count"] = Supplier.query.count()
        context["low_stock_for_reorder"] = _scoped_low_stock_items(items, None)[:8]
    elif role == "doctor":
        context["my_prescriptions"] = (
            Prescription.query.filter_by(written_by_id=current_user.id)
            .order_by(Prescription.date.desc())
            .limit(10)
            .all()
        )
        context["my_pending_count"] = Prescription.query.filter_by(
            written_by_id=current_user.id, status="Pending"
        ).count()
        context["recent_patients"] = (
            Patient.query.order_by(Patient.id.desc()).limit(6).all()
        )

    elif role == "registry":
        context["total_patients"] = Patient.query.count()
        context["recent_patients"] = (
            Patient.query.order_by(Patient.id.desc()).limit(10).all()
        )
        context["upcoming_appointments"] = (
            Appointment.query.filter(
                Appointment.status == "Scheduled",
                Appointment.scheduled_date >= date.today(),
            )
            .order_by(Appointment.scheduled_date.asc())
            .limit(10)
            .all()
        )

    return render_template("dashboard.html", **context)


# ---------------------------------------------------------------------------
# Inventory & Batches
# ---------------------------------------------------------------------------

@app.route("/inventory")
@login_required
def inventory_list():
    q = request.args.get("q", "").strip()
    category_id = request.args.get("category_id")

    query = Item.query
    if q:
        query = query.filter(Item.name.ilike(f"%{q}%") | Item.sku.ilike(f"%{q}%"))
    if category_id:
        query = query.filter_by(category_id=category_id)

    items = query.order_by(Item.name).all()
    categories = Category.query.order_by(Category.name).all()

    scope_locations = _current_scope_locations()
    for item in items:
        item.scoped_quantity = _scoped_quantity(item, scope_locations)
        item.scoped_is_low = item.scoped_quantity <= (item.reorder_level or 0)

    return render_template("inventory/list.html", items=items, categories=categories,
                            q=q, currency=Config.CURRENCY, scope_locations=scope_locations)

@app.route("/inventory/new", methods=["GET", "POST"])
@login_required
@role_required("admin", "supply_chain")
def inventory_new():
    categories = Category.query.order_by(Category.name).all()
    suppliers = Supplier.query.order_by(Supplier.name).all()

    if request.method == "POST":
        item = Item(
            sku=request.form["sku"].strip(),
            name=request.form["name"].strip(),
            category_id=request.form.get("category_id") or None,
            supplier_id=request.form.get("supplier_id") or None,
            unit_of_issue=request.form.get("unit_of_issue", "Units"),
            unit_cost=request.form.get("unit_cost", 0) or 0,
            reorder_level=request.form.get("reorder_level", 0) or 0,
        )
        db.session.add(item)
        db.session.flush()  # assigns item.id before the batch references it

        # Optional initial stock — batch number / expiry date / quantity are
        # all-or-nothing: only create a batch if these were actually filled
        # in, so a plain catalog-only item can still be added without one.
        batch_number = request.form.get("batch_number", "").strip()
        expiry_date_str = request.form.get("expiry_date", "").strip()
        initial_quantity = request.form.get("initial_quantity", "").strip()

        if batch_number and expiry_date_str and initial_quantity:
            quantity = float(initial_quantity)
            batch = Batch(
                item_id=item.id,
                batch_number=batch_number,
                expiry_date=datetime.strptime(expiry_date_str, "%Y-%m-%d").date(),
                quantity_received=quantity,
                quantity_remaining=quantity,
                location="Supply Chain Store",
            )
            db.session.add(batch)
            db.session.flush()

            log_movement(item, batch, "receipt", quantity, to_location="Supply Chain Store",
                         reference="Initial stock on item creation")

        db.session.commit()
        flash(f"Item {item.name} added.", "success")
        return redirect(url_for("inventory_list"))

    return render_template("inventory/form.html", categories=categories, suppliers=suppliers, item=None)

@app.route("/inventory/<int:item_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("admin", "supply_chain")
def inventory_edit(item_id):
    item = Item.query.get_or_404(item_id)
    categories = Category.query.order_by(Category.name).all()
    suppliers = Supplier.query.order_by(Supplier.name).all()

    if request.method == "POST":
        item.sku = request.form["sku"].strip()
        item.name = request.form["name"].strip()
        item.category_id = request.form.get("category_id") or None
        item.supplier_id = request.form.get("supplier_id") or None
        item.unit_of_issue = request.form.get("unit_of_issue", item.unit_of_issue)
        item.unit_cost = request.form.get("unit_cost", item.unit_cost) or 0

        manual_reorder = request.form.get("manual_reorder_level")
        if manual_reorder:
            item.reorder_level = manual_reorder
            item.reorder_level_manual_override = True

        db.session.commit()
        flash(f"Item {item.name} updated.", "success")
        return redirect(url_for("inventory_detail", item_id=item.id))

    return render_template("inventory/form.html", categories=categories, suppliers=suppliers, item=item)

@app.route("/inventory/<int:item_id>")
@login_required
def inventory_detail(item_id):
    item = Item.query.get_or_404(item_id)
    scope_locations = _current_scope_locations()

    if scope_locations:
        visible_batches = [b for b in item.batches if b.location in scope_locations]
        by_location = {loc: qty for loc, qty in item.quantity_on_hand_by_location.items()
                        if loc in scope_locations}
        scoped_quantity = sum(by_location.values())
    else:
        visible_batches = list(item.batches)
        by_location = item.quantity_on_hand_by_location
        scoped_quantity = item.quantity_on_hand

    return render_template("inventory/detail.html", item=item, currency=Config.CURRENCY,
                            expiry_months=Config.EXPIRY_ALERT_MONTHS, today=date.today(),
                            visible_batches=visible_batches, by_location=by_location,
                            scoped_quantity=scoped_quantity)

@app.route("/inventory/<int:item_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def inventory_delete(item_id):
    item = Item.query.get_or_404(item_id)
    db.session.delete(item)
    db.session.commit()
    flash("Item removed.", "info")
    return redirect(url_for("inventory_list"))


@app.route("/batches")
@login_required
def inventory_batches():
    """General batch listing — filterable by search, location, and expiry status.
    Backs templates/inventory/batches.html."""
    q = request.args.get("q", "").strip()
    location = request.args.get("location", "").strip()
    expiry_filter = request.args.get("expiry", "").strip()

    today = date.today()
    expiry_alert_cutoff = today + relativedelta(months=Config.EXPIRY_ALERT_MONTHS)

    scope_locations = _current_scope_locations()

    query = Batch.query.join(Item)
    if q:
        query = query.filter(Item.name.ilike(f"%{q}%") | Batch.batch_number.ilike(f"%{q}%"))
    if location:
        query = query.filter(Batch.location == location)
    if scope_locations:
        query = query.filter(Batch.location.in_(scope_locations))
    if expiry_filter == "near":
        query = query.filter(Batch.expiry_date >= today, Batch.expiry_date <= expiry_alert_cutoff)
    elif expiry_filter == "expired":
        query = query.filter(Batch.expiry_date < today)

    batches = query.order_by(Batch.expiry_date.asc()).all()

    return render_template(
        "inventory/batches.html",
        batches=batches,
        today=today,
        expiry_alert_cutoff=expiry_alert_cutoff,
        scope_locations=scope_locations,
        location_options=scope_locations or LOCATIONS,
    )
@app.route("/batches/export.csv")
@login_required
def batches_export():
    """Respects the same q / location / expiry filters as the /batches listing."""
    q = request.args.get("q", "").strip()
    location = request.args.get("location", "").strip()
    expiry_filter = request.args.get("expiry", "").strip()

    today = date.today()
    expiry_alert_cutoff = today + relativedelta(months=Config.EXPIRY_ALERT_MONTHS)

    scope_locations = _current_scope_locations()

    query = Batch.query.join(Item)
    if q:
        query = query.filter(Item.name.ilike(f"%{q}%") | Batch.batch_number.ilike(f"%{q}%"))
    if location:
        query = query.filter(Batch.location == location)
    if scope_locations:
        query = query.filter(Batch.location.in_(scope_locations))
    if expiry_filter == "near":
        query = query.filter(Batch.expiry_date >= today, Batch.expiry_date <= expiry_alert_cutoff)
    elif expiry_filter == "expired":
        query = query.filter(Batch.expiry_date < today)

    rows = [
        (b.item.name, b.batch_number, b.location, b.expiry_date,
         b.quantity_received, b.quantity_remaining)
        for b in query.order_by(Batch.expiry_date.asc()).all()
    ]
    return _csv_response(
        "batches.csv",
        ["Item", "Batch Number", "Location", "Expiry Date", "Qty Received", "Qty Remaining"],
        rows,
    )


@app.route("/batches/receive", methods=["GET", "POST"])
@login_required
@role_required("admin", "supply_chain")
def batch_receive():
    """Manual/ad-hoc receipt of stock directly into the Supply Chain Store (outside a PO)."""
    items = Item.query.order_by(Item.name).all()

    if request.method == "POST":
        item = Item.query.get_or_404(request.form["item_id"])
        quantity = request.form.get("quantity", 0) or 0

        batch = Batch(
            item_id=item.id,
            batch_number=request.form["batch_number"].strip(),
            expiry_date=datetime.strptime(request.form["expiry_date"], "%Y-%m-%d").date(),
            quantity_received=quantity,
            quantity_remaining=quantity,
            location="Supply Chain Store",
        )
        db.session.add(batch)
        db.session.flush()

        log_movement(item, batch, "receipt", quantity, to_location="Supply Chain Store",
                     reference=request.form.get("reference", "Manual receipt"))
        db.session.commit()
        flash(f"Received {quantity} units of {item.name} into Supply Chain Store.", "success")
        return redirect(url_for("inventory_detail", item_id=item.id))

    return render_template("inventory/receive.html", items=items)


ALLOWED_TRANSFER_DESTINATIONS = {
    "Supply Chain Store": ["Drug Store"],
    "Drug Store": ["Holding"],
    "Holding": ["Outpatient Pharmacy", "Inpatient Pharmacy"],
    # Outpatient Pharmacy / Inpatient Pharmacy are end points — stock only
    # leaves them via dispensing (prescription_dispense / outpatient_dispense
    # / inpatient_dispense), never via a manual transfer.
}


def _allowed_transfer_destinations(current_location):
    """Where a batch at `current_location` is allowed to move to next,
    enforcing the Supply Chain Store -> Drug Store -> Holding -> dispensing
    area pipeline. Locations not in the mapping (the two dispensing areas)
    have no valid transfer destinations at all."""
    return ALLOWED_TRANSFER_DESTINATIONS.get(current_location, [])


@app.route("/batches/<int:batch_id>/transfer", methods=["GET", "POST"])
@login_required
@role_required("admin", "store_officer", "pharmacist")
def batch_transfer(batch_id):
    batch = Batch.query.get_or_404(batch_id)
    allowed_destinations = _allowed_transfer_destinations(batch.location)

    if request.method == "POST":
        quantity = float(request.form.get("quantity", 0) or 0)
        to_location = request.form.get("to_location", "")

        if to_location not in allowed_destinations:
            flash(
                f"Stock at {batch.location} can only move to: "
                f"{', '.join(allowed_destinations) or 'nowhere — this is an end point'}.",
                "danger",
            )
            return redirect(url_for("batch_transfer", batch_id=batch.id))

        if quantity <= 0 or quantity > float(batch.quantity_remaining or 0):
            flash("Invalid transfer quantity.", "danger")
            return redirect(url_for("batch_transfer", batch_id=batch.id))

        from_location = batch.location
        batch.quantity_remaining = float(batch.quantity_remaining) - quantity

        # mirror the physical Stock Control Card: create a matching batch record at destination
        dest_batch = Batch.query.filter_by(
            item_id=batch.item_id, batch_number=batch.batch_number, location=to_location
        ).first()
        if not dest_batch:
            dest_batch = Batch(
                item_id=batch.item_id, batch_number=batch.batch_number,
                expiry_date=batch.expiry_date, quantity_received=0,
                quantity_remaining=0, location=to_location,
            )
            db.session.add(dest_batch)
            db.session.flush()
        dest_batch.quantity_remaining = float(dest_batch.quantity_remaining) + quantity

        log_movement(batch.item, batch, "transfer", quantity,
                     from_location=from_location, to_location=to_location,
                     reference=request.form.get("reference", "Stock Control Card transfer"))
        db.session.commit()
        flash(f"Transferred {quantity:g} units from {from_location} to {to_location}.", "success")
        return redirect(url_for("inventory_detail", item_id=batch.item_id))

    return render_template("inventory/transfer.html", batch=batch,
                            destinations=allowed_destinations, currency=Config.CURRENCY)

@app.route("/expiry")
@login_required
def expiry_report():
    scope_locations = _current_scope_locations()
    batches = [
        b for b in Batch.query.order_by(Batch.expiry_date).all()
        if (b.quantity_remaining or 0) > 0
        and (not scope_locations or b.location in scope_locations)
    ]
    near_expiry = [b for b in batches if b.is_near_expiry(Config.EXPIRY_ALERT_MONTHS)]
    return render_template("inventory/expiry.html", batches=batches, near_expiry=near_expiry,
                            months=Config.EXPIRY_ALERT_MONTHS, scope_locations=scope_locations)

@app.route("/expiry-alerts")
@login_required
def expiry_alerts():
    """Dedicated FEFO alert dashboard — summary counters + soonest-expiry-first table.
    Backs templates/inventory/expiry_alerts.html. This is separate from the
    existing /expiry route (expiry_report), which is a simpler combined view."""
    q = request.args.get("q", "").strip()
    location = request.args.get("location", "").strip()
    urgency = request.args.get("urgency", "").strip()

    today = date.today()

    scope_locations = _current_scope_locations()

    query = Batch.query.join(Item).filter(Batch.quantity_remaining > 0)
    if q:
        query = query.filter(Item.name.ilike(f"%{q}%") | Batch.batch_number.ilike(f"%{q}%"))
    if location:
        query = query.filter(Batch.location == location)
    if scope_locations:
        query = query.filter(Batch.location.in_(scope_locations))

    all_batches = query.order_by(Batch.expiry_date.asc()).all()

    # attach a computed days_remaining to each batch for the template
    alerts = []
    expired_count = critical_count = warning_count = watch_count = 0

    for b in all_batches:
        days_remaining = (b.expiry_date - today).days
        b.days_remaining = days_remaining  # attach for template use

        if days_remaining < 0:
            tier = "expired"
            expired_count += 1
        elif days_remaining <= 30:
            tier = "critical"
            critical_count += 1
        elif days_remaining <= 90:
            tier = "warning"
            warning_count += 1
        elif days_remaining <= (Config.EXPIRY_ALERT_MONTHS * 30):
            tier = "watch"
            watch_count += 1
        else:
            continue  # outside the 6-month window entirely — not an alert

        if urgency and urgency != tier:
            continue

        alerts.append(b)

    summary = {
        "expired_count": expired_count,
        "critical_count": critical_count,
        "warning_count": warning_count,
        "watch_count": watch_count,
    }

    return render_template(
        "inventory/expiry_alerts.html",
        alerts=alerts,
        summary=summary,
        currency=Config.CURRENCY,
        scope_locations=scope_locations,
    )

@app.route("/expiry-alerts/export.csv")
@login_required
def expiry_alerts_export():
    """Respects the same q / location / urgency filters as the /expiry-alerts dashboard."""
    q = request.args.get("q", "").strip()
    location = request.args.get("location", "").strip()
    urgency = request.args.get("urgency", "").strip()

    today = date.today()

    scope_locations = _current_scope_locations()

    query = Batch.query.join(Item).filter(Batch.quantity_remaining > 0)
    if q:
        query = query.filter(Item.name.ilike(f"%{q}%") | Batch.batch_number.ilike(f"%{q}%"))
    if location:
        query = query.filter(Batch.location == location)
    if scope_locations:
        query = query.filter(Batch.location.in_(scope_locations))

    all_batches = query.order_by(Batch.expiry_date.asc()).all()
    rows = []
    for b in all_batches:
        days_remaining = (b.expiry_date - today).days

        if days_remaining < 0:
            tier = "expired"
        elif days_remaining <= 30:
            tier = "critical"
        elif days_remaining <= 90:
            tier = "warning"
        elif days_remaining <= (Config.EXPIRY_ALERT_MONTHS * 30):
            tier = "watch"
        else:
            continue  # outside the alert window entirely

        if urgency and urgency != tier:
            continue

        rows.append((
            b.item.name, b.batch_number, b.location, b.expiry_date,
            days_remaining, b.quantity_remaining,
            float(b.quantity_remaining) * float(b.item.unit_cost),
        ))

    return _csv_response(
        "expiry_alerts.csv",
        ["Item", "Batch Number", "Location", "Expiry Date", "Days Remaining",
         "Qty Remaining", "Value at Risk (KES)"],
        rows,
    )


# ---------------------------------------------------------------------------
# Suppliers
# ---------------------------------------------------------------------------

@app.route("/suppliers")
@login_required
def supplier_list():
    suppliers = Supplier.query.order_by(Supplier.name).all()
    return render_template("suppliers/list.html", suppliers=suppliers)


@app.route("/suppliers/new", methods=["GET", "POST"])
@login_required
@role_required("admin", "supply_chain")
def supplier_new():
    if request.method == "POST":
        supplier = Supplier(
            name=request.form["name"].strip(),
            contact_person=request.form.get("contact_person"),
            phone=request.form.get("phone"),
            email=request.form.get("email"),
            address=request.form.get("address"),
        )
        db.session.add(supplier)
        db.session.commit()
        flash(f"Supplier {supplier.name} added.", "success")
        return redirect(url_for("supplier_list"))

    return render_template("suppliers/form.html", supplier=None)


@app.route("/suppliers/<int:supplier_id>")
@login_required
def supplier_detail(supplier_id):
    """Supplier profile page — contact details, the items-supplied catalog,
    and a full delivery history sorted soonest-expiry-first (FEFO) so store
    officers can see at a glance what this supplier has brought in and
    which batches need to move first."""
    supplier = Supplier.query.get_or_404(supplier_id)
    items = Item.query.order_by(Item.name).all()

    # Every batch this supplier has delivered, across all their deliveries,
    # sorted so the earliest-expiring stock shows first.
    delivery_lines = (
        DeliveryLineItem.query
        .join(Delivery)
        .filter(Delivery.supplier_id == supplier_id)
        .order_by(DeliveryLineItem.expiry_date.asc())
        .all()
    )

    today = date.today()
    near_expiry_cutoff = today + relativedelta(months=Config.EXPIRY_ALERT_MONTHS)

    return render_template(
        "suppliers/detail.html",
        supplier=supplier,
        items=items,
        delivery_lines=delivery_lines,
        today=today,
        near_expiry_cutoff=near_expiry_cutoff,
        currency=Config.CURRENCY,
    )


@app.route("/suppliers/<int:supplier_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("admin", "supply_chain")
def supplier_edit(supplier_id):
    supplier = Supplier.query.get_or_404(supplier_id)

    if request.method == "POST":
        supplier.name = request.form["name"].strip()
        supplier.contact_person = request.form.get("contact_person")
        supplier.phone = request.form.get("phone")
        supplier.email = request.form.get("email")
        supplier.address = request.form.get("address")
        db.session.commit()
        flash("Supplier updated.", "success")
        return redirect(url_for("supplier_list"))

    return render_template("suppliers/form.html", supplier=supplier)


# ---------------------------------------------------------------------------
# Hospitals
# ---------------------------------------------------------------------------

@app.route("/hospitals")
@login_required
@role_required("admin")
def hospital_list():
    hospitals = Hospital.query.order_by(Hospital.name).all()
    return render_template("hospitals/list.html", hospitals=hospitals)


@app.route("/hospitals/new", methods=["GET", "POST"])
@login_required
@role_required("admin")
def hospital_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        code = request.form.get("code", "").strip().upper()

        if not name or not code:
            flash("Hospital name and code are required.", "danger")
            return render_template("hospitals/form.html", hospital=None)

        if Hospital.query.filter((Hospital.name == name) | (Hospital.code == code)).first():
            flash("Hospital with that name or code already exists.", "danger")
            return render_template("hospitals/form.html", hospital=None)

        hospital = Hospital(
            name=name,
            code=code,
            address=request.form.get("address"),
            contact_person=request.form.get("contact_person"),
            phone=request.form.get("phone"),
            email=request.form.get("email"),
            is_active=request.form.get("is_active") == "on",
        )
        db.session.add(hospital)
        db.session.commit()
        flash(f"Hospital {hospital.name} added.", "success")
        return redirect(url_for("hospital_list"))

    return render_template("hospitals/form.html", hospital=None)


@app.route("/hospitals/<int:hospital_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("admin")
def hospital_edit(hospital_id):
    hospital = Hospital.query.get_or_404(hospital_id)

    if request.method == "POST":
        hospital.name = request.form.get("name", "").strip()
        hospital.code = request.form.get("code", "").strip().upper()
        hospital.address = request.form.get("address")
        hospital.contact_person = request.form.get("contact_person")
        hospital.phone = request.form.get("phone")
        hospital.email = request.form.get("email")
        hospital.is_active = request.form.get("is_active") == "on"

        db.session.commit()
        flash(f"Hospital {hospital.name} updated.", "success")
        return redirect(url_for("hospital_list"))

    return render_template("hospitals/form.html", hospital=hospital)


@app.route("/suppliers/<int:supplier_id>/catalog/add", methods=["POST"])
@login_required
@role_required("admin", "supply_chain")
def supplier_catalog_add(supplier_id):
    """Add an item to a supplier's catalog (what they typically stock,
    at what price, with what lead time). Purely informational."""
    supplier = Supplier.query.get_or_404(supplier_id)
    item_id = request.form.get("item_id")

    if not item_id:
        flash("Please select an item.", "danger")
        return redirect(url_for("supplier_detail", supplier_id=supplier.id))

    existing = SupplierItem.query.filter_by(supplier_id=supplier.id, item_id=item_id).first()
    if existing:
        flash("This item is already in the supplier's catalog.", "danger")
        return redirect(url_for("supplier_detail", supplier_id=supplier.id))

    entry = SupplierItem(
        supplier_id=supplier.id,
        item_id=item_id,
        typical_unit_cost=request.form.get("typical_unit_cost") or None,
        lead_time_days=request.form.get("lead_time_days") or None,
        notes=request.form.get("notes"),
    )
    db.session.add(entry)
    db.session.commit()

    flash("Item added to supplier catalog.", "success")
    return redirect(url_for("supplier_detail", supplier_id=supplier.id))


@app.route("/suppliers/<int:supplier_id>/catalog/<int:entry_id>/delete", methods=["POST"])
@login_required
@role_required("admin", "supply_chain")
def supplier_catalog_delete(supplier_id, entry_id):
    entry = SupplierItem.query.get_or_404(entry_id)
    if entry.supplier_id != supplier_id:
        abort(404)

    db.session.delete(entry)
    db.session.commit()

    flash("Item removed from supplier catalog.", "info")
    return redirect(url_for("supplier_detail", supplier_id=supplier_id))


# ---------------------------------------------------------------------------
# Procurement Plan
# ---------------------------------------------------------------------------

@app.route("/procurement-plan")
@login_required
def procurement_plan_list():
    year = request.args.get("year", date.today().year, type=int)
    plans = ProcurementPlan.query.filter_by(year=year).all()
    return render_template("procurement/list.html", plans=plans, year=year)


@app.route("/procurement-plan/new", methods=["GET", "POST"])
@login_required
@role_required("admin", "supply_chain")
def procurement_plan_new():
    items = Item.query.order_by(Item.name).all()

    if request.method == "POST":
        plan = ProcurementPlan(
            item_id=request.form["item_id"],
            year=request.form.get("year", date.today().year),
            planned_quantity=request.form.get("planned_quantity", 0) or 0,
        )
        db.session.add(plan)
        db.session.commit()
        flash("Procurement plan line added.", "success")
        return redirect(url_for("procurement_plan_list", year=plan.year))

    return render_template("procurement/form.html", items=items, default_year=date.today().year)


# ---------------------------------------------------------------------------
# Purchase Orders
# ---------------------------------------------------------------------------

@app.route("/purchase-orders")
@login_required
def po_list():
    pos = PurchaseOrder.query.order_by(PurchaseOrder.order_date.desc()).all()
    return render_template("purchase_orders/list.html", purchase_orders=pos)


@app.route("/purchase-orders/new", methods=["GET", "POST"])
@login_required
@role_required("admin", "supply_chain")
def po_new():
    suppliers = Supplier.query.order_by(Supplier.name).all()
    items = Item.query.order_by(Item.name).all()

    if request.method == "POST":
        po = PurchaseOrder(
            po_number=next_reference("PO", PurchaseOrder, "po_number"),
            supplier_id=request.form["supplier_id"],
            status="Draft",
            expected_date=(
                datetime.strptime(request.form["expected_date"], "%Y-%m-%d").date()
                if request.form.get("expected_date") else None
            ),
        )
        db.session.add(po)
        db.session.flush()

        item_ids = request.form.getlist("item_id[]")
        quantities = request.form.getlist("quantity[]")
        costs = request.form.getlist("unit_cost[]")

        for item_id, qty, cost in zip(item_ids, quantities, costs):
            if not item_id or not qty:
                continue
            db.session.add(PurchaseOrderLine(
                purchase_order_id=po.id, item_id=item_id,
                quantity_ordered=qty, unit_cost=cost or 0,
            ))

        db.session.commit()
        flash(f"Purchase order {po.po_number} created.", "success")
        return redirect(url_for("po_detail", po_id=po.id))

    return render_template("purchase_orders/form.html", suppliers=suppliers, items=items)


@app.route("/purchase-orders/<int:po_id>")
@login_required
def po_detail(po_id):
    po = PurchaseOrder.query.get_or_404(po_id)
    return render_template("purchase_orders/detail.html", po=po, currency=Config.CURRENCY)


@app.route("/purchase-orders/<int:po_id>/send", methods=["POST"])
@login_required
@role_required("admin", "supply_chain")
def po_send(po_id):
    po = PurchaseOrder.query.get_or_404(po_id)
    if po.status == "Draft":
        po.status = "Sent"
        db.session.commit()
        flash(f"{po.po_number} marked as Sent.", "success")
    return redirect(url_for("po_detail", po_id=po.id))


@app.route("/purchase-orders/<int:po_id>/receive", methods=["GET", "POST"])
@login_required
@role_required("admin", "supply_chain")
def po_receive(po_id):
    po = PurchaseOrder.query.get_or_404(po_id)

    if request.method == "POST":
        for line in po.lines:
            batch_number = request.form.get(f"batch_number_{line.id}")
            expiry_str = request.form.get(f"expiry_date_{line.id}")
            qty_received = request.form.get(f"quantity_received_{line.id}", 0) or 0
            qty_received = float(qty_received)

            if qty_received <= 0 or not batch_number or not expiry_str:
                continue

            batch = Batch(
                item_id=line.item_id,
                batch_number=batch_number,
                expiry_date=datetime.strptime(expiry_str, "%Y-%m-%d").date(),
                quantity_received=qty_received,
                quantity_remaining=qty_received,
                location="Supply Chain Store",
            )
            db.session.add(batch)
            db.session.flush()

            log_movement(line.item, batch, "receipt", qty_received,
                         to_location="Supply Chain Store", reference=po.po_number)

            line.quantity_received = (line.quantity_received or 0) + qty_received

        po.status = "Received"
        db.session.commit()
        flash(f"{po.po_number} received into Supply Chain Store.", "success")
        return redirect(url_for("po_detail", po_id=po.id))

    return render_template("purchase_orders/receive.html", po=po)


@app.route("/purchase-orders/<int:po_id>/cancel", methods=["POST"])
@login_required
@role_required("admin", "supply_chain")
def po_cancel(po_id):
    po = PurchaseOrder.query.get_or_404(po_id)
    po.status = "Cancelled"
    db.session.commit()
    flash(f"{po.po_number} cancelled.", "info")
    return redirect(url_for("po_list"))

# ---------------------------------------------------------------------------
# Purchase Requisitions (department -> Supply Chain -> Purchase Order)
#
# Different from Requisition (S11) above: that one issues stock the store
# ALREADY has. This one is for a department asking Supply Chain to actually
# buy something that isn't in stock — reviewed, then optionally converted
# into a Draft PurchaseOrder.
# ---------------------------------------------------------------------------

@app.route("/purchase-requisitions")
@login_required
def purchase_requisition_list():
    status = request.args.get("status")
    query = PurchaseRequisition.query
    if current_user.role not in ("admin", "supply_chain"):
        query = query.filter_by(requested_by_id=current_user.id)
    if status:
        query = query.filter_by(status=status)
    prs = query.order_by(PurchaseRequisition.created_at.desc()).all()
    return render_template("purchase_requisitions/list.html", prs=prs, status=status)


@app.route("/purchase-requisitions/new", methods=["GET", "POST"])
@login_required
def purchase_requisition_new():
    departments = Department.query.order_by(Department.name).all()
    items = Item.query.order_by(Item.name).all()

    if request.method == "POST":
        pr = PurchaseRequisition(
            pr_number=next_reference("PR", PurchaseRequisition, "pr_number"),
            department_id=request.form.get("department_id") or current_user.department_id,
            requested_by_id=current_user.id,
            status="Pending",
            notes=request.form.get("notes", "").strip() or None,
        )
        db.session.add(pr)
        db.session.flush()

        item_ids = request.form.getlist("item_id[]")
        item_names = request.form.getlist("item_name[]")
        quantities = request.form.getlist("quantity_requested[]")
        units = request.form.getlist("unit_of_issue[]")
        justifications = request.form.getlist("justification[]")

        line_count = 0
        for item_id, item_name, qty, unit, justification in zip(
            item_ids, item_names, quantities, units, justifications
        ):
            if not item_name.strip() or not qty:
                continue
            existing_item = Item.query.get(item_id) if item_id else None
            db.session.add(PurchaseRequisitionLine(
                purchase_requisition_id=pr.id,
                item_id=existing_item.id if existing_item else None,
                item_name=existing_item.name if existing_item else item_name.strip(),
                quantity_requested=float(qty),
                unit_of_issue=(unit.strip() if unit and unit.strip()
                               else (existing_item.unit_of_issue if existing_item else None)),
                justification=justification.strip() or None,
            ))
            line_count += 1

        if line_count == 0:
            db.session.rollback()
            flash("Add at least one item before submitting.", "danger")
            return render_template("purchase_requisitions/form.html", departments=departments, items=items)

        db.session.commit()
        flash(f"Purchase requisition {pr.pr_number} submitted to Supply Chain.", "success")
        return redirect(url_for("purchase_requisition_detail", pr_id=pr.id))

    return render_template("purchase_requisitions/form.html", departments=departments, items=items)

ROLE_RESTOCK_SOURCE_DEST = {
    "hod_pharmacy": ("Drug Store", "Holding"),
    "store_officer": ("Supply Chain Store", "Drug Store"),
}


@app.route("/requisitions/restock/new", methods=["GET", "POST"])
@login_required
@role_required("hod_pharmacy", "store_officer")
def requisition_restock_new():
    source, destination = ROLE_RESTOCK_SOURCE_DEST[current_user.role]
    items = Item.query.order_by(Item.name).all()

    if request.method == "POST":
        # Requisition.department_id is required — fall back to a
        # store-named department if this account has none assigned.
        department_id = current_user.department_id
        if not department_id:
            dept = Department.query.filter_by(name=source).first()
            if not dept:
                dept = Department(name=source, is_store=True)
                db.session.add(dept)
                db.session.flush()
            department_id = dept.id

        req = Requisition(
            req_number=next_reference("S11", Requisition, "req_number"),
            department_id=department_id,
            issue_point=source,
            destination_location=destination,
            requested_by_id=current_user.id,
            status="Pending",
        )
        db.session.add(req)
        db.session.flush()

        item_ids = request.form.getlist("item_id[]")
        quantities = request.form.getlist("quantity_required[]")
        remarks = request.form.getlist("remarks[]")

        line_count = 0
        for item_id, qty, remark in zip(item_ids, quantities, remarks):
            if not item_id or not qty:
                continue
            db.session.add(RequisitionLine(
                requisition_id=req.id, item_id=item_id,
                quantity_required=qty, remarks=remark,
            ))
            line_count += 1

        if line_count == 0:
            db.session.rollback()
            flash("Add at least one item before submitting.", "danger")
            return render_template("requisitions/restock_form.html", items=items,
                                    source=source, destination=destination)

        db.session.commit()
        flash(f"Restock requisition {req.req_number} sent: {source} → {destination}.", "success")
        return redirect(url_for("requisition_detail", req_id=req.id))

    return render_template("requisitions/restock_form.html", items=items,
                            source=source, destination=destination)

@app.route("/purchase-requisitions/<int:pr_id>")
@login_required
def purchase_requisition_detail(pr_id):
    pr = PurchaseRequisition.query.get_or_404(pr_id)
    suppliers = (
        Supplier.query.order_by(Supplier.name).all()
        if current_user.role in ("admin", "supply_chain") else []
    )
    return render_template("purchase_requisitions/detail.html", pr=pr, suppliers=suppliers)


@app.route("/purchase-requisitions/<int:pr_id>/approve", methods=["POST"])
@login_required
@role_required("admin", "supply_chain")
def purchase_requisition_approve(pr_id):
    pr = PurchaseRequisition.query.get_or_404(pr_id)
    if pr.status != "Pending":
        flash("Only pending purchase requisitions can be approved.", "warning")
        return redirect(url_for("purchase_requisition_detail", pr_id=pr.id))

    pr.status = "Approved"
    pr.reviewed_by_id = current_user.id
    pr.reviewed_at = datetime.utcnow()
    pr.review_notes = request.form.get("review_notes", "").strip() or None
    db.session.commit()
    flash(f"{pr.pr_number} approved.", "success")
    return redirect(url_for("purchase_requisition_detail", pr_id=pr.id))


@app.route("/purchase-requisitions/<int:pr_id>/reject", methods=["POST"])
@login_required
@role_required("admin", "supply_chain")
def purchase_requisition_reject(pr_id):
    pr = PurchaseRequisition.query.get_or_404(pr_id)
    if pr.status != "Pending":
        flash("Only pending purchase requisitions can be rejected.", "warning")
        return redirect(url_for("purchase_requisition_detail", pr_id=pr.id))

    pr.status = "Rejected"
    pr.reviewed_by_id = current_user.id
    pr.reviewed_at = datetime.utcnow()
    pr.review_notes = request.form.get("review_notes", "").strip() or None
    db.session.commit()
    flash(f"{pr.pr_number} rejected.", "info")
    return redirect(url_for("purchase_requisition_detail", pr_id=pr.id))


@app.route("/purchase-requisitions/<int:pr_id>/convert", methods=["POST"])
@login_required
@role_required("admin", "supply_chain")
def purchase_requisition_convert(pr_id):
    """Approved -> Draft PurchaseOrder. Only lines already linked to a real
    catalog Item carry over automatically; brand-new items (free-text only)
    are skipped and flagged, since a PurchaseOrderLine requires a real Item —
    add the item via Inventory first, then add it to the PO manually."""
    pr = PurchaseRequisition.query.get_or_404(pr_id)
    if pr.status != "Approved":
        flash("Only approved purchase requisitions can be converted to a PO.", "warning")
        return redirect(url_for("purchase_requisition_detail", pr_id=pr.id))

    supplier_id = request.form.get("supplier_id")
    if not supplier_id:
        flash("Choose a supplier to convert this into a purchase order.", "danger")
        return redirect(url_for("purchase_requisition_detail", pr_id=pr.id))

    po = PurchaseOrder(
        po_number=next_reference("PO", PurchaseOrder, "po_number"),
        supplier_id=supplier_id,
        status="Draft",
    )
    db.session.add(po)
    db.session.flush()

    skipped = []
    for line in pr.lines:
        if not line.item_id:
            skipped.append(line.item_name)
            continue
        db.session.add(PurchaseOrderLine(
            purchase_order_id=po.id,
            item_id=line.item_id,
            quantity_ordered=line.quantity_requested,
            unit_cost=line.item.unit_cost if line.item else 0,
        ))

    pr.status = "Converted"
    pr.purchase_order_id = po.id
    db.session.commit()

    if skipped:
        flash(
            f"{pr.pr_number} converted to {po.po_number}, but skipped (not yet in Inventory): "
            f"{', '.join(skipped)}. Add them via Inventory, then add to this PO manually.",
            "warning",
        )
    else:
        flash(f"{pr.pr_number} converted to purchase order {po.po_number}.", "success")

    return redirect(url_for("po_detail", po_id=po.id))

# ---------------------------------------------------------------------------
# Deliveries (always linked to a Purchase Order — po.status must be "Sent")
# ---------------------------------------------------------------------------

@app.route("/purchase-orders/<int:po_id>/deliveries/new", methods=["GET", "POST"])
@login_required
@role_required("admin", "supply_chain")
def delivery_new(po_id):
    po = PurchaseOrder.query.get_or_404(po_id)

    if po.status != "Sent":
        flash("Only purchase orders marked as Sent can receive a delivery.", "warning")
        return redirect(url_for("po_detail", po_id=po.id))

    if request.method == "POST":
        delivery = Delivery(
            delivery_number=next_reference("DEL", Delivery, "delivery_number"),
            purchase_order_id=po.id,
            supplier_id=po.supplier_id,
            delivery_note_number=request.form.get("delivery_note_number", "").strip(),
            delivery_date=(
                datetime.strptime(request.form["delivery_date"], "%Y-%m-%d").date()
                if request.form.get("delivery_date") else date.today()
            ),
            received_by_id=current_user.id,
            status="Received",
        )
        db.session.add(delivery)
        db.session.flush()  # assigns delivery.id before line items reference it

        item_ids = request.form.getlist("item_id[]")
        batch_numbers = request.form.getlist("batch_number[]")
        expiry_dates = request.form.getlist("expiry_date[]")
        quantities = request.form.getlist("quantity_delivered[]")
        unit_prices = request.form.getlist("unit_price[]")

        for item_id, batch_number, expiry_str, qty, price in zip(
            item_ids, batch_numbers, expiry_dates, quantities, unit_prices
        ):
            if not item_id or not qty or not batch_number or not expiry_str:
                continue

            item = Item.query.get(item_id)
            qty = float(qty)
            price = float(price) if price else float(item.unit_cost or 0)

            db.session.add(DeliveryLineItem(
                delivery_id=delivery.id,
                item_id=item.id,
                batch_number=batch_number,
                expiry_date=datetime.strptime(expiry_str, "%Y-%m-%d").date(),
                quantity_delivered=qty,
                unit_price=price,
            ))

            batch = Batch(
                item_id=item.id,
                batch_number=batch_number,
                expiry_date=datetime.strptime(expiry_str, "%Y-%m-%d").date(),
                quantity_received=qty,
                quantity_remaining=qty,
                location="Supply Chain Store",
            )
            db.session.add(batch)
            db.session.flush()

            # same audit-trail helper used everywhere else in the app
            log_movement(item, batch, "receipt", qty, to_location="Supply Chain Store",
                         reference=f"{delivery.delivery_number} / {po.po_number}")
            # keep the PO line's received quantity in sync, same as po_receive()
            po_line = next((l for l in po.lines if l.item_id == item.id), None)
            if po_line:
                po_line.quantity_received = (po_line.quantity_received or 0) + qty

        po.status = "Received"
        db.session.commit()
        flash(f"{delivery.delivery_number} recorded against {po.po_number} — stock updated.", "success")
        return redirect(url_for("po_detail", po_id=po.id))

    items = Item.query.order_by(Item.name).all()
    return render_template("purchase_orders/new_delivery.html", po=po, items=items,
                            currency=Config.CURRENCY, today=date.today().isoformat())


@app.route("/deliveries/<int:delivery_id>")
@login_required
def delivery_detail(delivery_id):
    delivery = Delivery.query.get_or_404(delivery_id)
    return render_template(
        "purchase_orders/delivery_detail.html", delivery=delivery, currency=Config.CURRENCY
    )


# ---------------------------------------------------------------------------
# Requisitions (S11 Counter Requisition and Issue Voucher)
# ---------------------------------------------------------------------------

@app.route("/requisitions")
@login_required
def requisition_list():
    status = request.args.get("status")
    query = Requisition.query
    if status:
        query = query.filter_by(status=status)
    reqs = query.order_by(Requisition.created_at.desc()).all()
    return render_template("requisitions/list.html", requisitions=reqs, status=status)


@app.route("/requisitions/new", methods=["GET", "POST"])
@login_required
def requisition_new():
    departments = Department.query.order_by(Department.name).all()
    items = Item.query.order_by(Item.name).all()

    if request.method == "POST":
        department_id = request.form["department_id"]

        req = Requisition(
            req_number=next_reference("S11", Requisition, "req_number"),
            department_id=department_id,
            issue_point=request.form["issue_point"],
            requested_by_id=current_user.id,
            status="Pending",
        )
        db.session.add(req)
        db.session.flush()

        item_ids = request.form.getlist("item_id[]")
        quantities = request.form.getlist("quantity_required[]")
        remarks = request.form.getlist("remarks[]")

        # ---- Validate each line against this department's own average
        # monthly consumption of that item, before creating anything. ----
        errors = []
        for item_id, qty in zip(item_ids, quantities):
            if not item_id or not qty:
                continue
            item = Item.query.get(item_id)
            qty = float(qty)
            avg = _department_avg_monthly_consumption(department_id, item_id)
            if avg is not None:
                cap = avg * Config.REQUISITION_MAX_MULTIPLIER
                if qty > cap:
                    errors.append(
                        f"{item.name}: requested {qty:g}, but this department's average "
                        f"monthly usage is {avg:.1f} (cap: {cap:.1f}). Reduce the quantity "
                        f"or contact an admin if this is a genuine spike."
                    )

        if errors:
            db.session.rollback()
            for e in errors:
                flash(e, "danger")
            return render_template("requisitions/form.html", departments=departments, items=items,
                                    locations=("Drug Store", "Holding"))

        for item_id, qty, remark in zip(item_ids, quantities, remarks):
            if not item_id or not qty:
                continue
            db.session.add(RequisitionLine(
                requisition_id=req.id, item_id=item_id,
                quantity_required=qty, remarks=remark,
            ))

        db.session.commit()
        flash(f"Requisition {req.req_number} submitted for approval.", "success")
        return redirect(url_for("requisition_detail", req_id=req.id))

    return render_template("requisitions/form.html", departments=departments, items=items,
                            locations=("Drug Store", "Holding"))

@app.route("/requisitions/<int:req_id>")
@login_required
def requisition_detail(req_id):
    req = Requisition.query.get_or_404(req_id)
    can_action = _can_action_requisition(current_user, req)
    return render_template("requisitions/detail.html", req=req, currency=Config.CURRENCY,
                            can_action=can_action)

@app.route("/requisitions/<int:req_id>/approve", methods=["POST"])
@login_required
def requisition_approve(req_id):
    req = Requisition.query.get_or_404(req_id)
    if not _can_action_requisition(current_user, req):
        flash("You don't have permission to approve this requisition.", "danger")
        return redirect(url_for("requisition_detail", req_id=req.id))
    if req.status != "Pending":
        flash("Only pending requisitions can be approved.", "warning")
        return redirect(url_for("requisition_detail", req_id=req.id))

    req.status = "Approved"
    req.approved_by_id = current_user.id
    db.session.commit()
    flash(f"{req.req_number} approved.", "success")
    return redirect(url_for("requisition_detail", req_id=req.id))


@app.route("/requisitions/<int:req_id>/reject", methods=["POST"])
@login_required
def requisition_reject(req_id):
    req = Requisition.query.get_or_404(req_id)
    if not _can_action_requisition(current_user, req):
        flash("You don't have permission to reject this requisition.", "danger")
        return redirect(url_for("requisition_detail", req_id=req.id))
    if req.status != "Pending":
        flash("Only pending requisitions can be rejected.", "warning")
        return redirect(url_for("requisition_detail", req_id=req.id))

    req.status = "Rejected"
    req.approved_by_id = current_user.id
    db.session.commit()
    flash(f"{req.req_number} rejected.", "info")
    return redirect(url_for("requisition_detail", req_id=req.id))


@app.route("/requisitions/<int:req_id>/issue", methods=["POST"])
@login_required
def requisition_issue(req_id):
    """Approved -> Issued. Deducts stock FEFO from the issue point and logs
    movements, and creates/updates the corresponding batch at the correct
    destination so stock doesn't vanish from the system:
      - Restock requisitions (destination_location set) -> that location.
      - Holding-sourced requisitions (OP/IP Pharmacy drawing on Holding)
        -> the requesting department's own location (Outpatient Pharmacy /
        Inpatient Pharmacy), since that's what dispensing draws from.
      - Everything else (e.g. a ward consuming from Drug Store) -> no
        destination batch; this is genuine consumption, not a transfer."""
    req = Requisition.query.get_or_404(req_id)
    if not _can_action_requisition(current_user, req):
        flash("You don't have permission to issue this requisition.", "danger")
        return redirect(url_for("requisition_detail", req_id=req.id))
    if req.status != "Approved":
        flash("Only approved requisitions can be issued.", "warning")
        return redirect(url_for("requisition_detail", req_id=req.id))

    if req.destination_location:
        destination = req.destination_location
    elif req.issue_point == "Holding":
        destination = req.department.name
    else:
        destination = None

    for line in req.lines:
        remaining_to_issue = float(line.quantity_required)
        batches = (
            Batch.query.filter_by(item_id=line.item_id, location=req.issue_point)
            .filter(Batch.quantity_remaining > 0)
            .order_by(Batch.expiry_date.asc())  # FEFO
            .all()
        )

        for batch in batches:
            if remaining_to_issue <= 0:
                break
            take = min(remaining_to_issue, float(batch.quantity_remaining))
            batch.quantity_remaining = float(batch.quantity_remaining) - take
            remaining_to_issue -= take

            log_movement(line.item, batch, "issue", take,
                         from_location=req.issue_point,
                         to_location=destination or req.department.name,
                         reference=req.req_number)

            if destination:
                dest_batch = Batch.query.filter_by(
                    item_id=batch.item_id,
                    batch_number=batch.batch_number,
                    location=destination,
                ).first()
                if not dest_batch:
                    dest_batch = Batch(
                        item_id=batch.item_id,
                        batch_number=batch.batch_number,
                        expiry_date=batch.expiry_date,
                        quantity_received=0,
                        quantity_remaining=0,
                        location=destination,
                    )
                    db.session.add(dest_batch)
                    db.session.flush()
                dest_batch.quantity_remaining = float(dest_batch.quantity_remaining) + take

        line.quantity_issued = float(line.quantity_required) - remaining_to_issue

    req.status = "Issued"
    req.issued_by_id = current_user.id
    db.session.commit()
    flash(f"{req.req_number} issued and stock deducted.", "success")
    return redirect(url_for("requisition_detail", req_id=req.id))@app.route("/requisitions/<int:req_id>/receive", methods=["POST"])
@login_required
@role_required("pharmacist")
def requisition_receive(req_id):
    """Confirms receipt of an issued requisition — only the pharmacy (the
    dispensing point everything actually flows to) or admin can do this.
    The drug store's job ends at issuing; it doesn't receive on the other
    end."""
    req = Requisition.query.get_or_404(req_id)
    req.received_by_id = current_user.id
    db.session.commit()
    flash(f"Receipt of {req.req_number} confirmed.", "success")
    return redirect(url_for("requisition_detail", req_id=req.id))
 
@app.route("/emergency-trolley")
@login_required
@role_required("admin", "store_officer", "pharmacist")
def emergency_trolley_list():
    """Everything currently issued onto the Emergency Trolley that hasn't
    been fully returned yet, one row per requisition line."""
    lines = (
        RequisitionLine.query
        .join(Requisition, RequisitionLine.requisition_id == Requisition.id)
        .join(Department, Requisition.department_id == Department.id)
        .filter(Department.name == "Emergency Trolley")
        .filter(Requisition.status == "Issued")
        .order_by(Requisition.created_at.desc())
        .all()
    )
    outstanding_lines = [line for line in lines if line.quantity_outstanding > 0]
 
    return render_template(
        "emergency_trolley/list.html",
        lines=outstanding_lines,
        currency=Config.CURRENCY,
        today=date.today().isoformat(),
    )
 
 
@app.route("/emergency-trolley/<int:line_id>/return", methods=["POST"])
@login_required
@role_required("admin", "store_officer", "pharmacist")
def emergency_trolley_return(line_id):
    line = RequisitionLine.query.get_or_404(line_id)
    req = line.requisition
 
    quantity = float(request.form.get("quantity", 0) or 0)
    batch_number = request.form.get("batch_number", "").strip()
    expiry_str = request.form.get("expiry_date", "")
 
    if quantity <= 0 or quantity > line.quantity_outstanding:
        flash("Invalid return quantity.", "danger")
        return redirect(url_for("emergency_trolley_list"))
 
    if not batch_number or not expiry_str:
        flash("Batch number and expiry date are required.", "danger")
        return redirect(url_for("emergency_trolley_list"))
 
    destination = req.issue_point  # wherever this requisition was issued from
 
    batch = Batch(
        item_id=line.item_id,
        batch_number=batch_number,
        expiry_date=datetime.strptime(expiry_str, "%Y-%m-%d").date(),
        quantity_received=quantity,
        quantity_remaining=quantity,
        location=destination,
    )
    db.session.add(batch)
    db.session.flush()
 
    log_movement(line.item, batch, "return", quantity,
                 from_location="Emergency Trolley", to_location=destination,
                 reference=f"TROLLEY-RETURN-{req.req_number}")
 
    line.quantity_returned = float(line.quantity_returned or 0) + quantity
    db.session.commit()
 
    flash(f"Returned {quantity:g} units of {line.item.name} to {destination}.", "success")
    return redirect(url_for("emergency_trolley_list"))
 

# ---------------------------------------------------------------------------
# Patients
# ---------------------------------------------------------------------------

@app.route("/patients")
@login_required
def patient_list():
    """Search/browse patients by name or IP/OP number. Read-only lookup —
    available to any logged-in role since store officers, pharmacists, and
    doctors all occasionally need to confirm a patient exists."""
    q = request.args.get("q", "").strip()

    query = Patient.query
    if q:
        query = query.filter(
            Patient.name.ilike(f"%{q}%") | Patient.ip_op_number.ilike(f"%{q}%")
        )

    patients = query.order_by(Patient.name).limit(100).all()
    return render_template("patients/list.html", patients=patients, q=q)


@app.route("/api/patients/search")
@login_required
def api_patient_search():
    """Typeahead search used by the prescription-writing form. Matches on
    name or IP/OP number, up to 10 results."""
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])

    patients = (
        Patient.query
        .filter(Patient.name.ilike(f"%{q}%") | Patient.ip_op_number.ilike(f"%{q}%"))
        .order_by(Patient.name)
        .limit(10)
        .all()
    )

    return jsonify([
        {
            "id": p.id,
            "name": p.name,
            "ip_op_number": p.ip_op_number,
            "patient_type": p.patient_type,
            "clinic_ward_unit": p.clinic_ward_unit or "",
        }
        for p in patients
    ])

@app.route("/api/requisitions/item-stats")
@login_required
def api_requisition_item_stats():
    """Live stats for the requisition form: this department's average
    monthly usage of an item (from Issued requisition history) and the
    actual max allowed quantity — same calculation requisition_new()
    enforces at submit time, so this preview can never disagree with it."""
    item_id = request.args.get("item_id", type=int)
    department_id = request.args.get("department_id", type=int)

    if not item_id:
        return jsonify({"error": "item_id is required"}), 400

    avg = _department_avg_monthly_consumption(department_id, item_id) if department_id else None
    max_allowed = round(avg * Config.REQUISITION_MAX_MULTIPLIER, 2) if avg is not None else None

    return jsonify({
        "avg": round(avg, 2) if avg is not None else None,
        "max_allowed": max_allowed,
    })
@app.route("/api/notifications/pending-requisitions-count")
@login_required
def api_pending_requisitions_count():
    role = current_user.role
    if role == "pharmacist":
        count = _recently_issued_for_pharmacist_count(current_user)
        kind = "issued"
    elif role in ("admin", "store_officer", "hod_pharmacy"):
        scope_locations = ROLE_LOCATION_SCOPE.get(role)
        count = _pending_action_requisitions_count(role, scope_locations)
        kind = "action_needed"
    else:
        count, kind = 0, "action_needed"

    return jsonify({"count": count, "kind": kind})
@app.route("/patients/new", methods=["GET", "POST"])
@login_required
@role_required("registry")
def patient_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        ip_op_number = request.form.get("ip_op_number", "").strip()
        patient_type = request.form.get("patient_type", "Outpatient")
        confirm_duplicate = request.form.get("confirm_duplicate") == "1"

        if not name or not ip_op_number:
            flash("Patient name and IP/OP number are required.", "danger")
            return render_template("patients/form.html", patient=None)

        if Patient.query.filter_by(ip_op_number=ip_op_number).first():
            flash("A patient with that IP/OP number is already registered.", "danger")
            return render_template("patients/form.html", patient=None)

        # Master patient index check: same name (case-insensitive) already
        # registered under a different number. Not blocked -- just a
        # warning registry must explicitly confirm past, to catch
        # accidental double-registration of the same person.
        if not confirm_duplicate:
            possible_matches = Patient.query.filter(Patient.name.ilike(name)).all()
            if possible_matches:
                return render_template(
                    "patients/form.html", patient=None,
                    possible_matches=possible_matches,
                    pending_form=request.form,
                )

        patient = Patient(
            name=name,
            ip_op_number=ip_op_number,
            gender=request.form.get("gender") or None,
            patient_type=patient_type,
            age=request.form.get("age") or None,
            weight=request.form.get("weight") or None,
            height=request.form.get("height") or None,
            contact=request.form.get("contact") or None,
            clinic_ward_unit=request.form.get("clinic_ward_unit") or None,
            drug_allergies=request.form.get("drug_allergies") or None,
        )
        db.session.add(patient)
        db.session.flush()

        # A new inpatient registration is itself an admission event.
        if patient_type == "Inpatient":
            db.session.add(PatientMovement(
                patient_id=patient.id,
                movement_type="Admission",
                ward_unit=patient.clinic_ward_unit,
                recorded_by_id=current_user.id,
            ))

        db.session.commit()
        flash(f"Patient {patient.name} registered.", "success")
        return redirect(url_for("patient_detail", patient_id=patient.id))

    return render_template("patients/form.html", patient=None)
@app.route("/patients/<int:patient_id>")
@login_required
def patient_detail(patient_id):
    patient = Patient.query.get_or_404(patient_id)
    prescriptions = (
        Prescription.query.filter_by(patient_id=patient.id)
        .order_by(Prescription.date.desc(), Prescription.id.desc())
        .all()
    )
    notes = (
        PatientNote.query.filter_by(patient_id=patient.id)
        .order_by(PatientNote.created_at.desc())
        .all()
    )
    movements = (
        PatientMovement.query.filter_by(patient_id=patient.id)
        .order_by(PatientMovement.created_at.desc())
        .all()
    )
    documents = (
        PatientDocument.query.filter_by(patient_id=patient.id)
        .order_by(PatientDocument.uploaded_at.desc())
        .all()
    )
    appointments = (
        Appointment.query.filter_by(patient_id=patient.id)
        .order_by(Appointment.scheduled_date.desc())
        .all()
    )
    return render_template(
        "patients/detail.html", patient=patient,
        prescriptions=prescriptions, notes=notes,
        movements=movements, documents=documents,
        appointments=appointments,
    )


@app.route("/patients/<int:patient_id>/notes", methods=["POST"])
@login_required
@role_required("admin", "doctor")
def patient_note_add(patient_id):
    patient = Patient.query.get_or_404(patient_id)
    text = request.form.get("note", "").strip()

    if not text:
        flash("Note text is required.", "danger")
        return redirect(url_for("patient_detail", patient_id=patient.id))

    note = PatientNote(patient_id=patient.id, author_id=current_user.id, note=text)
    db.session.add(note)
    db.session.commit()

    flash("Note added.", "success")
    return redirect(url_for("patient_detail", patient_id=patient.id))

# ---------------------------------------------------------------------------
# Patient movements (admission / transfer / discharge log — registry)
# ---------------------------------------------------------------------------

@app.route("/patients/<int:patient_id>/movements", methods=["POST"])
@login_required
@role_required("registry")
def patient_movement_add(patient_id):
    patient = Patient.query.get_or_404(patient_id)
    movement_type = request.form.get("movement_type", "")
    ward_unit = request.form.get("ward_unit", "").strip()
    notes = request.form.get("notes", "").strip()

    if movement_type not in PatientMovement.MOVEMENT_TYPES:
        flash("Invalid movement type.", "danger")
        return redirect(url_for("patient_detail", patient_id=patient.id))

    db.session.add(PatientMovement(
        patient_id=patient.id,
        movement_type=movement_type,
        ward_unit=ward_unit or None,
        notes=notes or None,
        recorded_by_id=current_user.id,
    ))

    # Keep patient_type / clinic_ward_unit in sync with the latest movement.
    if movement_type == "Admission":
        patient.patient_type = "Inpatient"
        if ward_unit:
            patient.clinic_ward_unit = ward_unit
    elif movement_type == "Transfer" and ward_unit:
        patient.clinic_ward_unit = ward_unit
    elif movement_type == "Discharge":
        patient.patient_type = "Outpatient"

    db.session.commit()
    flash(f"{movement_type} recorded for {patient.name}.", "success")
    return redirect(url_for("patient_detail", patient_id=patient.id))


# ---------------------------------------------------------------------------
# Patient file tracking (registry)
# ---------------------------------------------------------------------------

@app.route("/patients/<int:patient_id>/file", methods=["POST"])
@login_required
@role_required("registry")
def patient_file_update(patient_id):
    patient = Patient.query.get_or_404(patient_id)
    file_record = patient.file_record

    if not file_record:
        file_record = PatientFile(patient_id=patient.id)
        db.session.add(file_record)

    action = request.form.get("action")

    if action == "set_file_number":
        file_number = request.form.get("file_number", "").strip()
        if file_number and PatientFile.query.filter(
            PatientFile.file_number == file_number,
            PatientFile.patient_id != patient.id,
        ).first():
            flash("That file number is already assigned to another patient.", "danger")
            return redirect(url_for("patient_detail", patient_id=patient.id))
        file_record.file_number = file_number or None

    elif action == "toggle_scanned":
        file_record.digital_scanned = not file_record.digital_scanned

    elif action == "check_out":
        file_record.checked_out_to_id = current_user.id
        file_record.checked_out_at = datetime.utcnow()

    elif action == "check_in":
        file_record.checked_out_to_id = None
        file_record.checked_out_at = None

    db.session.commit()
    flash("File record updated.", "success")
    return redirect(url_for("patient_detail", patient_id=patient.id))

ALLOWED_DOCUMENT_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "doc", "docx"}


def _allowed_document_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_DOCUMENT_EXTENSIONS




@app.route("/patients/<int:patient_id>/documents", methods=["POST"])
@login_required
@role_required("registry")
def patient_document_upload(patient_id):
    patient = Patient.query.get_or_404(patient_id)
    file = request.files.get("document")
    description = request.form.get("description", "").strip()

    if not file or file.filename == "":
        flash("Please choose a file to upload.", "danger")
        return redirect(url_for("patient_detail", patient_id=patient.id))

    if not _allowed_document_file(file.filename):
        flash("Unsupported file type. Allowed: PDF, PNG, JPG, DOC, DOCX.", "danger")
        return redirect(url_for("patient_detail", patient_id=patient.id))

    original_filename = secure_filename(file.filename)
    ext = original_filename.rsplit(".", 1)[1].lower()
    stored_filename = f"{uuid.uuid4().hex}.{ext}"

    s3_client.upload_fileobj(file, Config.R2_BUCKET_NAME, stored_filename)

    doc = PatientDocument(
        patient_id=patient.id,
        uploaded_by_id=current_user.id,
        original_filename=original_filename,
        stored_filename=stored_filename,
        description=description or None,
    )
    db.session.add(doc)
    db.session.commit()

    flash("Document uploaded.", "success")
    return redirect(url_for("patient_detail", patient_id=patient.id))


@app.route("/patients/documents/<int:document_id>/download")
@login_required
def patient_document_download(document_id):
    doc = PatientDocument.query.get_or_404(document_id)
    url = s3_client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": Config.R2_BUCKET_NAME,
            "Key": doc.stored_filename,
            "ResponseContentDisposition": f'attachment; filename="{doc.original_filename}"',
        },
        ExpiresIn=300,  # link valid for 5 minutes
    )
    return redirect(url)

# ---------------------------------------------------------------------------
# Appointments (registry)
# ---------------------------------------------------------------------------

@app.route("/appointments")
@login_required
def appointment_list():
    status = request.args.get("status")
    query = Appointment.query
    if status:
        query = query.filter_by(status=status)
    appointments = query.order_by(Appointment.scheduled_date.asc()).all()
    return render_template("appointments/list.html", appointments=appointments, status=status)


@app.route("/patients/<int:patient_id>/appointments/new", methods=["POST"])
@login_required
@role_required("registry")
def appointment_new(patient_id):
    patient = Patient.query.get_or_404(patient_id)
    scheduled_date_str = request.form.get("scheduled_date", "")

    if not scheduled_date_str:
        flash("Appointment date is required.", "danger")
        return redirect(url_for("patient_detail", patient_id=patient.id))

    appointment = Appointment(
        patient_id=patient.id,
        clinic_ward_unit=request.form.get("clinic_ward_unit") or None,
        scheduled_date=datetime.strptime(scheduled_date_str, "%Y-%m-%d").date(),
        scheduled_time=request.form.get("scheduled_time") or None,
        reason=request.form.get("reason") or None,
        created_by_id=current_user.id,
    )
    db.session.add(appointment)
    db.session.commit()
    flash(f"Appointment scheduled for {patient.name} on {appointment.scheduled_date}.", "success")
    return redirect(url_for("patient_detail", patient_id=patient.id))


@app.route("/appointments/<int:appointment_id>/status", methods=["POST"])
@login_required
@role_required("registry")
def appointment_status_update(appointment_id):
    appointment = Appointment.query.get_or_404(appointment_id)
    new_status = request.form.get("status", "")

    if new_status not in Appointment.STATUSES:
        flash("Invalid status.", "danger")
        return redirect(url_for("appointment_list"))

    appointment.status = new_status
    db.session.commit()
    flash(f"Appointment marked {new_status}.", "success")
    return redirect(request.referrer or url_for("appointment_list"))

# ---------------------------------------------------------------------------
# Prescribers (typeahead used by the outpatient/inpatient dispense forms)
# ---------------------------------------------------------------------------

@app.route("/api/prescribers/search")
@login_required
def api_prescriber_search():
    """Typeahead search used by the dispense forms. Returns up to 10 matches
    for prescribers whose name contains the query string (case-insensitive).
    Frontend calls this as the user types 2+ characters into the prescriber
    field."""
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])

    prescribers = (
        Prescriber.query
        .filter(Prescriber.is_active == True)  # noqa: E712
        .filter(Prescriber.name.ilike(f"%{q}%"))
        .order_by(Prescriber.name)
        .limit(10)
        .all()
    )

    return jsonify([
        {
            "id": p.id,
            "name": p.name,
            "registration_number": p.registration_number or "",
            "designation": p.designation or "",
        }
        for p in prescribers
    ])


@app.route("/api/prescribers", methods=["POST"])
@login_required
@role_required("admin", "pharmacist")
def api_prescriber_create():
    """Quick-add used by the 'no match found, add new prescriber' option on
    the dispense forms. Returns the created record so the frontend can
    immediately select it without a second lookup."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()

    if not name:
        return jsonify({"error": "Prescriber name is required"}), 400

    existing = Prescriber.query.filter(Prescriber.name.ilike(name)).first()
    if existing:
        return jsonify({
            "id": existing.id,
            "name": existing.name,
            "registration_number": existing.registration_number or "",
            "designation": existing.designation or "",
        }), 200

    prescriber = Prescriber(
        name=name,
        registration_number=(data.get("registration_number") or "").strip() or None,
        designation=(data.get("designation") or "").strip() or None,
        phone=(data.get("phone") or "").strip() or None,
    )
    db.session.add(prescriber)
    db.session.commit()

    return jsonify({
        "id": prescriber.id,
        "name": prescriber.name,
        "registration_number": prescriber.registration_number or "",
        "designation": prescriber.designation or "",
    }), 201


@app.route("/prescribers")
@login_required
@role_required("admin", "pharmacist")
def prescriber_list():
    prescribers = Prescriber.query.order_by(Prescriber.name).all()
    return render_template("prescribers/list.html", prescribers=prescribers)


@app.route("/prescribers/new", methods=["GET", "POST"])
@login_required
@role_required("admin", "pharmacist")
def prescriber_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Prescriber name is required.", "danger")
            return render_template("prescribers/form.html", prescriber=None)

        prescriber = Prescriber(
            name=name,
            registration_number=request.form.get("registration_number") or None,
            designation=request.form.get("designation") or None,
            phone=request.form.get("phone") or None,
            is_active=request.form.get("is_active") == "on",
        )
        db.session.add(prescriber)
        db.session.commit()
        flash(f"Prescriber {prescriber.name} added.", "success")
        return redirect(url_for("prescriber_list"))

    return render_template("prescribers/form.html", prescriber=None)


@app.route("/prescribers/<int:prescriber_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("admin", "pharmacist")
def prescriber_edit(prescriber_id):
    prescriber = Prescriber.query.get_or_404(prescriber_id)

    if request.method == "POST":
        prescriber.name = request.form.get("name", "").strip()
        prescriber.registration_number = request.form.get("registration_number") or None
        prescriber.designation = request.form.get("designation") or None
        prescriber.phone = request.form.get("phone") or None
        prescriber.is_active = request.form.get("is_active") == "on"
        db.session.commit()
        flash(f"Prescriber {prescriber.name} updated.", "success")
        return redirect(url_for("prescriber_list"))

    return render_template("prescribers/form.html", prescriber=prescriber)


# ---------------------------------------------------------------------------
# Prescriptions — doctor writes, pharmacist dispenses
#
# This is the two-step workflow: a doctor writes a prescription against
# quantity_prescribed (status="Pending", no stock touched, written_by_id set
# to the doctor). A pharmacist later opens it from the pending queue and
# dispenses against it — quantities default to what was prescribed, stock
# deducts FEFO exactly as the walk-in dispense routes below do, and
# quantity_dispensed / status get updated. Nothing here is retyped by the
# pharmacist; they're just confirming/adjusting what the doctor already
# wrote and where quantities came from.
# ---------------------------------------------------------------------------
# -----------------------------------------------------------------------
# REPLACE the existing @app.route("/prescriptions/new", ...) function in
# app.py with this version. Everything else in app.py stays the same —
# this only changes how the patient is resolved (find-or-create by
# ip_op_number instead of requiring a pre-selected patient_id).
# -----------------------------------------------------------------------
@app.route("/prescriptions/new", methods=["GET", "POST"])
@login_required
@role_required("admin", "doctor")
def prescription_new():
    items = _prescribable_items()

    # The logged-in doctor's own Prescriber record — powers the Registration
    # Number / Designation display in the footer, and is created on first
    # use if it doesn't exist yet.
    doctor_prescriber = _get_or_create_own_prescriber(current_user)
    db.session.commit()

    # Support arriving here pre-filled from patient_new's redirect, or from
    # a "Write prescription" link on patient_detail.
    preselected_patient_id = request.args.get("patient_id", type=int)
    preselected_patient = (
        Patient.query.get(preselected_patient_id) if preselected_patient_id else None
    )

    if request.method == "POST":
        ip_op_number = request.form.get("ip_op_number", "").strip()
        name = request.form.get("patient_name", "").strip()

        if not ip_op_number or not name:
            flash("Patient name and IP/OP number are required.", "danger")
            return render_template("prescriptions/new.html", items=items,
                                    preselected_patient=None,
                                    doctor_prescriber=doctor_prescriber)

        gender = request.form.get("gender") or None
        patient_type = request.form.get("patient_type", "Outpatient")
        age = request.form.get("age") or None
        weight = request.form.get("weight") or None
        height = request.form.get("height") or None
        contact = request.form.get("contact") or None
        clinic_ward_unit = request.form.get("clinic_ward_unit") or None
        drug_allergies = request.form.get("drug_allergies") or None

        # Patients must already exist -- doctors don't register patients
        # (that's done by admin/store_officer via /patients/new). Refresh
        # visit-specific details (weight/age/clinic/allergies can change
        # visit to visit) on the existing record only.
        patient = Patient.query.filter_by(ip_op_number=ip_op_number).first()
        if not patient:
            flash(
                f"No patient found with IP/OP number {ip_op_number}. "
                "Please have them registered first.",
                "danger",
            )
            return render_template("prescriptions/new.html", items=items,
                                    preselected_patient=None,
                                    doctor_prescriber=doctor_prescriber)

        patient.name = name
        patient.gender = gender
        patient.patient_type = patient_type
        patient.age = age
        patient.weight = weight
        patient.height = height
        patient.contact = contact
        patient.clinic_ward_unit = clinic_ward_unit
        patient.drug_allergies = drug_allergies
        prescriber = doctor_prescriber

        prescription = Prescription(
            patient_id=patient.id,
            prescriber_id=prescriber.id,
            written_by_id=current_user.id,
            status="Pending",
            prescriber_name=prescriber.name,
            registration_number=prescriber.registration_number,
            designation=prescriber.designation,
        )
        db.session.add(prescription)
        db.session.flush()

        item_ids = request.form.getlist("item_id[]")
        doses = request.form.getlist("dose[]")
        routes = request.form.getlist("route[]")
        frequencies = request.form.getlist("frequency[]")
        durations = request.form.getlist("duration[]")
        quantities = request.form.getlist("quantity_prescribed[]")

        line_count = 0
        for item_id, dose, route, freq, dur, qty in zip(
            item_ids, doses, routes, frequencies, durations, quantities
        ):
            if not item_id or not qty:
                continue
            item = Item.query.get(item_id)
            db.session.add(PrescriptionLine(
                prescription_id=prescription.id, item_id=item.id,
                medicine_name=item.name, dose=dose, route=route,
                frequency=freq, duration=dur,
                quantity_prescribed=float(qty), quantity_dispensed=0,
                dispensed=False,
            ))
            line_count += 1

        if line_count == 0:
            db.session.rollback()
            flash("Add at least one medicine line before submitting.", "danger")
            return render_template("prescriptions/new.html", items=items,
                                    preselected_patient=patient,
                                    doctor_prescriber=doctor_prescriber)

        db.session.commit()
        flash(f"Prescription written for {patient.name} and sent to the pharmacy queue.", "success")
        return redirect(url_for("patient_detail", patient_id=patient.id))

    return render_template("prescriptions/new.html", items=items,
                            preselected_patient=preselected_patient,
                            doctor_prescriber=doctor_prescriber)
@app.route("/prescriptions/mine")
@login_required
@role_required("admin", "doctor")
def prescriptions_mine():
    """A doctor's own prescribing history."""
    prescriptions = (
        Prescription.query.filter_by(written_by_id=current_user.id)
        .order_by(Prescription.date.desc(), Prescription.id.desc())
        .all()
    )
    return render_template("prescriptions/mine.html", prescriptions=prescriptions)


@app.route("/prescriptions/pending")
@login_required
@role_required("admin", "pharmacist")
def prescriptions_pending():
    """The pharmacist's queue of doctor-written prescriptions waiting to be
    dispensed. Scoped to only the patient type(s) this pharmacist's
    assigned pharmacy point actually handles."""
    query = Prescription.query.filter(Prescription.status.in_(["Pending", "Partially Dispensed"]))

    if current_user.role == "pharmacist":
        scope = _pharmacist_scope_locations(current_user)
        allowed_types = []
        if "Outpatient Pharmacy" in scope:
            allowed_types.append("Outpatient")
        if "Inpatient Pharmacy" in scope:
            allowed_types.append("Inpatient")
        if len(allowed_types) == 1:
            query = query.join(Patient).filter(Patient.patient_type == allowed_types[0])

    prescriptions = query.order_by(Prescription.date.asc(), Prescription.id.asc()).all()
    return render_template("prescriptions/pending.html", prescriptions=prescriptions)

@app.route("/prescriptions/<int:prescription_id>")
@login_required
def prescription_detail(prescription_id):
    prescription = Prescription.query.get_or_404(prescription_id)
    return render_template("prescriptions/detail.html", prescription=prescription,
                            currency=Config.CURRENCY)


@app.route("/prescriptions/<int:prescription_id>/dispense", methods=["GET", "POST"])
@login_required
@role_required("admin", "pharmacist")
def prescription_dispense(prescription_id):
    """Pulls up exactly what the doctor wrote — item, dose, route,
    frequency, duration, quantity prescribed — so the pharmacist only
    confirms or adjusts the quantity actually being given out today,
    instead of retyping the prescription. Stock deducts FEFO from the
    correct pharmacy point based on the patient's type, same as the
    walk-in dispense routes."""
    prescription = Prescription.query.get_or_404(prescription_id)

    if prescription.status == "Dispensed":
        flash("This prescription has already been fully dispensed.", "info")
        return redirect(url_for("prescription_detail", prescription_id=prescription.id))

    location = (
        "Outpatient Pharmacy" if prescription.patient.patient_type == "Outpatient"
        else "Inpatient Pharmacy"
    )

    if current_user.role == "pharmacist" and location not in _pharmacist_scope_locations(current_user):
        flash(f"You're assigned to a different pharmacy point and can't dispense {location} prescriptions.", "danger")
        return redirect(url_for("prescriptions_pending"))

    if request.method == "POST":
        any_dispensed = False
        all_fully_dispensed = True

        for line in prescription.lines:
            remaining_prescribed = float(line.quantity_prescribed or 0) - float(line.quantity_dispensed or 0)
            requested = request.form.get(f"dispense_{line.id}", "")
            qty_to_dispense = float(requested) if requested else 0
            qty_to_dispense = max(0, min(qty_to_dispense, remaining_prescribed))

            if qty_to_dispense <= 0:
                if remaining_prescribed > 0:
                    all_fully_dispensed = False
                continue

            remaining = qty_to_dispense
            batches = (
                Batch.query.filter_by(item_id=line.item_id, location=location)
                .filter(Batch.quantity_remaining > 0)
                .order_by(Batch.expiry_date.asc())  # FEFO
                .all()
            )
            for batch in batches:
                if remaining <= 0:
                    break
                take = min(remaining, float(batch.quantity_remaining))
                batch.quantity_remaining = float(batch.quantity_remaining) - take
                remaining -= take
                log_movement(line.item, batch, "issue", take,
                             from_location=location, to_location="Patient",
                             reference=f"RX-{prescription.id}")

            actually_dispensed = qty_to_dispense - remaining
            line.quantity_dispensed = float(line.quantity_dispensed or 0) + actually_dispensed
            line.dispensed = float(line.quantity_dispensed) >= float(line.quantity_prescribed or 0)
            if actually_dispensed > 0:
                any_dispensed = True
            if not line.dispensed:
                all_fully_dispensed = False

        if not any_dispensed:
            flash("Enter at least one quantity to dispense.", "danger")
            return render_template("prescriptions/dispense.html", prescription=prescription,
                                    location=location)

        prescription.status = "Dispensed" if all_fully_dispensed else "Partially Dispensed"
        db.session.commit()
        flash(f"Dispensed against prescription for {prescription.patient.name}.", "success")
        return redirect(url_for("prescription_detail", prescription_id=prescription.id))

    return render_template("prescriptions/dispense.html", prescription=prescription, location=location)


# ---------------------------------------------------------------------------
# Outpatient Pharmacy
# ---------------------------------------------------------------------------

@app.route("/outpatient")
@login_required
def outpatient_list():
    prescriptions = (
        Prescription.query.join(Patient)
        .filter(Patient.patient_type == "Outpatient")
        .order_by(Prescription.date.desc())
        .all()
    )
    return render_template("outpatient/list.html", prescriptions=prescriptions)


@app.route("/outpatient/dispense", methods=["GET", "POST"])
@login_required
@role_required("admin", "pharmacist")
def outpatient_dispense():
    if current_user.role == "pharmacist" and "Outpatient Pharmacy" not in _pharmacist_scope_locations(current_user):
        flash("You're not assigned to Outpatient Pharmacy.", "danger")
        return redirect(url_for("dashboard"))
    items = Item.query.order_by(Item.name).all()

    if request.method == "POST":
        ip_op_number = request.form.get("ip_op_number", "").strip()
        patient = Patient.query.filter_by(ip_op_number=ip_op_number).first()

        if not patient:
            flash("Patient is not registered in the hospital system. Dispensing blocked.", "danger")
            return render_template("outpatient/dispense.html", items=items)

        # If a doctor has already written a prescription for this patient
        # that's waiting to be dispensed, send the pharmacist there instead
        # of letting them re-enter everything from scratch here.
        pending = (
            Prescription.query.filter(
                Prescription.patient_id == patient.id,
                Prescription.status.in_(["Pending", "Partially Dispensed"]),
            )
            .order_by(Prescription.date.desc())
            .first()
        )
        if pending and not request.form.get("ignore_pending"):
            flash(
                f"{patient.name} already has a prescription from Dr. "
                f"{pending.prescriber_name} waiting to be dispensed. "
                f"Use the pharmacy queue instead, or submit again to add this as a separate walk-in entry.",
                "warning",
            )
            return render_template("outpatient/dispense.html", items=items,
                                    pending_prescription=pending)

        prescriber_id = request.form.get("prescriber_id") or None
        prescriber = Prescriber.query.get(prescriber_id) if prescriber_id else None

        if prescriber:
            prescriber_name = prescriber.name
            registration_number = prescriber.registration_number
            designation = prescriber.designation
        else:
            # Fallback: no prescriber_id came through (e.g. JS disabled) —
            # keep accepting the typed field so the form still works.
            prescriber_name = request.form.get("prescriber_name", "").strip()
            registration_number = request.form.get("registration_number")
            designation = request.form.get("designation")

        if not prescriber_name:
            flash("Prescriber name is required.", "danger")
            return render_template("outpatient/dispense.html", items=items)

        prescription = Prescription(
            patient_id=patient.id,
            prescriber_id=prescriber.id if prescriber else None,
            prescriber_name=prescriber_name,
            registration_number=registration_number,
            designation=designation,
            # Written and dispensed in the same step (walk-in / phoned-in
            # prescription, no doctor login involved) — mark it dispensed
            # immediately rather than leaving it in the pending queue.
            status="Dispensed",
        )
        db.session.add(prescription)
        db.session.flush()

        item_ids = request.form.getlist("item_id[]")
        doses = request.form.getlist("dose[]")
        routes = request.form.getlist("route[]")
        frequencies = request.form.getlist("frequency[]")
        durations = request.form.getlist("duration[]")
        quantities = request.form.getlist("quantity[]")

        for item_id, dose, route, freq, dur, qty in zip(
            item_ids, doses, routes, frequencies, durations, quantities
        ):
            if not item_id or not qty:
                continue
            item = Item.query.get(item_id)
            qty = float(qty)

            line = PrescriptionLine(
                prescription_id=prescription.id, item_id=item.id,
                medicine_name=item.name, dose=dose, route=route,
                frequency=freq, duration=dur,
                quantity_prescribed=qty, quantity_dispensed=qty,
            )
            db.session.add(line)

            remaining = qty
            batches = (
                Batch.query.filter_by(item_id=item.id, location="Outpatient Pharmacy")
                .filter(Batch.quantity_remaining > 0)
                .order_by(Batch.expiry_date.asc())
                .all()
            )
            for batch in batches:
                if remaining <= 0:
                    break
                take = min(remaining, float(batch.quantity_remaining))
                batch.quantity_remaining = float(batch.quantity_remaining) - take
                remaining -= take
                log_movement(item, batch, "issue", take,
                             from_location="Outpatient Pharmacy", to_location="Patient",
                             reference=f"RX-{prescription.id}")

            line.dispensed = (remaining <= 0)

        db.session.commit()
        flash(f"Prescription recorded and dispensed for {patient.name}.", "success")
        return redirect(url_for("outpatient_list"))

    return render_template("outpatient/dispense.html", items=items)


# ---------------------------------------------------------------------------
# Inpatient Pharmacy & Discharge/Refund
# ---------------------------------------------------------------------------

@app.route("/inpatient")
@login_required
def inpatient_list():
    prescriptions = (
        Prescription.query.join(Patient)
        .filter(Patient.patient_type == "Inpatient")
        .order_by(Prescription.date.desc())
        .all()
    )
    return render_template("inpatient/list.html", prescriptions=prescriptions)


@app.route("/inpatient/dispense", methods=["GET", "POST"])
@login_required
@role_required("admin", "pharmacist")
def inpatient_dispense():
    if current_user.role == "pharmacist" and "Inpatient Pharmacy" not in _pharmacist_scope_locations(current_user):
        flash("You're not assigned to Inpatient Pharmacy.", "danger")
        return redirect(url_for("dashboard"))
    items = Item.query.order_by(Item.name).all()

    if request.method == "POST":
        ip_op_number = request.form.get("ip_op_number", "").strip()
        patient = Patient.query.filter_by(ip_op_number=ip_op_number).first()

        if not patient:
            flash("Patient is not registered in the hospital system. Dispensing blocked.", "danger")
            return render_template("inpatient/dispense.html", items=items)

        pending = (
            Prescription.query.filter(
                Prescription.patient_id == patient.id,
                Prescription.status.in_(["Pending", "Partially Dispensed"]),
            )
            .order_by(Prescription.date.desc())
            .first()
        )
        if pending and not request.form.get("ignore_pending"):
            flash(
                f"{patient.name} already has a prescription from Dr. "
                f"{pending.prescriber_name} waiting to be dispensed. "
                f"Use the pharmacy queue instead, or submit again to add this as a separate walk-in entry.",
                "warning",
            )
            return render_template("inpatient/dispense.html", items=items,
                                    pending_prescription=pending)

        prescriber_id = request.form.get("prescriber_id") or None
        prescriber = Prescriber.query.get(prescriber_id) if prescriber_id else None

        if prescriber:
            prescriber_name = prescriber.name
            registration_number = prescriber.registration_number
            designation = prescriber.designation
        else:
            prescriber_name = request.form.get("prescriber_name", "").strip()
            registration_number = request.form.get("registration_number")
            designation = request.form.get("designation")

        if not prescriber_name:
            flash("Prescriber name is required.", "danger")
            return render_template("inpatient/dispense.html", items=items)

        prescription = Prescription(
            patient_id=patient.id,
            prescriber_id=prescriber.id if prescriber else None,
            prescriber_name=prescriber_name,
            registration_number=registration_number,
            designation=designation,
            status="Dispensed",
        )
        db.session.add(prescription)
        db.session.flush()

        item_ids = request.form.getlist("item_id[]")
        quantities = request.form.getlist("quantity[]")

        for item_id, qty in zip(item_ids, quantities):
            if not item_id or not qty:
                continue
            item = Item.query.get(item_id)
            qty = float(qty)

            db.session.add(PrescriptionLine(
                prescription_id=prescription.id, item_id=item.id,
                medicine_name=item.name,
                quantity_prescribed=qty, quantity_dispensed=qty, dispensed=True,
            ))

            remaining = qty
            batches = (
                Batch.query.filter_by(item_id=item.id, location="Inpatient Pharmacy")
                .filter(Batch.quantity_remaining > 0)
                .order_by(Batch.expiry_date.asc())
                .all()
            )
            for batch in batches:
                if remaining <= 0:
                    break
                take = min(remaining, float(batch.quantity_remaining))
                batch.quantity_remaining = float(batch.quantity_remaining) - take
                remaining -= take
                log_movement(item, batch, "issue", take,
                             from_location="Inpatient Pharmacy", to_location="Patient",
                             reference=f"RX-{prescription.id}")

        db.session.commit()
        flash(f"Inpatient drugs dispensed for {patient.name}.", "success")
        return redirect(url_for("inpatient_list"))

    return render_template("inpatient/dispense.html", items=items)


@app.route("/discharge/<int:patient_id>", methods=["GET", "POST"])
@login_required
@role_required("admin", "pharmacist")
def discharge(patient_id):
    if current_user.role == "pharmacist" and "Inpatient Pharmacy" not in _pharmacist_scope_locations(current_user):
        flash("You're not assigned to Inpatient Pharmacy.", "danger")
        return redirect(url_for("dashboard"))
    """
    Checks the patient's last prescription against what was issued today.
    - Fully depleted  -> continuation prescription for take-home medicine.
    - Not depleted    -> auto-calculate refund (undepleted qty x unit cost).
    """
    patient = Patient.query.get_or_404(patient_id)

    last_prescription = (
        Prescription.query.filter_by(patient_id=patient.id)
        .order_by(Prescription.date.desc(), Prescription.id.desc())
        .first()
    )

    if not last_prescription:
        flash("No prescription found for this patient.", "warning")
        return redirect(url_for("inpatient_list"))

    today = date.today()
    issued_today_lines = [
        line for line in last_prescription.lines
        if last_prescription.date == today
    ]

    if request.method == "POST":
        results = []
        for line in issued_today_lines:
            quantity_issued_today = float(line.quantity_dispensed or 0)
            quantity_undepleted = float(
                request.form.get(f"undepleted_{line.id}", 0) or 0
            )

            if quantity_undepleted <= 0:
                outcome = "Continuation Prescription Issued"
                refund_amount = 0
            else:
                outcome = "Refund Processed"
                unit_cost = float(line.item.unit_cost) if line.item else 0
                refund_amount = quantity_undepleted * unit_cost

                # reverse the undepleted stock back into Inpatient Pharmacy
                batch = (
                    Batch.query.filter_by(item_id=line.item_id, location="Inpatient Pharmacy")
                    .order_by(Batch.expiry_date.asc())
                    .first()
                )
                if batch:
                    batch.quantity_remaining = float(batch.quantity_remaining) + quantity_undepleted
                    log_movement(line.item, batch, "refund_reversal", quantity_undepleted,
                                 from_location="Patient", to_location="Inpatient Pharmacy",
                                 reference=f"DISCHARGE-{patient.id}")

            refund = DischargeRefund(
                patient_id=patient.id,
                prescription_id=last_prescription.id,
                item_id=line.item_id,
                quantity_issued_today=quantity_issued_today,
                quantity_undepleted=quantity_undepleted,
                unit_cost=float(line.item.unit_cost) if line.item else 0,
                refund_amount=refund_amount,
                outcome=outcome,
            )
            db.session.add(refund)
            results.append(refund)

        db.session.commit()
        flash(f"Discharge processed for {patient.name}.", "success")
        return render_template("discharge/result.html", patient=patient, results=results,
                                currency=Config.CURRENCY)

    return render_template("discharge/review.html", patient=patient,
                            prescription=last_prescription, lines=issued_today_lines,
                            currency=Config.CURRENCY)


# ---------------------------------------------------------------------------
# Monitoring
# ---------------------------------------------------------------------------

@app.route("/monitoring")
@login_required
def monitoring_list():
    period = request.args.get("period")
    query = MonitoringRecord.query
    if period:
        query = query.filter_by(period=period)
    records = query.all()
    return render_template("monitoring/list.html", records=records, period=period)


# ---------------------------------------------------------------------------
# Reports (CSV export)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Reports (CSV export) — index page shows only the reports relevant to the
# logged-in user's role. admin always sees every report; other roles see
# a curated subset based on what they actually do day to day.
# ---------------------------------------------------------------------------

REPORTS = [
    {
        "key": "inventory_valuation",
        "title": "Inventory Valuation",
        "description": "Every item's SKU, name, quantity on hand, unit cost, and total stock value.",
        "icon": "📦",
        "icon_class": "report-icon-tan",
        "endpoint": "report_inventory_valuation",
        "roles": ["admin", "store_officer"],
    },
    {
        "key": "stock_movements",
        "title": "Stock Movement History",
        "description": "Full audit trail of receipts, transfers, and issues across every item and batch.",
        "icon": "🔄",
        "icon_class": "report-icon-blue",
        "endpoint": "report_stock_movements",
        "roles": ["admin", "store_officer", "pharmacist"],
    },
    {
        "key": "purchase_orders",
        "title": "Purchase Order Log",
        "description": "Every PO with supplier, status, order date, and total value.",
        "icon": "🛒",
        "icon_class": "report-icon-purple",
        "endpoint": "report_purchase_orders",
        "roles": ["admin", "store_officer"],
    },
    {
        "key": "requisitions",
        "title": "Requisition Log",
        "description": "Every S11 requisition with department, issue point, status, and date.",
        "icon": "🧾",
        "icon_class": "report-icon-teal",
        "endpoint": "report_requisitions",
        "roles": ["admin", "store_officer", "pharmacist"],
    },
    {
        "key": "expiry",
        "title": "Expiry Report",
        "description": "Every batch still in stock, with its expiry date and quantity remaining.",
        "icon": "⏰",
        "icon_class": "report-icon-red",
        "endpoint": "report_expiry",
        "roles": ["admin", "store_officer", "pharmacist"],
    },
    {
        "key": "discharge_refunds",
        "title": "Discharge Refund Report",
        "description": "Every discharge reconciliation: quantity issued, undepleted, refund amount, and outcome.",
        "icon": "💰",
        "icon_class": "report-icon-amber",
        "endpoint": "report_discharge_refunds",
        "roles": ["admin", "pharmacist"],
    },
    {
        "key": "analytics",
        "title": "Visual Analytics",
        "description": "Interactive charts: stock value trend, procurement vs. consumption, expiry timeline, stock by location, and top-moving items.",
        "icon": "📊",
        "icon_class": "report-icon-green",
        "endpoint": "reports_analytics",
        "roles": ["admin"],
        "highlight": True,
        "cta": "Open Analytics",
    },
    {
        "key": "doctor_activity",
        "title": "My Patient Activity",
        "description": "Patients seen and prescriptions written over time, plus your completion rate — how many prescriptions were fully dispensed at the pharmacy.",
        "icon": "🩺",
        "icon_class": "report-icon-teal",
        "endpoint": "reports_doctor_analytics",
        "roles": ["admin", "doctor"],
        "highlight": True,
        "cta": "View My Activity",
    },
]


@app.route("/reports")
@login_required
def reports_index():
    role = current_user.role
    visible_reports = [
        r for r in REPORTS if role == "admin" or role in r["roles"]
    ]
    return render_template("reports/index.html", reports=visible_reports)


@app.route("/reports/inventory-valuation.csv")
@login_required
def report_inventory_valuation():
    scope_locations = _current_scope_locations()
    rows = []
    for i in Item.query.all():
        qty = _scoped_quantity(i, scope_locations)
        rows.append((i.sku, i.name, qty, i.unit_cost, float(qty) * float(i.unit_cost or 0)))
    return _csv_response(
        "inventory_valuation.csv",
        ["SKU", "Name", "Qty on Hand", "Unit Cost (KES)", "Total Value (KES)"],
        rows,
    )

@app.route("/reports/stock-movements.csv")
@login_required
def report_stock_movements():
    scope_locations = _current_scope_locations()
    query = StockMovement.query
    if scope_locations:
        query = query.filter(
            (StockMovement.from_location.in_(scope_locations))
            | (StockMovement.to_location.in_(scope_locations))
        )
    rows = [
        (m.created_at.strftime("%Y-%m-%d %H:%M"), m.item.name, m.movement_type,
         m.quantity, m.from_location, m.to_location, m.reference)
        for m in query.order_by(StockMovement.created_at).all()
    ]
    return _csv_response(
        "stock_movements.csv",
        ["Date", "Item", "Type", "Quantity", "From", "To", "Reference"],
        rows,
    )


@app.route("/reports/purchase-orders.csv")
@login_required
def report_purchase_orders():
    rows = [
        (po.po_number, po.supplier.name, po.status, po.order_date, po.total_value)
        for po in PurchaseOrder.query.all()
    ]
    return _csv_response(
        "purchase_orders.csv",
        ["PO Number", "Supplier", "Status", "Order Date", "Total Value (KES)"],
        rows,
    )


@app.route("/reports/requisitions.csv")
@login_required
def report_requisitions():
    rows = [
        (r.req_number, r.department.name, r.issue_point, r.status,
         r.created_at.strftime("%Y-%m-%d"))
        for r in Requisition.query.all()
    ]
    return _csv_response(
        "requisitions.csv",
        ["Req Number", "Department", "Issue Point", "Status", "Date"],
        rows,
    )


@app.route("/reports/expiry.csv")
@login_required
def report_expiry():
    scope_locations = _current_scope_locations()
    query = Batch.query.filter(Batch.quantity_remaining > 0)
    if scope_locations:
        query = query.filter(Batch.location.in_(scope_locations))
    rows = [
        (b.item.name, b.batch_number, b.location, b.expiry_date, b.quantity_remaining)
        for b in query.all()
    ]
    return _csv_response(
        "expiry_report.csv",
        ["Item", "Batch Number", "Location", "Expiry Date", "Qty Remaining"],
        rows,
    )

@app.route("/reports/discharge-refunds.csv")
@login_required
def report_discharge_refunds():
    rows = [
        (r.patient.name, r.patient.ip_op_number, r.item.name,
         r.quantity_issued_today, r.quantity_undepleted, r.refund_amount, r.outcome)
        for r in DischargeRefund.query.all()
    ]
    return _csv_response(
        "discharge_refunds.csv",
        ["Patient", "IP/OP Number", "Item", "Qty Issued Today",
         "Qty Undepleted", "Refund Amount (KES)", "Outcome"],
        rows,
    )


# ---------------------------------------------------------------------------
# Reports — Analytics (charts backing templates/reports/analytics.html)
#
# These sit alongside the CSV exports above: same auth, same models, just
# aggregated into chart-friendly JSON instead of a downloadable file.
#
# Note on stock_value_trend: StockMovement doesn't store a signed value, so
# the running total is reconstructed from movement_type: receipts and
# refund_reversals add value back, issues remove it, transfers and
# adjustments are treated as value-neutral (a transfer only relocates stock;
# an adjustment's direction isn't captured in the current schema). If you
# later add a signed quantity_delta to StockMovement, swap that in here
# instead of the MOVEMENT_VALUE_SIGN lookup.
# ---------------------------------------------------------------------------


MOVEMENT_VALUE_SIGN = {
    "receipt": 1,
    "refund_reversal": 1,
    "return": 1,
    "issue": -1,
    "transfer": 0,
    "adjustment": 0,
}

@app.route("/reports/analytics")
@login_required
def reports_analytics():
    return render_template("reports/analytics.html")


@app.route("/reports/api/stock-value-trend")
@login_required
def api_stock_value_trend():
    """Monthly running stock value for the last `months` months (default 12).

    Query params:
        months    int, default 12
        location  str, optional — filter movements by to_location
    """
    months = request.args.get("months", 12, type=int)
    location = request.args.get("location", "").strip()

    start_date = date.today().replace(day=1) - relativedelta(months=months)

    query = StockMovement.query.filter(StockMovement.created_at >= start_date)
    if location:
        query = query.filter(StockMovement.to_location == location)

    movements = query.order_by(StockMovement.created_at.asc()).all()

    monthly_delta = {}
    for m in movements:
        sign = MOVEMENT_VALUE_SIGN.get(m.movement_type, 0)
        if sign == 0:
            continue
        month_key = m.created_at.strftime("%Y-%m")
        delta = sign * float(m.quantity) * float(m.item.unit_cost)
        monthly_delta[month_key] = monthly_delta.get(month_key, 0) + delta

    labels, values = [], []
    running_total = 0.0
    for month_key in sorted(monthly_delta.keys()):
        running_total += monthly_delta[month_key]
        labels.append(datetime.strptime(month_key, "%Y-%m").strftime("%b %Y"))
        values.append(round(running_total, 2))

    return jsonify({"labels": labels, "values": values})


@app.route("/reports/api/procurement-vs-consumption")
@login_required
def api_procurement_vs_consumption():
    """Planned vs. actual consumption per item for a given monitoring period.

    Query params:
        period  str, e.g. "2026-07" — defaults to the current month
        limit   int, default 10 — top N items by |variance|
    """
    period = request.args.get("period", date.today().strftime("%Y-%m"))
    limit = request.args.get("limit", 10, type=int)

    rows = (
        db.session.query(
            Item.name,
            MonitoringRecord.planned_quantity,
            MonitoringRecord.actual_issued,
        )
        .join(Item, Item.id == MonitoringRecord.item_id)
        .filter(MonitoringRecord.period == period)
        .order_by(func.abs(MonitoringRecord.planned_quantity - MonitoringRecord.actual_issued).desc())
        .limit(limit)
        .all()
    )

    return jsonify({
        "labels": [r.name for r in rows],
        "planned": [float(r.planned_quantity or 0) for r in rows],
        "actual": [float(r.actual_issued or 0) for r in rows],
    })


@app.route("/reports/api/expiry-timeline")
@login_required
def api_expiry_timeline():
    """Quantity remaining grouped by expiry month, split into 'within the
    6-month alert window' vs. 'later', for the next `months_ahead` months.
    Optional ?locations=A,B,C scopes to specific locations."""
    months_ahead = request.args.get("months_ahead", 12, type=int)
    locations_param = request.args.get("locations", "").strip()
    locations = (
        [loc.strip() for loc in locations_param.split(",") if loc.strip()]
        if locations_param else None
    )

    today = date.today()
    horizon = today + relativedelta(months=months_ahead)
    alert_cutoff = today + relativedelta(months=Config.EXPIRY_ALERT_MONTHS)

    query = (
        Batch.query.filter(Batch.quantity_remaining > 0)
        .filter(Batch.expiry_date <= horizon)
    )
    if locations:
        query = query.filter(Batch.location.in_(locations))

    batches = query.order_by(Batch.expiry_date.asc()).all()

    buckets = {}
    for b in batches:
        month_key = b.expiry_date.strftime("%Y-%m")
        bucket = buckets.setdefault(month_key, {"within_alert": 0.0, "later": 0.0})
        if b.expiry_date <= alert_cutoff:
            bucket["within_alert"] += float(b.quantity_remaining or 0)
        else:
            bucket["later"] += float(b.quantity_remaining or 0)

    ordered_keys = sorted(buckets.keys())
    return jsonify({
        "labels": [datetime.strptime(k, "%Y-%m").strftime("%b %Y") for k in ordered_keys],
        "within_alert": [buckets[k]["within_alert"] for k in ordered_keys],
        "later": [buckets[k]["later"] for k in ordered_keys],
    })


@app.route("/reports/api/fill-rate")
@login_required
@role_required("admin", "pharmacist")
def api_fill_rate():
    """Prescription fill rate: what share of prescribed quantity was
    actually dispensed, i.e. sum(quantity_dispensed) / sum(quantity_prescribed)
    across PrescriptionLine records. A shortfall usually means the pharmacy
    ran out of stock or the patient only picked up part of the order.

    Returns a monthly trend for the last `months` months (default 6) plus
    an overall rate across that whole window for the KPI number."""
    months = request.args.get("months", 6, type=int)
    start_date = date.today().replace(day=1) - relativedelta(months=months - 1)

    lines = (
        db.session.query(
            PrescriptionLine.quantity_prescribed,
            PrescriptionLine.quantity_dispensed,
            Prescription.date,
        )
        .join(Prescription, Prescription.id == PrescriptionLine.prescription_id)
        .filter(Prescription.date >= start_date)
        .all()
    )

    monthly_prescribed = {}
    monthly_dispensed = {}
    for prescribed, dispensed, rx_date in lines:
        month_key = rx_date.strftime("%Y-%m")
        monthly_prescribed[month_key] = monthly_prescribed.get(month_key, 0) + float(prescribed or 0)
        monthly_dispensed[month_key] = monthly_dispensed.get(month_key, 0) + float(dispensed or 0)

    ordered_keys = sorted(monthly_prescribed.keys())
    labels = [datetime.strptime(k, "%Y-%m").strftime("%b %Y") for k in ordered_keys]

    rates = []
    for k in ordered_keys:
        prescribed_total = monthly_prescribed[k]
        dispensed_total = monthly_dispensed.get(k, 0)
        rate = round((dispensed_total / prescribed_total) * 100, 1) if prescribed_total else 0.0
        rates.append(rate)

    total_prescribed = sum(monthly_prescribed.values())
    total_dispensed = sum(monthly_dispensed.values())
    overall_rate = round((total_dispensed / total_prescribed) * 100, 1) if total_prescribed else 0.0

    return jsonify({
        "labels": labels,
        "values": rates,
        "overall_rate": overall_rate,
    })


@app.route("/reports/api/stock-by-location")
@login_required
def api_stock_by_location():
    """Current stock value split across Drug Store / Holding / Pharmacy points.
    Optional ?locations=Drug Store,Holding scopes the breakdown to specific
    locations — used by the store_officer / pharmacist dashboard charts so
    each role only sees the locations relevant to their work."""
    locations_param = request.args.get("locations", "").strip()
    locations = (
        [loc.strip() for loc in locations_param.split(",") if loc.strip()]
        if locations_param else None
    )

    query = (
        db.session.query(
            Batch.location,
            func.sum(Batch.quantity_remaining * Item.unit_cost).label("value"),
        )
        .join(Item, Item.id == Batch.item_id)
        .filter(Batch.quantity_remaining > 0)
    )
    if locations:
        query = query.filter(Batch.location.in_(locations))

    rows = query.group_by(Batch.location).all()

    return jsonify({
        "labels": [r.location for r in rows],
        "values": [round(float(r.value or 0), 2) for r in rows],
    })


@app.route("/reports/api/top-moving-items")
@login_required
def api_top_moving_items():
    """Items with the highest issued quantity (from Issued requisitions) over
    a date range.

    Query params:
        start  ISO date, default 30 days ago
        end    ISO date, default today
        limit  int, default 10
    """
    start = request.args.get("start")
    end = request.args.get("end")
    limit = request.args.get("limit", 10, type=int)

    start_date = (
        datetime.strptime(start, "%Y-%m-%d").date() if start
        else date.today() - timedelta(days=30)
    )
    end_date = (
        datetime.strptime(end, "%Y-%m-%d").date() if end
        else date.today()
    )

    rows = (
        db.session.query(
            Item.name,
            func.sum(RequisitionLine.quantity_issued).label("total_issued"),
        )
        .join(Item, Item.id == RequisitionLine.item_id)
        .join(Requisition, Requisition.id == RequisitionLine.requisition_id)
        .filter(Requisition.status == "Issued")
        .filter(func.date(Requisition.created_at) >= start_date)
        .filter(func.date(Requisition.created_at) <= end_date)
        .group_by(Item.name)
        .order_by(func.sum(RequisitionLine.quantity_issued).desc())
        .limit(limit)
        .all()
    )

    return jsonify({
        "labels": [r.name for r in rows],
        "values": [float(r.total_issued or 0) for r in rows],
    })
# ---------------------------------------------------------------------------
# Reports — Doctor Analytics
#
# Doctor-facing equivalent of the admin analytics dashboard above, but scoped
# to the logged-in doctor's own prescribing activity instead of hospital-wide
# stock data. Sits alongside reports_analytics() / api_stock_value_trend() etc.
#
# "Attended" = a prescription that reached status "Dispensed" (fully given
# out). "Work percentage" = Dispensed / total prescriptions written, i.e.
# how much of what this doctor prescribes actually gets completed at the
# pharmacy rather than sitting Pending/Partially Dispensed or Rejected.
#
# admin can view any doctor's activity via ?doctor_id=<id>; a doctor viewing
# their own page always sees their own data regardless of query params.
# ---------------------------------------------------------------------------

def _resolve_doctor_id():
    """Doctors always see their own data. Admins may pass ?doctor_id=<id>
    to inspect a specific doctor; if omitted, admin sees all doctors combined
    (doctor_id=None)."""
    if current_user.role == "doctor":
        return current_user.id
    doctor_id = request.args.get("doctor_id", type=int)
    return doctor_id  # None means "all doctors" for admin


@app.route("/reports/doctor-analytics")
@login_required
@role_required("admin", "doctor")
def reports_doctor_analytics():
    doctors = User.query.filter_by(role="doctor").order_by(User.name).all() if current_user.role == "admin" else None
    selected_doctor_id = _resolve_doctor_id()
    return render_template(
        "reports/doctor_analytics.html",
        doctors=doctors,
        selected_doctor_id=selected_doctor_id,
    )


@app.route("/reports/api/doctor-patients-seen")
@login_required
@role_required("admin", "doctor")
def api_doctor_patients_seen():
    """Distinct patients seen per month, for the last `months` months."""
    months = request.args.get("months", 6, type=int)
    doctor_id = _resolve_doctor_id()

    start_date = date.today().replace(day=1) - relativedelta(months=months - 1)

    query = Prescription.query.filter(Prescription.date >= start_date)
    if doctor_id:
        query = query.filter(Prescription.written_by_id == doctor_id)
    else:
        query = query.filter(Prescription.written_by_id.isnot(None))

    prescriptions = query.all()

    buckets = {}
    for p in prescriptions:
        month_key = p.date.strftime("%Y-%m")
        buckets.setdefault(month_key, set()).add(p.patient_id)

    ordered_keys = sorted(buckets.keys())
    return jsonify({
        "labels": [datetime.strptime(k, "%Y-%m").strftime("%b %Y") for k in ordered_keys],
        "values": [len(buckets[k]) for k in ordered_keys],
    })


@app.route("/reports/api/doctor-prescriptions-written")
@login_required
@role_required("admin", "doctor")
def api_doctor_prescriptions_written():
    """Prescriptions written per month, for the last `months` months."""
    months = request.args.get("months", 6, type=int)
    doctor_id = _resolve_doctor_id()

    start_date = date.today().replace(day=1) - relativedelta(months=months - 1)

    query = Prescription.query.filter(Prescription.date >= start_date)
    if doctor_id:
        query = query.filter(Prescription.written_by_id == doctor_id)
    else:
        query = query.filter(Prescription.written_by_id.isnot(None))

    prescriptions = query.all()

    buckets = {}
    for p in prescriptions:
        month_key = p.date.strftime("%Y-%m")
        buckets[month_key] = buckets.get(month_key, 0) + 1

    ordered_keys = sorted(buckets.keys())
    return jsonify({
        "labels": [datetime.strptime(k, "%Y-%m").strftime("%b %Y") for k in ordered_keys],
        "values": [buckets[k] for k in ordered_keys],
    })


@app.route("/reports/api/doctor-status-breakdown")
@login_required
@role_required("admin", "doctor")
def api_doctor_status_breakdown():
    """Prescription status breakdown (Pending / Partially Dispensed /
    Dispensed / Rejected) for the doughnut/bar chart."""
    doctor_id = _resolve_doctor_id()

    query = db.session.query(Prescription.status, func.count(Prescription.id))
    if doctor_id:
        query = query.filter(Prescription.written_by_id == doctor_id)
    else:
        query = query.filter(Prescription.written_by_id.isnot(None))

    rows = query.group_by(Prescription.status).all()

    return jsonify({
        "labels": [status for status, _ in rows],
        "values": [count for _, count in rows],
    })


@app.route("/reports/api/doctor-completion-rate")
@login_required
@role_required("admin", "doctor")
def api_doctor_completion_rate():
    """Single 'work percentage' figure: what share of this doctor's
    prescriptions reached Dispensed (fully attended to) vs everything else
    (Pending / Partially Dispensed / Rejected)."""
    doctor_id = _resolve_doctor_id()

    query = Prescription.query
    if doctor_id:
        query = query.filter(Prescription.written_by_id == doctor_id)
    else:
        query = query.filter(Prescription.written_by_id.isnot(None))

    total = query.count()
    dispensed = query.filter(Prescription.status == "Dispensed").count()

    completion_rate = round((dispensed / total) * 100, 1) if total else 0.0

    return jsonify({
        "total_prescriptions": total,
        "dispensed": dispensed,
        "completion_rate": completion_rate,
    })

if __name__ == "__main__":
    app.run(debug=True)