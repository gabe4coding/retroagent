"""The suggestion ledger: every change a weekly retro suggested, what the owner decided, and whether it worked.

A retro's "## Suggested changes" bullets start with an id in brackets:
  - [new] <category> · <the change: which file, check, command or tool> · signature "<error signature>" (<short>)
  - [s-1a2b3c] <…>                 the same problem as an earlier suggestion, which came back
`kb pages finish` gives each [new] bullet its id (s- and 6 hex digits) and records every bullet in
pages/suggestions.json, which only finish writes. The signature is optional: it is the `kb stats errors` signature of
the error the change should remove, and finish refuses one that no session has.

The owner records decisions by hand in pages/decisions.json, like pages/config.json:
  {"s-1a2b3c": {"state": "applied", "date": "2026-10-09", "note": "hook patterns fixed in PR 12"}}
with state accepted, applied or rejected. `kb suggestions` joins the two with the index: a suggestion with a signature
is measured by the sessions that still have its error. Nothing here writes outside pages/suggestions.json.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path

from kb import freshness
from kb.pages import section
from kb.util import atomic_write

SUGGESTIONS_REL = "pages/suggestions.json"
DECISIONS_REL = "pages/decisions.json"
SECTION = "Suggested changes"
STATES = ("accepted", "applied", "rejected")
CATEGORIES = ("Navigation", "Automated checks", "Rules", "Steering bloat and no-ops", "Tool economy",
              "Information access")
RECENT_DAYS = 14             # an open suggestion is "still happening" when its error was seen this recently
SETTLE_DAYS = 7              # an applied suggestion is "fixed" when its error was not seen for this long after it
_ID = re.compile(r"^s-[0-9a-f]{6}$")
_ITEM = re.compile(r"^- \[(new|s-[0-9a-f]{6})\]\s*(.*)$")
_SIGNATURE = re.compile(r'\s*·?\s*signature\s+"([^"]*)"', re.I)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def items(body: str) -> list:
    """The bullets of a retro's "Suggested changes" as dicts: line (index in the section), id ("new" or s-…),
    category, text, signature, sources. A bullet without an id in brackets has id ""."""
    part = section(body, SECTION) or ""
    out = []
    for i, line in enumerate(part.splitlines()[1:], 1):
        if not line.startswith("- "):
            continue
        m = _ITEM.match(line)
        rest = m.group(2) if m else line[2:]
        text, ref, _ = freshness.parse_tail("- " + rest)
        text = text[2:].strip()
        sig = _SIGNATURE.search(text)
        signature = sig.group(1).strip() if sig else ""
        if sig:
            text = (text[: sig.start()] + text[sig.end():]).strip()
        head = text.split(" · ", 1)[0].strip().strip("*").strip()
        category = next((c for c in CATEGORIES if c.casefold() == head.casefold()), "")
        if category:
            text = text.split(" · ", 1)[1].strip() if " · " in text else ""
        out.append({"line": i, "id": m.group(1) if m else "", "category": category, "text": text,
                    "signature": signature, "sources": ref.shorts if ref else []})
    return out


def check(rel: str, body: str, ledger: dict, signatures=None) -> list:
    """Problems of a retro's suggestions (empty when fine). signatures: the error signatures the index knows, or None
    to skip that check."""
    out = []
    for it in items(body):
        where = f"{rel}: suggested change {it['line']}"
        if not it["id"]:
            out.append(f"{where}: start it with [new], or with the [s-…] id from `kb suggestions` when it came back")
        elif it["id"] != "new" and it["id"] not in ledger:
            out.append(f"{where}: no suggestion {it['id']}; `kb suggestions --all` lists them, else write [new]")
        if not it["text"]:
            out.append(f"{where}: no change written")
        if it["signature"] and signatures is not None and it["signature"] not in signatures:
            out.append(f"{where}: no session has the error signature {it['signature']!r}; copy it from "
                       "`kb stats errors` or leave it out")
    return out


def new_id(week: str, text: str, taken) -> str:
    n = 0
    while True:
        sid = "s-" + hashlib.sha1(f"{week}\n{text}\n{n}".encode("utf-8")).hexdigest()[:6]
        if sid not in taken:
            return sid
        n += 1


def assign(body: str, week: str, ledger: dict):
    """(body with every [new] replaced by a fresh id, the items with their ids)."""
    found = items(body)
    head = SECTION.casefold()
    lines = body.split("\n")
    start = next((i for i, l in enumerate(lines) if l.startswith("## ") and l[3:].strip().casefold().startswith(head)),
                 None)
    taken = set(ledger)
    for it in found:
        if it["id"] == "new":
            it["id"] = new_id(week, it["text"], taken)
            n = start + it["line"]
            lines[n] = lines[n].replace("[new]", f"[{it['id']}]", 1)
        taken.add(it["id"])
    return "\n".join(lines), [it for it in found if it["id"]]


def record(ledger: dict, week: str, found: list) -> dict:
    """The ledger after a retro of `week` was (re)written with these items: the week's old entries are replaced."""
    out = {}
    for sid, e in ledger.items():
        weeks = [w for w in e.get("weeks", []) if w != week]
        if weeks:
            out[sid] = {**e, "weeks": weeks}
    for it in found:
        e = out.get(it["id"], {})
        weeks = sorted(set(e.get("weeks", [])) | {week})
        if week == weeks[-1] or not e:            # the newest retro's wording wins
            e = {"category": it["category"], "text": it["text"], "signature": it["signature"],
                 "sources": it["sources"]}
        out[it["id"]] = {**e, "weeks": weeks}
    return dict(sorted(out.items()))


