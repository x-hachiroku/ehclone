# Repository Guidelines

## Project Layout and Runtime

- `src/ehclone/main.py` initializes PostgreSQL and runs repeated passes: download
  management, index sync, thumbnail vectorization, then deduplication. Passes wait
  `pipeline_interval_seconds` before repeating and check downloads submitted by
  earlier passes.
- `core/` contains orchestration, EH requests, filtering, and archive validation.
- `db/entities.py` defines persisted models; `db/crud/` owns queries;
  `db/session.py` and `db/locking.py` manage transactions and the application lock.
- `downloader/` contains aria2 and qBittorrent adapters
- `vectorizer/` loads the image model lazily and computes thumbnail embeddings.
- `appdata/config.example.json` is the configuration template. Configuration is
  loaded at import time from `$APPDATA/config.json`; `APPDATA` defaults to
  `/appdata`. PostgreSQL with pgvector and the download services run separately.
- `tests/` contains isolated tests. Image samples live in
  `src/ehclone/vectorizer/tests/`.

## Coding Style

Target Python>=3.14. Prefer native Python type hints for new public functions.
Keep database access in `db/`, orchestration in `core/`, and service details
behind client modules. Use the shared Loguru logger and `gallery_logger` for
gallery context instead of `print`. No formatter or linter is configured; keep
imports and formatting consistent with nearby code.

## Commands and Verification

- `uv sync --dev` installs the locked environment; Python 3.14 or newer is required.
- `uv run pytest` runs the test suite without live databases or containers.
- `uv run pytest tests/test_validator.py` runs a focused test module.
- `APPDATA="$PWD/appdata" uv run python -m ehclone.main` runs against the configured
  services. It can submit downloads and write archives; use isolated services
  and paths for manual verification.
- `podman build -t localhost/ehclone .` builds the image and downloads the embedding model.
- `podman run --rm -v "$PWD/appdata:/appdata" ehclone` runs with local application
  data mounted; configure service URLs and shared download paths accordingly.

Run relevant tests while developing and the full suite before completing code
changes. Build the container for Dockerfile or packaging changes. Documentation
changes need a content review and `git diff --check`, not a full test run. Report
what was verified and any skipped or unavailable checks.

Archive integration tests use installed `7z`, `zip`, and `rar` executables and may
skip when tools are missing; inspect skips when changing extraction or packaging.
Keep the image's tools compatible with both the default archiver and the example
configuration. Install project dependencies before copying source only with
`--no-install-project`; install the application after the source is present.
