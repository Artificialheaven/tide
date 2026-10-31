"""
Token 认证模块 — 基于 session 的简单密码验证
"""
from functools import wraps
from flask import session, request, jsonify

# 由主模块设置
AUTH_TOKEN = "test"


def init_auth(token: str):
    """设置认证密码"""
    global AUTH_TOKEN
    AUTH_TOKEN = token


def login_required(f):
    """装饰器：要求已登录"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return jsonify({"error": "未登录"}), 401
        return f(*args, **kwargs)
    return decorated


def try_login(password: str) -> bool:
    """验证密码"""
    pwd = (password or "").strip()
    token = (AUTH_TOKEN or "").strip()
    if pwd == token and pwd != "":
        session["authenticated"] = True
        session.permanent = True
        return True
    return False


def logout():
    """登出"""
    session.pop("authenticated", None)
