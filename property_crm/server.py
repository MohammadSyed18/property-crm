"""Small authenticated REST surface for a configured CRM tenant."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import hmac, json, os
from .repository import CRM, Conflict, NotFound
from .followups import Followups


def serve():
    token = os.environ.get("CRM_API_TOKEN", "")
    if len(token) < 24:
        raise RuntimeError("Set CRM_API_TOKEN to a secret of at least 24 characters")
    crm = CRM(os.environ.get("CRM_DATABASE", "data/crm.sqlite3"))
    tenant = os.environ.get("CRM_TENANT", "demo")
    followups = Followups(crm)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch()

        def dispatch(self):
            if not hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + token
            ):
                return self.send(401, {"error": "unauthorized"})
            try:
                parsed = urlparse(self.path)
                q = parse_qs(parsed.query)
                if self.command == "GET" and parsed.path == "/search":
                    return self.send(
                        200,
                        crm.search(
                            tenant, q.get("property", [""])[0], q.get("q", [""])[0]
                        ),
                    )
                if self.command == "GET" and parsed.path == "/pipeline":
                    return self.send(
                        200, crm.pipeline(tenant, q.get("property", [""])[0])
                    )
                if self.command == "GET" and parsed.path == "/followups":
                    return self.send(
                        200,
                        followups.due(
                            tenant,
                            q.get("through", [""])[0],
                            property_id=q.get("property", [None])[0],
                        ),
                    )
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 100_000:
                    raise ValueError("body too large")
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.command == "POST" and parsed.path == "/leads":
                    return self.send(
                        201,
                        crm.create_lead(
                            tenant, body["property"], body["name"], body["email"]
                        ),
                    )
                if self.command == "POST" and parsed.path == "/leads/stage":
                    return self.send(
                        200,
                        crm.transition(
                            tenant, body["id"], body["stage"], body["version"]
                        ),
                    )
                if self.command == "POST" and parsed.path == "/followups":
                    return self.send(
                        201,
                        followups.create(
                            tenant, body["lead_id"], body["title"], body["due_at"]
                        ),
                    )
                if self.command == "POST" and parsed.path == "/followups/complete":
                    return self.send(
                        200, followups.complete(tenant, body["id"], body["version"])
                    )
                if self.command == "POST" and parsed.path == "/followups/reschedule":
                    return self.send(
                        200,
                        followups.reschedule(
                            tenant, body["id"], body["version"], body["due_at"]
                        ),
                    )
                self.send(404, {"error": "not found"})
            except NotFound:
                self.send(404, {"error": "not found"})
            except Conflict as error:
                self.send(409, {"error": str(error)})
            except (ValueError, KeyError, TypeError):
                self.send(400, {"error": "invalid request"})

        def send(self, status, value):
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    ThreadingHTTPServer(
        ("127.0.0.1", int(os.environ.get("PORT", "8080"))), Handler
    ).serve_forever()


if __name__ == "__main__":
    serve()
