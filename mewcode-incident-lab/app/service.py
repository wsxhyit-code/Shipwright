"""A real local HTTP service with SQLite and JSONL request logs. Python 3.10+."""
import argparse
import json
import sqlite3
import subprocess
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

from contextlib import closing

from app.orders import summarize


def revision():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unversioned"


def make_server(host, port, runtime, database=None, shipping_url="http://127.0.0.1:9081/health"):
    runtime = Path(runtime).resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    database = Path(database).resolve() if database else runtime / "orders.sqlite3"
    log_path = runtime / "service.jsonl"
    lock = threading.Lock()
    deployed_revision = revision()
    started_at = datetime.now(timezone.utc).isoformat()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            started = time.monotonic()
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            request_id = uuid.uuid4().hex
            status, payload = 200, {}
            error_code, exception_type, stack = None, None, None
            if parts.path == "/health":
                # Liveness alone does not prove that every business endpoint is healthy.
                payload = {"status": "up", "revision": deployed_revision, "started_at": started_at}
            elif parts.path == "/api/orders/summary":
                try:
                    customer_id = int(query.get("customer_id", ["1001"])[0])
                except ValueError:
                    status, payload = 400, {"error": "customer_id must be an integer"}
                else:
                    try:
                        if not database.is_file():
                            raise FileNotFoundError("Configured database file does not exist")
                        # ⚠️ `with sqlite3.connect(...)` **不关闭连接** ——
                        # sqlite3 的连接上下文管理器只负责 commit/rollback。
                        # 用 closing() 才能真正释放文件句柄：否则 Windows 上
                        # 这个 db 文件在进程存活期间一直删不掉，
                        # 固定验收里的 TemporaryDirectory 清理会 WinError 32。
                        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
                            rows = conn.execute(
                                "SELECT amount_cents FROM orders WHERE customer_id = ?", (customer_id,)
                            ).fetchall()
                        payload = summarize([row[0] for row in rows])
                    except FileNotFoundError as exc:
                        status, error_code = 503, "DATABASE_PATH_MISSING"
                        exception_type, stack = type(exc).__name__, traceback.format_exc()
                        payload = {"error": error_code, "request_id": request_id}
                    except sqlite3.Error as exc:
                        status, error_code = 503, "DATABASE_ERROR"
                        exception_type, stack = type(exc).__name__, traceback.format_exc()
                        payload = {"error": error_code, "request_id": request_id}
                    except Exception as exc:
                        status, error_code = 500, "APPLICATION_EXCEPTION"
                        exception_type, stack = type(exc).__name__, traceback.format_exc()
                        payload = {"error": error_code, "request_id": request_id}
            elif parts.path == "/api/shipping/quote":
                try:
                    with urlopen(shipping_url, timeout=1) as upstream:
                        if upstream.status != 200:
                            raise RuntimeError("Upstream health check failed")
                    payload = {"shipping_cents": 600}
                except (URLError, TimeoutError, OSError, RuntimeError) as exc:
                    status, error_code = 503, "UPSTREAM_UNAVAILABLE"
                    exception_type, stack = type(exc).__name__, traceback.format_exc()
                    payload = {"error": error_code, "request_id": request_id}
            else:
                status, payload = 404, {"error": "not_found"}
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("X-Request-ID", request_id)
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass
            event = {
                "ts": time.time(), "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "http_request", "service": "order-service", "revision": deployed_revision,
                "request_id": request_id, "method": "GET", "path": parts.path,
                "query": query, "status": status,
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
                "error_code": error_code, "exception_type": exception_type, "traceback": stack,
                "database_exists": database.is_file(), "dependency_url": shipping_url,
            }
            # Exactly one event per request: no double counting error + access records.
            with lock:
                with log_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9080)
    parser.add_argument("--runtime", default="runtime")
    parser.add_argument("--database")
    parser.add_argument("--shipping-url", default="http://127.0.0.1:9081/health")
    args = parser.parse_args()
    server = make_server(args.host, args.port, args.runtime, args.database, args.shipping_url)
    print(f"order-service listening on http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
