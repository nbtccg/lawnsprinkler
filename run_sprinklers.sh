#!/bin/bash
cd /home/pi/git_repo/scripts/sprinkler/
. venv/bin/activate
/home/pi/git_repo/scripts/sprinkler/sprinkler.py --config /home/pi/git_repo/scripts/sprinkler/config.yaml
