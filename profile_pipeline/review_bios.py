"""Checks on generated CHI bios (profile_pipeline/output/chi_bios*.jsonl) and a side-by-side review file.
  python3 profile_pipeline/review_bios.py [--in output/chi_bios_sample.jsonl] [--show 20]
Checks: length, Markdown/word-count leaks, banned frames from summary_v2, provenance words, personal
names of the covered people leaking in, needs_review rate by single/multi-paper, and (if the local MiniLM
cache exists) cosine distance summary↔source like hfe-peapod's comparison."""
import argparse, collections, csv, json, re, statistics, unicodedata
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
FRAMES = re.compile(r"\b(this research|the work|research focuses on|the central question|rather than|not merely|not only .{0,40} but)\b", re.I)
PROVENANCE = re.compile(r"\b(CHI\b|conference|this paper|the paper|these papers|abstract|presentation|presented at|the source|available information|inferred)\b", re.I)
def fold(s): return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--in", dest="inp", default=str(ROOT / "profile_pipeline/output/chi_bios_sample.jsonl")); ap.add_argument("--show", type=int, default=20)
    a = ap.parse_args()
    rows = [json.loads(l) for l in open(a.inp) if l.strip()]
    names = {r["Person ID"]: r["Author"] for r in csv.DictReader(open(ROOT / "authors.csv", newline="", encoding="utf-8-sig"))}
    for r in rows:
        s = r["summary"]; f = []
        if "**" in s or re.search(r"word count", s, re.I): f.append("markdown/word-count leak")
        if r["word_count"] < 60: f.append(f"short: {r['word_count']} words")
        if r["word_count"] > 130: f.append(f"long: {r['word_count']} words")
        if m := FRAMES.findall(s): f.append("banned frame: " + ", ".join(sorted(set(x.lower() if isinstance(x, str) else x[0].lower() for x in m))))
        if m := PROVENANCE.findall(s): f.append("provenance: " + ", ".join(sorted(set(x.lower() for x in m))))
        leaked = [names[p] for p in r["person_ids"] if p in names and any(len(tok) > 3 and re.search(r"\b" + re.escape(tok) + r"\b", fold(s)) for tok in fold(names[p]).split())]
        if leaked: f.append("name leak: " + ", ".join(leaked[:3]))
        if "\n" in s: f.append("multi-paragraph")
        r["flags"] = f
    n = len(rows); wc = [r["word_count"] for r in rows]
    print(f"n={n} texts covering {sum(len(r['person_ids']) for r in rows)} people | words median {statistics.median(wc)} min {min(wc)} max {max(wc)} | in 90-120: {sum(90<=w<=120 for w in wc)}")
    for grp, sel in (("single-paper", [r for r in rows if r["papers"] == 1]), ("multi-paper", [r for r in rows if r["papers"] > 1])):
        print(f"{grp}: n={len(sel)} needs_review={sum(r['needs_review'] for r in sel)} ({100*sum(r['needs_review'] for r in sel)/max(1,len(sel)):.0f}%) median words {statistics.median([r['word_count'] for r in sel]) if sel else '-'}")
    c = collections.Counter(re.sub(r": .*", "", x) for r in rows for x in r["flags"]); print("flags:", dict(c))
    reasons = collections.Counter(re.sub(r"[^a-z ]", "", x.lower())[:60] for r in rows for x in r["review_reasons"]); print("top review reasons:", reasons.most_common(5))
    try:
        from sentence_transformers import SentenceTransformer; import numpy as np
        m = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2", local_files_only=True)
        A = m.encode([r["source"] for r in rows], normalize_embeddings=True); B = m.encode([r["summary"] for r in rows], normalize_embeddings=True)
        T = m.encode([r["source"].split("\n")[0][:200] for r in rows], normalize_embeddings=True)
        d = 1 - (A * B).sum(1); d0 = 1 - (A * T).sum(1)
        print(f"cosine distance summary↔source: mean {d.mean():.3f} (title-only baseline {d0.mean():.3f}); HFES v2 was 0.057")
    except Exception as e: print("embedding check skipped:", str(e)[:80])
    out = Path(a.inp).with_suffix(".review.md"); rows.sort(key=lambda r: (-len(r["flags"]), -r["needs_review"]))
    pick = [r for r in rows if r["papers"] == 1][: a.show // 2] + [r for r in rows if r["papers"] > 1][: a.show // 2]
    with out.open("w") as fh:
        fh.write(f"# CHI bio sample review ({n} texts)\n\nFlagged first. Read SUMMARY against SOURCE.\n\n")
        for i, r in enumerate(pick, 1):
            fh.write(f"## {i}. {', '.join(names.get(p, p) for p in r['person_ids'][:3])}{' …' if len(r['person_ids'])>3 else ''} · {r['papers']} paper(s) · {r['word_count']} words\n")
            fh.write(f"**flags:** {'; '.join(r['flags']) or 'none'} · **needs_review:** {r['needs_review']} {r['review_reasons'][:2]}\n\n**SUMMARY:** {r['summary']}\n\n<details><summary>SOURCE</summary>\n\n{r['source'][:1500]}\n\n</details>\n\n")
    print("review file:", out)
if __name__ == "__main__": main()
