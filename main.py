import os

from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

app = Flask(__name__)

API_TOKEN = os.getenv("API_TOKEN", "")


def is_authorized(req) -> bool:
    token = req.headers.get("X-API-KEY", "")
    return bool(API_TOKEN) and token == API_TOKEN


@app.get("/")
def index():
    return jsonify({
        "message": "crawl-server up",
        "status": "ok"
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "ok"
    })


@app.post("/api/ping")
def api_ping():
    if not is_authorized(request):
        return jsonify({
            "success": False,
            "message": "unauthorized"
        }), 401

    data = request.get_json(silent=True) or {}
    return jsonify({
        "success": True,
        "received": data
    })