#!/usr/bin/env bash
# Why does nginx answer 403 to video links? Run on the server:  sudo bash ./deploy/check-signature.sh
# Compares the media secret in the env file, the running web app and nginx (fingerprints
# only), and re-checks the last refused /m/ requests in the access log against each.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
PID="$(systemctl show -p MainPID --value privatube-web)"
python3 - "$PID" <<'PY'
import base64, glob, hashlib, re, sys, time, urllib.parse

def fp(s):
    return hashlib.sha256(s.encode()).hexdigest()[:10] if s else "(none)"

secrets = {}
for line in open("/etc/privatube/env"):
    if line.startswith("PT_MEDIA_SECRET="):
        secrets["env file"] = line.split("=", 1)[1].strip().strip('"').strip("'")
pid = sys.argv[1]
try:
    env = open(f"/proc/{pid}/environ", "rb").read().split(b"\0")
    secrets["web app"] = next((e.split(b"=", 1)[1].decode() for e in env if e.startswith(b"PT_MEDIA_SECRET=")), "")
except OSError as e:
    print("can't read web app environment:", e)
for f in glob.glob("/etc/nginx/sites-enabled/*") + glob.glob("/etc/nginx/conf.d/*.conf"):
    for i, m in enumerate(re.findall(r'secure_link_md5\s+"\$secure_link_expires\$uri\$arg_u ([^"]*)"', open(f).read())):
        secrets[f"nginx {f.rsplit('/', 1)[1]} #{i + 1}"] = m
for k, v in secrets.items():
    print(f"{k:32} {fp(v)}  ({len(v)} chars)")
print("all the same:", len({v for v in secrets.values()}) == 1)

def sig(secret, expires, uri, u):
    d = hashlib.md5(f"{expires}{uri}{u} {secret}".encode()).digest()
    return base64.urlsafe_b64encode(d).decode().rstrip("=")

lines = [l for l in open("/var/log/nginx/access.log", errors="replace") if '"GET /m/' in l and '" 403 ' in l][-4:]
print(f"\nlast {len(lines)} refused /m/ requests:")
for l in lines:
    target = l.split('"GET ', 1)[1].split(" ", 1)[0]
    path, _, query = target.partition("?")
    q = dict(urllib.parse.parse_qsl(query))
    uri = urllib.parse.unquote(path)
    e = q.get("e", "")
    left = (int(e) - time.time()) / 3600 if e.isdigit() else float("nan")
    print(f"  {l.split('[', 1)[1][:20]}  u={q.get('u')}  expires in {left:.1f} h  file={uri.rsplit('/', 1)[-1]}"
          f"  path-encoded={'%' in path}")
    for k, v in secrets.items():
        print(f"      matches {k}: {sig(v, e, uri, q.get('u', '')) == q.get('s')}")
PY
