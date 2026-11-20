from contextlib import closing
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import threading
from datetime import datetime, timezone
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool

import gitstore as gs
from gitstore import check, git, operation, Refused, EMPTY
from review import CATEGORIES, history, refs, comparison, historical_search, assertion_refs, stable, rev, STATUSES, testpath

BASE=Path(__file__).resolve().parent
DATA=Path(os.getenv("PATCHLENS_DATA") or (Path(os.getenv("LOCALAPPDATA") or os.getenv("XDG_DATA_HOME") or Path.home()/".local/share")/"patchlens-lite")).resolve()
ROOT=Path(os.environ["PATCHLENS_IMPORT_ROOT"]).resolve() if os.getenv("PATCHLENS_IMPORT_ROOT") else None
DATA.mkdir(mode=0o700,parents=True,exist_ok=True)
def db():
    c=sqlite3.connect(DATA/"research.sqlite3")
    c.row_factory=sqlite3.Row
    return c
with closing(db()) as c:
    c.executescript("""
    CREATE TABLE IF NOT EXISTS repos(id TEXT PRIMARY KEY,name TEXT,source TEXT,head TEXT,imported TEXT);
    CREATE TABLE IF NOT EXISTS notes(id TEXT PRIMARY KEY,rid TEXT,data TEXT);
    CREATE INDEX IF NOT EXISTS note_repo ON notes(rid);
    """)
TOKEN=secrets.token_urlsafe(32)
UPLOAD=threading.BoundedSemaphore(1)
app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
app.add_middleware(TrustedHostMiddleware,allowed_hosts=["localhost","127.0.0.1"])
templates=Jinja2Templates(directory=BASE/"templates")
templates.env.globals.update(token=TOKEN,qs=lambda **kw:urlencode(kw),statuses=STATUSES,testpath=testpath)
app.mount("/assets",StaticFiles(directory=BASE/"assets"),name="assets")


@app.middleware("http")
async def guard(req,call_next):
    if req.method=="POST":
        if req.headers.get("sec-fetch-site")=="cross-site":
            return PlainTextResponse("Cross-site form submission refused.",403)
        n=req.headers.get("content-length","")
        limit=gs.REPO+65536 if req.url.path=="/import" else 65536
        if not n.isdecimal() or len(n)>10 or int(n)>limit or "transfer-encoding" in req.headers:
            return PlainTextResponse("Request exceeds the fixed-length upload limit.",413)
    res=await call_next(req)
    res.headers.update({"Content-Security-Policy":"default-src 'none'; style-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
                        "X-Content-Type-Options":"nosniff","Cache-Control":"no-store","Referrer-Policy":"no-referrer"})
    return res


@app.exception_handler(Refused)
async def refused(req,exc):
    return templates.TemplateResponse(req,"error.html",{"message":str(exc)},status_code=400)


@app.exception_handler(UnicodeDecodeError)
async def encoding_error(req,exc):
    return templates.TemplateResponse(req,"error.html",{"message":"This repository contains an unsupported text encoding."},status_code=400)


def repository(rid):
    with closing(db()) as c:
        r=c.execute("SELECT * FROM repos WHERE id=?",(rid,)).fetchone()
    check(r,"Unknown repository.")
    return dict(r),DATA/r["id"]


def notes(rid):
    with closing(db()) as c:
        return [json.loads(r[0]) for r in c.execute("SELECT data FROM notes WHERE rid=? ORDER BY rowid DESC",(rid,))]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def checkform(f):
    check(f.get("token")==TOKEN,"Form expired or invalid. Reload the page.")


@app.get("/")
def home(request:Request):
    with closing(db()) as c:
        repositories=c.execute("SELECT * FROM repos ORDER BY rowid DESC").fetchall()
    return templates.TemplateResponse(request,"home.html",dict(repos=repositories,root=ROOT,limit=gs.REPO>>20))


