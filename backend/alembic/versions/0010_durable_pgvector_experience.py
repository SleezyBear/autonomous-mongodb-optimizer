"""Persist complete action-family-scoped pgvector experience evidence.

Revision ID: 0010_durable_pgvector_experience
Revises: 0009_durable_crash_recovery
Create Date: 2026-08-27
"""

from __future__ import annotations

from alembic import op


revision = "0010_durable_pgvector_experience"
down_revision = "0009_durable_crash_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE experience_records ADD COLUMN adapter_type varchar(64) NOT NULL DEFAULT 'mongodb'")
    op.execute("ALTER TABLE experience_records ADD COLUMN action_type varchar(64) NOT NULL DEFAULT 'CREATE_INDEX'")
    op.execute("ALTER TABLE experience_records ADD COLUMN query_structure_summary jsonb NOT NULL DEFAULT '{}'::jsonb")
    op.execute("ALTER TABLE experience_records ADD COLUMN workload_features jsonb NOT NULL DEFAULT '{}'::jsonb")
    op.execute("ALTER TABLE experience_records ADD COLUMN bottleneck varchar(128)")
    op.execute("ALTER TABLE experience_records ADD COLUMN candidate_summary jsonb NOT NULL DEFAULT '{}'::jsonb")
    op.execute("ALTER TABLE experience_records ADD COLUMN prediction jsonb NOT NULL DEFAULT '{}'::jsonb")
    op.execute("ALTER TABLE experience_records ADD COLUMN admission_outcome varchar(64)")
    op.execute("ALTER TABLE experience_records ADD COLUMN actual_postdeploy_outcome varchar(64)")
    op.execute("ALTER TABLE experience_records ADD COLUMN rollback_outcome varchar(64)")
    op.execute("ALTER TABLE experience_records ADD COLUMN embedding_model varchar(128) NOT NULL DEFAULT 'embeddinggemma'")
    op.execute("ALTER TABLE experience_records ADD COLUMN embedding_model_version varchar(128) NOT NULL DEFAULT 'unknown'")
    op.execute("CREATE INDEX ix_experience_action_type ON experience_records (action_type)")
    op.execute("CREATE INDEX ix_experience_embedding_cosine ON experience_records USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_experience_embedding_cosine")
    op.execute("DROP INDEX IF EXISTS ix_experience_action_type")
    for column in (
        "embedding_model_version", "embedding_model", "rollback_outcome", "actual_postdeploy_outcome",
        "admission_outcome", "prediction", "candidate_summary", "bottleneck", "workload_features",
        "query_structure_summary", "action_type", "adapter_type",
    ):
        op.execute(f"ALTER TABLE experience_records DROP COLUMN IF EXISTS {column}")
