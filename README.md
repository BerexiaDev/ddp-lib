# DeepKube shared application library

Canonical distribution: **ddp-lib**. Python imports remain `ddp_lib`.
Contains authentication helpers, permissions, audit logging, MongoDB documents,
pagination, filters, request parsing, DTOs, enums and serialization utilities.

Python 3.10+ is required. Install the reviewed release using your configured
package channel. Runtime dependencies are declared in `pyproject.toml`;
Flask 2 and PyMongo 3 remain supported for existing consumers.

## Authentication

`AuthHelper.get_logged_in_user(request)` and `@token_required(roles=...)` always
validate the Core DDP token contract. Configure `SECRET_KEY` (at least 32 bytes),
`DDP_TOKEN_ISSUER` and `DDP_TOKEN_AUDIENCE` in Flask's application configuration.
Token helpers require an application context and read this configuration.

Signature, expiry, issuer, audience, token type and required claims are validated.
Missing/malformed bearer headers, invalid tokens, missing or disabled users and
Keycloak users without a mapped role fail authentication. `roles=None` allows any
authenticated user; `roles=[]` denies access. Existing route exclusions remain in
effect. Invalid credentials return 401, denied roles return 403 and authentication
service failures return 503.

There is no legacy validation mode. Shared token encoders issue the same required
claims, normalize subjects to strings and raise configuration errors. Core Auth
controls its issuance lifetimes through its service configuration.

Audit logging accepts actor identifiers in either `id` or `_id`, preferring
an explicitly supplied `id`; output remains `user.id`.

## Validation and release

```sh
python -m pip install build twine
python scripts/smoke_install.py
python scripts/smoke_install.py --constraints tests/consumer-constraints.txt
```

The second command installs both a wheel and an sdist into separate empty
environments, checks dependencies, imports every shipped module and runs the
contract/regression tests against the installed package. The constraint profile
targets existing Python 3.10 consumers.

See [the consolidation audit](docs/KAN-5.md), [changes](CHANGELOG.md) and
[release procedure](RELEASING.md).

## Cleaning I/O

`ddp_lib.cleaning` provides lossless PostgreSQL input/COPY and transactional
staging/publication helpers shared by Core API and Sync. Install the optional
runtime dependencies with `pip install '.[cleaning]'`. The module itself imports
without pandas or psycopg2, so other library consumers remain lightweight.

Run `python -m unittest discover -s tests -p test_cleaning.py -v`; set
`CLEANING_TEST_POSTGRES_DSN` to include real database round trips and publication
failure tests. Each database test owns a unique schema.

## Population indexes

`ddp_lib.population_indexes` provides the index identity, restoration and ANALYZE
helpers used by Core API and Sync. Callers supply their population document and
connector factory; the module does not import either service's application code.
PostgreSQL operations require psycopg2 in the consuming service. Importing the
module and parsing index identities do not require the database driver.

Run `python -m unittest discover -s tests -p test_population_indexes.py -v` for
the identity contracts. Core's `ddp-sync/tests/test_population_indexes.py` covers
the database lifecycle through both API and Sync.
