"""Cut the draft walkthrough: python3 video/assemble.py  ->  video/build/peapod-draft.mp4
Inputs in video/build/: intro/intro.webm, sigchi/sigchi.webm, app/app.webm + marks.json, card_*.png, bezel.png.
Captions are the narration from script.md, burnt in, so the draft can be judged before any voice is recorded."""
import json, subprocess, os, re
from pathlib import Path
B = Path(__file__).resolve().parent / "build"; T = B / "tmp"; T.mkdir(exist_ok=True)
marks = {n: t for t, n in json.load(open(B / "app/marks.json"))}
marks2 = {n: t for t, n in json.load(open(B / "app2/marks.json"))}
def m(name, d=0): return marks[name] + d
def m2(name, d=0): return marks2[name] + d
app2 = str(B / "app2/app2.webm"); meet = str(B / "meet/meet.webm")

# scene list: (source, start, end, target_seconds, caption)
app = str(B / "app/app.webm"); intro = str(B / "intro/intro.webm"); sig = str(B / "sigchi/sigchi.webm")
SCENES = [
  ("intro", intro, 0, 18.5, 18.5, "CHI is enormous now. Thousands of people, more fields than anyone can follow, a dozen things at once. "
     "You find the two or three people you already know, and you stay with them. Everyone else gets \"how was your travel\"."),
  ("phone", sig, 8, 15, 6.5, "The programme app can tell you what's on. It can't tell you who's here."),
  ("intro", intro, 25, 40, 15, "Peapod is people-first, built for connection. A map of everyone presenting at CHI, from their titles and abstracts, "
     "so people who work on similar things end up next to each other."),
  ("phone", app2, m2("home_overview", -6.5), m2("home_overview", 0.3), 7, "This is Peapod. Everyone presenting at CHI, on one map, and the people nearest to you listed under it."),
  ("phone", app, m("keyin", -2), m("build_tap", 1.0), 20, "You type your name. If you're in the programme, Peapod finds you and asks if that's you. One tap. "
     "If you're not presenting, you write a few sentences about your work and you're placed on the same map in a second."),
  ("phone", app2, m2("matches", -4), m2("map_zoomed", 0.5), 33, "Your top matches are the people whose work sits closest to yours. Co-authors are left out. "
     "Tap someone and you see what they're presenting, when, and where. Save them for the week. The map is the whole conference; every dot is a person."),
  ("phone", app2, m2("picker", -2.5), m2("hobby_view", 0.5), 30, "Peapod doesn't replace the programme app, but it follows the live schedule. "
     "The student view is the same map, students only. The hobby view is optional: add what you're into outside work, and you get the people who share it."),
  ("intro", meet, 0, 11, 11, "There's no chat. Peapod points you at a person and a room. The meeting happens in person. That's the point."),
  ("card", str(B / "card_story.png"), 0, 0, 30, "[ webcam: the story ]"),
  ("card", str(B / "card_pilot.png"), 0, 0, 5, "176 people joined in five days at the pilot, a hundred of them students."),
  ("card", str(B / "card_close.png"), 0, 0, 4, "Peapod for CHI 2027. Open source. Come find your people."),
]
BG = "color=c=0x15171c:s=1920x1080:r=25"
PHONE = (f"[0:v]crop=390:844:0:0,scale=462:1000,setsar=1[p];{BG}[bg];[bg][p]overlay=729:40[c];[c][1:v]overlay=0:0,format=yuv420p")

def run(args): subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)
parts = []; srt = []; t = 0.0
for i, (kind, src, a, b, tgt, cap) in enumerate(SCENES):
    out = T / f"s{i:02d}.mp4"
    if kind == "card":
        run(["-loop", "1", "-framerate", "25", "-t", str(tgt), "-i", src, "-vf", "format=yuv420p", "-r", "25", str(out)])
    else:
        dur = b - a; speed = dur / tgt  # stretch or squeeze the footage to the narration (up to 1.6x slower), then hold
        vf = f"setpts=PTS/{speed:.4f}," if (speed > 1.02 or speed < 0.98) and speed > 1/1.6 else (f"setpts=PTS*1.6," if speed <= 1/1.6 else "")
        shown = dur if 0.98 <= speed <= 1.02 else (tgt if speed > 1/1.6 else dur*1.6)
        pad = f",tpad=stop_mode=clone:stop_duration={max(0.0, tgt - shown):.2f}"
        if kind == "phone":
            fc = PHONE.replace("[0:v]crop", f"[0:v]setpts=PTS-STARTPTS,{vf}crop") + pad + "[v]"
            run(["-ss", f"{a:.2f}", "-to", f"{b:.2f}", "-i", src, "-i", str(B / "bezel.png"), "-filter_complex", fc, "-map", "[v]", "-t", str(tgt), "-r", "25", str(out)])
        else:
            run(["-ss", f"{a:.2f}", "-to", f"{b:.2f}", "-i", src, "-vf", f"setpts=PTS-STARTPTS,{vf}scale=1920:1080,format=yuv420p{pad}", "-t", str(tgt), "-r", "25", str(out)])
    parts.append(out)
    def ts(x): return f"{int(x//3600):02d}:{int(x%3600//60):02d}:{int(x%60):02d},{int(round((x%1)*1000)):03d}"
    srt.append(f"{i+1}\n{ts(t+0.3)} --> {ts(t+tgt-0.3)}\n{cap}\n"); t += tgt
(T / "list.txt").write_text("".join(f"file '{p}'\n" for p in parts)); (T / "caps.srt").write_text("\n".join(srt))
style = "FontName=Nunito,FontSize=15,PrimaryColour=&H00F4F1EA,OutlineColour=&H80000000,BorderStyle=3,Outline=1,Shadow=0,MarginV=28,Alignment=2"
run(["-f", "concat", "-safe", "0", "-i", str(T / "list.txt"), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
     "-vf", f"subtitles={T / 'caps.srt'}:force_style='{style}'", "-shortest", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-movflags", "+faststart",
     str(B / "peapod-draft.mp4")])
print("wrote", B / "peapod-draft.mp4", f"{t:.0f}s")
