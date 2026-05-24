"""Debug POST to Discord webhook. Tries with explicit User-Agent first
since Discord's Cloudflare rejects the python-urllib default."""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

env_path = Path(__file__).resolve().parent.parent / ".env"
url = None
for line in env_path.read_text(encoding="utf-8").splitlines():
    if line.startswith("RENTMAP_DISCORD_WEBHOOK_URL="):
        url = line.split("=", 1)[1].strip()
        break

if not url:
    print("URL not found in .env")
    sys.exit(1)

print(f"URL: {url[:60]}...{url[-12:]}")

payload = {
    "content": "RentMap webhook smoke test (debug POST from host)",
}

for ua_label, ua in [
    ("default (urllib)", None),
    ("RentMap UA", "RentMap-Webhook/1.0 (+rentmap.local)"),
    ("DiscordBot UA", "DiscordBot (https://github.com/local/rentmap, 1.0)"),
]:
    headers = {"Content-Type": "application/json"}
    if ua:
        headers["User-Agent"] = ua
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 method="POST", headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        print(f"  [{ua_label}] HTTP {resp.status} OK")
        sys.exit(0)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        # ASCII-only print so cp949 console doesn't choke
        body_safe = body.encode("ascii", errors="backslashreplace").decode("ascii")
        print(f"  [{ua_label}] HTTP {e.code}: {body_safe[:300]}")
    except Exception as e:
        print(f"  [{ua_label}] network: {type(e).__name__}: {e}")