def load(root) -> dict:
    """The ledger {id: {category, text, signature, sources, weeks}}, {} when there is none or it is unreadable."""
    data = _read(Path(root) / SUGGESTIONS_REL)
    sugg = data.get("suggestions") if isinstance(data, dict) else None
    if not isinstance(sugg, dict):
        return {}
    return {k: v for k, v in sugg.items() if _ID.match(str(k)) and isinstance(v, dict)
            and isinstance(v.get("weeks"), list) and v["weeks"]}


def save(root, ledger: dict) -> None:
    text = json.dumps({"version": 1, "suggestions": ledger}, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    atomic_write(Path(root) / SUGGESTIONS_REL, text.encode("utf-8"))


def decisions(root) -> dict:
    """The owner's decisions {id: {state, date, note}}; entries with an unknown state are left out."""
    data = _read(Path(root) / DECISIONS_REL)
    out = {}
    for sid, d in (data.items() if isinstance(data, dict) else ()):
        if not isinstance(d, dict) or d.get("state") not in STATES:
            continue
        date = d.get("date") if isinstance(d.get("date"), str) and _DATE.match(d.get("date")) else ""
        note = d.get("note") if isinstance(d.get("note"), str) else ""
        out[sid] = {"state": d["state"], "date": date, "note": note}
    return out


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def report(ledger: dict, decided: dict, seen: dict, today: dt.date) -> list:
    """One dict per suggestion: id, state, weeks, category, text, signature, verdict, and sort keys. seen: the
    signature_sessions() of the ledger's signatures."""
    out = []
    recent_from = (today - dt.timedelta(days=RECENT_DAYS)).isoformat()
    for sid, e in ledger.items():
        d = decided.get(sid, {})
        state = d.get("state", "proposed")
        sig = e.get("signature") or ""
        starts = sorted(seen.get(sig, {}).values()) if sig else []
        rank = 2
        if state == "rejected":
            verdict, rank = "rejected", 4
        elif not sig:
            verdict = "not measured (no error signature)"
        elif state == "applied" and d.get("date"):
            after = [s for s in starts if s[:10] >= d["date"]]
            if after:
                verdict, rank = f"came back: {len(after)} sessions since {d['date']}, last {after[-1][:10]}", 0
            elif (today - dt.date.fromisoformat(d["date"])).days >= SETTLE_DAYS:
                verdict, rank = f"fixed: not seen since {d['date']}", 3
            else:
                verdict = f"applied {d['date']}: too early to tell"
        else:
            recent = [s for s in starts if s[:10] >= recent_from]
            if recent:
                verdict, rank = f"still happening: {len(recent)} sessions in {RECENT_DAYS} days, last {recent[-1][:10]}", 1
            else:
                verdict = f"not seen in {RECENT_DAYS} days" + (f" (last {starts[-1][:10]})" if starts else "")
        if state == "applied" and not d.get("date"):
            verdict += "; give the decision a date to measure it"
        out.append({"id": sid, "state": state, "weeks": e["weeks"], "category": e.get("category", ""),
                    "text": e.get("text", ""), "signature": sig, "verdict": verdict, "note": d.get("note", ""),
                    "sources": e.get("sources", []), "_rank": rank})
    out.sort(key=lambda r: (r["weeks"][-1], r["id"]), reverse=True)        # newest first within a rank
    out.sort(key=lambda r: (r["_rank"], -len(r["weeks"])))
    for r in out:
        del r["_rank"]
    return out
