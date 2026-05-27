#!/usr/bin/env python3
import os, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from deploy_light import connect, load_env_file, run_script
load_env_file(ROOT / "109" / "env")
c = connect(os.environ.get("MTR_OP_HOST", "101.89.68.109"))
_, out = run_script(c, r"""
for url in 'http://127.0.0.1:8808/api/static-routes' 'http://127.0.0.1:8808/api/static-routes?reconcile=true'; do
  echo "=== $url ==="
  curl -sS -o /tmp/sr.json -w 'http=%{http_code} time=%{time_total}\n' "$url"
  /root/mtr_op/venv/bin/python3 -c "import json;d=json.load(open('/tmp/sr.json')); print('n=',len(d), 'states=', [x.get('sync_state') for x in d[:6]])"
done
""", 120)
print(out)
c.close()
