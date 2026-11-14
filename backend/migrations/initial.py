import hashlib
import json
import os
from sqlalchemy import inspect, text, select
from src.config.database import Base, engine, Session
from src import models


def _add_columns(conn):
    """Lightweight in-place upgrade for databases created before period tracking."""
    existing = {c['name'] for c in inspect(conn).get_columns('reports')}
    additions = {
        'anchor_at': 'DOUBLE PRECISION DEFAULT 0 NOT NULL',
        'next_period': 'INTEGER DEFAULT 0 NOT NULL',
        'paused_at': 'DOUBLE PRECISION',
    }
    for name, ddl in additions.items():
        if name not in existing:
            conn.execute(text(f'ALTER TABLE reports ADD COLUMN {name} {ddl}'))


def _backfill_legacy_periods():
    """Expose pre-period deliveries as frozen, verified issues.

    Legacy rows keep their original file and stored CSV fingerprint. A missing
    or unreadable JSON is surfaced as a damaged archive instead of silently
    dropping history; originals are left untouched.
    """
    from src.config.settings import ARTIFACT_DIR
    from src.models import Delivery, Report, ReportPeriod
    with Session() as db:
        legacy = list(db.scalars(select(Delivery)))
        if not legacy:
            return
        for d in legacy:
            ref = f'delivery:{d.id}'
            if db.scalar(select(ReportPeriod.id).where(ReportPeriod.legacy_ref == ref)):
                continue
            if not db.get(Report, d.report_id):
                continue
            used = {p.period_index for p in db.scalars(
                select(ReportPeriod).where(ReportPeriod.report_id == d.report_id))}
            index = 0
            while index in used:
                index += 1
            json_path = os.path.join(ARTIFACT_DIR, d.filename + '.json')
            integrity = None
            json_sum = None
            if not os.path.exists(json_path):
                integrity = '旧版归档缺少配对 JSON，原件保留'
            else:
                with open(json_path, 'rb') as handle:
                    json_sum = hashlib.sha256(handle.read()).hexdigest()
                try:
                    with open(json_path, encoding='utf-8') as handle:
                        json.load(handle)
                except (ValueError, UnicodeDecodeError):
                    integrity = '旧版 JSON 归档无法解析，原件保留'
            db.add(ReportPeriod(
                report_id=d.report_id, period_index=index, due_at=d.created_at,
                destination=d.destination, status='corrupted' if integrity else 'completed',
                checksum=d.checksum, json_checksum=json_sum, filename=d.filename,
                json_filename=d.filename + '.json', created_at=d.created_at,
                completed_at=d.created_at, integrity_error=integrity,
                legacy_ref=ref, attempt_count=1,
            ))
        for r in db.scalars(select(Report)):
            if not r.anchor_at:
                r.anchor_at = r.next_run
            if r.next_period is None:
                r.next_period = 0
        db.commit()


def migrate():
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        if 'reports' in inspect(conn).get_table_names():
            _add_columns(conn)
    _backfill_legacy_periods()


if __name__ == '__main__':
    migrate()
