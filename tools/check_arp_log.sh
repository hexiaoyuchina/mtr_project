#!/bin/bash
tail -50 /tmp/arp_spoof_daemon.log
python3 -c "import scapy; print('scapy_ok', scapy.__version__)" 2>&1
