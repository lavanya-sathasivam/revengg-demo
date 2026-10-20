# Branch & merge protocol — 3 people, 1 repo, 2 weeks

**Read this before your first push.** It is 4 minutes and it will save you a
whole day of merge pain.

---

## 1. The branches

| branch | owner | what it holds | when it merges |
|---|---|---|---|
| `a-backend` | Person A | db.py, extract.py, app.py, A's 3 test files | into `main` when done |
| `b-graph` | Person B | clauses.py, adapter.py, graph.py, impact.py, llm.py, cli.py, seed.py, tests/ | into `main` when done |
| `c-ui` | Person C | retrieval.py, reasoning.py, templates/, authorities | into `main` when done |
| `main` | everyone | the integrated, working system | **the demo branch** |

`main` is the branch you demo from. It already exists and already contains
A's backend + B's relationship engine.

---

## 2. The rule that prevents 90% of conflicts

> **Always start your branch from `origin/main`, never from someone else's branch
> and never from `a-backend`.**

```powershell
git fetch origin
git checkout main
git pull origin main
git checkout -b my-work        # branch FROM the current main
```

If your branch is already created (C's `c-ui` was started from `a-backend`),
that is fine — `main` is a descendant of `a-backend`, so merging `main` into
`c-ui` works cleanly. We verified it: **zero conflicts**.

---

## 3. The daily loop (same 6 lines, every day)

```powershell
git checkout main
git pull origin main
git checkout b-graph          # your branch
git merge main                # take other people's work BEFORE you start
# ... work, only in your own files ...
git add <only your files>
git commit -m "day7: doubt engine, 29 tests green"
git push origin b-graph
```

**Merge `main` into your branch every morning.** It costs 10 seconds and it
means you never accumulate a two-week divergence that explodes on Day 14.

---

## 4. Merging INTO main (only A or B does this, and only after tests pass)

```powershell
git checkout main
git pull origin main
git merge --no-ff <branch>      # --no-ff keeps the branch history visible
# if a conflict appears:
git status                    # see which files
# EDIT EACH CONFLICTED FILE BY HAND, then:
git add <file>
git commit -m "merge branch 'c-ui' into main"
```

### How to resolve a conflict (the mechanical procedure)

Git marks the file with three lines:

```
<<<<<<< HEAD
the version on main (or your branch)
=======
the incoming version
>>>>>>> other-branch
```

1. Open the file in VS Code. VS Code shows **Accept Current / Accept Incoming /
   Accept Both** buttons directly in the conflict markers.
2. Pick the right one. For text files like `requirements.txt` and
   `.gitignore`, **accept both** and delete the duplicate lines.
3. `git add <file>` then `git commit`.

### Which side to pick, in our specific case

| file | pick | why |
|---|---|---|
| `requirements.txt` | **accept both**, then dedupe | both sides added a package |
| `.gitignore` | **accept both** | both sides added an ignore rule |
| `README.md` | **accept both** | each person added a section |
| any `.py` file | **never auto-pick** | open it, read it, decide |

**Our merge guide says: no `.py` file should ever conflict.** That is by
design — A owns `db.py`/`extract.py`/`app.py`, B owns `clauses.py`/`adapter.py`/
`graph.py`/`impact.py`, C owns `retrieval.py`/`reasoning.py`/`templates/`.
Nobody edits another person's file. The only files all three people touch are
`README.md`, `requirements.txt` and `.gitignore` — text files, trivial to
merge.

---

## 5. The tests are the real merge check

Never merge a branch into `main` on faith. Run this first:

```powershell
py -3.11 -m pip install -r requirements.txt
py -3.11 seed.py
py -3.11 -m unittest discover -s tests
```

**29 tests must pass.** One of them runs Person A's own three test files in a
clean subprocess, so `main` can never silently break the backend.

If the tests fail after a merge, the merge is bad. Revert it:
```powershell
git reset --hard origin/main
```

---

## 6. What we already verified (do not redo this)

Tested in a throwaway clone against the real GitHub repo:

| merge | result |
|---|---|
| `b-graph` → `a-backend` | **0 conflicts** |
| `main` → `c-ui` (C branched from a-backend) | **0 conflicts** |
| `b-graph` → `c-ui` | **0 conflicts** |
| simulated C (adds retrieval.py, reasoning.py, templates/) | **1 conflict** — `requirements.txt` only, 2 lines, both sides' content is wanted |
| simulated C (also edits README.md + .gitignore) | **3 conflicts** — all text files, all trivial |

So the worst realistic case is **3 text-file conflicts**, resolved in under
5 minutes, with no code involved.

---

## 7. `.gitattributes` — why we added it

Your laptop has `core.autocrlf = true` globally. Git converts line endings on
checkout. If A's laptop does it differently from yours, git sees changes in
files nobody edited and produces **phantom conflicts** — the most annoying
kind, because the code is identical.

We added `.gitattributes` with `* text=auto eol=lf`, which forces one rule for
everyone. **Do not delete it.** If you see whitespace-only conflicts, check
that this file exists.

---

## 8. Emergency rules

- **Never** `git push --force` to a shared branch. Only `git push --force-with-lease`
  on your *own* branch, and only after checking nobody else works on it.
- **Never** work directly on `main`. Branch first.
- **Never** commit `app.db`, `demo.db` or `.env` (`.gitignore` already blocks them).
- If you break `main`, it is not the end of the world: `git reset --hard
  origin/main` on your machine, then ask whoever merged it to redo it.
- The repo must stay **private** until the provisional patent is filed.

---

## 9. Day-by-day merge schedule for this project

| day | action |
|---|---|
| every morning | `git merge main` into your branch |
| Day 8 | A merges `a-backend` → `main`, B merges `b-graph` → `main` |
| Day 11 | freeze: `main` = demo candidate, tag `v1-demo`. Only bugfix merges after this. |
| Day 12–14 | C merges `c-ui` → `main` (final integration) |
| Day 14 | demo runs from `main` on one laptop; screenshots as backup |
| after Day 14 | file the provisional patent, THEN open the repo publicly |
