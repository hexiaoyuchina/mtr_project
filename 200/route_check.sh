#!/bin/bash
ip route get 10.133.152.204 from 10.133.153.204
ip route get 10.133.152.204
ip rule list | grep 153.204
ip neigh show dev iv204
tcpdump -i iv204 -c 5 icmp 2>/dev/null &
TCP=$!
sleep 1
ping -c1 -W1 -I 10.133.153.204 10.133.152.204
kill $TCP 2>/dev/null
wait $TCP 2>/dev/null
