"""The user docs (README.md and docs/*.mdx) follow the rules in CODING_STANDARDS.md, section "Docs".

Checked here: front matter and H1, GitHub-safe MDX, the mechanical ASD-STE100 rules, and relative links and heading
anchors (also the links from the files that point into the docs). Not checked: facts and word choice.
"""
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
USER_DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.mdx"))]
# Not written to the STE rules, but their links into the docs must keep working.
LINK_ONLY = [ROOT / "AGENTS.md", ROOT / "CODING_STANDARDS.md", *sorted((ROOT / "skills").rglob("*.md"))]

MAX_WORDS = 25
FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
FENCE = re.compile(r"^\s*(```|~~~)")
STE_RULES = [
    (re.compile(r"\b\w+n't\b|\b(it's|you're|we're|they're|that's|there's|let's)\b", re.I),
     'contraction: write the full form ("do not")'),
    (re.compile(r"\b(should|may|might|would|could)\b", re.I), 'modal verb: use "can", "must" or an imperative'),
    (re.compile(r"\b(please|simply|just|easily|obviously|basically)\b", re.I), "filler word"),
    (re.compile(r";"), "semicolon: write two sentences"),
    (re.compile(r"\b(e\.g\.|i\.e\.|etc\.)"), 'Latin abbreviation: write "for example" or "that is"'),
]


def prose_lines(text):
    """(line number, text) of each prose line: no front matter, code blocks, tables, headings or HTML.

    Inline code becomes X and a link keeps only its text, so neither counts against the rules.
    """
    match = FRONT_MATTER.match(text)
    offset = match.group(0).count("\n") if match else 0
    body = text[match.end():] if match else text
    fenced = False
    for number, raw in enumerate(body.split("\n"), start=offset + 1):
        if FENCE.match(raw):
            fenced = not fenced
            continue
        if fenced or re.match(r"^\s*(\||<|#)", raw):
            continue
        line = re.sub(r"`[^`]*`", "X", raw)
        line = re.sub(r"\]\([^)]*\)", "]", line).replace("**", "")
        yield number, line


def sentences(text):
    """The sentences of the prose. A sentence ends at . ! ? or : before a space, at a blank line or a list item."""
    prose = "\n".join(line for _, line in prose_lines(text))
    return re.split(r"(?<=[.!?:])\s+|\n\s*\n|\n\s*[-*]\s|\n\s*\d+\.\s", prose)


def slug(heading):
    """The anchor GitHub gives a heading."""
    heading = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading.replace("`", ""))
    kept = "".join(c for c in heading.lower() if c in " -_" or unicodedata.category(c)[0] in "LN")
    return kept.replace(" ", "-")


def anchors(path):
    found, seen, fenced = set(), {}, False
    for line in path.read_text(encoding="utf-8").split("\n"):
        if FENCE.match(line):
            fenced = not fenced
        heading = not fenced and re.match(r"^#{1,6}\s+(.*)$", line)
        if heading:
            base = slug(heading.group(1).strip())
            count = seen.get(base, 0)
            seen[base] = count + 1
            found.add(base if count == 0 else f"{base}-{count}")
    return found


def doc_problems(path):
    text = path.read_text(encoding="utf-8")
    rel = path.relative_to(ROOT)
    problems = []
    if path.suffix == ".mdx":
        match = FRONT_MATTER.match(text)
        meta = dict(re.findall(r"^(\w+):\s*(.+)$", match.group(1), re.M)) if match else {}
        if not meta.get("title") or not meta.get("description"):
            problems.append(f"{rel}:1: the front matter needs a title and a description")
        h1 = re.search(r"^# (.+)$", text[match.end():] if match else text, re.M)
        if meta.get("title") and (not h1 or h1.group(1) != meta["title"]):
            problems.append(f'{rel}: the H1 must be the front matter title "{meta.get("title")}"')

    fenced = False
    for number, raw in enumerate(text.split("\n"), start=1):
        if FENCE.match(raw):
            fenced = not fenced
        if fenced:
            continue
        line = re.sub(r"`[^`]*`", "", raw)
        if re.search(r"\{/\*|<!--", line):
            problems.append(f"{rel}:{number}: a comment that GitHub shows as text")
        if re.match(r"^(import|export)\s", line):
            problems.append(f"{rel}:{number}: import/export is not GitHub-safe")
        if re.search(r"<[A-Z][A-Za-z]*[\s/>]", line):
            problems.append(f"{rel}:{number}: a JSX component is not GitHub-safe")
        if re.search(r"<https?:", line):
            problems.append(f"{rel}:{number}: an autolink: write [text](url)")

    for number, line in prose_lines(text):
        for pattern, message in STE_RULES:
            if pattern.search(line):
                problems.append(f"{rel}:{number}: {message}")
    for sentence in sentences(text):
        words = len(sentence.split())
        if words > MAX_WORDS:
            problems.append(f'{rel}: a sentence of {words} words (max {MAX_WORDS}): "{sentence.strip()[:70]}…"')
    return problems


def link_problems(path):
    text = re.sub(r"```.*?```", "", path.read_text(encoding="utf-8"), flags=re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    rel = path.relative_to(ROOT)
    problems = []
    for match in re.finditer(r"\]\(([^)\s]+)\)|(?:src|href)=\"([^\"]+)\"", text):
        link = match.group(1) or match.group(2)
        if re.match(r"^[a-z]+:", link):
            continue
        target_path, _, anchor = link.partition("#")
        target = (path.parent / target_path).resolve() if target_path else path
        if not target.exists():
            problems.append(f"{rel}: a link to a missing file: {link}")
        elif anchor and target.suffix in (".md", ".mdx") and anchor not in anchors(target):
            problems.append(f"{rel}: a link to a missing heading: {link}")
    return problems


def test_slug_matches_github():
    assert slug("Every sync fails to pull") == "every-sync-fails-to-pull"
    assert slug("`kb find` and [search](x.mdx)") == "kb-find-and-search"
    assert slug("Speed, and summaries!") == "speed-and-summaries"


def test_the_checks_catch_each_rule(tmp_path):
    doc = tmp_path / "x.mdx"
    long = " ".join(["word"] * 26) + "."
    doc.write_text("---\ntitle: X\ndescription: Y\n---\n\n# Z\n\nIt's here; you should just look, e.g. now.\n\n"
                   + long + "\n\n`code; should` is fine.\n", encoding="utf-8")
    global ROOT
    root, ROOT = ROOT, tmp_path
    try:
        found = "\n".join(doc_problems(doc))
    finally:
        ROOT = root
    for message in ("H1 must be", "contraction", "modal verb", "filler word", "semicolon", "Latin", "26 words"):
        assert message in found, message
    assert ":12:" not in found


def test_user_docs_follow_the_rules():
    problems = [p for doc in USER_DOCS for p in doc_problems(doc)]
    assert not problems, "\n".join(problems + ["", 'The rules are in CODING_STANDARDS.md, section "Docs".'])


def test_links_and_anchors_resolve():
    problems = [p for path in USER_DOCS + LINK_ONLY for p in link_problems(path)]
    assert not problems, "\n".join(problems)
