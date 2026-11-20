# PatchLens Lite

A local, single-user patch review notebook. Select two Git revisions, compare exact changed lines, document a research hypothesis and export it with its source. All Git reads are offline. The application has one Python process and one SQLite database. Its only external executable is Git. No model, research agent, scanner, container or background service is required.

## Start

Use Python 3.11+ with SQLite and a maintained Git CLI on PATH. In the directory containing this file:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
export PATCHLENS_IMPORT_ROOT="$HOME/review-inputs"
python -m uvicorn app:app --host 127.0.0.1 --port 8766
```

On Windows PowerShell:

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:PATCHLENS_IMPORT_ROOT = 'C:\review-inputs'
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8766
```

Open [PatchLens Lite](http://127.0.0.1:8766). The import root is optional for bundle-only use. Choose a directory controlled by your OS account containing stationary repositories you intend to import. Enter a direct child **folder name**, such as `notebook`. The working tree and uncommitted files are excluded. For a portable input, upload a self-contained SHA-1 bundle, for example one already produced by `git bundle create notebook.bundle --all` in a trusted checkout. Import creates a private snapshot. Import again for newer history.

## Review a patch

1. Open the snapshot. The header identifies its imported HEAD. Branch and tag links select a history tip. History shows the author, author date, subject and commit ID. Select a commit to compare it to its first parent, or enter two full commit IDs under **Compare revisions**. A root commit compares against the empty tree.
2. Use **Find changes by review category** for identity, session, validation, paths, serialization, cryptography, permissions, privilege boundaries or security configuration. For custom search, choose Git pickaxe literal (`-S`, changed occurrence count) or regex (`-G`, changed matching lines). The bounded regex grammar allows text, dot, alternatives and anchors. Click a result's changed-line link to inspect its source. These are review signals, not vulnerability determinations.
3. Choose a changed file. The columns show before and after, with separate line numbers. Git hunk headers retain available function context. **Revision navigation** links parents, children visible from imported refs and earlier patches touching the selected path. A multiple-commit comparison establishes the endpoint content; use individual commits to attribute the change.
4. Select a numbered addition or removal. Edit the deterministic hypothesis, suspected boundary, supporting and contradictory evidence, missing information, manual review questions and investigation status. Saving captures the selected code from Git. Later edits preserve the record ID. Each repository/comparison/path/side/line has one editable note.
5. For an **added, single-line Python test assertion**, compare the same assertion across tests at the after revision. Exact mode compares trimmed physical lines. Structural mode compares Python assertion syntax, ignoring formatting, grouping and quote style. It tokenizes source and parses short expressions without executing tests. Results include revision, file, line, reason, considered files and skipped files. This feature documents regression-test overlap. It does not search removed vulnerable implementations, rank attack targets or establish exploitability.

## Exports and storage

Each export represents the researcher's **saved** selections for that snapshot. Searches alone do not add records. Markdown files are suitable for manual text import into a research notebook. JSON carries structured fields and stable evidence anchors.

| File | Contents |
|---|---|
| `PATCH_ANALYSIS.md` | Before/after commits, selected revision and line, hunk context, code and supporting notes |
| `SECURITY_CHANGES.md` | Selected changes, built-in category matches or manual selection |
| `VARIANT_CANDIDATES.md` | Positive regression assertion comparisons and candidate test references |
| `RESEARCH_HYPOTHESES.md` | Research questions, evidence, counterevidence, missing information and status |
| `research-results.json` | Version 1 snapshot metadata, note IDs, code anchors, comparisons, research fields and assertion-review results |

The application uses the platform's per-user data directory (`%LOCALAPPDATA%\patchlens-lite` on Windows; `$XDG_DATA_HOME/patchlens-lite` or `~/.local/share/patchlens-lite` on Linux). Set `PATCHLENS_DATA` to use another private directory. Stop the app and copy the **entire data directory** for backup or migration. The SQLite database contains notes and the UUID-named directories contain the original Git evidence. Markdown may contain sensitive source or untrusted markup; use a viewer that disables active HTML and external resource loading.

## Resource and trust boundaries

Use one Uvicorn worker bound to loopback; limits and form tokens are per process. Access is based on the OS user, with no web accounts. Each import uses a fresh bare Git store. Local imports copy only object storage, HEAD and refs, never Git config, working tree files, hooks or external object stores. The app rejects linked repository storage, incremental bundles and shallow repositories. Linux and Windows paths must use supported UTF-8 names. Non-commit tags are listed without history links. Renames appear as a deletion and an addition. Binary or mode-only changes have no textual line to annotate.

Git commands use argument arrays, validated object IDs and literal pathspecs. The private environment disables Git transports, inherited user/system configuration, hooks, external diffs and text conversion. The app bounds packed and loose object inflation before rendering. It runs no build scripts, uploaded code or test cases. Repository content is escaped in HTML. Avoid placing the private data directory inside the import root; keep the source stationary during import and inaccessible to untrusted local writers. These controls do not sandbox defects in Git itself. Use a patched Git binary and an unprivileged OS account.

| Environment variable | Default | Enforced scope |
|---|---:|---|
| `PATCHLENS_REPO_MIB` | 96 | Uploaded bundle, copied repository data, and final private store; combined expanded objects capped at four times this value |
| `PATCHLENS_FILE_MIB` | 4 | Individual expanded Git objects, including reconstructed deltas |
| `PATCHLENS_SECONDS` | 30 | Shared time budget for Git operations in one request, including multi-command searches |
| `PATCHLENS_OUTPUT_KIB` | 512 | Combined stdout and stderr per Git invocation; maximum exported document size |
| `PATCHLENS_CONCURRENCY` | 1 | Simultaneous Git workflows; one upload parses at a time |

The UI limits a single diff to 3,000 lines and a comparison to 1,000 changed paths. History pages contain 30 commits. Pickaxe inspects first-parent history and displays up to 12 matching commits, 60 files and four cited changes per matched file. Regression assertion review considers up to 80 Python test files and 200 additional matches. Oversized operations report a limit instead of presenting partial results as complete. Allow disk space for source bundles, temporary upload staging and retained snapshots; there is no scheduled cleanup. On Linux each Git child has a 512 MiB virtual address-space limit and a file-size limit. On Windows byte, time, output and job-count limits apply; there is no OS-enforced process memory ceiling.

## Verification

```sh
python -m pip install httpx==0.28.1
python -m unittest discover -s tests -v
```

Tests create a real repository, branches, an annotated tag, two commits, a packed local store and a Git bundle in temporary directories. They verify source coordinates directly using Git, both import paths, three history search modes, root diffs, exact and structural test references, all five exports, note persistence after reopening in another process, and rejection of unsafe paths, external object stores, malformed data and over-limit requests.

The intended ordinary small-repository host is **1–2 vCPU and 512 MiB–1 GiB RAM**. A loopback Edge run with a two-commit fixture completed local import, revision comparison, line selection, hypothesis save and verified Markdown download. The Windows Uvicorn process peaked at **60.3 MiB working set**, excluding Git children, the browser and OS. This is a small-fixture measurement; a constrained Linux host, worst-case pack and large histories have not been benchmarked.
