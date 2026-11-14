from sqlalchemy import String, Double, Integer, ForeignKey, Boolean, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class Report(Base):
    __tablename__ = 'reports'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    job_id: Mapped[int] = mapped_column(ForeignKey('jobs.id'))
    destination: Mapped[str] = mapped_column(String(120))
    interval_seconds: Mapped[int]
    # next_run is the original due time of the next period. It is only ever
    # advanced by an exact number of intervals, so restarts cannot drift it.
    next_run: Mapped[float] = mapped_column(Double)
    # First original due time, kept for schedule provenance / pause alignment.
    anchor_at: Mapped[float] = mapped_column(Double, default=0.0, server_default='0')
    # Monotonic sequence of the next period to generate.
    next_period: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    paused_at: Mapped[float | None] = mapped_column(Double, nullable=True)


class ReportPeriod(Base):
    """One scheduled issue (期次) and its immutable delivered archive.

    A period is keyed by (report_id, period_index) so concurrent workers or
    retries can never archive the same issue twice.
    """

    __tablename__ = 'report_periods'
    __table_args__ = (UniqueConstraint('report_id', 'period_index', name='uq_report_period'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey('reports.id'))
    period_index: Mapped[int] = mapped_column(Integer)
    # Original scheduled due time of this issue, not the worker execution time.
    due_at: Mapped[float] = mapped_column(Double)
    destination: Mapped[str] = mapped_column(String(120), default='')
    status: Mapped[str] = mapped_column(String(20), default='pending')
    snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    snapshot_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    json_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    filename: Mapped[str] = mapped_column(String(160), default='')
    json_filename: Mapped[str] = mapped_column(String(160), default='')
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    error: Mapped[str] = mapped_column(String(1000), default='')
    integrity_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    claim_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    claimed_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    created_at: Mapped[float] = mapped_column(Double, default=0.0)
    completed_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    # Best-effort link to the pre-periods deliveries table for migrated rows.
    legacy_ref: Mapped[str | None] = mapped_column(String(80), nullable=True, unique=True)


class Delivery(Base):
    """Legacy archive rows from the pre-period schema (read-only, backfilled)."""

    __tablename__ = 'deliveries'
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey('reports.id'))
    created_at: Mapped[float] = mapped_column(Double)
    checksum: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(160))
    destination: Mapped[str] = mapped_column(String(120))
