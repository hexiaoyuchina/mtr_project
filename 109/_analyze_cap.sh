#!/bin/bash
PCAP=$(ls -t /tmp/mtr_cap_*.pcap 2>/dev/null | head -1)
echo "PCAP=$PCAP"
echo "=== eno1np0 Out (all) ==="
tcpdump -nn -r "$PCAP" 2>/dev/null | grep "eno1np0 Out" | head -15
echo "count eno1np0 Out:" $(tcpdump -nn -r "$PCAP" 2>/dev/null | grep -c "eno1np0 Out")
echo "=== time exceeded (all) ==="
tcpdump -nn -r "$PCAP" 2>/dev/null | grep "time exceeded" | wc -l
echo "=== time exceeded eno1np0 Out ==="
tcpdump -nn -r "$PCAP" 2>/dev/null | grep "time exceeded" | grep "eno1np0 Out" | head -10
echo "count:" $(tcpdump -nn -r "$PCAP" 2>/dev/null | grep "time exceeded" | grep -c "eno1np0 Out")
echo "=== mangle FORWARD counters ==="
iptables -t mangle -L FORWARD -v -n 2>/dev/null | head -6
