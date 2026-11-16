"""Repair the system setup from the app (Settings → App → Repair): runs setup-system.sh with administrator rights.

    venv/bin/python -m pipeline.repair

pkexec shows the desktop's password window; the script installs missing packages, starts the database server
(and lets it start with the computer) and creates the database user. Output goes to the job log.
"""
import getpass
import shutil
import subprocess

from backend.common import ROOT
from .util import log


def main():
    script = ROOT / "setup-system.sh"
    if not shutil.which("pkexec"):
        raise SystemExit(f"No password window available (pkexec missing). In a terminal:  sudo sh {script} {getpass.getuser()}")
    log("asking for the administrator password …")
    rc = subprocess.run(["pkexec", "sh", str(script), getpass.getuser(), "--job"]).returncode
    if rc in (126, 127):
        raise SystemExit("Cancelled – the password is needed to repair.")
    if rc:
        raise SystemExit("Repair failed (see the log above).")
    log("done")


if __name__ == "__main__":
    main()
