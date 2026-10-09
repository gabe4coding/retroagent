# retroagent data

The private data of [retroagent](https://github.com/gabe4coding/retroagent): Claude Code and Codex sessions and memory
files synced from every machine, and the project pages and weekly retros the cloud routine writes from them.

Keep this repo **private**. The sync redacts secrets and scans every commit it makes with gitleaks, but the
sessions still hold your prompts, file paths and the output of your tools.

- `sessions/<host>/`, `raw/<host>/`, `catalog/<host>/`, `memories/<host>/` — written by `kb sync` on the machine that
  owns `<host>`. Never edit them by hand.
- `decisions/<host>/` — your answers to the suggestions of the retros, from `kb decide` on the machine that owns
  `<host>`.
- `pages/` — written by the cloud routine. Only `pages/config.json` is yours to edit, and your own entries of
  `pages/decisions.json`.
- `.github/workflows/pages-trigger.yml` — fires the routine; `kb setup routine` writes it.

Read it with `kb` (`kb --help`), not by opening files. Set up a machine with the `retroagent:setup` skill, or see the
retroagent README.
