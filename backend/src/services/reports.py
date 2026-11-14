"""Deterministic scheduled briefs (期次).

Rules implemented here:

* A period is identified by its *original* due time. Due times are computed as
  ``anchor + index * interval`` — never ``now + interval`` — so a restart or a
  delayed worker cannot shift the schedule.
* Every period is claimed with a conditional UPDATE (compare-and-set), so two
  workers, or a worker retrying after a crash, cannot archive the same issue
  twice. The (report_id, period_index) unique constraint is the final guard.
* A period freezes the approved analysis result as an immutable snapshot the
  first time it is claimed; retries reuse that snapshot and never swap content.
* CSV and JSON are written to temporary files, fsynced, then atomically renamed
  into place. Both are hashed and the stored checksums must agree before the
  period is published. A crash mid-write leaves only temp files; the period
  stays ``delivering`` and is re-claimed once its lease expires. A later attempt
  adopts any byte-identical artefact that already landed, so a crash between
  rename and DB commit does not rewrite or lose the archive.
* Downloads re-hash the archived files against the stored checksums. A damaged
  file is reported with a reason and refused; the original file is never
  rewritten or deleted.
"""
import csv
import hashlib
import io
import json
import logging
import os
import time
import uuid

from fastapi import HTTPException
from sqlalchemy import select, update, or_, and_

from src.config.settings import ARTIFACT_DIR, PERIOD_CLAIM_TTL, CATCHUP_BATCH
from src.models import Job, Report, ReportPeriod
from src.services import audit
from src.utils.serialization import checksum

MAX_ATTEMPTS = 6
RETRY_BACKOFF = 30


def due_time(report, index):
    """Original due time of period ``index``; purely schedule arithmetic."""
    anchor = float(report.anchor_at or report.next_run)
    return anchor + int(index) * int(report.interval_seconds)


def due_indices(report, now, start=None):
    """Period indices (>= ``start``) whose original due time is <= ``now``."""
    anchor = float(report.anchor_at or report.next_run)
    interval = int(report.interval_seconds)
    if interval <= 0 or now < anchor:
        return []
    first = 0 if start is None else max(0, int(start))
    last = int((now - anchor) // interval)
    if last < first:
        return []
    return list(range(first, last + 1))


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def _safe(value):
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    return "'" + text if text.startswith(('=', '+', '-', '@', '\t', '\r')) else text


def render_csv(snapshot):
    result = snapshot.get('result') or {}
    table = result.get('table') or {}
    columns = table.get('columns') or []
    out = io.StringIO()
    out.write('﻿')
    writer = csv.writer(out)
    writer.writerow([_safe(c.get('label', c.get('key', ''))) for c in columns])
    for row in table.get('rows') or []:
        writer.writerow([_safe(row.get(c.get('key'), '')) for c in columns])
    # The CSV is a table view; append the frozen metadata so the file is
    # self-describing and both formats describe the same issue.
    writer.writerow([])
    writer.writerow([_safe('简报名称'), _safe(snapshot.get('report_name', ''))])
    writer.writerow([_safe('期次'), _safe(snapshot.get('period_index', ''))])
    writer.writerow([_safe('计划到期时刻'), _safe(snapshot.get('due_at_iso', ''))])
    writer.writerow([_safe('分析任务'), _safe(snapshot.get('job_name', ''))])
    writer.writerow([_safe('结论'), _safe(result.get('summary', ''))])
    return out.getvalue().encode('utf-8')


def render_json(snapshot):
    payload = {
        'report_id': snapshot.get('report_id'),
        'report_name': snapshot.get('report_name'),
        'period_index': snapshot.get('period_index'),
        'due_at': snapshot.get('due_at'),
        'due_at_iso': snapshot.get('due_at_iso'),
        'destination': snapshot.get('destination'),
        'job_id': snapshot.get('job_id'),
        'job_name': snapshot.get('job_name'),
        'params': snapshot.get('params'),
        'result': snapshot.get('result'),
        'frozen_at': snapshot.get('frozen_at'),
    }
    return json.dumps(payload, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, indent=2).encode('utf-8')


def freeze_snapshot(report, job, index, due):
    return {
        'report_id': report.id,
        'report_name': report.name,
        'period_index': index,
        'due_at': due,
        'due_at_iso': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(due)),
        'destination': report.destination,
        'job_id': job.id,
        'job_name': job.name,
        'params': job.params,
        'result': job.result,
        'frozen_at': time.time(),
    }


