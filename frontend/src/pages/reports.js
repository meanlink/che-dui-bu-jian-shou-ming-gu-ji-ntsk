import {jobs,reports} from '../api/entities.js';
import {download} from '../api/client.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {esc,date,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';

const PERIOD_STATUS={
 pending:'排队中', delivering:'写入中', completed:'已归档', failed:'归档失败', corrupted:'文件受损', skipped:'暂停跳过'
};
const statusTag=p=>'<span class="status '+esc(p.status)+'">'+esc(PERIOD_STATUS[p.status]||p.status)+'</span>';
const reason=p=>{
 if(p.status==='failed')return p.error;
 if(p.status==='corrupted')return p.integrity_error;
 if(p.status==='pending')return '等待后台按原定到期时刻生成';
 if(p.status==='delivering')return '正在写入 CSV 与 JSON';
 if(p.status==='skipped')return '计划暂停期间到期，未生成归档';
 return '';
};
const fmtInterval=s=>s%86400===0?s/86400+' 天':s%3600===0?s/3600+' 小时':s+' 秒';

export async function reportPage(){
 const [list,deliveries,allJobs]=await Promise.all([reports.list(),reports.deliveries(),jobs.list()]);workspace.reports=list;
 const approved=allJobs.filter(j=>j.approved&&j.status==='completed');
 const periodsByReport={};
 await Promise.all(list.map(async r=>{const d=await reports.periods(r.id);periodsByReport[r.id]=d.periods;}));
 const rows=deliveries.map(d=>'<tr><td><strong>'+esc(d.destination)+'</strong><br><small>计划 #'+d.report_id+'</small></td><td>第 '+(d.period_index+1)+' 期<br><small>'+date(d.due_at)+'</small></td><td>'+statusTag(d)+'</td><td>'+(d.status==='completed'?'<small>CSV '+esc((d.checksum||'').slice(0,16))+'…<br>JSON '+esc((d.json_checksum||'').slice(0,16))+'…</small>':'<small class="failure">'+esc(reason(d)||'—')+'</small>')+'</td><td><span class="delivery-actions">'+(d.status==='completed'?'<button class="text-button" data-download="'+d.id+'" data-format="csv">CSV ↓</button><button class="text-button" data-download="'+d.id+'" data-format="json">JSON ↓</button>':'<span class="muted">不可下载</span>')+(d.status==='failed'&&session.user.role==='admin'?'<button class="text-button" data-retry="'+d.id+'">重试 ↻</button>':'')+'</span></td></tr>').join('');
 const planCards=list.map(r=>{
   const periods=periodsByReport[r.id]||[];
   const recent=periods.slice(0,6);
   const badge=r.enabled?'<span class="status completed">运行中</span>':'<span class="status paused">已暂停</span>';
   const next=r.enabled?'下次原定 '+date(r.next_run):'暂停后不再生成新期次';
   return '<article class="plan-card"><div class="plan-head"><div><strong>'+esc(r.name)+'</strong><p>'+esc(r.destination)+' · 周期 '+fmtInterval(r.interval_seconds)+'</p><small>'+esc(next)+' · 累计 '+r.period_count+' 期（成功 '+r.completed_count+' / 异常 '+r.failed_count+' / 暂停跳过 '+(r.skipped_count||0)+' / 待生成 '+r.pending_count+'）</small></div>'+badge+'<button class="button small" data-toggle="'+r.id+'" '+(session.user.role!=='admin'?'disabled':'')+'>'+(r.enabled?'暂停计划':'恢复计划')+'</button></div><div class="period-list">'+recent.map(p=>'<div class="period-row"><span>第 '+(p.period_index+1)+' 期</span><span>'+date(p.due_at)+'</span>'+statusTag(p)+'<span class="failure">'+esc(reason(p)||'')+'</span></div>').join('')+(!recent.length?'<p class="empty small">尚无到期期次。后台会按原定时刻生成第一期。</p>':'')+'</div></article>';
 }).join('');
 return heading('05 / DISTRIBUTION','定时分发与归档','每期按原定到期时刻冻结批准结果；CSV 与 JSON 校验一致才可下载，中断可恢复，受损文件保留原件。','<button class="button" id="refresh-reports">刷新收件箱 ↻</button>')+'<div class="report-grid"><section class="panel"><div class="section-title"><h2>创建分发计划</h2></div><form id="report-form"><label>简报名称<input name="name" required minlength="2" value="日间业务复核简报"></label><label>已批准分析<select name="job_id" required>'+approved.map(j=>'<option value="'+j.id+'">'+esc(j.name)+'</option>').join('')+'</select></label><label>业务接收组<input name="destination" value="业务复核组 · 本地收件箱" required></label><div class="parameter-grid"><label>分发周期<select name="interval_seconds"><option value="86400">每日</option><option value="604800">每周</option><option value="60">每分钟（验收）</option></select></label><label>首次执行<select name="delay_seconds"><option value="60">1 分钟后</option><option value="0">立即</option><option value="3600">1 小时后</option></select></label></div><button type="submit" class="primary" '+(session.user.role!=='admin'||!approved.length?'disabled':'')+'>保存分发计划</button></form><p class="hint">期次按首次到期时刻与周期推算，重启或延迟不会漂移；暂停后不再生成新期次，恢复后逐期补齐漏过期次。</p></section><section><div class="section-title"><h2>运行中的计划</h2><span>'+list.filter(r=>r.enabled).length+' 条已启用</span></div><div class="schedule-list">'+(planCards||'<p class="empty">尚未建立分发计划。</p>')+'</div><section class="panel inbox"><div class="section-title"><h2>各期归档</h2><span>'+deliveries.length+' 条期次记录</span></div>'+(rows?'<div class="table-wrap"><table><thead><tr><th>接收组 / 计划</th><th>期次 / 原定到期</th><th>状态</th><th>校验 / 失败原因</th><th>操作</th></tr></thead><tbody>'+rows+'</tbody></table></div>':'<p class="empty">计划执行后，每期 CSV 与 JSON 会出现在此处。等待片刻后刷新。</p>')+'</section></section></div>';
}
export function bindReports(refresh){document.querySelector('#refresh-reports').onclick=refresh;submit(document.querySelector('#report-form'),async f=>{await reports.create({name:f.get('name'),job_id:Number(f.get('job_id')),destination:f.get('destination'),interval_seconds:Number(f.get('interval_seconds')),delay_seconds:Number(f.get('delay_seconds'))});notify('计划已保存，后台将按到期时间归档');await refresh();});document.querySelectorAll('[data-toggle]').forEach(b=>b.onclick=async()=>{try{await reports.toggle(b.dataset.toggle);await refresh();}catch(e){notify(e.message,true);}});document.querySelectorAll('[data-retry]').forEach(b=>b.onclick=async()=>{try{await reports.retry(b.dataset.retry);notify('已重新排队，冻结内容保持不变');await refresh();}catch(e){notify(e.message,true);}});document.querySelectorAll('[data-download]').forEach(b=>b.onclick=()=>download(b.dataset.download,b.dataset.format).catch(e=>notify(e.message,true)));}
