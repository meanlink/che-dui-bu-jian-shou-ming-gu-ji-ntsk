import hashlib, json
def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
def dataset_dict(d):
    return {'id':d.id, 'name':d.name, 'source':d.source, 'rows':len(d.records), 'checksum':d.checksum, 'created_at':d.created_at}
def job_dict(j):
    return {k:getattr(j,k) for k in ['id','name','dataset_id','params','status','result','error','approved','created_at','finished_at']}
def report_dict(r):
    return {k:getattr(r,k) for k in ['id','name','job_id','destination','interval_seconds','next_run','anchor_at','next_period','enabled','paused_at']}
def period_dict(p):
    return {k:getattr(p,k) for k in [
        'id','report_id','period_index','due_at','destination','status',
        'checksum','json_checksum','filename','json_filename','attempt_count',
        'error','integrity_error','created_at','completed_at',
        'snapshot_checksum',
    ]}
def delivery_dict(d):
    return {k:getattr(d,k) for k in ['id','report_id','created_at','checksum','filename','destination']}
