# KAN-5 semantic consolidation audit

## Baselines and coverage

Audited full, non-shallow history and code at:
- Donor cmr-etl-lib/main: 9898f1bb26f39c1692affcb454c0cab1a0da5b3e (73 commits).
- Canonical ddp-lib/main: 262ab6ecdf833484e4a901d7d6e9fc3c07c3c9dc (12 commits).
- Canonical initial import 0d77c0a has exactly the donor-tip tree:
  da94fd89db621e98abccd49c55dd8408f28cf7eb. All 28 files were already imported.
- 16 of 24 shared package files remain identical after namespace normalization.
  History was inspected for removed fixes as well as current-head differences.

Paths below are relative to each package root unless marked otherwise.
The donor is evidence only; all implementation lives in ddp_lib.

## Consolidation matrix

| Donor behavior/fix | Donor commit / file | Canonical equivalent | Classification | Implemented action |
|---|---|---|---|---|
| Header guards and safe request identity | 81430ec; removed 80e5992; auth/auth_helper.py | Unchecked splitting at baseline | PORT / ADAPT TO CANONICAL | Add keyword-only strict=True; validate bearer header, decoder result, subject and user; retain legacy defaults and success fields |
| None versus explicit empty role set | 80711ed; regressed 63420f5; auth/decorator.py | if-not-roles bypass | PORT / ADAPT TO CANONICAL | Strict mode denies []; None stays authenticated/unrestricted; default untouched |
| Audit actor identity | 142d3be; regression 23b8b5e; audit_logger/audit_logger_module.py | Producer returns _id, projection reads id | PORT / ADAPT TO CANONICAL | Fall back to _id only when id is absent; preserve actor projection and collection |
| Runtime dependency declarations | a901557 requirements.txt; deletion 651ec8f | Missing Flask/Mongo declarations; stale Python metadata | PORT / ADAPT TO CANONICAL | Reconcile direct dependencies, retain all exact main pins, add wheel/sdist checks |
| JWT/password/blacklist contracts | d5b24e9, b9d34e5; auth/user.py, auth/black_list_token.py | d78c2cc PyJWT update; 6fbb88e explicit HS256 | ALREADY PRESENT / SUPERSEDED | Keep user.py unchanged; lock token strings, subjects, lifetimes, failures and blacklist contracts in tests |
| User identity/governance fields | d2651a6, f3b0d89; auth/user.py, auth/auth_helper.py | Retained; 1b60cce adds g.user | ALREADY PRESENT / SUPERSEDED | Preserve references/populations/domains/sub-domains fields and request context |
| Obsolete raw-token helper | d3fdeee; auth/auth_helper.py | Explicitly removed by 1b60cce | ALREADY PRESENT / SUPERSEDED | Do not restore obsolete field assumptions or compatibility packages |
| Audit model/controller/DTO and target collection | fe650e8, 73f118c, 142d3be; audit package, dto.py | Already imported | ALREADY PRESENT / SUPERSEDED | Preserve, with actor-ID regression test |
| Mixed-type audit list comparisons | 5d21c29; audit_logger/utils.py | Normalized-identical | ALREADY PRESENT / SUPERSEDED | Retain sorting fallback; add baseline check |
| Pagination/counting/offset adjustment | 1a64d67, 15a42c0; document.py, audit service | Identical; earlier forced page size superseded | ALREADY PRESENT / SUPERSEDED | Retain requested-size behavior |
| User/date filters | 195f48a; filters.py | Retained; d2838ed adds JSON-string inputs | ALREADY PRESENT / SUPERSEDED | Preserve list and JSON forms |
| Execution/query/data enums | 39eaa28, 931d6c7, 42519f0, fedc732, 2872c5c, 35c5225; enums.py | All donor tip values present; canonical project enums added | ALREADY PRESENT / SUPERSEDED | Preserve cancellation, skipped, script error, log-table, incremental and project values |
| Permissions/role matrix | 80711ed, 187eb7f through 3a3a8ea; permission_utils.py | Donor final grants plus canonical PROJECTS | ALREADY PRESENT / SUPERSEDED | Preserve matrix; only additive strict empty-set enforcement |
| Route/audit exclusions | 5aa2e05, 7b21a18, 16245e2, 859c678, 397071b, 0f1e27c, 1d2776f, 8df72de | Identical exclusions | ALREADY PRESENT / SUPERSEDED | Preserve OPTIONS/swagger/search and existing public paths |
| Generic CRUD/parser/DTO/lookup helpers | a901557; document.py, paginator.py, reqparse.py, dto.py, utils.py, decorators.py | Retained; d2838ed errors/DTOs and d1e010c serialization are newer | ALREADY PRESENT / SUPERSEDED | Keep canonical implementations |
| Historical ESG/client mappings | a901557; audit/filter/utility mappings | Removed or already retained as shared public helpers | CLIENT-SPECIFIC | Do not import client datasets, deployment prefixes or removed mappings; do not delete current helpers |
| External identity mapping | 7822ab0; auth/auth_helper.py | Superseded principal-email/decoded-token mapping | OWNED BY ANOTHER CORE TICKET | KAN-9; no Keycloak mapping restored |
| Consumer adoption/configuration | Application pins and deployment integration | Platform-owned | OWNED BY ANOTHER CORE TICKET | KAN-8 adoption; KAN-6 client deployment configuration |

## Other references and compatibility decisions

The unmerged canonical runtime branch at 16ae697 was reviewed. Its packaging
work is reconciled here; its changed JWT error, subject, byte and required-claim
contracts are deliberately excluded. It is not classified as another ticket
merely because it is a branch. Broader token hardening requires explicit contract
decisions in KAN-9. The branch-only cryptography helper (23c43b6) and project
migration enums (f5f436d) are absent from both audited mains and are not donor-main
gaps.

Existing consumers use ddp-lib 0.0.4, which is exactly the audited canonical main.
The regression baseline exercises real Flask contexts and PyJWT; MongoDB calls
are isolated at the database boundary. No consumer source/pins or donor files
are changed.

The default request helper retains its historical exceptions, and the default
decorator retains its historical empty-role behavior. Strict validation is an
additive choice, not a claim that all existing routes now enforce it. Public
route exclusions remain in both modes. Database outages are not converted into
successful authentication. Token encoding/decoding remains byte-for-byte
unchanged in source.

## Packaging and validation

Release target: ddp-lib 0.2.0, with the existing ddp_lib namespace. The source
already requires Python 3.10 syntax; metadata now reflects it. Flask 2,
Flask-PyMongo 2 and PyMongo 3 are declared because Document uses legacy driver
APIs. Existing exact pins, particularly PyJWT 2.8.0, are retained.

Run scripts/smoke_install.py for separate clean wheel and sdist installs, pip
check, all-module imports and all contract/regression tests. Add
--constraints tests/consumer-constraints.txt for the current Python 3.10 shared
consumer stack. This is a shared-runtime profile, not a replacement for complete
application requirements or KAN-8 service integration.

See RELEASING.md for release order and publication. See VALIDATION.md for actual
executed checks and limitations.
