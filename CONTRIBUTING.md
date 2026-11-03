# Contributing

```sh
python3.14 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
ruff check . && pytest
```

Reports for inverter models other than the SUN-15K-SG05LP3 are the most useful
contribution: attach the diagnostics file, and say which values look wrong.

Never put a real serial number or Bluetooth address in an issue, a test or a commit.
