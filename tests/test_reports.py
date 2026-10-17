import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from finops_agent.detect import detect
from finops_agent.recommend import recommend
from finops_agent.report import dashboard, digest


@pytest.fixture(scope="module")
def outputs(dataset, prepared):
    return detect(prepared, "seasonal"), recommend(prepared, dataset.metrics)


def test_digest_has_spend_incidents_and_savings(prepared, outputs):
    md = digest.markdown(prepared, *outputs, {}, "Acme Freight")
    assert md.startswith("# Acme Freight: weekly cloud cost digest")
    assert "## Still running" in md and "## Top savings" in md and "| Azure |" in md


def test_webhook_payloads():
    md = "# Title\n\n**Spend: $1**"
    assert digest.slack_payload(md)["text"] == "# Title\n\n*Spend: $1*"
    card = digest.teams_payload(md)["attachments"][0]
    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert len(card["content"]["body"]) == 2


def test_post_sends_json_to_the_webhook():
    received = []

    class Hook(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.handle_request, daemon=True).start()
    status = digest.post("hello", "slack", url=f"http://127.0.0.1:{server.server_port}/hook")
    server.server_close()
    assert status == 200 and received == [{"text": "hello"}]


def test_dashboard_is_self_contained(prepared, outputs, tmp_path):
    meta = {"company": "Acme Freight", "start": "a", "end": "b", "focus_version": "1.4", "prices_fetched": "c"}
    out = dashboard.build(prepared, *outputs, {}, meta, tmp_path / "d.html")
    html = out.read_text(encoding="utf-8")
    assert "/*__DATA__*/" not in html and '"incidents":[' in html
    assert "<script src" not in html and "<link" not in html
