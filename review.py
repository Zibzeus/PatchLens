"""Bounded history, diff evidence, and positive regression-assertion references."""
import ast
from itertools import zip_longest
from hashlib import sha256
import json
import re
import io
import tokenize

from gitstore import EMPTY, HEX, check, git

CATEGORIES = {
    "Identity and access": r"authenticat|authoriz|authenticate|require_user|login|role_check",
    "Session management": r"session|cookie|samesite|csrf",
    "Input validation": r"validat|sanitiz|allowlist|fullmatch",
    "File and path handling": r"realpath|resolve\(|pathlib|safe_join|basename",
    "Serialization": r"deserializ|serializ|pickle|safe_load|unmarshal",
    "Cryptography": r"cipher|encrypt|decrypt|hmac|digest|verify_signature",
    "Permission checks": r"permission|has_perm|check_access|authorize",
    "Privilege boundaries": r"privilege|setuid|sandbox|tenant|ownership",
    "Security configuration": r"content-security-policy|httponly|secure_cookie|tls|cors",
}
STATUSES=("Open","In review","Supported by review","Contradicted","Deferred")


def stable(*parts):
    return sha256(json.dumps(parts,ensure_ascii=True,separators=(",",":")).encode()).hexdigest()[:32]


def rev(repo,end,value,allow_empty=False):
    if allow_empty and value==EMPTY:
        return EMPTY
    check(HEX.fullmatch(value or ""),"Select a complete revision ID.")
    resolved=git(repo,end,"rev-parse","--verify",value+"^{commit}").decode().strip()
    check(HEX.fullmatch(resolved),"Unknown revision.")
    return resolved


def history(repo,end,tip="HEAD",skip=0):
    check(0<=skip<=100000,"History offset outside limit.")
    if tip!="HEAD":
        rev(repo,end,tip)
    out=git(repo,end,"log","--no-show-signature","--max-count=30","--skip="+str(skip),
            "--format=%H%x00%P%x00%an%x00%aI%x00%s%x00",tip,"--")
    result=[]
    for line in out.splitlines():
        if not line:
            continue
        fields=line.decode("utf8","replace").rstrip("\x00").split("\x00")
        check(len(fields)==5,"Unsupported commit metadata.")
        sha,parents,author,date,subject=fields
        result.append(dict(sha=sha,parents=parents.split(),base=parents.split()[0] if parents else EMPTY,author=author,date=date,subject=subject))
    return result


def refs(repo,end):
    result=[]
    for line in git(repo,end,"for-each-ref","--format=%(refname)%00%(objectname)%00%(*objectname)%00%(objecttype)%00%(*objecttype)","refs/heads","refs/tags").splitlines():
        name,oid,peeled,kind,pkind=line.decode("utf8","replace").split("\x00")
        result.append((name,peeled if pkind=="commit" else oid if kind=="commit" else ""))
    return result


def filenames(repo,end,base,tip):
    raw=git(repo,end,"diff","--no-ext-diff","--no-textconv","--no-renames","--name-only","-z",base,tip,"--")
    paths=[p.decode("utf8") for p in raw.split(b"\x00") if p]
    check(len(paths)<=1000,"Comparison exceeds 1,000 changed files. Select a smaller revision range.")
    check(all(len(p)<512 and not any(ord(c)<32 for c in p) for p in paths),"Unsupported path encoding or control character.")
    return paths


def comparison(repo,end,base,tip,path):
    paths=filenames(repo,end,base,tip)
    if not paths:
        return paths, "", [], []
    path=path or paths[0]
    check(path in paths,"Select a changed path in this comparison.")
    raw=git(repo,end,"diff","--no-ext-diff","--no-textconv","--no-renames","--no-color","--unified=3",base,tip,"--",path)
    try:
        lines=raw.decode("utf8").split("\n")
    except UnicodeDecodeError as exc:
        from gitstore import Refused
        raise Refused("Diff contains non-UTF-8 text. Choose another changed file.") from exc
    check(len(lines)<=3000,"File diff exceeds 3,000 lines. Select a smaller revision range.")
    display=[]; changes=[]; removed=[]; added=[]
    old=new=None
    context=""
    def flush():
        display.extend(dict(old=o,new=n) for o,n in zip_longest(removed,added))
        removed.clear(); added.clear()
    for raw_line in lines:
        line=raw_line.removesuffix("\r")
        match=re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$",line)
        if match:
            flush()
            old,new=int(match[1]),int(match[2])
            context=match[3].strip()
            display.append(dict(hunk=line))
        elif old is not None and line.startswith((" ","-","+")):
            op=line[0]; text=line[1:]
            if op==" ":
                flush()
                display.append(dict(old=(old,text,""),new=(new,text,"")))
            else:
                side="before" if op=="-" else "after"
                ln=old if op=="-" else new
                matches=[k for k,pat in CATEGORIES.items() if re.search(pat,text,re.I)]
                changes.append(dict(side=side,line=ln,text=text,categories=matches,context=context,
                                    revision=base if side=="before" else tip,path=path,base=base,tip=tip))
                (removed if op=="-" else added).append((ln,text,op))
            old+=op!="+"
            new+=op!="-"
    flush()
    return paths,path,display,changes


