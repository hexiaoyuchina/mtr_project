#!/bin/bash
journalctl -u bgp-agent -n 200 --no-pager 2>/dev/null | grep -iE '235|1836|vbgp10133152235|error|fail|204' | tail -50
