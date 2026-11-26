# -*- coding: utf-8 -*-
"""插件页迁移收尾（M2）：8 个页面统一走 base.html 外壳，不再各自带 <style>。

历史：外壳迁移只做了一半——页面虽然 {% extends "base.html" %}，但 head 里仍保留
旧独立页的 <style>，里面对 body / nav / button / input / table 的全局规则会盖掉
外壳样式（而且 nav 早已没有对应元素）。这些规则已上收到 ui/web/static/tds/pages.css，
本文件防止它们回潮。
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
TPL = ROOT / "plugins" / "deepseek" / "templates"
SHELL_PAGES = [
    "dashboard", "group", "settings", "memory", "mcp", "prompts", "runs", "bili_live",
]
LEGACY_COLORS = ("#1a1d23", "#22262e", "#5e81ac", "#3b4252")


def _read(name):
    return (TPL / f"{name}.html").read_text(encoding="utf-8")


def test_shell_pages_do_not_carry_their_own_style_blocks():
    offenders = [name for name in SHELL_PAGES if "<style" in _read(name)]
    assert not offenders, f"这些页面还带内联样式：{offenders}"


def test_shell_pages_do_not_keep_legacy_global_rules():
    for name in SHELL_PAGES:
        text = _read(name)
        assert "body{background:#1a1d23" not in text, name
        assert "nav a{color:#7aa2f7" not in text, name
        assert "button{background:#5e81ac" not in text, name
        assert "input,select,textarea{background:#22262e" not in text, name


def test_shell_pages_do_not_load_extra_stylesheets():
    """样式只由 base.html 统一引入，页面自己不许再挂 <link rel=stylesheet>。"""
    for name in SHELL_PAGES:
        assert "<link" not in _read(name), name


def test_base_loads_shared_layers_in_order():
    base = (ROOT / "ui" / "web" / "templates" / "base.html").read_text(encoding="utf-8")
    order = [base.index("/static/tds/tokens.css"),
             base.index("/static/tds/app.css"),
             base.index("/static/tds/pages.css")]
    assert order == sorted(order), "token → shell → pages 的引入顺序被打乱"
    assert "/static/tds/legacy.css" not in base, "迁移后页面不该再拉 legacy.css"


def test_pages_css_covers_migrated_components():
    css = (ROOT / "ui" / "web" / "static" / "tds" / "pages.css").read_text(encoding="utf-8")
    required = [
        ".tds-content fieldset",
        ".tds-content legend",
        ".tds-content label",
        ".tds-content button:not(.btn)",
        ".tds-content table",
        ".tds-content .hint",
        ".tds-content .tabs",
        ".tds-content #detail",
        ".tds-content .tag.k-wake",
        ".tds-content .badge.b-run",
    ]
    for sel in required:
        assert sel in css, f"pages.css 缺少 {sel}"


def test_pages_css_is_scoped_to_shell_content():
    """新上收的规则必须限定在 .tds-content 内，不能污染框架面板与旧页面。"""
    css = (ROOT / "ui" / "web" / "static" / "tds" / "pages.css").read_text(encoding="utf-8")
    # 去掉注释后，所有规则选择器都必须以 .tds-content / @media / 注释开头
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    selectors = re.findall(r"(?m)^([^{}\n][^{}]*)\{", stripped)
    unscoped = [s.strip() for s in selectors
                if not s.strip().startswith("@media")
                and not any(part.strip().startswith(".tds-content") for part in s.split(","))]
    assert not unscoped, f"这些选择器没有限定在 .tds-content 内：{unscoped}"
