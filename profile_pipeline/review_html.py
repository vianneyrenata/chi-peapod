"""Self-contained HTML to browse generated CHI bios: per person(s) the new paragraph, the current
displayed bio, their papers (session, time, room, DOI link), flags, model review reasons, and the full
source abstracts. Filters, search and sorting are client-side; no dependencies.
  python3 profile_pipeline/review_html.py [--in output/chi_bios_sample_full.jsonl]"""
import argparse, csv, json, re, unicodedata
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
FRAMES = re.compile(r"\b(this research|the work|research focuses on|the central question|rather than|not merely)\b", re.I)
def fold(s): return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--in", dest="inp", default=str(ROOT / "profile_pipeline/output/chi_bios_sample_full.jsonl")); a = ap.parse_args()
    rows = [json.loads(l) for l in open(a.inp) if l.strip()]
    people = {r["Person ID"]: r for r in csv.DictReader(open(ROOT / "authors.csv", newline="", encoding="utf-8-sig"))}
    prog = {}
    for p in csv.DictReader(open(ROOT / "program.csv", newline="", encoding="utf-8-sig")):
        prog.setdefault(p["Presentation Title"], []).append(p)
    items = []
    for r in rows:
        s = r["summary"]; flags = []
        if "**" in s or re.search(r"word count", s, re.I): flags.append("markdown leak")
        if r["word_count"] < 60: flags.append(f"short ({r['word_count']}w)")
        if r["word_count"] > 130: flags.append(f"long ({r['word_count']}w)")
        fr = sorted({m.lower() for m in FRAMES.findall(s)})
        if fr: flags.append("frame: " + ", ".join(fr))
        if re.search(r"\b(unable|cannot be generated|insufficient source)\b", s, re.I): flags.append("refusal text")
        ppl = []
        for pid in r["person_ids"]:
            p = people.get(pid)
            if not p: continue
            if any(len(t) > 3 and re.search(r"\b" + re.escape(t) + r"\b", fold(s)) for t in fold(p["Author"]).split()): flags.append("name leak: " + p["Author"])
            papers = []
            for t in [x for x in p["Presentation Titles"].split("; ") if x.strip()]:
                ses = prog.get(t, [])
                papers.append({"title": t, "sessions": [{"when": f"{x['Day'][:3]} {x['Date'][5:]} {x['Start Time']}", "room": x["Location"], "session": x["Session Name"], "type": x["Session Type"], "url": x["Program URL"]} for x in ses]})
            ppl.append({"id": pid, "name": p["Author"], "aff": p["Affiliation"], "current": p["Interest Profile"], "papers": papers})
        items.append({"hash": r["source_hash"][:10], "people": ppl, "papers": r["papers"], "words": r["word_count"], "summary": s,
                      "needs_review": r["needs_review"], "reasons": r["review_reasons"], "flags": flags, "source": r["source"]})
    html = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CHI bio sample review</title>
