"""Send actual HTTP requests; no synthetic error logs are injected."""
import argparse
import json
import time
from urllib.error import HTTPError
from urllib.request import urlopen


def request(base, path):
    try:
        response = urlopen(base.rstrip("/") + path, timeout=3)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, json.loads(response.read())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:9080")
    parser.add_argument("--scenario", choices=["code", "dependency", "config", "normal"], default="code")
    parser.add_argument("--count", type=int, default=12)
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be positive")
    for index in range(args.count):
        if args.scenario == "code":
            path = f"/api/orders/summary?customer_id={1001 if index < 4 else 1002}"
        elif args.scenario in ("config", "normal"):
            path = "/api/orders/summary?customer_id=1001"
        else:
            path = "/api/shipping/quote"
        status, body = request(args.base, path)
        print(f"{index + 1:02d} HTTP {status} {json.dumps(body, ensure_ascii=False)}")
        time.sleep(0.08)


if __name__ == "__main__":
    main()
