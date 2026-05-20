#!/bin/bash
DOWN=eno1np0
MAC=$(ip neigh show dev "$DOWN" | awk '/139.159.43.208/ {print $3; exit}')
echo "208 MAC=$MAC"
if [ -z "$MAC" ] || [ "$MAC" = "FAILED" ]; then
  echo "no 208 MAC, abort"
  exit 1
fi
ip neigh replace 139.159.105.94 lladdr "$MAC" dev "$DOWN" nud reachable
ip neigh show dev "$DOWN" | grep -E '105\.94|43\.208'
