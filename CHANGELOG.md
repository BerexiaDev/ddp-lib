# Changelog

## 0.2.0 — ready for release, not published yet

- Put the package settings in `pyproject.toml`.
- Support CPython 3.10–3.12.
- List Flask, Flask-PyMongo, PyMongo and python-dotenv as required libraries. Python-dotenv preserves local Core loading of repository `.env` settings.
- Make PyJWT 2 token functions return text and require a user ID stored as text. Check that tokens include the user ID, creation time and expiry time.
- Raise an error when a token cannot be created. Check blocked tokens the same way whether the input is text or bytes.
- Add automatic checks for building, installing, importing and compatible library versions. Also check token handling and the required PyMongo 3 APIs.
