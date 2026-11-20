"""Operate on a private, hook-free SHA-1 object store. Never invoke Git in an input repository."""
import contextlib
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import zlib


class Refused(ValueError):
    pass


def setting(name, default, maximum):
    value = int(os.environ.get("PATCHLENS_" + name, default))
    if not 1 <= value <= maximum:
        raise ValueError(f"PATCHLENS_{name} must be 1–{maximum}")
    return value


REPO = setting("REPO_MIB", 96, 1024) << 20
FILE = setting("FILE_MIB", 4, 32) << 20
OUTPUT = setting("OUTPUT_KIB", 512, 4096) << 10
TIME = setting("SECONDS", 30, 180)
SLOTS = threading.BoundedSemaphore(setting("CONCURRENCY", 1, 4))
GIT = shutil.which("git")
HEX = re.compile("[0-9a-f]{40}")
BRANCH = re.compile(r"refs/(heads|tags)/[\w./-]+", re.ASCII)
EMPTY = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def check(ok, message):
    if not ok:
        raise Refused(message)


@contextlib.contextmanager
def operation():
    check(SLOTS.acquire(blocking=False), "Another Git operation is running. Retry shortly.")
    try:
        yield time.monotonic() + TIME
    finally:
        SLOTS.release()


def remaining(end):
    seconds = end - time.monotonic()
    check(seconds > 0, "Operation timed out. Narrow the comparison.")
    return seconds


def git(repo, end, *args, stdin=None, ok=(0,)):
    check(GIT, "Install Git and restart PatchLens.")
    remaining(end)
    env = {k: os.environ[k] for k in ("SystemRoot","WINDIR","TMP","TEMP","TMPDIR","LANG") if k in os.environ}
    env.update(PATH=str(Path(GIT).parent)+os.pathsep+os.defpath, GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_ATTR_NOSYSTEM="1", GIT_ALLOW_PROTOCOL="",
               GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1", GIT_PAGER="cat", LC_ALL="C")
    cmd = [GIT, "--no-pager", "--literal-pathspecs", "-c", "core.hooksPath="+os.devnull,
           "-c","maintenance.auto=false","-c","gc.auto=0","-c","pack.threads=1","-c","core.deltaBaseCacheLimit=16m",
           "-c","core.attributesFile="+os.devnull, "--git-dir="+str(repo), *args]
    if sys.platform.startswith("linux"):
        cmd = [sys.executable,"-I",str(Path(__file__).with_name("git_limit.py")),str(REPO),*cmd]
    proc = subprocess.Popen(cmd, cwd=repo.parent, env=env, stdin=stdin or subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=os.name=="posix")
    out,err = bytearray(),bytearray()
    def drain(pipe,buffer):
        while chunk := pipe.read(16384):
            buffer.extend(chunk)
            if len(out)+len(err)>OUTPUT:
                proc.kill()
                break
    readers=[threading.Thread(target=drain,args=pair,daemon=True) for pair in ((proc.stdout,out),(proc.stderr,err))]
    for thread in readers:
        thread.start()
    try:
        proc.wait(timeout=max(.001,end-time.monotonic()))
    except subprocess.TimeoutExpired:
        if os.name=="posix":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid,signal.SIGKILL)
        else:
            proc.kill()
        proc.wait()
        raise Refused("Git timed out. Narrow the comparison.")
    finally:
        for thread in readers:
            thread.join()
        proc.stdout.close()
        proc.stderr.close()
    check(len(out)+len(err)<=OUTPUT, "Git output exceeded the configured limit. Narrow the query.")
    check(proc.returncode in ok, "Git rejected the object store, revision, or search expression.")
    return bytes(out)


def safe_file(p):
    s = p.lstat()
    check(not p.is_symlink() and not (getattr(s,"st_file_attributes",0)&0x400), "Links in repository storage are unsupported.")
    return s


