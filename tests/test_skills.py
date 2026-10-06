from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1] / "plugin" / "skills"


def test_skills_have_front_matter_and_stay_short():
    for name in ("kb-search", "kb-retro", "kb-stats"):
        text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        assert text.startswith(f"---\nname: {name}\ndescription: "), name
        body = text.split("\n---\n", 1)[1]
        assert len(body.splitlines()) < 60, name
