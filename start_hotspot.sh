#!/bin/bash
# Starts the BW-Lab hotspot. Phone USB tethering must be ON first.
nmcli -t -f TYPE,STATE device | grep -q "^ethernet:connected" || echo "WARNING: no USB tethering/ethernet internet found"
sudo iw reg set IN
sudo nmcli connection up Hotspot || sudo nmcli device wifi hotspot ifname wlp0s20f3 con-name Hotspot ssid BW-Lab password bwlab12345 band bg channel 6
ip -4 addr show wlp0s20f3 | grep inet
