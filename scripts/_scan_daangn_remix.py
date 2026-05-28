#!/usr/bin/env python3
"""Scan daangn.com Remix bundles for API endpoints and GraphQL operations."""
import requests
import re
import json

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
BASE = "https://www.daangn.com"
sess = requests.Session()
sess.headers.update({"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})

# Key bundles to scan
KEY_BUNDLES = [
    "/_remix/kr.realty._index-w6x9cMps.js",   # listing route
    "/_remix/root-pSLdBsLT.js",                # root
    "/_remix/index-DlEwfGZr.js",
    "/_remix/index-AdjLeCHO.js",
    "/_remix/alpha-kr-CIWutKR8.js",           # Korea-specific?
]

found_apis = []
found_gql = {}

for path in KEY_BUNDLES:
    url = BASE + path
    try:
        r = sess.get(url, timeout=30)
        text = r.text
        size = len(text)
    except Exception as e:
        print(f"SKIP {path}: {e}")
        continue

    print(f"\n=== {path} ({size} bytes) ===")

    # Find fetch/API calls
    fetch_matches = re.findall(r'fetch\(["\']([^"\']+)["\']', text)
    api_matches = re.findall(r'["\'](https?://[^"\']+(?:api|graphql|realty|karrot)[^"\']{0,100})["\']', text, re.I)
    for m in sorted(set(fetch_matches + api_matches)):
        if not any(x in m for x in ["cdn", "font", ".css", ".png", ".jpg", "polyfill"]):
            print(f"  API: {m}")
            found_apis.append(m)

    # Find GraphQL operation hashes
    gql_matches = re.findall(r'id:["`\']([0-9a-f]{64})["`\'].{0,400}?name:["`\'](\w+)["`\']', text, re.S)
    gql_matches2 = re.findall(r'name:["`\'](\w+)["`\'].{0,400}?id:["`\']([0-9a-f]{64})["`\']', text, re.S)
    for h, name in gql_matches:
        found_gql[name] = h
        print(f"  GQL: {name}: {h}")
    for name, h in gql_matches2:
        found_gql[name] = h
        print(f"  GQL: {name}: {h}")

    # Find loader data keys (Remix SSR)
    loader_keys = re.findall(r'loaderData\[["\']([^"\']+)["\']', text)
    for k in sorted(set(loader_keys)):
        print(f"  loaderData key: {k}")

    # Look for karrotmarket references
    km_refs = re.findall(r'karrotmarket\.com[^"\'<> ]{0,100}', text)
    for ref in sorted(set(km_refs))[:5]:
        print(f"  karrotmarket ref: {ref}")

print("\n\n=== SUMMARY ===")
print(f"API endpoints found: {len(set(found_apis))}")
for a in sorted(set(found_apis)):
    print(f"  {a}")
print(f"\nGraphQL operations: {len(found_gql)}")
for name, h in sorted(found_gql.items()):
    print(f"  {name}: {h}")
