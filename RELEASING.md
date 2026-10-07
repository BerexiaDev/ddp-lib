# Release procedure

The proposed release is **ddp-lib 0.2.0**. Existing version tags must remain
unchanged. The import namespace remains `ddp_lib`; Python package indexes
normalize underscores and hyphens to the same distribution name.

1. Review the KAN-5 matrix in [docs/KAN-5.md](docs/KAN-5.md) and the changelog.
2. Use Python 3.10+ and install build tooling:
   `python -m pip install build twine`.
3. Run `python scripts/smoke_install.py` and repeat with
   `--constraints tests/consumer-constraints.txt` on Python 3.10.
4. Build clean artifacts with `python -m build`, then run
   `python -m twine check --strict dist/*`. Keep wheel/sdist hashes and smoke logs
   with the release record.
5. Release ddp-lib 0.2.0 independently of ddp-connectors. The connector package
   owns its Oracle serializer and no longer depends on ddp-lib.
6. Tag the reviewed commit `0.2.0` and publish artifacts through the team's
   existing package release channel. Do not move earlier tags or publish donor
   packages. No publication is automated by this change.
7. KAN-8 updates consumer pins and runs service integration tests. Validate actual
   database drivers/services there; isolated import tests do not establish live
   database connectivity.

CI runs wheel/sdist smoke checks on Python 3.10, 3.11 and 3.12; the consumer
constraint profile is tested on 3.10, matching existing service images.

The dependency bounds deliberately retain PyMongo 3 for Document.save/remove/insert
and PyJWT 2.8.0 for existing subjects and token error/return contracts. Strict
request validation is opt-in; enabling it across applications is an explicit
consumer decision. Broader token/identity changes belong to KAN-9.
