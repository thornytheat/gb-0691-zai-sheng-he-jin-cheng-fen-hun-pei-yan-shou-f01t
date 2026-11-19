import sqlite3, json, os
from pathlib import Path
from app.errors import DomainError
def connection():
    path=Path(os.environ.get('APP_DB','data/runs.sqlite3'));path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, payload TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT "待复核")')
    db.execute('CREATE TABLE IF NOT EXISTS schedule_events(seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, furnace TEXT NOT NULL, heat_key TEXT NOT NULL, kind TEXT NOT NULL, action TEXT, stop_index INTEGER, created_at TEXT NOT NULL, UNIQUE(run_id, idempotency_key))')
    db.execute('CREATE INDEX IF NOT EXISTS idx_schedule_run ON schedule_events(run_id, seq)')
    return db
def save(result):
    with connection() as db:
        ident=result['manifest']['run_id'];row=db.execute('SELECT payload,revision,decision FROM runs WHERE id=?',(ident,)).fetchone()
        if row: return {**json.loads(row['payload']), 'revision':row['revision'], 'decision':row['decision'], 'reused':True}
        db.execute('INSERT INTO runs(id,payload) VALUES (?,?)',(ident,json.dumps(result,ensure_ascii=False)))
    return {**result,'revision':1,'decision':'待复核','reused':False}
def get_run(ident):
    with connection() as db: row=db.execute('SELECT payload,revision,decision FROM runs WHERE id=?',(ident,)).fetchone()
    if row is None: raise DomainError('NOT_FOUND','批次不存在',404)
    return {**json.loads(row['payload']),'revision':row['revision'],'decision':row['decision']}
def latest():
    with connection() as db: row=db.execute('SELECT payload,revision,decision FROM runs ORDER BY rowid DESC LIMIT 1').fetchone()
    return {**json.loads(row['payload']),'revision':row['revision'],'decision':row['decision']} if row else None
def history():
    with connection() as db: return [dict(r) for r in db.execute('SELECT id,revision,decision FROM runs ORDER BY rowid DESC')]
def review(ident, revision, decision):
    if decision not in ('通过','退回'): raise DomainError('DECISION','复核动作无效')
    with connection() as db:
        changed=db.execute('UPDATE runs SET revision=revision+1,decision=? WHERE id=? AND revision=?',(decision,ident,revision)).rowcount
        if not changed: raise DomainError('VERSION','版本已变化，请重新载入',409)
    return {'revision':revision+1,'decision':decision}

def schedule_events(ident):
    """按发生顺序读取某批次的连炉确认事件。"""
    with connection() as db:
        rows=db.execute('SELECT seq,idempotency_key,furnace,heat_key,kind,action,stop_index,created_at FROM schedule_events WHERE run_id=? ORDER BY seq',(ident,)).fetchall()
    return [dict(r) for r in rows]
def find_schedule_event(ident, key):
    with connection() as db:
        row=db.execute('SELECT seq,idempotency_key,furnace,heat_key,kind,action,stop_index,created_at FROM schedule_events WHERE run_id=? AND idempotency_key=?',(ident,key)).fetchone()
    return {**dict(row),'reused':True} if row else None
def append_schedule_event(ident, event):
    """追加确认事件；相同幂等键重复提交直接返回既有事件，不重复累计。"""
    with connection() as db:
        existing=db.execute('SELECT seq,idempotency_key,furnace,heat_key,kind,action,stop_index,created_at FROM schedule_events WHERE run_id=? AND idempotency_key=?',(ident,event['idempotency_key'])).fetchone()
        if existing: return {**dict(existing), 'reused':True}
        cursor=db.execute('INSERT INTO schedule_events(run_id,idempotency_key,furnace,heat_key,kind,action,stop_index,created_at) VALUES (?,?,?,?,?,?,?,?)',(ident,event['idempotency_key'],event['furnace'],event['heat_key'],event['kind'],event.get('action'),event.get('stop_index'),event['created_at']))
        row=db.execute('SELECT seq,idempotency_key,furnace,heat_key,kind,action,stop_index,created_at FROM schedule_events WHERE seq=?',(cursor.lastrowid,)).fetchone()
    return {**dict(row), 'reused':False}