<style>
:root{--ink:#1d1d2b;--gray:#6b6b7e;--line:#e4e4ee;--blue:#1858b0;--tint:#eef4fc;--warn:#fff6e5;--warn-ink:#8a5f00;--bad:#fde8e8;--bad-ink:#b42318}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--ink);background:#f6f6fa}
header{position:sticky;top:0;background:#fff;border-bottom:1px solid var(--line);padding:12px 20px;z-index:2}
h1{font-size:18px;margin:0 0 8px}.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
input[type=search]{flex:1;min-width:220px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;font:inherit}
label.chip{display:inline-flex;gap:6px;align-items:center;padding:6px 10px;border:1px solid var(--line);border-radius:999px;background:#fff;cursor:pointer;font-size:13px;user-select:none}
label.chip input{margin:0}select{padding:6px 8px;border:1px solid var(--line);border-radius:8px;font:inherit}
.count{color:var(--gray);font-size:13px;margin-left:auto}
main{max-width:1100px;margin:0 auto;padding:16px 20px 60px}
.card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin:0 0 14px}
.card.hidden{display:none}.top{display:flex;flex-wrap:wrap;gap:6px 12px;align-items:baseline;margin-bottom:6px}
.who{font-weight:700;font-size:16px}.aff{color:var(--gray);font-size:13px}.meta{color:var(--gray);font-size:12px;margin-left:auto;white-space:nowrap}
.tag{display:inline-block;font-size:11px;font-weight:600;padding:2px 8px;border-radius:999px;background:var(--tint);color:var(--blue);margin:2px 4px 2px 0}
.tag.warn{background:var(--warn);color:var(--warn-ink)}.tag.bad{background:var(--bad);color:var(--bad-ink)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:10px}@media(max-width:760px){.cols{grid-template-columns:1fr}}
.box{border:1px solid var(--line);border-radius:10px;padding:10px 12px;background:#fbfbfe}.box h4{margin:0 0 6px;font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--gray)}
.summary{font-size:15px}.current{white-space:pre-line;font-size:13.5px;color:#333}
.papers{margin:10px 0 0;padding:0;list-style:none}.papers li{padding:6px 0;border-top:1px dashed var(--line);font-size:13.5px}
.papers .t{font-weight:600}.papers .s{color:var(--gray);font-size:12.5px}.papers a{color:var(--blue);text-decoration:none;font-size:12.5px;margin-left:6px}
details{margin-top:10px}summary{cursor:pointer;color:var(--blue);font-size:13px;font-weight:600}.src{white-space:pre-wrap;font-size:13px;color:#333;background:#fbfbfe;border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-top:6px}
.reasons{font-size:13px;color:var(--warn-ink);background:var(--warn);border-radius:8px;padding:8px 10px;margin-top:8px}.reasons li{margin:2px 0}
mark{background:#fff3b0;padding:0 1px}.legend{font-size:12.5px;color:var(--gray);margin:0 0 14px}
</style></head><body>
<header><h1>CHI bio sample review · <span id="n"></span> texts</h1>
<div class="bar"><input type="search" id="q" placeholder="Search name, affiliation, paper title, summary…">
<label class="chip"><input type="checkbox" id="fReview"> needs_review only</label>
<label class="chip"><input type="checkbox" id="fFlag"> flagged only</label>
<select id="fPapers"><option value="">single + multi</option><option value="1">single-paper</option><option value="2">multi-paper</option></select>
<select id="sort"><option value="flags">sort: most flags first</option><option value="words">sort: shortest first</option><option value="wordsd">sort: longest first</option><option value="name">sort: name</option></select>
<span class="count" id="count"></span></div></header>
<main><p class="legend">Left: the generated paragraph (what would go into <code>s_profile</code>). Right: what the app shows today. Papers list each item's session, time, room and link. Tags: <span class="tag">single/multi</span> <span class="tag warn">needs_review</span> (the model's own doubt) <span class="tag bad">check</span> (automatic flags: length, banned framing, name leak, refusal text). Open "Source" to read the abstracts the paragraph was written from.</p>
<div id="list"></div></main>
<script>
const DATA = __DATA__;
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const hl = (s, q) => { s = esc(s); if (!q) return s; const re = new RegExp('(' + q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'ig'); return s.replace(re, '<mark>$1</mark>'); };
function card(it, q){
  const names = it.people.map(p => `${hl(p.name,q)} <span class="aff">${hl(p.aff,q)}</span>`).join(' · ');
  const papers = (it.people[0]?.papers || []).map(p => `<li><div class="t">${hl(p.title,q)}</div>${p.sessions.map(s => `<div class="s">${esc(s.type)} · ${esc(s.session)} · ${esc(s.when)}${s.room ? ' · ' + esc(s.room) : ''}${s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noopener">link ↗</a>` : ''}</div>`).join('') || '<div class="s">not in a scheduled session</div>'}</li>`).join('');
  const tags = [`<span class="tag">${it.papers > 1 ? 'multi' : 'single'}-paper</span>`, `<span class="tag">${it.words} words</span>`,
    it.needs_review ? '<span class="tag warn">needs_review</span>' : '', ...it.flags.map(f => `<span class="tag bad">${esc(f)}</span>`)].join('');
  const more = it.people.length > 1 ? `<div class="aff">${it.people.length} co-authors share this text</div>` : '';
  return `<div class="card" data-id="${it.hash}"><div class="top"><span class="who">${names}</span><span class="meta">#${it.hash}</span></div>${more}<div>${tags}</div>
  <div class="cols"><div class="box"><h4>Generated paragraph</h4><div class="summary">${hl(it.summary,q)}</div>${it.reasons.length ? `<div class="reasons"><b>Model's reasons:</b><ul>${it.reasons.map(r => `<li>${esc(r)}</li>`).join('')}</ul></div>` : ''}</div>
  <div class="box"><h4>Shown today</h4><div class="current">${hl(it.people[0]?.current || '', q)}</div></div></div>
  <ul class="papers">${papers}</ul>
  <details><summary>Source (titles + abstracts, ${it.source.split(/\s+/).length} words)</summary><div class="src">${hl(it.source,q)}</div></details></div>`;
}
function render(){
  const q = document.getElementById('q').value.trim().toLowerCase(), rv = document.getElementById('fReview').checked, fl = document.getElementById('fFlag').checked,
        pp = document.getElementById('fPapers').value, sort = document.getElementById('sort').value;
  let items = DATA.filter(it => (!rv || it.needs_review) && (!fl || it.flags.length) && (!pp || (pp === '1') === (it.papers === 1)));
  if (q) items = items.filter(it => [it.summary, it.source, ...it.people.flatMap(p => [p.name, p.aff, ...p.papers.map(x => x.title)])].join(' ').toLowerCase().includes(q));
  const key = { flags: it => -(it.flags.length * 10 + (it.needs_review ? 5 : 0)), words: it => it.words, wordsd: it => -it.words, name: it => it.people[0]?.name || '' }[sort];
  items.sort((a, b) => key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0);
  document.getElementById('list').innerHTML = items.map(it => card(it, q)).join('');
  document.getElementById('count').textContent = `${items.length} of ${DATA.length} shown · ${DATA.filter(i => i.needs_review).length} needs_review · ${DATA.filter(i => i.flags.length).length} flagged`;
}
document.getElementById('n').textContent = DATA.length;
for (const id of ['q','fReview','fFlag','fPapers','sort']) document.getElementById(id).addEventListener('input', render);
render();
</script></body></html>"""
    out = Path(a.inp).with_suffix(".html")
    out.write_text(html.replace("__DATA__", json.dumps(items).replace("</", "<\\/")), encoding="utf-8")
    print(out, f"{out.stat().st_size/1e6:.1f} MB, {len(items)} cards")
if __name__ == "__main__": main()
