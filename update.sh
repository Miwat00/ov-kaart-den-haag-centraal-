#!/bin/bash
# Haalt de nieuwste versie op (als de map een git-kloon is) en herstart het tussenstation.
set -e
cd "$(dirname "$0")"
if [ -d .git ]; then git pull --ff-only; fi
sudo systemctl restart ov-kaart-live
sleep 2
systemctl --no-pager --lines=0 status ov-kaart-live | grep -E "●|Active:"
