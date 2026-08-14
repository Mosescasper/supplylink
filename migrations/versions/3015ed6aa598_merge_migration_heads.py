"""merge migration heads

Revision ID: 3015ed6aa598
Revises: 1a6e96057ac8, baded13e50e0
Create Date: 2026-08-14 11:36:43.747985

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3015ed6aa598'
down_revision = ('1a6e96057ac8', 'baded13e50e0')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
