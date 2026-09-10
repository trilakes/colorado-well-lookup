"""QA pass over the generated pages before publishing.

Checks: duplicate titles/H1s, title and meta length, duplicated prose across
pages (shingled sentence overlap), and that every page has the funnel blocks
(lead form, Tri-Lakes block, feasibility section, FAQ schema, internal links).
Prints a report; exits non-zero if anything is broken enough to block publish.
"""
from __future__ import annotations

import io
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent / "wells"


def text_of(html: str) -> str:
    html = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def main() -> None:
    pages = sorted(p for p in ROOT.glob("*/index.html"))
    print(f"pages on disk: {len(pages)}")
    titles, h1s, metas = Counter(), Counter(), Counter()
    problems: list[str] = []
    sentence_owner: dict[str, str] = {}
    dup_sentences = defaultdict(set)
    words = []
    for p in pages:
        slug = p.parent.name
        html = p.read_text(encoding="utf-8")
        t = re.search(r"<title>(.*?)</title>", html, re.S)
        m = re.search(r'<meta name="description" content="(.*?)"', html)
        h = re.search(r"<h1>(.*?)</h1>", html, re.S)
        title = (t.group(1) if t else "").strip()
        meta = (m.group(1) if m else "").strip()
        h1 = re.sub(r"<[^>]+>", "", h.group(1) if h else "").strip()
        titles[title] += 1
        h1s[h1] += 1
        metas[meta] += 1
        if not (25 <= len(title) <= 70):
            problems.append(f"{slug}: title length {len(title)}: {title!r}")
        if not (60 <= len(meta) <= 170):
            problems.append(f"{slug}: meta length {len(meta)}")
        for block, label in (('id="tl-lead"', "lead form"), ('id="tri-lakes"', "Tri-Lakes block"),
                             ('id="feasibility"', "feasibility section"), ('"@type": "FAQPage"', "FAQ schema"),
                             ('rel="canonical"', "canonical"), ("trilakeshq.com/api/track.js", "HQ tracker")):
            if block not in html:
                problems.append(f"{slug}: missing {label}")
        internal = len(re.findall(r'href=[\'"]/wells/[a-z0-9-]+/', html))
        if internal < 4:
            problems.append(f"{slug}: only {internal} internal /wells/ links")
        if "None" in re.findall(r"<b>(.*?)</b>", html):
            problems.append(f"{slug}: a stat card rendered 'None'")
        # prose uniqueness: AI sections only (between hero and the permits table)
        body = text_of(html)
        words.append(len(body.split()))
        sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body) if len(s.split()) >= 9]
        for s in sents:
            key = re.sub(r"[^a-z ]", "", s.lower())
            if key in sentence_owner and sentence_owner[key] != slug:
                dup_sentences[key].add(slug)
                dup_sentences[key].add(sentence_owner[key])
            else:
                sentence_owner.setdefault(key, slug)

    # template sentences legitimately repeat (disclaimer, feasibility copy). Report only
    # sentences shared by a handful of pages, which indicates AI copy repeating itself.
    ai_dups = {k: v for k, v in dup_sentences.items() if 2 <= len(v) <= 40}
    template_dups = {k: v for k, v in dup_sentences.items() if len(v) > 40}
    print(f"duplicate titles: {sum(1 for c in titles.values() if c > 1)}   duplicate H1s: {sum(1 for c in h1s.values() if c > 1)}   duplicate metas: {sum(1 for c in metas.values() if c > 1)}")
    print(f"words per page: min {min(words)}, median {sorted(words)[len(words)//2]}, max {max(words)}")
    print(f"template sentences shared site-wide: {len(template_dups)} (expected: disclaimer, feasibility, Tri-Lakes copy)")
    print(f"AI sentences repeated across 2-40 pages: {len(ai_dups)}")
    for k, v in sorted(ai_dups.items(), key=lambda kv: -len(kv[1]))[:8]:
        print(f"   x{len(v)}: {k[:110]}")
    for t, c in titles.most_common(5):
        if c > 1:
            print(f"   dup title x{c}: {t}")
    print(f"\nproblems: {len(problems)}")
    for x in problems[:40]:
        print("  ", x)
    blocking = [x for x in problems if "missing" in x or "None" in x]
    sys.exit(1 if blocking else 0)


if __name__ == "__main__":
    main()
