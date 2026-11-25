"""Second phone session: re-enter the existing Vianney profile (claim path), then home overview, person card,
map with one zoom step, the view picker used on screen (Student, Hobby), and the FAQ. python3 video/record_app2.py"""
from playwright.sync_api import sync_playwright
import os, shutil, time, json, re
out="/home/rena/projects/chi-peapod/video/build/app2"; shutil.rmtree(out, ignore_errors=True); os.makedirs(out)
marks=[]; t0=None
def mark(pg,name,shot=True):
    marks.append((round(time.time()-t0,2),name)); print(f"[{marks[-1][0]:6.1f}s] {name}", flush=True)
    if shot: pg.screenshot(path=f"{out}/{len(marks):02d}_{name}.png")
def active(pg, s): return pg.locator(f'[data-screen="{s}"].active').count()>0
with sync_playwright() as p:
    b=p.chromium.launch(); ctx=b.new_context(viewport={"width":390,"height":844}, device_scale_factor=2, record_video_dir=out, record_video_size={"width":780,"height":1688}, has_touch=True, is_mobile=True)
    pg=ctx.new_page(); t0=time.time(); pg.goto("http://127.0.0.1:8765/"); pg.wait_for_timeout(2000)
    pg.get_by_role("button", name="Continue").first.click(); pg.wait_for_function("document.getElementById('inName') && !document.getElementById('inName').disabled", timeout=60000); pg.wait_for_timeout(800)
    pg.click("#inName"); pg.type("#inName","Vianney Renata",delay=40); pg.click("#inAff"); pg.type("#inAff","University of Wisconsin-Madison",delay=25); pg.evaluate("typeof hideAffOptions==='function' && hideAffOptions()"); pg.wait_for_timeout(500)
    pg.click("#findBtn")
    for i in range(40):
        pg.wait_for_timeout(500)
        if active(pg,"profile") or active(pg,"typo"): break
    if active(pg,"typo"): pg.locator('[data-screen="typo"] .row').first.click(); pg.wait_for_timeout(1500)
    pg.wait_for_timeout(1000); mark(pg,"found")
    pg.get_by_role("button", name=re.compile("that's me", re.I)).first.click()
    for i in range(240):
        pg.wait_for_timeout(1000)
        if active(pg,"askhobby"): pg.get_by_role("button", name=re.compile("Skip")).first.click(); pg.wait_for_timeout(1500)
        if active(pg,"askbio"): pg.get_by_role("button", name=re.compile("take me to the map", re.I)).first.click()
        if pg.locator("#topMatches .rowname").first.is_visible(): break
    pg.wait_for_timeout(1000); pg.evaluate("window.scrollTo({top:0})"); pg.wait_for_timeout(7000); mark(pg,"home_overview")
    pg.evaluate("document.getElementById('topMatchesTitle').scrollIntoView({behavior:'smooth', block:'start'})"); pg.wait_for_timeout(4000); mark(pg,"matches")
    pg.locator("#topMatches .rowname").first.click(); pg.wait_for_timeout(6000); mark(pg,"person_card")
    sb=pg.get_by_role("button", name=re.compile("save", re.I)).first
    if sb.is_visible(): sb.click(); pg.wait_for_timeout(3000); mark(pg,"saved")
    cl=pg.get_by_role("button", name=re.compile("close", re.I)).first
    if cl.is_visible(): cl.click(); pg.wait_for_timeout(1500)
    pg.evaluate("document.getElementById('mapStage').scrollIntoView({behavior:'smooth', block:'center'})"); pg.wait_for_timeout(5000); mark(pg,"map")
    pg.locator("button[aria-label='Zoom in']").first.click(); pg.wait_for_timeout(6000); mark(pg,"map_zoomed")
    pg.evaluate("window.scrollTo({top:0,behavior:'smooth'})"); pg.wait_for_timeout(1500)
    pg.click("#viewModeToggle"); pg.wait_for_timeout(2500); mark(pg,"picker")
    pg.get_by_role("button", name=re.compile("Student")).first.click(); pg.wait_for_timeout(8000); mark(pg,"student_view")
    pg.click("#viewModeToggle"); pg.wait_for_timeout(2000)
    pg.get_by_role("button", name=re.compile("Hobby")).first.click(); pg.wait_for_timeout(10000); mark(pg,"hobby_view")
    pg.click("#viewModeToggle"); pg.wait_for_timeout(1500); pg.get_by_role("button", name=re.compile("Academic|Everyone")).first.click(); pg.wait_for_timeout(1500)
    pg.evaluate("openFaq()"); pg.wait_for_timeout(2000)
    q=pg.get_by_text("How can I actually meet").first
    if q.is_visible(): q.scroll_into_view_if_needed(); pg.wait_for_timeout(300); q.click()
    pg.wait_for_timeout(6000); mark(pg,"faq_no_messaging")
    ctx.close(); b.close()
v=[f for f in os.listdir(out) if f.endswith(".webm")][0]; os.rename(f"{out}/{v}", f"{out}/app2.webm")
json.dump(marks, open(f"{out}/marks.json","w")); print("app2 done")
