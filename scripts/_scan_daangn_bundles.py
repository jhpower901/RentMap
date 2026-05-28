#!/usr/bin/env python3
import requests
import re

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"

base = "https://realty.daangn.com"
# Also include the listing page bundle
bundle_paths = [
    "/assets/main-DlOTVHVG.js",
    "/assets/realty-CVCvEv6r.js",
]

# Also get all bundle paths from the main page
r0 = requests.get(base + "/", headers={"User-Agent": UA}, timeout=20)
extra_paths = re.findall(r'/assets/[A-Za-z0-9_-]+-[A-Za-z0-9]+\.js', r0.text)
bundle_paths = list(dict.fromkeys(bundle_paths + extra_paths[:30]))

all_ops = {}
for path in bundle_paths:
    url = base + path
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Referer": base + "/"}, timeout=30)
        text = r.text
    except Exception as e:
        print(f"SKIP {path}: {e}")
        continue

    found = False
    # Pattern: id:"<hash>"... name:"<OpName>"
    for h, name in re.findall(r'id:["`\']([0-9a-f]{64})["`\'].{0,400}?name:["`\'](\w+)["`\']', text, re.S):
        all_ops[name] = h
        found = True
    # Pattern: name:"<OpName>"... id:"<hash>"
    for name, h in re.findall(r'name:["`\'](\w+)["`\'].{0,400}?id:["`\']([0-9a-f]{64})["`\']', text, re.S):
        all_ops[name] = h
        found = True

    if found:
        print(f"Found ops in {path}")

print("\n=== All GraphQL operations found ===")
for name, h in sorted(all_ops.items()):
    print(f"  {name}: {h}")
