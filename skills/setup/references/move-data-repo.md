# Move to another data repo

`kb disable`, and wait until `kb status` shows no running sync. Then either rename the repo on GitHub
(`gh repo rename`; then `git -C <data root> remote set-url origin <new url>`), or copy it to a new private repo:
create it (the **Data repo** step of `SKILL.md`), then after a yes `git -C <data root> push <new url> main` and
`git -C <data root> remote set-url origin <new url>`. Run `kb setup routine` again and update the routine's sources;
a new repo also needs the two secrets again. If the user set up cloud environments, run `kb setup cloud` again:
its setup script and repositories name the data repo, so the user updates each environment and the routine.
`kb enable` at the end.
