"""Initial TrafficFlow schema.

This revision is deliberately explicit so production startup can use Alembic
instead of relying on SQLAlchemy ``create_all`` to mutate an existing schema.
"""

import sqlalchemy as sa

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )


def _has_column(table_name: str, column_name: str) -> bool:
    """Check a legacy database before applying compatibility changes."""
    return any(
        column["name"] == column_name
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    )


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=128), nullable=True),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="viewer"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        _created_at(),
        sa.Column("last_login", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("username", name="uq_users_username"),
        if_not_exists=True,
    )
    op.create_index("ix_users_username", "users", ["username"], unique=False, if_not_exists=True)

    # Older local databases were created with ``Base.metadata.create_all``
    # before Alembic became the startup migration source of truth. Preserve
    # those users and add the column introduced by the versioned schema.
    if not _has_column("users", "token_version"):
        op.add_column(
            "users",
            sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
        )

    op.create_table(
        "vehicle_count_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("camera_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("lane_id", sa.String(length=64), nullable=False),
        sa.Column("track_id", sa.Integer(), nullable=False),
        sa.Column("vehicle_type", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("frame_id", sa.Integer(), nullable=False),
        _created_at(),
        sa.Column("crop_path", sa.String(length=256), nullable=True),
        if_not_exists=True,
    )
    op.create_index(
        "ix_vehicle_count_events_camera_id", "vehicle_count_events", ["camera_id"], if_not_exists=True
    )
    op.create_index(
        "idx_event_camera_created", "vehicle_count_events", ["camera_id", "created_at"], if_not_exists=True
    )

    op.create_table(
        "traffic_aggregates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("camera_id", sa.String(length=64), nullable=False),
        sa.Column("lane_id", sa.String(length=64), nullable=False),
        sa.Column("vehicle_type", sa.String(length=32), nullable=False),
        sa.Column("window", sa.String(length=16), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "camera_id", "lane_id", "vehicle_type", "window", "window_start",
            name="uq_aggregate_key",
        ),
        if_not_exists=True,
    )

    op.create_table(
        "lane_change_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("camera_id", sa.String(length=64), nullable=False),
        sa.Column("track_id", sa.Integer(), nullable=False),
        sa.Column("class_name", sa.String(length=32), nullable=False),
        sa.Column("previous_lane_id", sa.String(length=64), nullable=True),
        sa.Column("current_lane_id", sa.String(length=64), nullable=False),
        sa.Column("frame_id", sa.Integer(), nullable=False),
        _created_at(),
        if_not_exists=True,
    )
    op.create_index("ix_lane_change_events_camera_id", "lane_change_events", ["camera_id"], if_not_exists=True)
    op.create_index(
        "idx_lanechange_camera_created", "lane_change_events", ["camera_id", "created_at"], if_not_exists=True
    )

    op.create_table(
        "runtime_metrics",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("camera_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("fps", sa.Float(), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        _created_at(),
        if_not_exists=True,
    )
    op.create_index("ix_runtime_metrics_camera_id", "runtime_metrics", ["camera_id"], if_not_exists=True)
    op.create_index(
        "idx_runtime_camera_created", "runtime_metrics", ["camera_id", "created_at"], if_not_exists=True
    )

    op.create_table(
        "inference_jobs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("camera_id", sa.String(length=64), nullable=False),
        sa.Column("model_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_frames", sa.Integer(), nullable=True),
        sa.Column("fps", sa.Float(), nullable=True),
        sa.Column("ingested_events", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("output_dir", sa.String(length=512), nullable=False),
        sa.Column("source", sa.String(length=1024), nullable=False),
        _created_at(),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        if_not_exists=True,
    )
    op.create_index("ix_inference_jobs_camera_id", "inference_jobs", ["camera_id"], if_not_exists=True)
    op.create_index("ix_inference_jobs_status", "inference_jobs", ["status"], if_not_exists=True)
    op.create_index("idx_job_camera_created", "inference_jobs", ["camera_id", "created_at"], if_not_exists=True)

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("resource", sa.String(length=256), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        if_not_exists=True,
    )
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"], if_not_exists=True)
    op.create_index("idx_audit_created", "audit_logs", ["created_at"], if_not_exists=True)
    op.create_index("idx_audit_user", "audit_logs", ["user_id"], if_not_exists=True)


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("inference_jobs")
    op.drop_table("runtime_metrics")
    op.drop_table("lane_change_events")
    op.drop_table("traffic_aggregates")
    op.drop_table("vehicle_count_events")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
