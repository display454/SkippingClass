"""Request and credential boundaries for the two loopback applications."""
from __future__ import annotations

import copy
import ipaddress
import secrets
from urllib.parse import urlsplit

from flask import jsonify, request

SECRET_FIELDS = ("api_key", "access_key", "app_id")


def local_url(url: str) -> bool:
    try:
        host = urlsplit(url).hostname
        return host == "localhost" or ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def endpoint(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def public_config(value):
    if isinstance(value, list):
        return [public_config(item) for item in value]
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    result = {k: public_config(v) for k, v in value.items() if k not in SECRET_FIELDS}
    for key in SECRET_FIELDS:
        if key in value:
            result[key] = ""
            result[f"has_{key}"] = bool(value[key])
    return result


def bind_endpoint(old: dict, new: dict, patch: dict) -> None:
    if endpoint(old.get("base_url", "")) != endpoint(new.get("base_url", "")):
        for key in SECRET_FIELDS:
            if not patch.get(key) or "******" in str(patch[key]):
                new[key] = ""


def redact_message(message, config: dict) -> str:
    text = str(message)
    def visit(value):
        nonlocal text
        if isinstance(value, dict):
            for key, item in value.items():
                if key in SECRET_FIELDS and isinstance(item, str) and len(item) >= 4:
                    text = text.replace(item, "[凭证已隐藏]")
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(config)
    return text


def install(app) -> None:
    token = secrets.token_urlsafe(32)
    app.extensions["local_security_token"] = token

    @app.before_request
    def validate_request():
        # Validate the authority before serving even a read-only/token endpoint.
        if not local_url(f"http://{request.host}"):
            return jsonify(ok=False, error="仅允许本机地址访问"), 403
        origin = request.headers.get("Origin")
        if origin and origin != request.host_url.rstrip("/"):
            return jsonify(ok=False, error="已拒绝跨站请求"), 403
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            received = request.headers.get("X-CSRF-Token", "")
            if not secrets.compare_digest(received, token):
                return jsonify(ok=False, error="页面会话已失效，请刷新后重试"), 403
            if request.is_json and not isinstance(request.get_json(silent=True), dict):
                return jsonify(ok=False, error="请求内容必须是 JSON 对象"), 400

    @app.get("/api/security")
    def session_token():
        response = jsonify(token=token)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.after_request
    def protect_response(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response
