"""Allow consecutive runs to freeze overlapping telemetry observations.

Migration 0013 made ``workload_snapshot_metrics.source_metric_observation_id``
globally unique, so a second run on the same target within the 15-minute
lookback failed with an integrity error while materializing its snapshot.
Each snapshot still freezes an observation at most once.
"""

from alembic import op


revision = "0024_shared_snapshot_obs"
down_revision = "0023_approval_continuation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE workload_snapshot_metrics DROP CONSTRAINT IF EXISTS workload_snapshot_metrics_source_metric_observation_id_key")
    op.execute(
        "ALTER TABLE workload_snapshot_metrics ADD CONSTRAINT uq_snapshot_metric_observation "
        "UNIQUE (workload_snapshot_id, source_metric_observation_id)"
    )


def downgrade() -> None:
    raise RuntimeError(
        "Restoring global observation uniqueness would reject existing overlapping "
        "snapshots; use an approved maintenance plan."
    )