# ---------------------------------------------------------------------------
# atomic file helpers
# ---------------------------------------------------------------------------
def _target_paths(report_id, index):
    base = f'report-{report_id}-period-{index:06d}'
    return base + '.csv', base + '.json'


def _atomic_write(path, data):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, '.tmp-' + uuid.uuid4().hex + '-' + os.path.basename(path))
    try:
        with open(tmp, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _cleanup_temp(path):
    """Remove leftover temp files from an interrupted write of this target."""
    directory = os.path.dirname(path)
    prefix = '.tmp-'
    suffix = '-' + os.path.basename(path)
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if name.startswith(prefix) and name.endswith(suffix):
            try:
                os.unlink(os.path.join(directory, name))
            except OSError:
                pass


def _checkpoint(_stage):
    """Hook used by tests/simulations to inject a crash mid-write."""


def _read_checked(path, expected):
    """Return (ok, reason, data). Never mutates the file."""
    if not os.path.exists(path):
        return False, '归档文件缺失', None
    try:
        with open(path, 'rb') as handle:
            data = handle.read()
    except OSError as exc:
        return False, f'归档文件无法读取：{exc}', None
    actual = hashlib.sha256(data).hexdigest()
    if expected and actual != expected:
        return False, '校验指纹不一致，文件可能已损坏或被改动', data
    return True, '', data


# ---------------------------------------------------------------------------
# period generation (worker side)
# ---------------------------------------------------------------------------
def _note_pause_gap(db, report, resumed_at):
    """Record periods that fell due while the schedule was paused.

    They are marked ``skipped`` so the status page shows the outage honestly and
    no archive is fabricated for a window the operator deliberately stopped.
    Periods that fell due *before* the pause are still owed and are left for
    normal catch-up.
    """
    paused_at = report.paused_at
    if not paused_at:
        return 0, -1
    anchor = float(report.anchor_at or report.next_run)
    interval = int(report.interval_seconds)
    if interval <= 0 or paused_at < anchor:
        return 0, -1
    first = int((paused_at - anchor - 1e-9) // interval) + 1
    first = max(first, 0)
    last = int((resumed_at - anchor) // interval)
    created = 0
    for index in range(first, last + 1):
        if db.scalar(select(ReportPeriod.id).where(
                ReportPeriod.report_id == report.id,
                ReportPeriod.period_index == index)):
            continue
        try:
            with db.begin_nested():
                db.add(ReportPeriod(
                    report_id=report.id, period_index=index,
                    due_at=due_time(report, index), destination=report.destination,
                    status='skipped', created_at=resumed_at,
                    error='计划暂停期间到期，未生成归档',
                ))
            created += 1
        except Exception:
            continue
    return created, last


def ensure_periods(db, report, now=None):
    """Materialise pending rows for every due-but-uncreated period, in order.

    Idempotent and safe under concurrency: the unique constraint arbitrates
    duplicate inserts. Returns the number of rows created this call.
    """
    now = time.time() if now is None else now
    if not report.enabled:
        # A paused schedule materialises nothing; periods due during the pause
        # are recorded as skipped on resume instead of being archived.
        return 0
    pending_indices = due_indices(report, now, start=report.next_period or 0)
    if not pending_indices:
        return 0
    batch = pending_indices[:CATCHUP_BATCH]
    created = 0
    for index in batch:
        if db.scalar(select(ReportPeriod.id).where(
                ReportPeriod.report_id == report.id,
                ReportPeriod.period_index == index)):
            continue
        try:
            with db.begin_nested():
                db.add(ReportPeriod(
                    report_id=report.id, period_index=index,
                    due_at=due_time(report, index), destination=report.destination,
                    status='pending', created_at=now,
                ))
            created += 1
        except Exception:  # another worker inserted the same period first
            continue
    cursor = batch[-1] + 1
    # Conditional advance: a concurrent worker with a stale view must never
    # move the schedule cursor backwards.
    db.execute(
        update(Report)
        .where(Report.id == report.id, Report.next_period < cursor)
        .values(next_period=cursor, next_run=due_time(report, cursor))
    )
    if (report.next_period or 0) < cursor:
        report.next_period = cursor
        report.next_run = due_time(report, cursor)
    return created


def _claim(db, period, now):
    """Compare-and-set claim. Returns an owner token, or None if taken."""
    token = uuid.uuid4().hex
    stale = now - PERIOD_CLAIM_TTL
    retryable_failed = and_(
        ReportPeriod.status == 'failed',
        ReportPeriod.attempt_count < MAX_ATTEMPTS,
        or_(ReportPeriod.claimed_at == None,  # noqa: E711
            ReportPeriod.claimed_at < now - RETRY_BACKOFF),
    )
    result = db.execute(
        update(ReportPeriod)
        .where(
            ReportPeriod.id == period.id,
            or_(
                ReportPeriod.status == 'pending',
                and_(ReportPeriod.status == 'delivering',
                     ReportPeriod.claimed_at != None,  # noqa: E711
                     ReportPeriod.claimed_at < stale),
                retryable_failed,
            ),
        )
        .values(status='delivering', claim_owner=token, claimed_at=now,
                attempt_count=ReportPeriod.attempt_count + 1)
    )
    if result.rowcount != 1:
        db.rollback()
        return None
    db.commit()
    return token


def _publish(db, period, token, csv_data, json_data, snapshot, now):
    csv_name, json_name = _target_paths(period.report_id, period.period_index)
    csv_path = os.path.join(ARTIFACT_DIR, csv_name)
    json_path = os.path.join(ARTIFACT_DIR, json_name)
    csv_sum = hashlib.sha256(csv_data).hexdigest()
    json_sum = hashlib.sha256(json_data).hexdigest()

    # Resume after an interrupted write: if a previous attempt already landed
    # byte-identical artefacts (crash between rename and DB commit), adopt them
    # instead of rewriting. The frozen snapshot makes rendering deterministic.
    csv_ok, _, _ = _read_checked(csv_path, csv_sum)
    json_ok, _, _ = _read_checked(json_path, json_sum)
    if not csv_ok:
        _atomic_write(csv_path, csv_data)
        _checkpoint('csv_written')
    if not json_ok:
        _atomic_write(json_path, json_data)
        _checkpoint('json_written')
    # Verify what actually landed on disk matches what we hashed.
    ok_csv, why_csv, _ = _read_checked(csv_path, csv_sum)
    ok_json, why_json, _ = _read_checked(json_path, json_sum)
    if not (ok_csv and ok_json):
        raise RuntimeError('归档写入后校验失败：' + (why_csv or why_json))
    _cleanup_temp(csv_path)
    _cleanup_temp(json_path)
    # Cross-check that the JSON carries the same frozen analysis as the
    # snapshot the CSV was rendered from.
    try:
        embedded = json.loads(json_data.decode('utf-8'))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(f'JSON 归档无法解析：{exc}') from None
    if checksum(embedded.get('result')) != period.snapshot_checksum:
        raise RuntimeError('CSV 与 JSON 结果指纹不一致')

    changed = db.execute(
        update(ReportPeriod)
        .where(ReportPeriod.id == period.id, ReportPeriod.claim_owner == token,
               ReportPeriod.status == 'delivering')
        .values(status='completed', checksum=csv_sum, json_checksum=json_sum,
                filename=csv_name, json_filename=json_name,
                completed_at=now, claim_owner=None, error='', integrity_error=None)
    )
    if changed.rowcount != 1:
        db.rollback()
        raise RuntimeError('期次在写入过程中被其他进程接管')
    db.commit()
    return csv_name


def deliver_period(db, period_id, now=None):
    """Deliver exactly one period. Safe to call repeatedly and concurrently."""
    now = time.time() if now is None else now
    period = db.get(ReportPeriod, period_id)
    if period is None or period.status in ('completed', 'corrupted'):
        return None
    report = db.get(Report, period.report_id)
    if report is None:
        return None
    token = _claim(db, period, now)
    if token is None:
        return None  # another worker owns it; retry later
    db.refresh(period)

    try:
        job = db.get(Job, report.job_id)
        if not job or not job.approved or job.status != 'completed':
            raise RuntimeError('批准结果不可用（任务未完成或未获批准），本期不归档')

        # Freeze once, reuse forever: retries never swap the content.
        if not period.snapshot:
            period.snapshot = freeze_snapshot(report, job, period.period_index, period.due_at)
            period.snapshot_checksum = checksum(period.snapshot.get('result'))
            db.commit()
        snapshot = period.snapshot
        if checksum(snapshot.get('result')) != period.snapshot_checksum:
            raise RuntimeError('冻结快照与登记指纹不一致，拒绝归档')

        csv_data = render_csv(snapshot)
        json_data = render_json(snapshot)
        filename = _publish(db, period, token, csv_data, json_data, snapshot, now)
        audit.record(db, 'worker', 'report.period.delivered', {
            'report_id': report.id, 'period': period.period_index,
            'delivery': filename, 'attempt': period.attempt_count,
        })
        db.commit()
        return filename
    except Exception as exc:  # noqa: BLE001 - persisted for the status page
        logging.exception('Period delivery failed report=%s period=%s',
                          period.report_id, period.period_index)
        db.rollback()
        period = db.get(ReportPeriod, period_id)
        if period is not None and period.status == 'delivering' and period.claim_owner == token:
            period.status = 'failed'
            period.error = str(exc)[:990]
            period.claim_owner = None
            audit.record(db, 'worker', 'report.period.failed', {
                'report_id': period.report_id, 'period': period.period_index,
                'error': period.error,
            })
            db.commit()
        return None


# ---------------------------------------------------------------------------
# verification on download
# ---------------------------------------------------------------------------
def status_reason(period):
    mapping = {
        'pending': '期次排队中，尚未归档',
        'delivering': '期次正在写入，稍后重试',
        'completed': '',
        'failed': period.error or '期次归档失败',
        'corrupted': period.integrity_error or '归档校验失败',
    }
    return mapping.get(period.status, '期次状态未知')


def verify_archive(db, period):
    """Check both artefacts against stored fingerprints.

    Returns (ok, reason). On failure the period is flagged ``corrupted`` with
    the reason, while the original files are left untouched.
    """
    if period.status != 'completed':
        return False, status_reason(period)
    if period.integrity_error:
        return False, period.integrity_error
    # Pre-period archives (legacy_ref) have no frozen snapshot to compare
    # against; they are verified by their stored file fingerprints only.
    csv_path = os.path.join(ARTIFACT_DIR, period.filename)
    json_path = os.path.join(ARTIFACT_DIR, period.json_filename)
    ok_csv, why_csv, _ = _read_checked(csv_path, period.checksum)
    ok_json, why_json, json_data = _read_checked(json_path, period.json_checksum)
    reason = why_csv or why_json
    if ok_json:
        try:
            payload = json.loads(json_data.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            ok_json, reason = False, 'JSON 归档无法解析'
        else:
            if period.legacy_ref:
                pass
            elif checksum(payload.get('result')) != period.snapshot_checksum:
                ok_json, reason = False, 'JSON 内容与冻结结果指纹不一致'
            elif payload.get('period_index') != period.period_index:
                ok_json, reason = False, 'JSON 期次编号与登记不一致'
    if ok_csv and ok_json:
        return True, ''
    period.status = 'corrupted'
    period.integrity_error = reason
    audit.record(db, 'system', 'report.period.corrupted', {
        'report_id': period.report_id, 'period': period.period_index, 'reason': reason,
    })
    db.commit()
    return False, reason


# ---------------------------------------------------------------------------
# public API used by routes
# ---------------------------------------------------------------------------
def create(db, data):
    job = db.get(Job, data.job_id)
    if not job or job.status != 'completed' or not job.approved:
        raise HTTPException(409, '请先选择已完成且获批准的分析结果')
    now = time.time()
    first_due = now + data.delay_seconds
    report = Report(
        name=data.name, job_id=job.id, destination=data.destination,
        interval_seconds=data.interval_seconds, next_run=first_due,
        anchor_at=first_due, next_period=0, enabled=True,
    )
    db.add(report)
    db.flush()
    return report


def toggle(db, report, now=None):
    now = time.time() if now is None else now
    if report.enabled:
        # Pause: stop generating new issues; history and already-owed
        # (but not yet materialised) periods stay queued.
        report.enabled = False
        report.paused_at = now
    else:
        # Resume from the fixed schedule. Record the pause window as skipped
        # so no archive is fabricated for a window the operator stopped.
        # The schedule cursor is *not* moved past owed periods: issues that
        # fell due before the pause are still backfilled normally, and
        # ensure_periods skips the rows already registered above.
        _note_pause_gap(db, report, now)
        report.enabled = True
        report.paused_at = None
    return report


def retry(db, period):
    """Manually requeue a failed/corrupted period; frozen content is kept."""
    if period.status not in ('failed', 'corrupted'):
        raise HTTPException(409, '只有失败的期次可以重试')
    if period.status == 'corrupted':
        # Content check failed: refuse to silently overwrite damaged originals.
        raise HTTPException(409, '归档文件已损坏，原件保留，不能在原期次重试')
    period.status = 'pending'
    period.claim_owner = None
    period.attempt_count = 0
    period.error = ''
    db.flush()
    return period
