"""Competition entry point: POST / and callback(json_data)."""
import argparse
import logging
import os

from flask import Flask, request, jsonify

if __package__:
    from .hunter.agent import Agent
    from .hunter.protocol import strict_json, empty_response
    from .hunter.rules import Rules, Policy
else:
    from hunter.agent import Agent
    from hunter.protocol import strict_json, empty_response
    from hunter.rules import Rules, Policy

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # Local input budget, not an official limit.
app.json.ensure_ascii = False
agent = Agent(rules=Rules.load(os.environ["HUNTER_RULES_PATH"]) if os.environ.get("HUNTER_RULES_PATH") else None,
              policy=Policy.load(os.environ["HUNTER_POLICY_PATH"]) if os.environ.get("HUNTER_POLICY_PATH") else None)


def callback(json_data):
    return agent.callback(json_data)


@app.route("/", methods=["POST"])
def process_request():
    try:
        data = strict_json(request.get_data(cache=False).decode("utf-8"))
    except (ValueError, UnicodeError):
        logging.getLogger("hunter").warning("malformed input JSON")
        return jsonify(empty_response())
    return jsonify(callback(data))


@app.errorhandler(413)
def oversized_request(_error):
    return jsonify(empty_response()), 413


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("port", type=int)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    logging.basicConfig(level=logging.WARNING)
    app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False, threaded=False)
