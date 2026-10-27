"""Add the Compose project name of a deployment

The name Docker Compose gives the project of a deployment (the ``com.docker.compose.project``
label of its containers). It is stored as typed and never derived from the display name. The
column is nullable text: a deployment that has none set keeps NULL.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-27 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0023'
down_revision: str | None = '0022'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable column in place; no existing deployment has a project name recorded."""
    op.add_column('deployments', sa.Column('compose_project', sa.String(length=63), nullable=True))


def downgrade() -> None:
    """Drop the column in place; the recorded project names are lost."""
    op.drop_column('deployments', 'compose_project')