def import_repo(name,stream):
    rid=uuid4().hex
    dest=DATA/rid
    with operation() as end:
        # Claim the directory before entering the cleanup block. Never reinitialize an existing snapshot.
        try:
            dest.mkdir(mode=0o700)
        except FileExistsError as e:
            raise Refused("Snapshot identifier already in use. Retry the import.") from e
        try:
            git(dest,end,"init","--bare","--quiet","--template=",str(dest))
            if stream is None:
                gs.import_local(ROOT,name,dest,end)
                gs.local_budget(dest,end)
            else:
                gs.import_bundle(stream,dest,end)
            head=gs.validate(dest,end)
            check(sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())<=gs.REPO,"Private object store exceeds the configured limit.")
            with closing(db()) as c,c:
                c.execute("INSERT INTO repos VALUES(?,?,?,?,?)",(rid,name,"Local repository" if stream is None else "Git bundle",head,now()))
        except BaseException as e:
            if dest.exists():
                shutil.rmtree(dest)
            if isinstance(e,(OSError,UnicodeError)):
                raise Refused("Repository unavailable, invalid, or unsupported.") from e
            raise
    return rid


@app.post("/import")
async def upload(req:Request):
    check(UPLOAD.acquire(blocking=False),"Another import is in progress.")
    try:
        async with req.form(max_files=1,max_fields=4,max_part_size=65536) as f:
            checkform(f)
            file=f.get("bundle")
            name=file.filename if file and hasattr(file,"filename") else f.get("name","")
            check(isinstance(name,str) and re.fullmatch(r"[\w .-]{1,120}",name),"Use a plain repository or bundle filename.")
            rid=await run_in_threadpool(import_repo,name,file.file if file and hasattr(file,"file") else None)
    finally:
        UPLOAD.release()
    return RedirectResponse("/r/"+rid,303)


@app.get("/r/{rid}")
def overview(request:Request,rid:str,tip:str="",skip:int=0,mode:str="literal",query:str=""):
    r,d=repository(rid)
    with operation() as end:
        selected=rev(d,end,tip or r["head"])
        commits=history(d,end,selected,skip)
        branches=refs(d,end)
        hits=historical_search(d,end,selected,mode,query) if query else []
    return templates.TemplateResponse(request,"repo.html",dict(r=r,selected=selected,skip=skip,commits=commits,refs=branches,
                                      notes=notes(rid),hits=hits,mode=mode,query=query,formats=FORMATS,categories=CATEGORIES))


def pair(d,end,base,tip):
    return rev(d,end,base,True),rev(d,end,tip)


@app.get("/r/{rid}/commit/{sha}")
def one_commit(rid:str,sha:str):
    _,d=repository(rid)
    with operation() as end:
        item=history(d,end,rev(d,end,sha))[0]
    return RedirectResponse(f"/r/{rid}/compare?"+urlencode(dict(base=item["base"],tip=sha)),303)


@app.get("/r/{rid}/compare")
def compare(request:Request,rid:str,base:str,tip:str,path:str="",side:str="",line:int=0):
    r,d=repository(rid)
    with operation() as end:
        base,tip=pair(d,end,base,tip)
        paths,path,display,changes=comparison(d,end,base,tip,path)
        selected=next((v for v in changes if v["side"]==side and v["line"]==line),None)
        check(not side or selected,"Line is not a change in this comparison.")
        parents=history(d,end,tip)[0]
        graph=git(d,end,"rev-list","--all","--children").decode().splitlines()
        children=next((v.split()[1:] for v in graph if v.split()[0]==tip),[])
        related=git(d,end,"log","-n","8","--format=%H",tip,"--",path).decode().splitlines() if path else []
    nid=stable(rid,base,tip,path,side,line)
    saved=next((n for n in notes(rid) if n["id"]==nid),{})
    return templates.TemplateResponse(request,"compare.html",dict(r=r,base=base,tip=tip,path=path,paths=paths,display=display,
       changes=changes,selected=selected,n=saved,commit=parents,children=children,related=related,empty=EMPTY))


