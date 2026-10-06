"""Draft a name-free research-interest paragraph per DISTINCT CHI source text (titles + abstracts from
embed_text.json) with Haiku via `claude -p`, same prompt/schema/flags as hfe-peapod's summary_v2 run.
Checkpointed JSONL keyed by source hash; rerun = resume. --sample N picks N texts, half from
single-paper people and half from multi-paper people (seeded), so the review covers both cases.
  python3 profile_pipeline/generate_bios.py --sample 200 --workers 4
"""
import argparse, csv, hashlib, json, random, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPT = Path(__file__).resolve().parent / "summary_v2.txt"
MODEL = "claude-haiku-4-5-20251001"
SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}, "needs_review": {"type": "boolean"},
          "review_reasons": {"type": "array", "items": {"type": "string"}}},
          "required": ["summary", "needs_review", "review_reasons"], "additionalProperties": False}
PREFIX = "Source: the titles and abstracts of this person's contributions to CHI 2026.\n\n"

def sha(s): return hashlib.sha256(s.encode()).hexdigest()

def call(source):
    cmd = ["claude", "-p", "--model", MODEL, "--system-prompt-file", str(PROMPT), "--tools", "",
           "--output-format", "json", "--json-schema", json.dumps(SCHEMA), "--no-session-persistence",
           "--setting-sources", "", "--settings", '{"alwaysThinkingEnabled":false}']
    import os; env = dict(os.environ, MAX_THINKING_TOKENS="0")
    for attempt in range(5):
        try:
            done = subprocess.run(cmd, input=PREFIX + source, capture_output=True, text=True, cwd="/tmp", env=env, timeout=300)
            out = json.loads(done.stdout); parsed = out.get("structured_output")
            if done.returncode or out.get("is_error") or not isinstance(parsed, dict) or not isinstance(parsed.get("summary"), str):
                raise RuntimeError(f"claude -p failed: {out.get('subtype')} {done.stderr[:200]}")
            parsed["summary"] = re.sub(r"\s+", " ", parsed["summary"]).strip()
            return parsed
        except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired):
            if attempt == 4: raise
            time.sleep(min(2 ** attempt, 16))

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--sample", type=int, default=0); ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default=str(ROOT / "profile_pipeline/output/chi_bios.jsonl")); ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--full", action="store_true", help="use untruncated titles+abstracts from the feed (recommended)")
    a = ap.parse_args()
    embed = json.load(open(ROOT / "embed_text.json"))
    if a.full:
        # embed_text.json is cut at chi_feed.EMBED_CHARS (1500 chars; MiniLM reads no further), which
        # truncates every multi-paper source mid-sentence and makes Haiku refuse. Build the untruncated
        # titles + abstracts from the feed itself for generation; grouping by person stays identical.
        sys.path.insert(0, str(ROOT)); import chi_feed
        version = chi_feed._get_json(f"{chi_feed.FEED_BASE}/{chi_feed.EDITION}/version.json", timeout=20)
        feed = chi_feed._get_json(version["revision_url"])
        titles, abstracts = {}, {}
        for c in feed["contents"]:
            if c.get("isBreak"): continue
            for au in c.get("authors", []):
                titles.setdefault(au["personId"], []).append(chi_feed._clean(c["title"]))
                abstracts.setdefault(au["personId"], []).append(chi_feed._clean(c.get("abstract")))
        full = {str(p): " ".join(list(dict.fromkeys(titles[p])) + [x for x in abstracts.get(p, []) if x]) for p in titles}
        embed = {pid: (v[0], full.get(pid, v[1])) for pid, v in embed.items()}
    papers = {r["Person ID"]: len([t for t in r["Presentation Titles"].split("; ") if t.strip()])
              for r in csv.DictReader(open(ROOT / "authors.csv", newline="", encoding="utf-8-sig"))}
    by_text = {}
    for pid, (_, src) in embed.items():
        by_text.setdefault(src, []).append(pid)
    texts = list(by_text)
    if a.sample and a.full:
        # same seeded pick as the truncated run: choose on embed_text's (truncated) grouping, map to full text
        trunc = json.load(open(ROOT / "embed_text.json")); tb = {}
        for pid, (_, src) in trunc.items(): tb.setdefault(src, []).append(pid)
        rnd = random.Random(a.seed); tl = list(tb)
        single = [t for t in tl if max(papers.get(p, 1) for p in tb[t]) == 1]; ss = set(single)
        multi = [t for t in tl if t not in ss]
        chosen = rnd.sample(single, a.sample // 2) + rnd.sample(multi, a.sample - a.sample // 2)
        texts = list(dict.fromkeys(embed[tb[t][0]][1] for t in chosen))
    elif a.sample:
        rnd = random.Random(a.seed)
        single = [t for t in texts if max(papers.get(p, 1) for p in by_text[t]) == 1]
        multi = [t for t in texts if t not in set(single)]
        texts = rnd.sample(single, a.sample // 2) + rnd.sample(multi, a.sample - a.sample // 2)
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    done = {json.loads(l)["source_hash"] for l in out.open()} if out.exists() else set()
    todo = [t for t in texts if sha(t) not in done]
    print(f"{len(texts)} texts, {len(done)} done, {len(todo)} to go", file=sys.stderr)
    prompt_hash = sha(PROMPT.read_text())
    with out.open("a") as fh, ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(call, t): t for t in todo}
        for i, f in enumerate(as_completed(futs), 1):
            t = futs[f]
            try: r = f.result()
            except Exception as e: print("FAIL", sha(t)[:8], e, file=sys.stderr); continue
            fh.write(json.dumps({"source_hash": sha(t), "person_ids": by_text[t], "papers": max(papers.get(p, 1) for p in by_text[t]),
                                 "source": t, "summary": r["summary"], "needs_review": bool(r["needs_review"]),
                                 "review_reasons": r.get("review_reasons", []), "word_count": len(r["summary"].split()),
                                 "prompt_hash": prompt_hash, "model": MODEL}) + "\n"); fh.flush()
            if i % 20 == 0: print(f"{i}/{len(todo)}", file=sys.stderr)

if __name__ == "__main__": main()
