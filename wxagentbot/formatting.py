"""Presentation of AI results for WeChat; the transport sends text verbatim."""
from __future__ import annotations

import re


PLAIN_TEXT_RULES = (
    "最终回复会直接作为微信普通文本发送。请使用自然段、换行和 1. 2. 这样的编号。"
    "不要使用 Markdown 加粗、斜体、标题标记、表格、引用标记或代码围栏。"
    "链接使用完整 URL；比较信息按每项一段呈现；代码直接以纯文本和缩进呈现。"
    "这些约束只针对微信回复，不改变用户要求生成的文件格式、代码语法或数学符号。"
)


def _protect_code(text, *, plain=False):
    prefix = "\x00wechat-code-"
    while prefix in text:
        prefix += "x"
    codes = []
    def save(match):
        index = len(codes)
        codes.append(match.group(2) if plain else match.group(0))
        return f"{prefix}{index}\x00"
    protected = re.sub(r"(?<!`)(`+)([^\n]*?)\1(?!`)", save, text)
    def restore(value):
        for index, code in enumerate(codes):
            value = value.replace(f"{prefix}{index}\x00", code)
        return value
    return protected, restore


def _inline(text):
    text, restore = _protect_code(text, plain=True)
    prefix, urls = "\x00wechat-url-", []
    while prefix in text:
        prefix += "x"
    def keep_url(value):
        urls.append(value)
        return f"{prefix}{len(urls) - 1}\x00"
    text = re.sub(r"!?\[([^\]\n]*)\]\(([^()\s]+(?:\([^()\s]*\)[^()\s]*)*)\)",
                  lambda m: keep_url(m[2]) if not m[1] or m[1] == m[2] else f"{m[1]}：{keep_url(m[2])}", text)
    text = re.sub(r"https?://[^\s<>\x00]+", lambda m: keep_url(m[0]), text)
    text = re.sub(r"(?<![A-Za-z0-9_\\])(\*\*|__)(?=\S)(.+?)(?<=\S)\1(?![A-Za-z0-9_])", r"\2", text)
    text = re.sub(r"(?<![\w*\\])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])", r"\1", text)
    text = re.sub(r"(?<!\\)~~(?=\S)(.+?)(?<=\S)~~", r"\1", text)
    for index, url in enumerate(urls):
        text = text.replace(f"{prefix}{index}\x00", url)
    return restore(text)


def _cells(line):
    protected, restore = _protect_code(line)
    cells = re.split(r"(?<!\\)\|", protected.strip())
    if cells and not cells[0].strip():
        cells.pop(0)
    if cells and not cells[-1].strip():
        cells.pop()
    return [restore(c.strip()).replace(r"\|", "|") for c in cells]


def wechat_plain_text(text: str) -> str:
    """Clean common prose markup, retaining code contents, URLs and operators."""
    lines = text.splitlines()
    output, fence = [], None
    index = 0
    separator = re.compile(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*")
    while index < len(lines):
        line = lines[index]
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line)
        if fence is not None:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
                output.append("")
            else:
                output.append(line)
            index += 1
            continue
        if marker:
            fence = marker[1]
            output.append("")
            index += 1
            continue
        if index + 1 < len(lines) and "|" in line and separator.fullmatch(lines[index + 1]):
            headers = _cells(line)
            rows, cursor = [], index + 2
            while cursor < len(lines) and "|" in lines[cursor]:
                values = _cells(lines[cursor])
                if len(values) != len(headers):
                    break
                rows.append("；".join(f"{_inline(h)}：{_inline(v)}" for h, v in zip(headers, values)))
                cursor += 1
            if rows:
                output.extend(rows)
                index = cursor
                continue
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)
        line = re.sub(r"^\s{0,3}(?:>\s*)+", "", line)
        line = re.sub(r"^(\s*)[-+*]\s+", r"\1• ", line)
        if re.fullmatch(r"\s*(?:-{3,}|\*{3,}|_{3,})\s*", line):
            line = ""
        output.append(_inline(line))
        index += 1
    return "\n".join(output).strip("\n")
