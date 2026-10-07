# KAN-5 validation record

Executed locally on Windows x86-64, 2026-10-06. Candidate version: 0.2.0.
The CI workflow additionally defines Linux runs; hosted CI has not been run here.

| Check | Result |
|---|---|
| Python 3.10.21 source suite | 24 passed |
| Python 3.11.16 clean, non-editable installed package | 24 passed; uv pip check passed |
| Python 3.12.14 clean, non-editable installed package | 24 passed; uv pip check passed |
| Python 3.10 wheel + sdist, default dependencies | Both passed: 24 tests each, pip check and strict twine checks |
| Python 3.10 wheel + sdist, consumer constraint profile | Both passed: 24 tests each, pip check and strict twine checks |
| Complete existing consumer requirements, Python 3.10/Linux resolution | All 7 profiles resolved without changing any non-library pin |
| Independent review | No open blocking source/metadata findings; both identified issues fixed with regressions |
| git diff --check | Passed |

Canonical main and consumer tag 0.0.4 identify the same commit. The baseline
tests passed before behavior changes. The token implementation in auth/user.py
has no diff from main. Tests use real PyJWT and Flask contexts, replacing only
database boundaries; they cover legacy defaults and strict opt-in separately.

## Consumer dependency evidence

Requirements were copied outside consumer repositories, replacing only the two
library references with the local canonical candidates, then resolved with:

    uv pip compile <copied-requirements> --python-version 3.10 --python-platform x86_64-manylinux2014

The unchanged non-library requirements resolved for:
- deepkube-data-platform: API, Sync, Auth and Migration (checkout 018d06d).
- data-platform: API, Sync and Auth (checkout ee08a76).

These retain each consumer's Flask/Werkzeug/Mongo/PyJWT/NumPy/pandas/Oracle pins,
including Werkzeug 2.2.3 and 2.3.8 profiles. The clean-install constraint test
uses the older shared pins; it is not presented as a complete application install.

## Regression evidence and limits

Every ported behavior has a failing-before/passing-after regression. Follow-up
review also reproduced and fixed structured JWT subjects in strict mode and
non-ASCII SQL keyword/numeric lookalikes; quoted Unicode literals remain valid.

No donor or consumer source was modified. Original canonical checkouts remain
unchanged; changes are isolated on feat/kan-5-consolidation worktrees. SQL recovery
and consumer upgrades remain KAN-7/KAN-8; broad token redesign remains KAN-9.
Strict authentication and literal defaults are additive opt-ins.

Import/factory checks cover all shipped modules and Python drivers; they do not
prove connection behavior against every external database, vendor ODBC driver,
JDBC jar or SFTP server. Full service integration belongs to the adoption ticket.
Each package's CI checks out its own repository. No extra sibling-repository
credential is required. No tags, publication or consumer rollout were performed.
