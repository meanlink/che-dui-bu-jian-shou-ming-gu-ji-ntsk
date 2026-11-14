import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from redis import Redis
from sqlalchemy import select, or_, and_

from src.config.database import Session
from src.config.settings import REDIS_URL, PERIOD_CLAIM_TTL
from src.models import Job, Dataset, Report, ReportPeriod
from src.services import reports
from src.services.business import analyze
from src.services.audit import record

logging.basicConfig(level=logging.INFO)
DELIVERY_WORKERS = 4


def execute(job_id):
    with Session() as db:
        j = db.get(Job, job_id)
        if not j or j.status != 'queued':
            return
        j.status = 'running'
        db.commit()
        try:
            d = db.get(Dataset, j.dataset_id)
            result = analyze(d.records, j.params)
            j.result = result
            j.status = 'completed'
            j.finished_at = time.time()
            record(db, 'worker', 'job.completed', {'id': j.id})
        except Exception as e:
            logging.exception('Analysis failed')
            j.status = 'failed'
            j.error = str(e)[:990]
            j.finished_at = time.time()
            record(db, 'worker', 'job.failed', {'id': j.id, 'error': j.error})
        db.commit()


def _deliver_one(period_id):
    with Session() as db:
        try:
            reports.deliver_period(db, period_id)
        except Exception:
            logging.exception('Period %s delivery crashed', period_id)


def materialise():
    """Create rows for every period whose original due time has passed."""
    now = time.time()
    created = 0
    with Session() as db:
        for report in db.scalars(select(Report).where(Report.enabled == True)):  # noqa: E712
            created += reports.ensure_periods(db, report, now)
            db.commit()  # persist the cursor per report before the next one
    return created


def deliverable_ids(limit=12):
    now = time.time()
    stale = now - PERIOD_CLAIM_TTL
    with Session() as db:
        ids = list(db.scalars(
            select(ReportPeriod.id)
            .where(or_(
                ReportPeriod.status == 'pending',
                and_(ReportPeriod.status == 'delivering',
                     ReportPeriod.claimed_at != None,  # noqa: E711
                     ReportPeriod.claimed_at < stale),
                and_(ReportPeriod.status == 'failed',
                     ReportPeriod.attempt_count < reports.MAX_ATTEMPTS,
                     or_(ReportPeriod.claimed_at == None,  # noqa: E711
                         ReportPeriod.claimed_at < now - reports.RETRY_BACKOFF)),
            ))
            .order_by(ReportPeriod.due_at, ReportPeriod.id)
            .limit(limit)
        ))
    return ids


def tick():
    with Session() as db:
        queued = list(db.scalars(select(Job.id).where(Job.status == 'queued').limit(3)))
    for job_id in queued:
        execute(job_id)
    materialise()
    ids = deliverable_ids()
    if ids:
        with ThreadPoolExecutor(max_workers=min(DELIVERY_WORKERS, len(ids))) as pool:
            list(pool.map(_deliver_one, ids))


def main():
    r = Redis.from_url(REDIS_URL, socket_timeout=3)
    # A crash must not strand its running jobs.
    with Session() as db:
        for j in db.scalars(select(Job).where(Job.status == 'running')):
            j.status = 'queued'
            record(db, 'worker', 'job.recovered', {'id': j.id})
        db.commit()
    while True:
        Path('/tmp/heartbeat').touch()
        try:
            item = r.blpop('analysis:queue', timeout=1)
            if item:
                execute(int(item[1]))
            tick()
        except Exception:
            logging.exception('Worker retry')
            time.sleep(2)


if __name__ == '__main__':
    main()
