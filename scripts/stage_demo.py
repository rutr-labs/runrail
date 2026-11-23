"""Stage the README's run-form, what-changed and variables scenes in a demo home.

    python scripts/stage_demo.py http://127.0.0.1:8795 /tmp/rr-orders-repo

Against a running `runrail serve` on a seeded demo home (see capture_demo.mjs):
makes a small git repository with an orders pipeline, an `orders_daily`
workflow with a run form, runs it once successfully, changes the code (one
commit, one uncommitted edit) so the second run fails, and adds a few
variables and secrets. The values are placeholders.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

base, root = sys.argv[1], sys.argv[2]

def api(method, path, body=None):
    req = urllib.request.Request(base + "/api" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read() or "null")

def git(*args):
    subprocess.run(["git", "-C", root, *args], check=True, capture_output=True)

def write(rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write(text)

write("etl/sources.py", 'def table_for(region: str) -> str:\n    return f"warehouse.orders_{region.lower()}"\n')
write("etl/load_orders.py", '''import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from sources import table_for
table = table_for(os.environ.get("RUNRAIL_PARAM_REGION", "EU"))
print(f"reading {table}")
rows = 0 if "_v2_" in table else 48210
print(f"loaded {rows} rows")
if not rows:
    print(f"no rows in {table}: refusing to publish an empty report", file=sys.stderr)
    sys.exit(1)
print("published orders report")
''')
write("etl/config.yml", "batch_size: 5000\n")
git("init", "-q"); git("add", "."); git("-c", "user.name=demo", "-c", "user.email=demo@example.com", "commit", "-qm", "orders pipeline")

project = api("POST", "/projects", {"name": "orders-repo", "root_path": root})
flow = api("POST", "/workflows", {
    "name": "orders_daily", "description": "Daily orders report", "project_id": project["id"],
    "run_parameters_json": [
        {"name": "report_date", "type": "date", "default": "yesterday", "label": "Report date", "help": "The day the report covers"},
        {"name": "region", "type": "choice", "choices": ["EU", "US", "APAC"], "default": "EU", "label": "Region"},
        {"name": "row_limit", "type": "number", "default": 50000, "label": "Row limit"},
        {"name": "dry_run", "type": "bool", "default": False, "label": "Dry run", "help": "Read everything, write nothing"},
    ]})
api("POST", f"/workflows/{flow['id']}/tasks", {"name": "load_orders", "task_type": "shell", "project_id": project["id"],
                                               "command": f"{sys.executable} etl/load_orders.py"})

def run_and_wait():
    run = api("POST", f"/workflows/{flow['id']}/run", {"parameters": {"region": "EU"}})
    for _ in range(120):
        status = api("GET", f"/runs/{run['id']}")["status"]
        if status not in ("queued", "running"):
            return run["id"], status
        time.sleep(0.5)
    raise SystemExit("run did not finish")

print("good", run_and_wait())
write("etl/sources.py", 'def table_for(region: str) -> str:\n    return f"warehouse.orders_v2_{region.lower()}"\n')
git("-c", "user.name=demo", "-c", "user.email=demo@example.com", "commit", "-qam", "read the v2 tables")
write("etl/config.yml", "batch_size: 10000\n")  # left uncommitted
print("bad", run_and_wait())

for name, value, secret, desc in [
    ("WAREHOUSE_HOST", "warehouse.internal:5432", False, "The reporting database"),
    ("EXPORT_FOLDER", "/srv/exports", False, "Where reports are written"),
    ("WAREHOUSE_PASSWORD", "demo-not-a-real-password", True, "Read-only reporting user"),
    ("SLACK_BOT_TOKEN", "demo-token-0000", True, "Posts the morning summary"),
]:
    api("POST", "/variables", {"name": name, "value": value, "secret": secret, "description": desc})
print("staged", flow["id"])