def import_local(root, name, dest, end):
    check(root and re.fullmatch(r"[\w -][\w .-]{0,99}",name) and not name.endswith((".", " ")), "Choose a direct child of the configured import directory.")
    src = root / name
    safe_file(src)
    check(src.is_dir(), "Local repository directory not found.")
    dot = src/".git"
    if dot.exists() or dot.is_symlink():
        safe_file(dot)
        check(dot.is_dir(), "Linked worktrees are unsupported. Upload a self-contained bundle.")
        src = dot
    check(not (src/"shallow").exists(), "Shallow repository. Import a complete history.")
    count = total = 0
    def copy(p, rel, size):
        nonlocal total
        total += size
        check(total <= REPO, "Repository exceeds the size limit.")
        target = dest / rel
        target.parent.mkdir(exist_ok=True,parents=True)
        with p.open("rb") as source, target.open("wb") as out:
            copied=0
            while block:=source.read(65536):
                remaining(end)
                copied+=len(block)
                check(copied<=size,"Repository changed while importing.")
                out.write(block)
            check(copied==size,"Repository changed while importing.")
    def walk(p, rel):
        nonlocal count
        count+=1
        remaining(end)
        check(count<=30000 and len(rel)<512, "Repository entry limit exceeded.")
        s=safe_file(p)
        if stat.S_ISDIR(s.st_mode):
            for child in p.iterdir():
                walk(child,rel+"/"+child.name)
            return
        check(stat.S_ISREG(s.st_mode), "Unsupported repository storage entry.")
        check(rel!="objects/info/alternates" and rel!="objects/info/http-alternates","External object stores are unsupported.")
        storage = re.fullmatch(r"objects/(?:[0-9a-f]{2}/[0-9a-f]{38}|pack/pack-[0-9a-f]{40}\.(?:idx|pack))",rel)
        if storage or rel in ("HEAD","packed-refs") or BRANCH.fullmatch(rel):
            check(size_ok(rel,s.st_size),"Oversized Git metadata.")
            copy(p,rel,s.st_size)
    for entry in ("HEAD","objects","refs","packed-refs"):
        p=src/entry
        if entry in ("HEAD","objects") or p.exists() or p.is_symlink():
            walk(p,entry)


def size_ok(rel,size):
    return size <= (REPO if rel.startswith("objects/pack/") else FILE+65536 if rel.startswith("objects/") else OUTPUT)


def inflate(stream,end,limit):
    """Discard decompressed data in chunks, retaining only enough header for size checks."""
    decoder=zlib.decompressobj()
    size=0
    prefix=bytearray()
    pending=b""
    while not decoder.eof:
        remaining(end)
        block=pending or stream.read(65536)
        check(block,"Truncated compressed Git object.")
        try:
            out=decoder.decompress(block,min(65536,limit-size+1))
        except zlib.error as exc:
            raise Refused("Invalid compressed Git object.") from exc
        size+=len(out)
        check(size<=limit,"Expanded Git object exceeds file limit.")
        prefix.extend(out[:128-len(prefix)])
        pending=decoder.unconsumed_tail
    stream.seek(-len(decoder.unused_data),1)
    return size,bytes(prefix)


def pack_budget(stream,end):
    import struct
    header=stream.read(12)
    check(len(header)==12 and header[:4]==b"PACK","Invalid pack header.")
    version,count=struct.unpack(">II",header[4:])
    check(version in (2,3) and count<=30000,"Unsupported pack version or object count.")
    total=0
    def byte():
        b=stream.read(1)
        check(b,"Truncated pack header.")
        return b[0]
    for _ in range(count):
        first=byte()
        kind=(first>>4)&7
        check(kind in (1,2,3,4,6,7),"Unsupported pack object.")
        n=first&15; shift=4; current=first
        while current&128:
            check(shift<40,"Invalid pack object size.")
            current=byte()
            n|=(current&127)<<shift
            shift+=7
        check(n<=FILE,"Declared pack object exceeds file limit.")
        if kind==6:
            for _ in range(10):
                if not byte()&128:
                    break
            else:
                raise Refused("Invalid pack offset.")
        if kind==7:
            check(len(stream.read(20))==20,"Truncated delta reference.")
        size,prefix=inflate(stream,end,n)
        check(size==n,"Pack size mismatch.")
        if kind in (6,7):
            pos=0
            for _ in range(2):
                logical=shift=0
                while True:
                    check(pos<len(prefix) and shift<40,"Invalid delta header.")
                    b=prefix[pos]; pos+=1
                    logical|=(b&127)<<shift
                    shift+=7
                    if not b&128:
                        break
                check(logical<=FILE,"Reconstructed delta exceeds file limit.")
            size=logical
        total+=size
        check(total<=REPO*4,"Expanded pack exceeds repository limit.")
    check(len(stream.read(21))==20,"Unexpected pack trailer.")
    return total


