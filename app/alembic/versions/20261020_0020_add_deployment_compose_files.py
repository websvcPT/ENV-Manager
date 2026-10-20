"""Add the Compose files a deployment was imported from

Importing a deployment's services and files from its Compose files stores the chosen files, as
paths relative to the root with the base file first, so a later scan can start from the same
choice. The column is a nullable JSON list: a deployment that was not imported keeps NULL.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-20 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0020'
down_revision: str | None = '0019'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable column in place; no existing deployment has Compose files recorded."""
    op.add_column('deployments', sa.Column('compose_files', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Drop the column in place; the recorded Compose files are lost."""
    op.drop_column('deployments', 'compose_files')
