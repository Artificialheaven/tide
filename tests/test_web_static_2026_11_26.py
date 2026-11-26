# -*- coding: utf-8 -*-
"""Web 面板静态资源回归：页面里写的 /static/... 必须真的能被访问到。

背景：Flask app 的 static_folder 指向 templates 目录却没有指定 static_url_path，
Flask 按目录基名推出 /templates/...，于是所有继承 base.html 的页面（含「群开关」）
里的 tokens.css / app.css 全是 404，页面变成裸 HTML。本文件锁住正确行为。
"""
import os
import re
import pathlib

import pytest

from ui.web.server import _app

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEMPLATE_DIRS = [
    ROOT / "ui" / "web" / "templates",
    ROOT / "plugins" / "deepseek" / "templates",
]
STATIC_REF = re.compile(r"""(?:href|src)=["'](/static/[^"'?#]+)""")
EXPECTED_MIME = {
    ".css": "text/css",
    ".js": "text/javascript",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/vnd.microsoft.icon",
}


def _template_files():
    files = []
    for folder in TEMPLATE_DIRS:
        if folder.is_dir():
            files.extend(sorted(folder.rglob("*.html")))
    return files


def _static_refs():
    refs = set()
    for path in _template_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        refs.update(STATIC_REF.findall(text))
    return sorted(refs)


def test_static_url_path_is_static():
    """static_url_path 必须显式是 /static，不能退回 Flask 推导的 /templates。"""
    assert _app.static_url_path == "/static"
    rules = [str(r) for r in _app.url_map.iter_rules() if r.endpoint == "static"]
    assert "/static/<path:filename>" in rules


def test_templates_do_not_reference_templates_prefix():
    """页面里不能残留 /templates/... 的静态引用（那是 Flask 推导出来的错误前缀）。"""
    for path in _template_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert '/templates/tds/' not in text, f"{path} 仍引用 /templates/ 静态路径"


def test_every_referenced_static_asset_is_served():
    """模板里出现的每个 /static/xxx 都必须 200 且 MIME 正确。"""
    refs = _static_refs()
    assert refs, "模板里没有任何静态资源引用，脚本可能失效"
    client = _app.test_client()
    missing = []
    for url in refs:
        resp = client.get(url)
        if resp.status_code != 200:
            missing.append((url, resp.status_code))
            continue
        ext = os.path.splitext(url)[1].lower()
        want = EXPECTED_MIME.get(ext)
        if want is not None:
            assert resp.content_type.startswith(want), f"{url} MIME 是 {resp.content_type}"
    assert not missing, f"以下静态资源 404：{missing}"


def test_base_page_css_is_reachable():
    """「群开关」等继承 base.html 的页面用的两份 CSS 必须可访问。"""
    client = _app.test_client()
    for url in ("/static/tds/tokens.css", "/static/tds/app.css", "/static/tds/legacy.css"):
        resp = client.get(url)
        assert resp.status_code == 200, url
        assert resp.content_type.startswith("text/css")


def test_socketio_bundle_is_local_and_reachable():
    """index.html 已把 socket.io 换成本地文件，路径必须与磁盘位置一致。"""
    index = (ROOT / "ui" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
    assert "/static/vendor/socket.io.min.js" in index
    assert 'src="/static/socket.io.min.js"' not in index
    resp = _app.test_client().get("/static/vendor/socket.io.min.js")
    assert resp.status_code == 200
    assert resp.content_type.startswith("text/javascript")
    assert len(resp.data) > 10_000
    assert not (ROOT / "ui" / "web" / "templates" / "static").exists()


def test_missing_static_asset_still_404():
    """不能因为修 static_url_path 就把任意路径都放过去。"""
    assert _app.test_client().get("/static/tds/does-not-exist.css").status_code == 404


def test_templates_are_not_exposed_as_static():
    """静态根必须是 ui/web/static，不能是 templates —— 否则模板源码可被直接下载。"""
    assert pathlib.Path(_app.static_folder).name == "static"
    assert (ROOT / "ui" / "web" / "static" / "tds" / "app.css").is_file()
    client = _app.test_client()
    for leaked in ("/static/base.html", "/static/partials/nav.html", "/static/index.html"):
        assert client.get(leaked).status_code == 404, leaked


@pytest.mark.parametrize("page", ["base.html", "index.html"])
def test_shell_pages_load_expected_stylesheets(page):
    text = (ROOT / "ui" / "web" / "templates" / page).read_text(encoding="utf-8")
    assert "/static/tds/tokens.css" in text
