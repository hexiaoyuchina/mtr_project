#!/bin/bash
echo "rules:"; ip -4 rule show | grep -E '207|249'
echo "route:"; ip route get 139.159.43.249 from 139.159.43.207
echo "nc test:"; timeout 3 bash -c 'echo | nc -w2 -s 139.159.43.207 139.159.43.249 179' && echo ok || echo fail
echo "ss:"; ss -tnp | grep -E 'bgp|249|179' | head -15
journalctl -u bgp-agent -n 30 --no-pager | tail -20
