import os
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from src.config.database import get_db
from src.config.settings import ARTIFACT_DIR
from src.models import Report, ReportPeriod
from src.schemas.report import ReportInput
from src.middlewares.auth import current_user, admin_only
from src.services import reports
from src.services.audit import record
from src.utils.serialization import report_dict, period_dict

router = APIRouter()


def _own_periods(db, report_id):
    return list(db.scalars(
        select(ReportPeriod).where(ReportPeriod.report_id == report_id)
        .order_by(ReportPeriod.period_index.desc())
    ))


@router.get('/reports')
def listing(db=Depends(get_db), u=Depends(current_user)):
    rows = []
    for r in db.scalars(select(Report).order_by(Report.id.desc())):
        periods = _own_periods(db, r.id)
        total = len(periods)
        completed = sum(1 for p in periods if p.status == 'completed')
        failed = sum(1 for p in periods if p.status in ('failed', 'corrupted'))
        skipped = sum(1 for p in periods if p.status == 'skipped')
        rows.append({
            **report_dict(r),
            'period_count': total,
            'completed_count': completed,
            'failed_count': failed,
            'skipped_count': skipped,
            'pending_count': total - completed - failed - skipped,
            'last_period': period_dict(periods[0]) if periods else None,
        })
    return rows


@router.get('/reports/{id}/periods')
def period_listing(id: int, limit: int = 100, db=Depends(get_db), u=Depends(current_user)):
    r = db.get(Report, id)
    if not r:
        raise HTTPException(404, '分发计划不存在')
    periods = _own_periods(db, id)
    return {
        'report': report_dict(r),
        'periods': [period_dict(p) for p in periods[:max(1, min(limit, 500))]],
    }


@router.post('/reports', status_code=201)
def create(data: ReportInput, db=Depends(get_db), u=Depends(admin_only)):
    r = reports.create(db, data)
    record(db, u.email, 'report.schedule', {'id': r.id, 'destination': r.destination})
    db.commit()
    return report_dict(r)


@router.post('/reports/{id}/toggle')
def toggle(id: int, db=Depends(get_db), u=Depends(admin_only)):
    r = db.get(Report, id)
    if not r:
        raise HTTPException(404, '分发计划不存在')
    reports.toggle(db, r)
    record(db, u.email, 'report.toggle', {'id': id, 'enabled': r.enabled})
    db.commit()
    return report_dict(r)


@router.get('/deliveries')
def deliveries(db=Depends(get_db), u=Depends(current_user)):
    """List archived issues (each row is one frozen, fully-written 期次)."""
    rows = list(db.scalars(
        select(ReportPeriod)
        .where(ReportPeriod.status.in_(('completed', 'corrupted', 'failed', 'skipped')))
        .order_by(ReportPeriod.due_at.desc(), ReportPeriod.id.desc())
        .limit(200)
    ))
    return [period_dict(p) for p in rows]


@router.post('/deliveries/{id}/retry')
def retry(id: int, db=Depends(get_db), u=Depends(admin_only)):
    p = db.get(ReportPeriod, id)
    if not p:
        raise HTTPException(404, '归档不存在')
    reports.retry(db, p)
    record(db, u.email, 'report.period.retry', {'report_id': p.report_id, 'period': p.period_index})
    db.commit()
    return period_dict(p)


@router.get('/deliveries/{id}/download')
def download(id: int, format: str = 'csv', db=Depends(get_db), u=Depends(current_user)):
    p = db.get(ReportPeriod, id)
    if not p:
        raise HTTPException(404, '归档不存在')
    if format not in ('csv', 'json'):
        raise HTTPException(422, '支持 CSV 或 JSON')

    ok, reason = reports.verify_archive(db, p)
    if not ok:
        return JSONResponse(status_code=409, content={
            'detail': '归档不可下载：' + reason,
            'period': p.period_index,
            'reason': reason,
            'status': p.status,
        })

    filename = p.json_filename if format == 'json' else p.filename
    path = os.path.join(ARTIFACT_DIR, filename)
    return FileResponse(
        path,
        filename=filename,
        media_type='application/json' if format == 'json' else 'text/csv; charset=utf-8',
    )


@router.get('/deliveries/{id}/verify')
def verify(id: int, db=Depends(get_db), u=Depends(current_user)):
    p = db.get(ReportPeriod, id)
    if not p:
        raise HTTPException(404, '归档不存在')
    ok, reason = reports.verify_archive(db, p)
    return {'ok': ok, 'reason': reason, 'status': p.status}
