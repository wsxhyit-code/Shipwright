"""Expose real lab evidence in the HTTP shapes Shipwright already understands.

This is a small compatibility adapter, not a Loki/Prometheus installation.
It supports only documented selectors and named metrics, rejecting unknown queries.
"""
import argparse
import json
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def read_events(runtime):
    path = Path(runtime) / "service.jsonl"
    output = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                output.append(json.loads(line))
            except ValueError:
                continue
    return output


def log_line(event):
    level = "ERROR" if event["status"] >= 500 else "INFO"
    frames = re.findall(r'File "([^"]+)", line (\d+), in ([^\n]+)', event.get("traceback") or "")
    location = ""
    if frames:
        filename, line, function = frames[-1]
        location = f"{Path(filename).name}:{line} in {function}"
    return (f"{event['timestamp']} {level} {event.get('exception_type') or 'request'} {location} "
            f"code={event.get('error_code')} endpoint={event['path']} status={event['status']} "
            f"request_id={event['request_id']} revision={event['revision']} "
            f"query={json.dumps(event.get('query', {}), ensure_ascii=False)}")


def alertmanager_rows(runtime):
    rows = []
    for path in (Path(runtime) / "alerts").glob("*.json"):
        incident = json.loads(path.read_text(encoding="utf-8"))
        if incident["status"] != "firing":
            continue
        rows.append({
            "labels": {"alertname": incident["alert_id"], "service": incident["service"],
                       "severity": "critical", "endpoint": incident["endpoint"],
                       "revision": incident["revision"], "classification": incident["classification"],
                       "code_fix_candidate": str(incident["code_fix_candidate"]).lower()},
            "annotations": {"summary": f"HTTP 5xx rate {incident['metrics']['error_rate']:.1%}; {incident['classification_rationale']}"},
            "startsAt": datetime.fromtimestamp(incident["fired_at"], timezone.utc).isoformat(),
            "status": {"state": "active"},
        })
    return rows


def loki_response(runtime, params):
    query = params.get("query", [""])[0]
    match = re.fullmatch(r'\{(?:service="([^"]*)")?(?:,?level=~"\(\?i\)([^"]*)")?\}(?: \|~ "\(\?i\)([^"]*)")?', query)
    if match is None:
        raise ValueError("Lab adapter supports service + level selectors and one regex only; no LogQL engine")
    service, level, pattern = match.groups()
    start = int(params.get("start", [0])[0]) / 1e9
    end = int(params.get("end", [int(time.time() * 1e9)])[0]) / 1e9
    limit = min(max(int(params.get("limit", [5000])[0]), 1), 5000)
    groups = defaultdict(list)
    for event in read_events(runtime):
        event_level = "ERROR" if event["status"] >= 500 else "INFO"
        if not start <= event["ts"] <= end or (service and event["service"] != service):
            continue
        if level and not re.search(level, event_level, re.I):
            continue
        line = log_line(event)
        if pattern and not re.search(pattern, line, re.I):
            continue
        groups[(event["service"], event_level)].append([str(int(event["ts"] * 1e9)), line])
    result = []
    remaining = limit
    for (service, level), values in groups.items():
        values.sort(key=lambda pair: int(pair[0]), reverse=params.get("direction", ["backward"])[0] == "backward")
        values = values[:remaining]
        remaining -= len(values)
        if values:
            result.append({"stream": {"service": service, "level": level}, "values": values})
    return {"status": "success", "data": {"resultType": "streams", "result": result}}


def prometheus_response(runtime, params):
    metric = params.get("query", [""])[0]
    if metric not in ["http_5xx_rate", "http_requests_count", "http_errors_count", "http_p99_ms"]:
        raise ValueError("Supported lab metrics: http_5xx_rate, http_requests_count, http_errors_count, http_p99_ms; no PromQL engine")
    start = float(params.get("start", [time.time() - 900])[0])
    end = float(params.get("end", [time.time()])[0])
    step = max(float(params.get("step", [15])[0]), 1)
    if end < start or (end - start) / step > 1000:
        raise ValueError("Invalid or excessive query range")
    groups = defaultdict(list)
    for event in read_events(runtime):
        if event["path"].startswith("/api/") and start <= event["ts"] <= end:
            groups[event["path"]].append(event)
    results = []
    for endpoint, events in groups.items():
        values = []
        point = start
        while point <= end:
            samples = [e for e in events if point - 60 <= e["ts"] <= point]
            if samples:
                errors = sum(e["status"] >= 500 for e in samples)
                if metric == "http_5xx_rate":
                    value = 100 * errors / len(samples)
                elif metric == "http_requests_count":
                    value = len(samples)
                elif metric == "http_errors_count":
                    value = errors
                else:
                    durations = sorted(e["duration_ms"] for e in samples)
                    value = durations[min(int(len(durations) * .99), len(durations) - 1)]
                values.append([point, str(value)])
            point += step
        # Existing backend passes integer seconds: always include observed final
        # data up to that requested end; do not invent baseline zeroes for no traffic.
        if values:
            results.append({"metric": {"__name__": metric, "service": "order-service", "endpoint": endpoint}, "values": values})
    return {"status": "success", "data": {"resultType": "matrix", "result": results}}


def make_server(host, port, runtime):
    runtime = Path(runtime).resolve()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            parts = urlsplit(self.path)
            params = parse_qs(parts.query)
            status = 200
            try:
                if parts.path == "/api/v2/alerts":
                    body = alertmanager_rows(runtime)
                elif parts.path == "/loki/api/v1/query_range":
                    body = loki_response(runtime, params)
                elif parts.path == "/api/v1/query_range":
                    body = prometheus_response(runtime, params)
                elif parts.path == "/health":
                    body = {"status": "up", "adapter": "lab evidence compatibility API"}
                else:
                    status, body = 404, {"error": "not_found"}
            except (ValueError, re.error) as exc:
                status, body = 400, {"status": "error", "error": str(exc)}
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", default="runtime")
    parser.add_argument("--port", type=int, default=9082)
    args = parser.parse_args()
    server = make_server("127.0.0.1", args.port, args.runtime)
    print(f"Real log/alert evidence API: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
