import re
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1] / "skills"
LIMITS = {"setup": 90}                       # body line limit of a SKILL.md, 60 for a skill not named here


def test_skills_have_front_matter_and_stay_short():
    for skill in (p for p in SKILLS.iterdir() if p.is_dir()):
        text = (skill / "SKILL.md").read_text(encoding="utf-8")
        assert text.startswith(f"---\nname: {skill.name}\ndescription: "), skill.name
        body = text.split("\n---\n", 1)[1]
        assert len(body.splitlines()) < LIMITS.get(skill.name, 60), skill.name


def test_the_setup_skill_uses_only_commands_that_exist():
    text = (SKILLS / "setup" / "SKILL.md").read_text(encoding="utf-8")
    for cmd in ("kb setup check", "kb setup routine", "kb enable updates", "kb backfill --summaries", "install.sh --repo",
                "kb embed --status", "kb embed --off", "kb setup cloud", "kb cloud import --on",
                "kb setup repo-check", "kb backfill --recent", "kb disable"):
        assert cmd in text, cmd


def test_skill_references_exist_and_none_is_orphaned():
    for skill in (p for p in SKILLS.iterdir() if p.is_dir()):
        text = (skill / "SKILL.md").read_text(encoding="utf-8")
        mentioned = set(re.findall(r"references/[\w.-]+\.md", text))
        for ref in mentioned:
            assert (skill / ref).is_file(), f"{skill.name}: {ref}"
        refs = skill / "references"
        present = {f"references/{f.name}" for f in refs.iterdir()} if refs.is_dir() else set()
        assert present <= mentioned, f"{skill.name}: not named in SKILL.md: {sorted(present - mentioned)}"
