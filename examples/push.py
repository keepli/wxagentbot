#!/usr/bin/env python3
"""Standard-library HTTP client; never prints the access token."""
import argparse
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description="向配置中的微信会话主动推送")
    p.add_argument("--base-url", default="http://127.0.0.1:8765")
    p.add_argument("--conversation", required=True)
    p.add_argument("--message", required=True)
    p.add_argument("--idempotency-key", default=None)
    p.add_argument("--wait", action="store_true")
    args = p.parse_args()
    token = os.environ.get("BRIDGE_API_TOKEN")
    if not token:
        token = (Path(__file__).resolve().parents[1] / ".secrets/api-token").read_text().strip()
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    headers["Idempotency-Key"] = args.idempotency_key or uuid.uuid4().hex

    def request(path, body=None):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        req = urllib.request.Request(args.base_url.rstrip("/") + path, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.load(response)

    try:
        response = request("/v1/messages/send", {"conversation": args.conversation, "message": args.message})
        print(json.dumps(response, ensure_ascii=False))
        if args.wait:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                status = request(response["status_url"])
                if status["status"] in ("sent", "failed", "unknown"):
                    print(json.dumps(status, ensure_ascii=False))
                    return 0 if status["status"] == "sent" else 1
                time.sleep(0.5)
            print("等待超时，请使用记录 ID 查询；不要更换幂等键盲目重发。")
            return 1
        return 0
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read(4096).decode(errors='replace')}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
