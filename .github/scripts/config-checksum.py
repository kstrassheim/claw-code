"""Checksum every ConfigMap the claw-code pod mounts that comes from k8s/.

Derived from the Deployment's own volume list rather than a hand-kept list, so
a ConfigMap added to the Deployment is covered without editing the workflow.
The Deployment itself is never hashed — it carries the resulting annotation,
which would make the hash depend on itself.
"""
import glob, hashlib, json, sys

try:
    import yaml
except ImportError:
    # Preinstalled on GitHub runners, and validate.yml's check-model-config.py
    # already relies on it. Failing loudly beats installing it here: a package
    # fetched at deploy time is one more thing that can be down when you deploy.
    sys.exit("::error::PyYAML is required to compute checksum/config")

docs, dep = [], None
for f in sorted(glob.glob("k8s/*.yaml")):
    for d in yaml.safe_load_all(open(f, encoding="utf-8")):
        if not d:
            continue
        docs.append(d)
        if d.get("kind") == "Deployment" and d.get("metadata", {}).get("name") == "claw-code":
            dep = d

if dep is None:
    sys.exit("::error::claw-code Deployment not found in k8s/")

mounted = set()
for v in dep["spec"]["template"]["spec"].get("volumes") or []:
    cm = v.get("configMap")
    if cm and cm.get("name"):
        mounted.add(cm["name"])
    for src in (v.get("projected") or {}).get("sources") or []:
        c = src.get("configMap")
        if c and c.get("name"):
            mounted.add(c["name"])

h, covered = hashlib.sha256(), []
for d in docs:
    if d.get("kind") == "ConfigMap" and d.get("metadata", {}).get("name") in mounted:
        name = d["metadata"]["name"]
        covered.append(name)
        h.update(json.dumps(d, sort_keys=True).encode())

# The rest are built by the workflow itself and hashed by the other two
# annotations; anything left over is a real gap worth naming.
external = sorted(mounted - set(covered))
print(f"covered by checksum/config: {', '.join(sorted(covered)) or 'none'}", file=sys.stderr)
print(f"built by the workflow (hashed separately): {', '.join(external) or 'none'}", file=sys.stderr)
print(h.hexdigest()[:16])
