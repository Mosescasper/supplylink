import re
import os
from app import app

# collect every url_for('...') reference used in templates
template_refs = set()
for root, _, files in os.walk("templates"):
    for f in files:
        if f.endswith(".html"):
            path = os.path.join(root, f)
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
            for match in re.findall(r"url_for\(\s*['\"](\w+)['\"]", content):
                template_refs.add(match)

real_endpoints = set(app.view_functions.keys())
missing = sorted(template_refs - real_endpoints)

print("Referenced in templates but NOT a real route:")
for name in missing:
    print(" -", name)
print()
print(f"Total missing: {len(missing)}")