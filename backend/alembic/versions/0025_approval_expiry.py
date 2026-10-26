"""Let an expired approval end its run instead of leaving it pending forever.

Approvals expire after 15 minutes and can no longer be applied, but nothing
moved the bound run out of APPROVAL_PENDING. The worker now completes such runs
with ``APPROVAL_EXPIRED`` (nothing is deployed), mirroring a rejection.
"""

from alembic import op


revision = "0025_approval_expiry"
down_revision = "0024_shared_snapshot_obs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE run_completion_reason ADD VALUE IF NOT EXISTS 'APPROVAL_EXPIRED'")


def downgrade() -> None:
    # PostgreSQL cannot drop an enum value; leaving it is harmless.
    pass