def local_budget(dest,end):
    total=0
    for p in (dest/"objects").rglob("*"):
        remaining(end)
        if p.is_file():
            with p.open("rb") as stream:
                if p.suffix==".pack":
                    total+=pack_budget(stream,end)
                elif p.suffix!=".idx":
                    size,prefix=inflate(stream,end,FILE+64)
                    m=re.match(rb"^(blob|tree|commit|tag) ([0-9]{1,12})\x00",prefix)
                    check(m and int(m[2])<=FILE and size==int(m[2])+m.end() and not stream.read(1),"Invalid or oversized loose object.")
                    total+=size
            check(total<=REPO*4,"Expanded repository exceeds limit.")


def import_bundle(stream, dest, end):
    stream.seek(0,2)
    check(stream.tell()<=REPO,"Bundle exceeds repository limit.")
    stream.seek(0)
    check(stream.readline(64) in (b"# v2 git bundle\n",b"# v3 git bundle\n"),"Expected a complete Git bundle.")
    refs={}
    while line:=stream.readline(1024):
        remaining(end)
        if line==b"\n":
            break
        if line==b"@object-format=sha1\n":
            continue
        m=re.fullmatch(rb"([0-9a-f]{40}) (HEAD|refs/(?:heads|tags)/[a-zA-Z0-9_./-]+)\n",line)
        check(m and len(refs)<4096,"Unsupported bundle reference, capability or prerequisite.")
        oid,ref=(v.decode() for v in m.groups())
        check(ref not in refs,"Duplicate reference in bundle header.")
        refs[ref]=oid
    pos=stream.tell()
    check(refs and stream.read(4)==b"PACK","Missing bundle references or pack.")
    stream.fileno()
    stream.seek(pos)
    pack_budget(stream,end)
    stream.seek(pos)
    git(dest,end,"index-pack","--stdin","--threads=1","--max-input-size="+str(REPO),stdin=stream)
    for ref,oid in refs.items():
        if ref!="HEAD":
            git(dest,end,"update-ref",ref,oid)
    head=refs.get("HEAD") or next((oid for ref,oid in refs.items() if ref.startswith("refs/heads/")),None)
    check(head,"Bundle must include HEAD or a branch.")
    (dest/"HEAD").write_text(head+"\n",encoding="ascii")


def validate(dest,end):
    head=(dest/"HEAD").read_bytes().strip()
    check(re.fullmatch(rb"(?:[0-9a-f]{40}|ref: refs/heads/[a-zA-Z0-9_./-]+)",head),"Unsupported HEAD.")
    if head.startswith(b"ref: "):
        git(dest,end,"check-ref-format",head[5:].decode())
    # Validate expanded sizes before asking Git to render any code or commit message.
    sizes=git(dest,end,"cat-file","--batch-all-objects","--batch-check=%(objectsize)").splitlines()
    expanded=0
    for s in sizes:
        n=int(s)
        expanded+=n
        check(n<=FILE and expanded<=REPO*4,"Object size or total expanded repository limit exceeded.")
    git(dest,end,"fsck","--strict","--no-reflogs","--no-dangling")
    return git(dest,end,"rev-parse","--verify","HEAD^{commit}").decode().strip()
