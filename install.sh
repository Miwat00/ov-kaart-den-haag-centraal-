#!/bin/bash
# Eenmalig draaien op de Pi: installeert het pakket voor de live datastroom en maakt twee
# vaste diensten aan die automatisch starten (ook na een herstart):
#   ov-kaart-web   de kaart op poort 8000
#   ov-kaart-live  het tussenstation op poort 8787 (vertrektijden en voertuigen)
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
WHO="$(id -un)"
echo "Map: $DIR   gebruiker: $WHO"
sudo apt-get install -y python3-zmq
pkill -f "http.server 8000" 2>/dev/null || true
pkill -f live_proxy.py 2>/dev/null || true
sudo tee /etc/systemd/system/ov-kaart-web.service >/dev/null <<UNIT
[Unit]
Description=OV-kaart Den Haag Centraal (kaart)
After=network-online.target
Wants=network-online.target
[Service]
User=$WHO
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 -m http.server 8000
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
sudo tee /etc/systemd/system/ov-kaart-live.service >/dev/null <<UNIT
[Unit]
Description=OV-kaart Den Haag Centraal (live tussenstation)
After=network-online.target
Wants=network-online.target
[Service]
User=$WHO
WorkingDirectory=$DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 $DIR/live_proxy.py
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now ov-kaart-web ov-kaart-live
sleep 3
systemctl --no-pager --lines=0 status ov-kaart-web ov-kaart-live | grep -E "●|Active:"
echo "Klaar. Log van het tussenstation: journalctl -u ov-kaart-live -n 30 --no-pager"