def historical_search(repo,end,tip,mode,query):
    check(mode in ("literal","regex","category") and 1<=len(query)<=96 and all(ord(c)>=32 for c in query),"Enter a search expression of 1–96 characters.")
    if mode=="category":
        check(query in CATEGORIES,"Select a listed review category.")
        query=CATEGORIES[query]
    if mode=="regex":
        check(all(re.fullmatch(r"\^?[A-Za-z0-9_ .:/-]+\$?",p) for p in query.split("|")),
              "Regex supports text, dot, alternatives, and start/end anchors.")
    # Diff filtering is delegated to Git's bounded subprocess, never a Python regex on arbitrary repository text.
    flags=["-i"] if mode=="category" else []
    hashes=git(repo,end,"log",*flags,"--first-parent","--max-count=12","--format=%H","--no-ext-diff","--no-textconv",
               ("-S" if mode=="literal" else "-G")+query,tip,"--").decode().splitlines()
    hits=[]
    for sha in hashes:
        check(HEX.fullmatch(sha),"Invalid history result.")
        parent=git(repo,end,"show","--no-patch","--format=%P",sha,"--").decode().split()
        base=parent[0] if parent else EMPTY
        raw=git(repo,end,"diff",*flags,"--no-renames","--no-ext-diff","--no-textconv",("-S" if mode=="literal" else "-G")+query,
                "--name-only","-z",base,sha,"--")
        for path in raw.split(b"\0"):
            if path:
                name=path.decode("utf8")
                _,_,_,changes=comparison(repo,end,base,sha,name)
                evidence=[c for c in changes if (query in c["text"] if mode=="literal" else re.search(query,c["text"],re.I if mode=="category" else 0))]
                if evidence:
                    hits.append(dict(base=base,tip=sha,path=name,query=query,mode=mode,evidence=evidence[:4]))
                check(len(hits)<=60,"More than 60 matching files. Narrow the query.")
    return hits


def testpath(path):
    return path.endswith(".py") and (path.rsplit("/",1)[-1].startswith("test_") or path.endswith("_test.py") or "tests" in path.split("/")[:-1])


def assertion_refs(repo,end,change,mode):
    check(change["side"]=="after" and testpath(change["path"]),"Select an added assertion in a Python test.")
    check(mode in ("exact","structural"),"Unknown assertion comparison.")
    snippet=change["text"].strip()
    check(0<len(snippet)<=512,"Select a single-line test assertion under 513 characters.")
    try:
        node=ast.parse(snippet).body
    except (SyntaxError,RecursionError):
        node=[]
    check(len(node)==1 and isinstance(node[0],ast.Assert),"Select a complete, single-line Python assert statement.")
    tip=change["tip"]
    paths=[p.decode("utf8") for p in git(repo,end,"ls-tree","-rz","--name-only",tip).split(b"\0") if p]
    tests=[p for p in paths if testpath(p)]
    check(len(tests)<=80,"Comparison limited to snapshots with at most 80 Python test files.")
    found=[]; skipped=[]
    shape=ast.dump(node[0],include_attributes=False)
    for p in tests:
        data=git(repo,end,"show",tip+":"+p)
        try:
            src=data.decode("utf8")
            if mode=="structural":
                # Parse only small assertion expressions. Never build a full AST for imported files.
                tokens=tokenize.generate_tokens(io.StringIO(src).readline)
                lines=[]
                for token in tokens:
                    if token.type!=tokenize.NAME or token.string!="assert":
                        continue
                    text=token.line.strip()
                    if len(text)>512 or not text.startswith("assert "):
                        continue
                    try:
                        nodes=ast.parse(text).body
                    except (SyntaxError,RecursionError):
                        continue
                    if len(nodes)==1 and ast.dump(nodes[0],include_attributes=False)==shape:
                        lines.append(token.start[0])
            else:
                lines=[i for i,s in enumerate(src.splitlines(),1) if s.strip()==snippet]
        except (UnicodeError,SyntaxError,RecursionError,ValueError,tokenize.TokenError):
            skipped.append(p)
            continue
        for ln in lines:
            if (p,ln)!=(change["path"],change["line"]):
                found.append(dict(revision=tip,path=p,line=ln,reason="The same added regression assertion appears here. Review whether the tests document the same contract."))
            check(len(found)<=200,"More than 200 assertion references. Narrow the repository snapshot.")
    return dict(mode=mode,matches=found,skipped=skipped,files_considered=len(tests))
