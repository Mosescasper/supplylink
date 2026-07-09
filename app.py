from dateutil.relativedelta import relativedelta
import csv
import io
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (
    Flask, render_template, redirect, url_for, request, flash,
    Response, abort, jsonify
)
from flask_login import (
    login_user, logout_user, login_required, current_user
)
from flask_migrate import Migrate
from sqlalchemy import func

from config import Config
from extensions import db, login_manager
from models import (
    Department, User, Category, Supplier, Item, Batch, StockMovement,
    PurchaseOrder, PurchaseOrderLine, ProcurementPlan, MonitoringRecord,
    Requisition, RequisitionLine, Patient, Prescription, PrescriptionLine,
    DischargeRefund, LOCATIONS
)

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)
login_manager.init_app(app)
migrate = Migrate(app, db)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


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

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        role = request.form.get("role", "")
        department_id = request.form.get("department_id") or None

        if not name or not email or not password:
            flash("Name, email, and password are required.", "danger")
            return render_template("register.html", departments=departments, roles=roles)

        if password != confirm_password:
            flash("Passwords do not match.", "danger")
            return render_template("register.html", departments=departments, roles=roles)

        if role not in User.ROLES:
            flash("Please select a valid role (Admin, Store Officer, or Pharmacist).", "danger")
            return render_template("register.html", departments=departments, roles=roles)

        if User.query.filter_by(email=email).first():
            flash("An account with that email already exists.", "danger")
            return render_template("register.html", departments=departments, roles=roles)

        user = User(name=name, email=email, role=role, department_id=department_id)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        flash("Account created. Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("register.html", departments=departments, roles=roles)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user)
            flash(f"Welcome back, {user.name}.", "success")
            next_url = request.args.get("next")
            return redirect(next_url or url_for("dashboard"))

        flash("Invalid email or password.", "danger")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

ROLE_LOCATION_SCOPE = {
    "store_officer": ["Drug Store", "Holding"],
    "pharmacist": ["Holding", "Outpatient Pharmacy", "Inpatient Pharmacy"],
    # admin falls through to None (unscoped) — the system now only has
    # these three roles: admin, store_officer, pharmacist.
}


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


@app.route("/")
@app.route("/dashboard")
@login_required
def dashboard():
    role = current_user.role
    scope_locations = ROLE_LOCATION_SCOPE.get(role)  # None = unscoped

    items = Item.query.all()

    near_expiry_batches = _scoped_near_expiry_batches(scope_locations)
    low_stock_items = _scoped_low_stock_items(items, scope_locations)
    total_stock_value = _scoped_stock_value(items, scope_locations)

    kpis = {
        "total_items": len(items),
        "near_expiry_count": len(near_expiry_batches),
        "low_stock_count": len(low_stock_items),
        "open_purchase_orders": PurchaseOrder.query.filter(
            PurchaseOrder.status.in_(["Draft", "Sent"])
        ).count(),
        "pending_requisitions": Requisition.query.filter_by(status="Pending").count(),
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
    )

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

    elif role == "store_officer":
        context["drug_store_value"] = _scoped_stock_value(items, ["Drug Store"])
        context["holding_value"] = _scoped_stock_value(items, ["Holding"])
        context["incoming_pos"] = (
            PurchaseOrder.query.filter(PurchaseOrder.status.in_(["Draft", "Sent"]))
            .order_by(PurchaseOrder.order_date.desc())
            .limit(6)
            .all()
        )
        context["pending_requisitions_to_action"] = (
            Requisition.query.filter(
                Requisition.status == "Pending",
                Requisition.issue_point.in_(["Drug Store", "Holding"]),
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

    elif role == "pharmacist":
        context["holding_value"] = _scoped_stock_value(items, ["Holding"])
        context["outpatient_value"] = _scoped_stock_value(items, ["Outpatient Pharmacy"])
        context["inpatient_value"] = _scoped_stock_value(items, ["Inpatient Pharmacy"])
        context["my_requisitions"] = (
            Requisition.query.filter_by(requested_by_id=current_user.id)
            .order_by(Requisition.created_at.desc())
            .limit(6)
            .all()
        )
        context["recent_discharge_refunds"] = (
            DischargeRefund.query.order_by(DischargeRefund.created_at.desc()).limit(6).all()
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
    return render_template("inventory/list.html", items=items, categories=categories,
                            q=q, currency=Config.CURRENCY)


@app.route("/inventory/new", methods=["GET", "POST"])
@login_required
@role_required("admin", "store_officer")
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
        db.session.commit()
        flash(f"Item {item.name} added.", "success")
        return redirect(url_for("inventory_list"))

    return render_template("inventory/form.html", categories=categories, suppliers=suppliers, item=None)


@app.route("/inventory/<int:item_id>")
@login_required
def inventory_detail(item_id):
    item = Item.query.get_or_404(item_id)
    return render_template("inventory/detail.html", item=item, currency=Config.CURRENCY,
                            expiry_months=Config.EXPIRY_ALERT_MONTHS)


@app.route("/inventory/<int:item_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("admin", "store_officer")
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

    query = Batch.query.join(Item)
    if q:
        query = query.filter(Item.name.ilike(f"%{q}%") | Batch.batch_number.ilike(f"%{q}%"))
    if location:
        query = query.filter(Batch.location == location)
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

    query = Batch.query.join(Item)
    if q:
        query = query.filter(Item.name.ilike(f"%{q}%") | Batch.batch_number.ilike(f"%{q}%"))
    if location:
        query = query.filter(Batch.location == location)
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
@role_required("admin", "store_officer")
def batch_receive():
    """Manual/ad-hoc receipt of stock directly into the Drug Store (outside a PO)."""
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
            location="Drug Store",
        )
        db.session.add(batch)
        db.session.flush()

        log_movement(item, batch, "receipt", quantity, to_location="Drug Store",
                     reference=request.form.get("reference", "Manual receipt"))
        db.session.commit()
        flash(f"Received {quantity} units of {item.name} into Drug Store.", "success")
        return redirect(url_for("inventory_detail", item_id=item.id))

    return render_template("inventory/receive.html", items=items)


@app.route("/batches/<int:batch_id>/transfer", methods=["GET", "POST"])
@login_required
@role_required("admin", "store_officer", "pharmacist")
def batch_transfer(batch_id):
    batch = Batch.query.get_or_404(batch_id)

    if request.method == "POST":
        quantity = float(request.form.get("quantity", 0) or 0)
        to_location = request.form["to_location"]

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
        flash(f"Transferred {quantity} units from {from_location} to {to_location}.", "success")
        return redirect(url_for("inventory_detail", item_id=batch.item_id))

    return render_template("inventory/transfer.html", batch=batch, locations=LOCATIONS)


@app.route("/expiry")
@login_required
def expiry_report():
    batches = [
        b for b in Batch.query.order_by(Batch.expiry_date).all()
        if (b.quantity_remaining or 0) > 0
    ]
    near_expiry = [b for b in batches if b.is_near_expiry(Config.EXPIRY_ALERT_MONTHS)]
    return render_template("inventory/expiry.html", batches=batches, near_expiry=near_expiry,
                            months=Config.EXPIRY_ALERT_MONTHS)


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

    query = Batch.query.join(Item).filter(Batch.quantity_remaining > 0)
    if q:
        query = query.filter(Item.name.ilike(f"%{q}%") | Batch.batch_number.ilike(f"%{q}%"))
    if location:
        query = query.filter(Batch.location == location)

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
    )


@app.route("/expiry-alerts/export.csv")
@login_required
def expiry_alerts_export():
    """Respects the same q / location / urgency filters as the /expiry-alerts dashboard."""
    q = request.args.get("q", "").strip()
    location = request.args.get("location", "").strip()
    urgency = request.args.get("urgency", "").strip()

    today = date.today()

    query = Batch.query.join(Item).filter(Batch.quantity_remaining > 0)
    if q:
        query = query.filter(Item.name.ilike(f"%{q}%") | Batch.batch_number.ilike(f"%{q}%"))
    if location:
        query = query.filter(Batch.location == location)

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
@role_required("admin", "store_officer")
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


@app.route("/suppliers/<int:supplier_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("admin", "store_officer")
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
@role_required("admin", "store_officer")
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
@role_required("admin", "store_officer")
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
@role_required("admin", "store_officer")
def po_send(po_id):
    po = PurchaseOrder.query.get_or_404(po_id)
    if po.status == "Draft":
        po.status = "Sent"
        db.session.commit()
        flash(f"{po.po_number} marked as Sent.", "success")
    return redirect(url_for("po_detail", po_id=po.id))


@app.route("/purchase-orders/<int:po_id>/receive", methods=["GET", "POST"])
@login_required
@role_required("admin", "store_officer")
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
                location="Drug Store",
            )
            db.session.add(batch)
            db.session.flush()

            log_movement(line.item, batch, "receipt", qty_received,
                         to_location="Drug Store", reference=po.po_number)

            line.quantity_received = (line.quantity_received or 0) + qty_received

        po.status = "Received"
        db.session.commit()
        flash(f"{po.po_number} received into Drug Store.", "success")
        return redirect(url_for("po_detail", po_id=po.id))

    return render_template("purchase_orders/receive.html", po=po)


@app.route("/purchase-orders/<int:po_id>/cancel", methods=["POST"])
@login_required
@role_required("admin", "store_officer")
def po_cancel(po_id):
    po = PurchaseOrder.query.get_or_404(po_id)
    po.status = "Cancelled"
    db.session.commit()
    flash(f"{po.po_number} cancelled.", "info")
    return redirect(url_for("po_list"))


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
        req = Requisition(
            req_number=next_reference("S11", Requisition, "req_number"),
            department_id=request.form["department_id"],
            issue_point=request.form["issue_point"],
            requested_by_id=current_user.id,
            status="Pending",
        )
        db.session.add(req)
        db.session.flush()

        item_ids = request.form.getlist("item_id[]")
        quantities = request.form.getlist("quantity_required[]")
        remarks = request.form.getlist("remarks[]")

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
    return render_template("requisitions/detail.html", req=req, currency=Config.CURRENCY)


@app.route("/requisitions/<int:req_id>/approve", methods=["POST"])
@login_required
@role_required("admin", "store_officer")
def requisition_approve(req_id):
    req = Requisition.query.get_or_404(req_id)
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
@role_required("admin", "store_officer")
def requisition_reject(req_id):
    req = Requisition.query.get_or_404(req_id)
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
@role_required("admin", "store_officer")
def requisition_issue(req_id):
    """Approved -> Issued. Deducts stock FEFO from the issue point and logs movements."""
    req = Requisition.query.get_or_404(req_id)
    if req.status != "Approved":
        flash("Only approved requisitions can be issued.", "warning")
        return redirect(url_for("requisition_detail", req_id=req.id))

    dest_location = "Outpatient Pharmacy" if req.issue_point == "Holding" else req.issue_point

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
                         to_location=req.department.name,
                         reference=req.req_number)

        line.quantity_issued = float(line.quantity_required) - remaining_to_issue

    req.status = "Issued"
    req.issued_by_id = current_user.id
    db.session.commit()
    flash(f"{req.req_number} issued and stock deducted.", "success")
    return redirect(url_for("requisition_detail", req_id=req.id))


@app.route("/requisitions/<int:req_id>/receive", methods=["POST"])
@login_required
def requisition_receive(req_id):
    """Requesting department confirms receipt (closes the S11 approval chain)."""
    req = Requisition.query.get_or_404(req_id)
    req.received_by_id = current_user.id
    db.session.commit()
    flash(f"Receipt of {req.req_number} confirmed.", "success")
    return redirect(url_for("requisition_detail", req_id=req.id))


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
    items = Item.query.order_by(Item.name).all()

    if request.method == "POST":
        ip_op_number = request.form.get("ip_op_number", "").strip()
        patient = Patient.query.filter_by(ip_op_number=ip_op_number).first()

        if not patient:
            flash("Patient is not registered in the hospital system. Dispensing blocked.", "danger")
            return render_template("outpatient/dispense.html", items=items)

        prescription = Prescription(
            patient_id=patient.id,
            prescriber_name=request.form["prescriber_name"],
            registration_number=request.form.get("registration_number"),
            designation=request.form.get("designation"),
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
                frequency=freq, duration=dur, quantity_dispensed=qty,
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
    items = Item.query.order_by(Item.name).all()

    if request.method == "POST":
        ip_op_number = request.form.get("ip_op_number", "").strip()
        patient = Patient.query.filter_by(ip_op_number=ip_op_number).first()

        if not patient:
            flash("Patient is not registered in the hospital system. Dispensing blocked.", "danger")
            return render_template("inpatient/dispense.html", items=items)

        prescription = Prescription(
            patient_id=patient.id,
            prescriber_name=request.form["prescriber_name"],
            registration_number=request.form.get("registration_number"),
            designation=request.form.get("designation"),
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
                medicine_name=item.name, quantity_dispensed=qty, dispensed=True,
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

@app.route("/reports")
@login_required
def reports_index():
    return render_template("reports/index.html")


@app.route("/reports/inventory-valuation.csv")
@login_required
def report_inventory_valuation():
    rows = [
        (i.sku, i.name, i.quantity_on_hand, i.unit_cost,
         i.quantity_on_hand * i.unit_cost)
        for i in Item.query.all()
    ]
    return _csv_response(
        "inventory_valuation.csv",
        ["SKU", "Name", "Qty on Hand", "Unit Cost (KES)", "Total Value (KES)"],
        rows,
    )


@app.route("/reports/stock-movements.csv")
@login_required
def report_stock_movements():
    rows = [
        (m.created_at.strftime("%Y-%m-%d %H:%M"), m.item.name, m.movement_type,
         m.quantity, m.from_location, m.to_location, m.reference)
        for m in StockMovement.query.order_by(StockMovement.created_at).all()
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
    rows = [
        (b.item.name, b.batch_number, b.location, b.expiry_date, b.quantity_remaining)
        for b in Batch.query.filter(Batch.quantity_remaining > 0).all()
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
    """
    months_ahead = request.args.get("months_ahead", 12, type=int)
    today = date.today()
    horizon = today + relativedelta(months=months_ahead)
    alert_cutoff = today + relativedelta(months=Config.EXPIRY_ALERT_MONTHS)

    batches = (
        Batch.query.filter(Batch.quantity_remaining > 0)
        .filter(Batch.expiry_date <= horizon)
        .order_by(Batch.expiry_date.asc())
        .all()
    )

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


@app.route("/reports/api/stock-by-location")
@login_required
def api_stock_by_location():
    """Current stock value split across Drug Store / Holding / Pharmacy points."""
    rows = (
        db.session.query(
            Batch.location,
            func.sum(Batch.quantity_remaining * Item.unit_cost).label("value"),
        )
        .join(Item, Item.id == Batch.item_id)
        .filter(Batch.quantity_remaining > 0)
        .group_by(Batch.location)
        .all()
    )

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


if __name__ == "__main__":
    app.run(debug=True)