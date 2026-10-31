"""Имена элементов WordprocessingML и мелкие помощники для разбора XML."""

from __future__ import annotations

from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


class _NS:
    def __init__(self, uri: str):
        self._uri = uri

    def __getattr__(self, name: str) -> str:
        return f"{{{self._uri}}}{name}"

    def __call__(self, name: str) -> str:
        return f"{{{self._uri}}}{name}"


W = _NS(W_NS)
M = _NS(M_NS)
MC = _NS(MC_NS)

_FALSE = {"0", "false", "off", "none"}


def wattr(el: etree._Element | None, name: str = "val") -> str | None:
    if el is None:
        return None
    return el.get(W(name))


def on_off(el: etree._Element | None) -> bool | None:
    """Значение переключателя `<w:b/>`, `<w:b w:val="0"/>`; None, если элемента нет."""
    if el is None:
        return None
    val = el.get(W("val"))
    return val is None or val.lower() not in _FALSE


def int_attr(el: etree._Element | None, name: str = "val") -> int | None:
    val = wattr(el, name)
    if val is None:
        return None
    try:
        return int(val)
    except ValueError:
        try:
            return int(float(val))
        except ValueError:
            return None


def local(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]
