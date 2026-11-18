# Publishing

Steps to turn this local project into a public repo and a `pip install`-able
package. No CI is configured by default.

## 1. Check the names

- **PyPI distribution: `codex-usage-limits`** — `pip install codex-usage-limits`.
- Import package: `codex_usage` under `src/`.
- Console command: `codex-usage`.
- GitHub repo: `javaisbetterthanpython/codex-usage`.

The PyPI name `codex-usage` belongs to another project. Keep the badge and
install commands on `codex-usage-limits`. The Homepage and Issues URLs and
contributor author are already filled in.

## 2. Run the tests locally

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e . pytest
.venv/bin/pytest -q
.venv/bin/codex-usage --help
.venv/bin/codex-usage --version
```

Tests use temporary config directories and mock the HTTP boundary. For a live
protocol check, run the [one-liner](HOW_THIS_WAS_BUILT.md#9-how-to-re-verify-quickly-one-liner-macos-and-linux) once.
On Windows, use `.venv\Scripts\python` / `pip` / `pytest` / `codex-usage` instead.

## 3. Create the public GitHub repo

Run from the project root when ready to publish:

```bash
gh repo create javaisbetterthanpython/codex-usage --public --source=. --remote=origin --push
```

The README embeds `docs/demo.gif` by its raw GitHub URL, so it renders on PyPI
too once the repo is public.

## 4. Publish to PyPI

Activate the venv (`source .venv/bin/activate`) first, then:

```bash
python -m pip install --upgrade build twine
python -m build                 # creates dist/*.whl and dist/*.tar.gz
twine check dist/*
twine upload dist/*
```

Configure your PyPI upload token in twine's credential store. Keep the token
out of shell history, command arguments, and this repo. The installed CLI
has no dependencies.

Then anyone can:

```bash
pip install codex-usage-limits
codex-usage
```

## 5. Versioning

Bump `version` in both `pyproject.toml` and `src/codex_usage/__init__.py`, commit,
tag `vX.Y.Z`, and push the commit and tag:

```bash
git tag v0.1.0
git push origin main
git push origin v0.1.0
```

<details>
<summary>Optional: automate PyPI releases on tag (requires GitHub Actions)</summary>

If you later want automated releases, add `.github/workflows/release.yml`
using PyPI Trusted Publishing (OIDC):

```yaml
name: release
on:
  push:
    tags: ["v*"]
permissions:
  id-token: write
jobs:
  pypi:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install build && python -m build
      - uses: pypa/gh-action-pypi-publish@release/v1
```

Configure the matching Trusted Publisher on PyPI for project **codex-usage-limits**,
repository **javaisbetterthanpython/codex-usage**, and that workflow filename.
No workflow is included in the initial project.
</details>

## Note on data & secrets

Nothing account-specific belongs in this repo. Virtualenvs, build output,
egg-info, and caches are git-ignored. Credentials and usage caches stay in
Codex's local config directory, outside the repo; tokens are read only
at runtime and are never committed.
