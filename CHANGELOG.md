# Changelog

## 0.3.0 - 2026-11-27

Dataset index lifecycle for the KAN-20 epic (KAN-21 to KAN-27). New runtime
dependencies: `pglast>=7.23,<8` and `psycopg2-binary>=2.9.9,<3`.

- Persist dataset-owned secondary index intent in Mongo (revision, desired
  state, status, origin, parsed signature) before any DDL, protect it from
  ordinary metadata writes and keep deletion tombstones (KAN-21).
- Parse and render index SQL with pglast in `ddp_lib.index_sql`; move PostgreSQL
  index handling into `ddp_lib.postgres_indexes`, so ddp-lib no longer depends
  on ddp-connectors.
- Reconcile saved source-index policies without runtime rediscovery (KAN-22).
- Ignore legacy null population dependency placeholders and report missing
  dataset IDs (KAN-23).
- Add online and post-load index creation modes for committed-data
  restoration (KAN-24).
- Prepare indexes transactionally around bulk reloads, with attempt-bound
  tokens for crash recovery (KAN-25).
- Separate index-repair queue identities and fence repair tokens so repairs run
  safely alongside refresh jobs (KAN-26).
- Add a reviewed index inventory and metadata-only migration, and retire the
  legacy replay path (KAN-27).
- `DatasetIndexes.listing()` takes no advisory lock, so listing stays available
  during a refresh; `list_indexes` skips an index dropped mid-read and retries
  catalog races on autocommit connections.
- `token_required` keeps its 503 response but now logs the exception.

## 0.2.3 - 2026-11-02

- Release the consolidated library from `main` with updated package metadata.
- Carry forward the functionality from 0.2.1 without runtime changes.

## 0.2.1 - 2026-10-29

- Require the Core DDP token contract for all authentication helpers. Remove the
  legacy validation mode and the `strict` keyword argument; this version targets
  new Core applications.
- Validate signature, expiry, issuer, audience, token type and required claims
  using Flask configuration. Shared encoders issue the same claims.
- Always enforce active users and Keycloak role assignments, deny empty role
  lists and return 503 for authentication service failures.
- Share population index identity, restoration and ANALYZE helpers between Core
  API and Sync in `ddp_lib.population_indexes`, with lazy PostgreSQL imports.
- Add shared lossless cleaning input/COPY serialization and transactional staging
  and publication helpers for Core API/Sync.
- Preserve exact numerics, nullable values, JSON scalars, bytes, dates and text.
- Bound COPY buffering and add real PostgreSQL round-trip/failure regressions.
- Keep cleaning dependencies optional through the `cleaning` extra.

## 0.2.0

- Restore header, decoder-result and user checks through the additive
  `AuthHelper.get_logged_in_user(request, strict=True)` option.
- Add `token_required(roles=None, *, strict=False)`. In strict mode, `None`
  allows authenticated users and `[]` denies access. Legacy defaults, route
  exclusions and token encoding/decoding contracts remain unchanged.
- Restore audit actor IDs when supplied through a document's `_id`.
- Declare missing Flask, Flask-PyMongo and legacy PyMongo runtime dependencies.
  Keep existing exact dependency pins, including PyJWT 2.8.0.
- Correct Python metadata to >=3.10, matching the source and current consumers.
- Add contract, regression and isolated wheel/sdist installation tests.

This is a release candidate in source until tagged and published through the
normal release process. Consumer adoption is tracked separately in KAN-8.
