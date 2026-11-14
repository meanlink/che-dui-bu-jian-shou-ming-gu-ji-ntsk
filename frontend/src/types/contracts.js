/** @typedef {{id:number,name:string,source:string,rows:number,checksum:string}} Dataset */
/** @typedef {{id:number,name:string,dataset_id:number,status:string,result:Object,approved:boolean}} AnalysisJob */
/** @typedef {{id:number,name:string,job_id:number,destination:string,next_run:number,anchor_at:number,next_period:number,enabled:boolean,period_count:number,completed_count:number,failed_count:number,pending_count:number}} ReportSchedule */
/** @typedef {{id:number,report_id:number,period_index:number,due_at:number,destination:string,status:'pending'|'delivering'|'completed'|'failed'|'corrupted',checksum:string|null,json_checksum:string|null,filename:string,json_filename:string,attempt_count:number,error:string,integrity_error:string|null,completed_at:number|null}} ReportPeriod */
export {};
