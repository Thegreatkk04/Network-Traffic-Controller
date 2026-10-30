#!/bin/bash
# Removes all shaping if the engine crashed and internet feels stuck
sudo tc qdisc del dev wlp0s20f3 root 2>/dev/null
sudo tc qdisc del dev wlp0s20f3 ingress 2>/dev/null
sudo tc qdisc del dev ifb0 root 2>/dev/null
sudo nft delete table inet bwmgr 2>/dev/null
echo "shaping cleared"
