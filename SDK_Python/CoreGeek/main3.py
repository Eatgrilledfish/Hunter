"""Competition entry point: POST / and callback(json_data)."""
if __package__:
    from .hunter.log_mode import configure_process_output
else:
    from hunter.log_mode import configure_process_output

LOG_MODE = configure_process_output()

import base64
import argparse
import logging
import os
from pathlib import Path

from flask import Flask, request, jsonify

if __package__:
    from .hunter.diagnostics import Diagnostics
    from .hunter.agent import Agent
    from .hunter.protocol import strict_json, empty_response
    from .hunter.rules import Rules, Policy
else:
    from hunter.diagnostics import Diagnostics
    from hunter.agent import Agent
    from hunter.protocol import strict_json, empty_response
    from hunter.rules import Rules, Policy

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # Local input budget, not an official limit.
app.json.ensure_ascii = False
diagnostics = Diagnostics(mode=LOG_MODE)
logging.getLogger("hunter").addHandler(diagnostics)
logging.getLogger("hunter").propagate = False  # Avoid a second unbounded traceback via the root handler.
logging.getLogger("werkzeug").setLevel(logging.WARNING)  # Successful POST access lines are redundant.
agent = Agent(diagnostics=diagnostics, rules=Rules.load(os.environ["HUNTER_RULES_PATH"]) if os.environ.get("HUNTER_RULES_PATH") else None,
              policy=Policy.load(os.environ.get("HUNTER_POLICY_PATH") or Path(__file__).resolve().parent/'config/strategy_rear_open.json'))

try:
    diagnostics.startup(agent)
except Exception:
    logging.getLogger("hunter").exception("startup diagnostic failed")


def callback(json_data):
    return agent.callback(json_data)


@app.route("/", methods=["POST"])
def process_request():
    body = request.get_data(cache=False)
    try:
        data = strict_json(body.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        detail = {"raw_base64":base64.b64encode(body).decode("ascii"),"response":empty_response()} if diagnostics.mode == "full" else {
            "bytes":len(body),"error":type(exc).__name__,"detail":str(exc)[:160]}
        diagnostics.event("malformed_request", **detail, http_status=200)
        return jsonify(empty_response())
    return jsonify(callback(data))


@app.errorhandler(413)
def oversized_request(_error):
    diagnostics.event("oversized_request", content_length=request.content_length,
                      response=empty_response(), http_status=413)
    return jsonify(empty_response()), 413


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("port", type=int)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    logging.basicConfig(level=logging.WARNING)
    app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False, threaded=False)
