# DDP shared library

`ddp-lib` is the shared library for Deepkube Core. Import it as `ddp_lib`.
It provides authentication, audit logging, Mongo documents, Flask-RESTX
DTOs and request parsing, pagination, filters, and permission helpers.

## Installation

`pyproject.toml` is the source of truth for the version and dependencies.
For the release version and how to install a release tag, see [RELEASING.md](RELEASING.md).

To install from a local checkout:

```sh
python -m pip install ./ddp-lib
```

This installs the package with all its dependencies. Use underscores for imports
(`ddp_lib`) and hyphens for the package name (`ddp-lib`).

## Supported versions

| Area | Python | Main requirements |
| --- | --- | --- |
| API and audit services | CPython 3.10–3.12 | Flask 2.2.5–3.x, Flask-RESTX 1.2.0, Flask-PyMongo 2.x, PyMongo 3.x |
| Authentication | CPython 3.10–3.12 | PyJWT 2.8–2.x, Flask-Bcrypt 1.x |
| Workers and sync | CPython 3.10–3.12 | ddp-connectors 0.2.x (it needs ddp-lib 0.2.x) |

- Python below 3.10 and 3.13 or above are not supported.
- CI tests each supported Python with the versions pip picks inside our ranges.
- Applications must keep their own lock files.

