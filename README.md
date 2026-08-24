# stock-dashboard

## Setup

Requires **Python 3.12** (pinned in `.python-version`). Python 3.13+ is not supported:
`numba` (a transitive dependency of `pandas-ta`) does not yet publish wheels for 3.13+,
so `pip install -r requirements.txt` fails at build time on newer interpreters.

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Tests

```bash
pytest
```
