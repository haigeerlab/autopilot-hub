"""App installation tokens for the local orchestrator (slice 10).

The private key is read from the macOS keychain and handed to `openssl` on stdin; the JWT goes to `curl`
on stdin (`-H @-`), so neither appears in a file or in any process's argv. The installation token is
narrowed to one repository and the permissions the run needs, and revoked when the run ends.
Values follow research/2026-11-21-GitHub机器人身份/README.md in the ledger.
"""
import base64
import json
import os
import subprocess
import tempfile
import time

CLIENT_ID = "Iv23liThDIicC3gmcdiR"
KEYCHAIN_SERVICE = "haigeerlab-autopilot-private-key"
API = "https://api.github.com"


class TokenError(Exception):
    pass


def _b64u(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwt():
    key = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                         capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
    if key.returncode != 0:
        raise TokenError("钥匙串里读不到机器人私钥（服务名 %s）" % KEYCHAIN_SERVICE)
    pem = base64.b64decode(key.stdout.strip())
    now = int(time.time())
    signing = _b64u(b'{"alg":"RS256","typ":"JWT"}') + "." + _b64u(
        json.dumps({"iat": now - 60, "exp": now + 540, "iss": CLIENT_ID}).encode())
    with tempfile.NamedTemporaryFile() as data:  # holds only the unsigned header.payload
        data.write(signing.encode())
        data.flush()
        sig = subprocess.run(["openssl", "dgst", "-sha256", "-binary", "-sign", "/dev/stdin", data.name],
                             input=pem, capture_output=True, timeout=60)
    if sig.returncode != 0:
        raise TokenError("签 JWT 失败：%s" % sig.stderr.decode(errors="replace")[:200])
    return signing + "." + _b64u(sig.stdout)


def _curl(jwt, path, method="GET", body=None):
    args = ["curl", "-sS", "-f", "-X", method, "-H", "@-", API + path]
    if body is not None:
        args[5:5] = ["-d", json.dumps(body)]
    headers = "Authorization: Bearer %s\nAccept: application/vnd.github+json\n" % jwt
    r = subprocess.run(args, input=headers.encode(), capture_output=True, timeout=60)
    if r.returncode != 0:
        raise TokenError("%s %s 失败：%s" % (method, path, r.stderr.decode(errors="replace")[:200]))
    return json.loads(r.stdout)


def mint(repo, permissions):
    """Installation token for `repo` ("owner/name") limited to `permissions`."""
    jwt = _jwt()
    inst = _curl(jwt, "/repos/%s/installation" % repo)["id"]
    tok = _curl(jwt, "/app/installations/%d/access_tokens" % inst, "POST",
                {"repositories": [repo.split("/", 1)[1]], "permissions": permissions})
    return tok["token"]


def revoke(token):
    env = dict(os.environ, GH_TOKEN=token)
    subprocess.run(["gh", "api", "--method", "DELETE", "/installation/token"], env=env,
                   capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
