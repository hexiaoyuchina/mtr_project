#!/bin/bash
journalctl -u bgp-agent --since "20 min ago" --no-pager 2>/dev/null | grep 208 | tail -40
