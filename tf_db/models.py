from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class VehicleCountEvent(Base):
    __tablename__ = "vehicle_count_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    camera_id = Column(String(64), nullable=False, index=True)
    job_id = Column(String(64), nullable=False)
    lane_id = Column(String(64), nullable=False)
    track_id = Column(Integer, nullable=False)
    vehicle_type = Column(String(32), nullable=False)
    direction = Column(String(16), nullable=True)
    confidence = Column(Float, nullable=True)
    frame_id = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    crop_path = Column(String(256), nullable=True)

    __table_args__ = (
        Index("idx_event_camera_created", "camera_id", "created_at"),
    )


class TrafficAggregate(Base):
    __tablename__ = "traffic_aggregates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    camera_id = Column(String(64), nullable=False)
    lane_id = Column(String(64), nullable=False)
    vehicle_type = Column(String(32), nullable=False)
    window = Column(String(16), nullable=False)
    window_start = Column(DateTime(timezone=True), nullable=False)
    count = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "camera_id", "lane_id", "vehicle_type", "window", "window_start",
            name="uq_aggregate_key",
        ),
    )


class LaneChangeEvent(Base):
    """True lane-change event — vehicle changed its stable lane."""

    __tablename__ = "lane_change_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    camera_id = Column(String(64), nullable=False, index=True)
    track_id = Column(Integer, nullable=False)
    class_name = Column(String(32), nullable=False)
    previous_lane_id = Column(String(64), nullable=True)
    current_lane_id = Column(String(64), nullable=False)
    frame_id = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("idx_lanechange_camera_created", "camera_id", "created_at"),
    )


class RuntimeMetric(Base):
    __tablename__ = "runtime_metrics"

    id = Column(Integer, primary_key=True, autoincrement=True)
    camera_id = Column(String(64), nullable=False, index=True)
    job_id = Column(String(64), nullable=False)
    fps = Column(Float, nullable=True)
    latency_ms = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class User(Base):
    """Application user with role-based access control.

    Roles: admin, operator, viewer.
    Passwords stored as bcrypt hashes via passlib.
    """

    __tablename__ = "users"

    id = Column(String(36), primary_key=True)  # UUID
    username = Column(String(64), unique=True, nullable=False, index=True)
    email = Column(String(128), default="")
    password_hash = Column(String(256), nullable=False)  # bcrypt hash
    role = Column(String(16), nullable=False, default="viewer")  # admin | operator | viewer
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    last_login = Column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    """Immutable audit trail for security-sensitive operations."""

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    username = Column(String(64), nullable=False)
    action = Column(String(64), nullable=False, index=True)  # login, user_create, user_delete, lane_update, etc.
    resource = Column(String(256), default="")
    detail = Column(Text, default="")
    ip_address = Column(String(45), default="")  # IPv6-ready
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("idx_audit_created", "created_at"),
        Index("idx_audit_user", "user_id"),
    )
