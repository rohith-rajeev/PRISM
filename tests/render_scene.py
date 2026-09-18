"""Render PRISM's screens and save PNGs — a visual smoke test.

Exists because the rendering differences that matter are per-platform: the
rounded shapes and font metrics that look right on one OS can look wrong on
another, and no unit test catches that. CI runs this on macOS and Windows and
uploads the images, so a regression is visible rather than reported later.

    python3 tests/render_scene.py <output-dir>
"""
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app as A  # noqa: E402
import jobs as J  # noqa: E402


def grab(root, path):
    """Save a picture of the window, however this platform allows."""
    root.lift()
    root.attributes("-topmost", True)
    root.update()
    time.sleep(1.2)
    root.update()
    x, y = root.winfo_rootx(), root.winfo_rooty()
    w, h = root.winfo_width(), root.winfo_height()
    if sys.platform == "darwin":
        # -x suppresses the capture sound; -R takes a screen rectangle.
        cmd = ["screencapture", "-x", f"-R{x},{y},{w},{h}", str(path)]
    elif sys.platform == "win32":
        ps = (f"Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
              f"$b=New-Object Drawing.Bitmap {w},{h}; "
              f"$g=[Drawing.Graphics]::FromImage($b); "
              f"$g.CopyFromScreen({x},{y},0,0,$b.Size); "
              f"$b.Save('{path}')")
        cmd = ["powershell", "-NoProfile", "-Command", ps]
    else:
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "x11grab",
               "-video_size", f"{w}x{h}", "-i", f":0.0+{x},{y}",
               "-frames:v", "1", str(path)]
    subprocess.run(cmd, check=False)
    ok = Path(path).exists() and Path(path).stat().st_size > 2000
    print(f"{'saved' if ok else 'FAILED'} {path} ({w}x{h} at {x},{y})")
    return ok


def pump(root, seconds=0.6):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def build(root):
    root.manager = J.JobManager(root.log_q, runner=lambda **k: {})
    pump(root, 0.8)
    spec = lambda pr, repo: J.JobSpec(project_dir=".", repo_name=repo, pr_id=pr)  # noqa: E731
    rows = (("12948", "example-service-be", J.RUNNING, "⚠️ Approve with comments", "6/10"),
            ("12951", "example-service-be", J.RUNNING, "", ""),
            ("884", "example-portal-fe", J.DONE, "✅ Approve", "2/10"),
            ("885", "example-portal-fe", J.QUEUED, "", ""))
    for pr, repo, status, verdict, impact in rows:
        job = root.manager.create(spec(pr, repo))
        job.status, job.verdict_raw, job.impact = status, verdict, impact
        job.verdict_key = A._verdict_key_of(verdict)
    return root.manager


def main(out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    root = A.App()
    root.geometry("1020x900+40+40")
    manager = build(root)
    print(f"platform={sys.platform} font_family={A._FAMILY!r}")

    ok = True
    root.show_jobs()
    pump(root, 1.0)
    ok &= grab(root, out / f"jobs-{sys.platform}.png")

    detail = list(manager.jobs.values())[0]
    detail.append_log("▸ Target: CodeCommit repo 'example-service-be'  |  PR #12948")
    detail.append_log("⚙ bash: git diff --stat origin/main...origin/feature", "tool")
    detail.append_log("The diff touches shared auth middleware.", "agent")
    detail.stages = {A.STAGE_REVIEW: "done", A.STAGE_DESCRIBE: "active"}
    root.show_detail(detail.id)
    pump(root, 1.0)
    ok &= grab(root, out / f"detail-{sys.platform}.png")

    root.show_new()
    pump(root, 1.0)
    ok &= grab(root, out / f"newjob-{sys.platform}.png")

    # The manual, rendered from Markdown into the app's own palette.
    root.show_help()
    pump(root, 1.0)
    ok &= grab(root, out / f"help-{sys.platform}.png")

    # Switch screens and capture with the bare minimum of event processing.
    # Hand-drawn widgets paint from their <Configure> handler, and macOS defers
    # those on re-map — which left screens blank until an unrelated event
    # forced an expose. This is the condition that exposed it; a correct build
    # shows a fully drawn screen here.
    # The reported sequence: new job -> jobs -> new job. The second visit is
    # where cards came back as empty outlines, so capture that one.
    for name, go in (("fastswitch-new", root.show_new),
                     ("fastswitch-jobs", root.show_jobs),
                     ("fastswitch-new2", root.show_new),
                     ("fastswitch-jobs2", root.show_jobs),
                     ("fastswitch-help", root.show_help),
                     ("fastswitch-detail", lambda: root.show_detail(1))):
        go()
        root.update_idletasks()      # geometry only — no redraw events pumped
        time.sleep(0.35)
        ok &= grab(root, out / f"{name}-{sys.platform}.png")

    print("RENDER OK" if ok else "RENDER INCOMPLETE")
    sys.stdout.flush()
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "render-out")
