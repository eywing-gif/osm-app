"""Install a new version of the app from GitHub.

    venv/bin/python -m pipeline.update v0.6.0

Git checkout: fetch + fast-forward to the tag. Otherwise (zip download): download the tag and copy the files over
the installation (data/, venv/ and .git stay). Then the Python packages are updated.
"""
import io
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from backend.appinfo import config
from backend.common import ROOT, USER_AGENT
from .util import log, progress

KEEP = {"data", "venv", ".git", "__pycache__"}


def run(*cmd):
    subprocess.run(cmd, cwd=ROOT, check=True)


def main(tag: str):
    repo = config()["repo"]
    progress(5, "downloading")
    if (ROOT / ".git").exists():
        run("git", "fetch", "--tags", "--quiet", "origin")
        run("git", "merge", "--ff-only", "--quiet", f"refs/tags/{tag}")
    else:
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}/zipball/{tag}", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        progress(40, "installing")
        with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(io.BytesIO(data)) as z:
            z.extractall(tmp)
            top = next(Path(tmp).iterdir())          # <owner>-<repo>-<sha>/
            for src in top.rglob("*"):
                rel = src.relative_to(top)
                if src.is_file() and not KEEP & set(rel.parts):
                    (ROOT / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, ROOT / rel)
    progress(70, "packages")
    run(sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt")
    progress(100, "restart the app")
    log("updated to", tag)


if __name__ == "__main__":
    main(sys.argv[1])
