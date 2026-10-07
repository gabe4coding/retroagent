# Pages routine (setup step 12)

Explain in two lines: a cloud routine on claude.ai writes one page per project and one retro per closed week into
the data repo, fired by a GitHub workflow in the data repo at most every 3 hours (routine runs count against the
account's daily routine limit). Ask now, later or no. For now:

- `kb setup routine` pushes `pages/config.json` (kept when present) and the trigger workflow, and prints JSON:
  `routine` (the create body), `secrets`, `test`.
- Claude Code: `RemoteTrigger list`. If a routine already has this data repo in its sources, offer to update it
  (`RemoteTrigger update` with the spec's `job_config`). Else take `environment_id` from an existing routine (ask
  when there are several, or ask the user to create any routine at claude.ai/code/routines first), show name,
  repos and model, and after a yes `RemoteTrigger create` with the spec. Other agents: give the user the values to
  create it at https://claude.ai/code/routines (both repos, the prompt, the model, no schedule, no connectors).
- The user opens the routine page, adds an **API** trigger and copies its URL and token. Then, in their own
  terminal, they run the two `secrets` commands and paste the values when asked.
- Test, after a yes: the `test` command fires it once. The first run writes to the `claude/pages-bootstrap`
  branch and keeps a draft pull request; when it is ready, the user reviews and merges it, and later runs write
  to `main`.
