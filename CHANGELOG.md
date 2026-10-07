# Changelog

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
