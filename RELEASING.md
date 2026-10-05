# GitHub release procedure

The release prepared here is `ddp-lib==0.2.0`, with Git tag `0.2.0` in
`BerexiaDev/ddp-lib`. The version in `pyproject.toml`, the built packages and
the Git tag must match. These instructions do not publish anything automatically.

1. Merge the reviewed changes to canonical `main` and require the Python
   3.10–3.12 smoke workflow to pass. Publish ddp-lib before ddp-connectors so the connector release check can
   install the ddp-lib GitHub tag.
2. Build from a clean checkout of the release commit:

   ```sh
   python -m pip install build twine
   python -m build
   python -m twine check --strict dist/*
   ```

   Twine only checks the package metadata here; it does not upload to PyPI.
3. Confirm the generated files carry `0.2.0`. Tag that exact commit, push the tag
   and create a GitHub Release with the wheel and source package attached:

   ```sh
   git tag -a 0.2.0 -m "Release 0.2.0"
   git push origin 0.2.0
   gh release create 0.2.0 dist/* --repo BerexiaDev/ddp-lib --verify-tag --title "0.2.0" --notes-file CHANGELOG.md
   ```

   Run these commands with Git/GitHub credentials that can publish to this repository.
   Never move a published tag or replace its release files with a different build.
4. From an empty virtual environment, install the published GitHub tag:

   ```sh
   python -m pip install 'ddp-lib @ git+https://github.com/BerexiaDev/ddp-lib.git@refs/tags/0.2.0'
   python -m pip check
   ```

   Run the tests from `tests/` outside the checkout. Then pin the GitHub release
   URLs in Core and verify its full dependency set and database integration.
   GitHub is not a Python package index, so connectors consumers must provide
   both the ddp-lib and ddp-connectors URLs. Private repositories need Git read access.

Patch releases in 0.2.x preserve the documented runtime/API contract. Incompatible
changes require a new minor release while the packages are below version 1.0.
Update metadata, changelog, smoke version assertions and the consumer matrix together.
Each package has its own version. Select a ddp-lib tag whose package version meets
`ddp-connectors`' declared dependency range.

References: [pip Git installations](https://pip.pypa.io/en/stable/topics/vcs-support/)
and [GitHub release creation](https://cli.github.com/manual/gh_release_create).
