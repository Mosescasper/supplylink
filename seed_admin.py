"""
seed_admin.py
Creates a first admin user so you can log in and call the protected
/api/... routes right away (every route except /api/register and
/api/login requires flask_login's @login_required).

Usage:
    python seed_admin.py you@example.com yourpassword
"""

import sys

from main import create_app
from database import db, User


def seed_admin(email, password, name="Admin"):
    app = create_app()
    with app.app_context():
        user = User.query.filter_by(email=email).first()
        if user:
            print(f"User {email} already exists (id={user.id}, role={user.role})")
            return

        user = User(name=name, email=email, role="admin")
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        print(f"Created admin user: {email} (id={user.id})")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python seed_admin.py <email> <password>")
        sys.exit(1)
    seed_admin(sys.argv[1], sys.argv[2])