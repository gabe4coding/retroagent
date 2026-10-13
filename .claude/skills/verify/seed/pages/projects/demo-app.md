---
kind: "project"
name: "demo-app"
updated: "2026-10-01T10:00:00Z"
sessions: 2
sources: ["1a2b3c4d", "5e6f7a8b"]
---

# demo-app

## Current state
- The parser reads the config file at start.

## Errors seen → fixes
- `make check` fails: ModuleNotFoundError no module named yaml → install the dev extras with `pip install -e .[dev]` (1a2b3c4d)
- Lock file conflict after rebase in the build cache folder → delete the stale lock and rerun (5e6f7a8b)

## Open threads
- Retry the flaky fetch test (5e6f7a8b)
