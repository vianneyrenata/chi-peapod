"""Record the phone walkthrough of the local app (devproxy on :8765) with time marks. python3 video/record_app.py"""
from playwright.sync_api import sync_playwright
import os, shutil, time, json, re
out="/home/rena/projects/chi-peapod/video/build/app"; shutil.rmtree(out, ignore_errors=True); os.makedirs(out)
marks=[]; t0=None
def mark(pg,name,shot=True):
    marks.append((round(time.time()-t0,2),name)); print(f"[{marks[-1][0]:6.1f}s] {name}", flush=True)
    if shot: pg.screenshot(path=f"{out}/{len(marks):02d}_{name}.png")
with sync_playwright() as p:
    b=p.chromium.launch(); ctx=b.new_context(viewport={"width":390,"height":844}, device_scale_factor=2, record_video_dir=out, record_video_size={"width":780,"height":1688}, has_touch=True, is_mobile=True)
    pg=ctx.new_page(); t0=time.time(); pg.goto("http://127.0.0.1:8765/"); pg.wait_for_timeout(2500); mark(pg,"splash")
    pg.get_by_role("button", name="Continue").first.click(); pg.wait_for_timeout(800)
    pg.wait_for_function("document.getElementById('inName') && !document.getElementById('inName').disabled", timeout=60000); pg.wait_for_timeout(800); mark(pg,"keyin")
    pg.click("#inName"); pg.type("#inName","Yoshio Ishiguro",delay=90); pg.wait_for_timeout(400)
    pg.click("#inAff"); pg.type("#inAff","The University of Tokyo",delay=60); pg.wait_for_timeout(600); pg.evaluate("typeof hideAffOptions==='function' && hideAffOptions()"); mark(pg,"typed")
    pg.click("#findBtn"); pg.wait_for_timeout(2500); mark(pg,"found")
    pg.get_by_text("Yes, that's me").first.click(timeout=15000); mark(pg,"claim_tap",shot=False)
    for i in range(180):
        pg.wait_for_timeout(1000)
        if pg.locator('[data-screen="askhobby"].active').count() or pg.locator("#topMatches .rowname").first.is_visible(): break
    mark(pg,"after_claim")
    if pg.locator('[data-screen="askhobby"].active').count():
        ta=pg.locator("textarea:visible").first; ta.click(); pg.keyboard.type("history, hiking", delay=80); pg.wait_for_timeout(500); mark(pg,"hobby_typed")
        pg.get_by_role("button", name=re.compile("Continue")).first.click(); pg.wait_for_timeout(1500); mark(pg,"askbio")
        pg.get_by_role("button", name=re.compile("take me to the map", re.I)).first.click()
    for i in range(180):
        pg.wait_for_timeout(1000)
        if pg.locator("#topMatches .rowname").first.is_visible(): break
    pg.wait_for_timeout(2500); mark(pg,"home")
    pg.locator("#topMatches .rowname").first.click(); pg.wait_for_timeout(2000); mark(pg,"person_card")
    sb=pg.get_by_role("button", name=re.compile("save", re.I)).first
    if sb.is_visible(): sb.click(); pg.wait_for_timeout(1200); mark(pg,"saved")
    cl=pg.get_by_role("button", name=re.compile("close", re.I)).first
    if cl.is_visible(): cl.click(); pg.wait_for_timeout(800)
    pg.evaluate("document.getElementById('matchesAndMap').scrollIntoView({behavior:'smooth'})"); pg.wait_for_timeout(1500); mark(pg,"map")
    pg.evaluate("zoomActive(1)"); pg.wait_for_timeout(900); pg.evaluate("zoomActive(1)"); pg.wait_for_timeout(1500); mark(pg,"map_zoomed")
    pg.evaluate("window.scrollTo({top:0,behavior:'smooth'})"); pg.wait_for_timeout(1200)
    pg.evaluate("setViewMode('student')"); pg.wait_for_timeout(3000); mark(pg,"student_view")
    pg.evaluate("setViewMode('side')"); pg.wait_for_timeout(3500); mark(pg,"hobby_view")
    pg.evaluate("setViewMode('everyone')"); pg.wait_for_timeout(1500)
    pg.evaluate("openFaq()"); pg.wait_for_timeout(1000)
    q=pg.get_by_text("How can I actually meet").first
    if q.is_visible(): q.scroll_into_view_if_needed(); pg.wait_for_timeout(300); q.click()
    pg.wait_for_timeout(2500); mark(pg,"faq_no_messaging")
    ctx.close(); b.close()
v=[f for f in os.listdir(out) if f.endswith(".webm")][0]; os.rename(f"{out}/{v}", f"{out}/app.webm")
json.dump(marks, open(f"{out}/marks.json","w")); print("app done")