NOTE_FIELDS=("hypothesis","boundary","support","contradictions","missing","questions","status")
def record(rid,f):
    r,d=repository(rid)
    with operation() as end:
        base,tip=pair(d,end,f.get("base",""),f.get("tip",""))
        _,path,_,changes=comparison(d,end,base,tip,f.get("path",""))
        change=next((v for v in changes if v["side"]==f.get("side") and str(v["line"])==f.get("line")),None)
        check(change,"Select a changed line from the diff.")
        id=stable(rid,base,tip,path,change["side"],change["line"])
        values={k:f.get(k,"") for k in NOTE_FIELDS}
        check(all(isinstance(v,str) and len(v)<=5000 for v in values.values()) and values["hypothesis"].strip() and values["status"] in STATUSES,"Enter a hypothesis and valid status. Fields are limited to 5,000 characters.")
        mode=f.get("assertions","")
        result=assertion_refs(d,end,change,mode) if mode else None
        entry=dict(id=id,repository_id=rid,change=change,**values,assertion_review=result,updated=now())
        with closing(db()) as c,c:
            c.execute("INSERT OR REPLACE INTO notes VALUES(?,?,?)",(id,rid,json.dumps(entry,ensure_ascii=True)))
    return id


@app.post("/r/{rid}/notes")
async def save(req:Request,rid:str):
    async with req.form(max_files=0,max_fields=20,max_part_size=65536) as f:
        checkform(f)
        id=await run_in_threadpool(record,rid,dict(f))
    return RedirectResponse(f"/r/{rid}/notes/{id}",303)


@app.get("/r/{rid}/notes/{nid}")
def note(request:Request,rid:str,nid:str):
    r,_=repository(rid)
    n=next((v for v in notes(rid) if v["id"]==nid),None)
    check(n,"Unknown note in this repository.")
    return templates.TemplateResponse(request,"note.html",dict(r=r,n=n,selected=n["change"]))


FORMATS=("PATCH_ANALYSIS.md","SECURITY_CHANGES.md","VARIANT_CANDIDATES.md","RESEARCH_HYPOTHESES.md","research-results.json")


def markdown(name,r,items):
    out=[f"# {name[:-3].replace('_',' ').title()}",f"Repository: {r['name']} ({r['id']})",f"Snapshot HEAD: {r['head']}",f"Source: {r['source']} · imported {r['imported']}"]
    if name==FORMATS[2]:
        out.append("Coverage of added regression assertions in Python tests. Matches document test overlap; each needs manual review.")
    for n in items:
        c=n["change"]
        if name==FORMATS[2] and not n["assertion_review"]:
            continue
        out+=["## "+n["hypothesis"],"Record: "+n["id"],f"Comparison: {c['base']} → {c['tip']}",f"Source line ({c['side']}): {c['revision']} · {c['path']}:{c['line']}", "    "+c["text"]]
        if name==FORMATS[2]:
            a=n["assertion_review"]
            out.append(f"{a['mode']} comparison · {a['files_considered']} Python test files considered.")
            out.extend(f"- {m['revision']} · {m['path']}:{m['line']} — {m['reason']}" for m in a["matches"])
            if not a["matches"]:
                out.append("No additional matching assertion in the inspected files.")
            if a["skipped"]:
                out.append("Skipped: "+", ".join(a["skipped"]))
        elif name==FORMATS[1]:
            out.append("Review signals: "+(", ".join(c["categories"]) or "Manual selection")+". Researcher-selected change; classification requires review.")
        elif name==FORMATS[0]:
            out.extend(["Hunk context: "+(c["context"] or "(none supplied by Git)"),"Research status: "+n["status"],"Supporting evidence: "+n["support"]])
        else:
            out.extend(k.title()+": "+(n[k] or "Not recorded") for k in NOTE_FIELDS[1:])
    if not any(s.startswith("## ") for s in out):
        out.append("No saved records in this section.")
    return "\n\n".join(out)+"\n"


@app.get("/r/{rid}/export/{name}")
def export(rid:str,name:str):
    check(name in FORMATS,"Unknown export.")
    r,_=repository(rid)
    items=notes(rid)
    body=json.dumps(dict(schema_version=1,repository=r,records=items),ensure_ascii=False,indent=2) if name==FORMATS[-1] else markdown(name,r,items)
    check(len(body.encode())<=gs.OUTPUT,"Export exceeds the output budget. Increase PATCHLENS_OUTPUT_KIB to export.")
    return Response(body,media_type="application/json" if name.endswith(".json") else "text/markdown",
                    headers={"Content-Disposition":f'attachment; filename="{name}"'})
