# Contributing

## Development

```bash
pip install -e . --group dev               # pip >= 25.1

ruff format --check . && ruff check .     # every rule enabled
mypy                                      # strict + extra error codes, Python 3.10 target
basedpyright                              # typeCheckingMode = "all", Python 3.10 target
pytest --cov                              # offline: in-memory fake API + local forward proxy
```

Live tests call the real AMLBot API. They only run when selected with `-m live`, and need credentials and an allowlisted IP address. Tests that spend credits need a second opt-in:

```bash
AMLBOT_ACCESS_ID=... AMLBOT_ACCESS_KEY=... AMLBOT_PROXY=http://egress:3128 pytest -m live
AMLBOT_LIVE_PAID=1 ... pytest -m live    # also runs a real address check (1 credit)
```

The CI (`.github/workflows/ci.yml`) runs the same checks on every push to `master` and every pull request, with the tests on Python 3.10 to 3.14.

## Releasing

1. Bump `__version__` in `src/amlbot/_version.py` and merge it into `master`.
2. Publish a GitHub release whose tag is that version prefixed with `v`, such as `v0.2.0`.

`.github/workflows/release.yml` then runs the CI again, checks that the tag matches the version, builds the sdist and wheel, publishes them to PyPI, and attaches them to the GitHub release.

Publishing uses PyPI [trusted publishing](https://docs.pypi.org/trusted-publishers/), so no API token is stored. It needs a one-time setup:

- on PyPI, add a trusted publisher for this repository, workflow `release.yml`, environment `pypi` (a "pending publisher" for the first release);
- on GitHub, create the `pypi` environment, optionally restricted to release tags or protected by required reviewers.
