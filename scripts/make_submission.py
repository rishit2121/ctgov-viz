"""Build the submission zip with a simple top level:

    README.md            the project README, links rewritten for this layout
    demo/                the demo video and a text file linking to the live site
    code/                the runnable project (from git: committed files only, no .env)
    examples/            the example runs: one request and one response JSON per run

    uv run python scripts/make_submission.py --video "path/to/recording.mov" [--out file.zip]
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VIDEO_NAME = "ctgov-viz-demo.mov"
LIVE_DEMO_TXT = """ctgov-viz: live demo

Try it:      https://ctgov-viz.onrender.com
API docs:    https://ctgov-viz.onrender.com/docs
Source code: https://github.com/rishit2121/ctgov-viz

The site runs on Render's free tier. If nobody has used it for a while, the first page load can
take up to a minute while the server wakes up. After that, each question takes about 10-20
seconds, because every matching ClinicalTrials.gov study is downloaded and analyzed.

To try it: type a question or click one of the example questions, then click any bar, point,
network node or connection line to see the studies behind it and the exact record text that
supports each number.
"""

# (pattern, replacement) applied to the top-level README, in order.
README_REWRITES = [
    (r"\]\(docs/", "](code/docs/"),
    (r"\]\(examples/runs/\)", "](examples/)"),
    (r"`examples/runs/`", "`examples/`"),
    (r"`examples/plans/`", "`code/examples/plans/`"),
    (r"`tests/`", "`code/tests/`"),
    (r"\]\(PLAN\.md\)", "](code/PLAN.md)"),
    (r"`PLAN\.md`", "`code/PLAN.md`"),
    (r"git clone https://github\.com/rishit2121/ctgov-viz\.git\ncd ctgov-viz\n",
     "cd code   # or: git clone https://github.com/rishit2121/ctgov-viz.git && cd ctgov-viz\n"),
]
CODE_README = """# ctgov-viz (code)

This folder is the runnable project. Setup, usage and documentation are in the README one level
up (`../README.md`), also at https://github.com/rishit2121/ctgov-viz.
"""


def build(video: Path, out: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        code = stage / "code"
        archive = stage / "code.tar"
        subprocess.run(["git", "archive", "--format=tar", "-o", str(archive), "HEAD"],
                       cwd=ROOT, check=True)
        code.mkdir()
        subprocess.run(["tar", "-xf", str(archive), "-C", str(code)], check=True)
        archive.unlink()

        shutil.move(str(code / "examples" / "runs"), str(stage / "examples"))

        readme = (code / "README.md").read_text()
        for pattern, replacement in README_REWRITES:
            readme, n = re.subn(pattern, replacement, readme)
            if n == 0:
                print(f"note: pattern not found in README: {pattern}")
        (stage / "README.md").write_text(readme)
        (code / "README.md").write_text(CODE_README)  # pyproject needs a README next to it

        (stage / "demo").mkdir()
        shutil.copyfile(video, stage / "demo" / VIDEO_NAME)
        (stage / "demo" / "live-demo.txt").write_text(LIVE_DEMO_TXT)

        out.unlink(missing_ok=True)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    method = zipfile.ZIP_STORED if path.suffix == ".mov" else zipfile.ZIP_DEFLATED
                    z.write(path, path.relative_to(stage), compress_type=method)
    print(f"wrote {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--out", type=Path,
                        default=Path.home() / "Desktop" / "ctgov-viz-submission.zip")
    args = parser.parse_args()
    build(args.video, args.out)
