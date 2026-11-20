import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import zlib
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


class Workflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root=Path(cls.tmp.name)
        cls.src=cls.root/"sample"
        cls.src.mkdir()
        os.environ["PATCHLENS_DATA"]=str(cls.root/"private")
        os.environ["PATCHLENS_IMPORT_ROOT"]=str(cls.root)
        import app
        cls.app=app
        cls.client=TestClient(app.app,base_url="http://localhost",follow_redirects=False)
        cls.addClassCleanup(cls.client.close)
        cls.token=re.search('name="token" value="([^"]+)"',cls.client.get("/").text)[1]
        def g(*args):
            return subprocess.check_output(["git","-c","core.autocrlf=false","-c","core.hooksPath="+os.devnull,
                "-c","commit.gpgsign=false","-c","user.name=Fixture","-c","user.email=test@example.invalid",*args],cwd=cls.src).decode().strip()
        cls.git=staticmethod(g)
        g("init","-q","-b","main","--template=")
        tests=cls.src/"tests"
        tests.mkdir()
        (tests/"test_policy.py").write_text("def test_policy():\n    mode = 'private'\n    assert mode\n",encoding="utf8")
        (tests/"test_backup.py").write_text("def test_backup():\n    mode = 'private'\n    assert mode == 'private'\n",encoding="utf8")
        (tests/"test_export.py").write_text('def test_export():\n    mode = "private"\n    assert (mode == "private")\n',encoding="utf8")
        g("add",".");g("commit","-qm","Baseline policy tests")
        cls.base=g("rev-parse","HEAD")
        (tests/"test_policy.py").write_text("def test_policy():\n    # authorization policy\n    mode = 'private'\n    assert mode == 'private'\n",encoding="utf8")
        g("add",".");g("commit","-qm","Clarify policy expectation")
        cls.tip=g("rev-parse","HEAD")
        g("branch","audit");g("tag","-a","v1","-m","fixture")
        cls.bundle=cls.root/"sample.bundle"
        g("bundle","create",str(cls.bundle),"--all")
        # These repository-controlled settings must never enter or execute in the private store.
        g("config","diff.external","UNTRUSTED_DIFF_COMMAND")
        g("config","core.hooksPath","untrusted-hooks")
        cls.local=cls.post("/import",name="sample")
        assert cls.local.status_code==303,cls.local.text
        cls.route=cls.local.headers["location"]

    @classmethod
    def post(cls,url,**kw):
        return cls.client.post(url,data=dict(token=cls.token,**kw))

    def test_local_diff_and_every_line(self):
        r=self.client.get(self.route)
        self.assertEqual(r.status_code,200,r.text)
        for s in ("refs/heads/audit","refs/tags/v1","Clarify policy expectation","Fixture"):
            self.assertIn(s,r.text)
        p="tests/test_policy.py"
        q=dict(base=self.base,tip=self.tip,path=p)
        response=self.client.get(self.route+"/compare",params=q)
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn("authorization policy",response.text)
        d=self.app.repository(self.route.split("/")[-1])[1]
        self.assertNotIn("UNTRUSTED_DIFF_COMMAND",(d/"config").read_text())
        with self.app.operation() as end:
            changes=self.app.comparison(d,end,self.base,self.tip,p)[3]
        self.assertEqual({(c["side"],c["line"]) for c in changes},{("after",2),("before",3),("after",4)})
        for c in changes:
            actual=self.git("show",c["revision"]+":"+c["path"]).splitlines()[c["line"]-1]
            self.assertEqual(actual,c["text"])
        for mode,query in [("literal","authorization"),("regex","authoriz.tion|permission"),("category","Identity and access")]:
            r=self.client.get(self.route,params={"query":query,"mode":mode})
            self.assertEqual(r.status_code,200,r.text)
            self.assertIn("after L2",r.text)

    def test_bundle_notes_exports_and_positive_assertions(self):
        r=self.client.post("/import",data={"token":self.token},files={"bundle":("sample.bundle",self.bundle.read_bytes())})
        self.assertEqual(r.status_code,303,r.text)
        route=r.headers["location"]
        source=dict(base=self.base,tip=self.tip,path="tests/test_policy.py",side="after",line="4")
        fields=dict(**source,hypothesis="Review the documented default",boundary="Record ownership",support="Regression test",
                    contradictions="None recorded",missing="Policy spec",questions="Which requirement applies?",status="Open")
        n=self.post(route+"/notes",**fields,assertions="exact")
        self.assertEqual(n.status_code,303,n.text)
        note=n.headers["location"]
        def records():
            return self.client.get(route+"/export/research-results.json").json()
        one=records()["records"][0]
        self.assertEqual(len(one["assertion_review"]["matches"]),1)
        fields["status"]="In review"
        other=self.post(route+"/notes",**fields,assertions="structural")
        self.assertEqual(other.headers["location"],note)
        result=records()
        self.assertEqual(result["repository"]["head"],self.tip)
        self.assertEqual(len(result["records"]),1)
        n=result["records"][0]
        self.assertEqual(n["status"],"In review")
        self.assertEqual(len(n["assertion_review"]["matches"]),2)
        for m in n["assertion_review"]["matches"]:
            self.assertIn("assert",self.git("show",m["revision"]+":"+m["path"]).splitlines()[m["line"]-1])
        self.assertEqual(self.client.get(note).status_code,200)
        for name in self.app.FORMATS[:-1]:
            res=self.client.get(route+"/export/"+name)
            self.assertEqual(res.status_code,200)
            for s in (self.tip,source["path"]+":4",n["id"]):
                self.assertIn(s,res.text)
        cmd="from fastapi.testclient import TestClient; from app import app; import sys; print(TestClient(app,base_url='http://localhost').get(sys.argv[1]).text)"
        reopened=json.loads(subprocess.check_output([sys.executable,"-c",cmd,route+"/export/research-results.json"],encoding="utf8"))
        self.assertEqual(result,reopened)
        self.assertEqual(self.client.get(self.route+"/notes/"+n["id"]).status_code,400)

    def test_input_limits_and_empty_tree(self):
        self.assertEqual(self.post("/import",name="../sample").status_code,400)
        self.assertEqual(self.client.post("/import",data={"name":"sample"}).status_code,400)
        self.assertEqual(self.client.get("/",headers={"Host":"dns.invalid"}).status_code,400)
        self.assertEqual(self.client.post("/import",data={"token":self.token},files={"bundle":("bad.bundle",b"no pack")}).status_code,400)
        self.assertEqual(self.client.get(self.route+"/compare",params={"tip":"--help","base":self.base}).status_code,400)
        self.assertEqual(self.client.get(self.route+"/compare",params={"tip":self.tip,"base":self.base,"path":"../../secret"}).status_code,400)
        root=self.client.get(self.route+"/commit/"+self.base,follow_redirects=True)
        self.assertEqual(root.status_code,200,root.text)
        self.assertIn("Root commit",root.text)
        self.assertIn(self.tip[:12],root.text)
        with patch("gitstore.FILE",16):
            self.assertEqual(self.post("/import",name="sample").status_code,400)
        with patch("gitstore.OUTPUT",16):
            self.assertEqual(self.client.get(self.route).status_code,400)
        with self.app.operation():
            self.assertEqual(self.client.get(self.route).status_code,400)
        with self.assertRaises(self.app.Refused):
            self.app.gs.remaining(0)

    def test_import_failure_preserves_snapshot(self):
        rid=self.route.split("/")[-1]
        _,dest=self.app.repository(rid)
        before=self.client.get(self.route+"/export/research-results.json").json()
        prior_paths=set(self.app.DATA.iterdir())
        with patch("app.uuid4") as identifier:
            identifier.return_value.hex=rid
            self.assertEqual(self.post("/import",name="sample").status_code,400)
        bad=io.BytesIO(b"# v2 git bundle\n"+(self.tip+" HEAD\n").encode()*2+b"\n")
        response=self.client.post("/import",data={"token":self.token},files={"bundle":("bad.bundle",bad)})
        self.assertEqual(response.status_code,400)
        self.assertIn("Duplicate reference", response.text)
        self.assertEqual(set(self.app.DATA.iterdir()),prior_paths)
        self.assertEqual(self.client.get(self.route+"/export/research-results.json").json(),before)
        with self.app.operation() as end:
            self.assertEqual(self.app.gs.validate(dest,end),self.tip)

    def test_pack_inflation_and_link_boundaries(self):
        gs=self.app.gs
        end=time.monotonic()+5
        with self.assertRaises(gs.Refused):
            gs.inflate(io.BytesIO(zlib.compress(b"x"*65536)),end,256)
        with self.assertRaises(gs.Refused):
            gs.pack_budget(io.BytesIO(b"PACK"+bytes.fromhex("0000000200000001")+b"\xb0\xff\xff\xff\x7f"),end)
        p=self.root/"elsewhere"
        p.mkdir(exist_ok=True)
        alt=self.src/".git"/"objects"/"info"/"alternates"
        alt.write_text(str(p),encoding="utf8")
        try:
            self.assertEqual(self.post("/import",name="sample").status_code,400)
        finally:
            alt.unlink()
        self.git("repack","-ad")
        res=self.post("/import",name="sample")
        self.assertEqual(res.status_code,303,res.text)


if __name__=="__main__":
    unittest.main()
