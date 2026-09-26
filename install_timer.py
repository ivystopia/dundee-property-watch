#!/usr/bin/env python3
"""Install the reviewed user timer, backing up any existing unit files first."""
from datetime import datetime
from pathlib import Path
import shutil
import subprocess

root = Path(__file__).resolve().parent
destination = Path.home() / ".config/systemd/user"
destination.mkdir(parents=True, exist_ok=True)
timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
for name in ("dundee-property-watch.service", "dundee-property-watch.timer"):
    target = destination / name
    if target.exists():
        backup = target.with_name(target.name + ".backup-" + timestamp)
        shutil.copy2(target, backup)
        print("Backup:", backup)
    shutil.copy2(root / name, target)
    print("Installed:", target)
subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
subprocess.run(["systemctl", "--user", "enable", "--now", "dundee-property-watch.timer"], check=True)
subprocess.run(["systemctl", "--user", "list-timers", "dundee-property-watch.timer", "--no-pager"], check=True)
