# DeepKube shared application library

Canonical distribution: **ddp-lib**. Python imports remain `ddp_lib`.
Contains authentication helpers, permissions, audit logging, MongoDB documents,
pagination, filters, request parsing, DTOs, enums and serialization utilities.

Python 3.10+ is required. Install the reviewed release using your configured
package channel. Runtime dependencies are declared in `pyproject.toml`;
Flask 2 and PyMongo 3 remain supported for existing consumers.

## Compatibility options

Existing calls retain their default behavior. Request validation can be enabled
explicitly with `AuthHelper.get_logged_in_user(request, strict=True)` or
`@token_required(roles=..., strict=True)`.

In strict mode, missing/malformed bearer headers, failed decoding, tokens without
a scalar subject and missing users return a failed authentication result. Structured
subjects are rejected before they can become MongoDB query operators. `roles=None`
allows any authenticated user; `roles=[]` denies access. Existing route exclusions
remain in effect. Without strict mode, the legacy empty-role and error behavior
is retained. Token strings, numeric subjects, lifetimes, decoder dictionaries and
encoder error returns are unchanged.

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
