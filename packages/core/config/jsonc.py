from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_COMMENT_BLOCK_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_COMMENT_LINE_RE = re.compile(r"(^|\s)//.*$", re.MULTILINE)


def strip_jsonc_comments(text: str) -> str:
    """Remove basic JSONC comments so the content can be parsed as JSON."""
    no_block = _COMMENT_BLOCK_RE.sub("", text)
    return _COMMENT_LINE_RE.sub("", no_block)


def load_jsonc(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    cleaned = strip_jsonc_comments(raw)
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        msg = f"JSON root must be object: {path}"
        raise ValueError(msg)
    return data


def _write_jsonc_safe(path: Path, data: dict[str, Any]) -> None:
    """Atomically write a dict to a JSONC file (strips comments — use
    :func:`update_jsonc_key` when comments need preserving)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(dump_json(data), encoding="utf-8")
    tmp.replace(path)


def _find_jsonc_value_span(text: str, key: str) -> tuple[int, int] | None:
    """Find the span ``[start, end)`` of a top-level JSON value for *key*.

    Handles objects ``{}``, arrays ``[]``, strings, numbers, booleans,
    and ``null``.  Returns ``None`` if *key* is not found.
    """
    pat = re.compile(r'"' + re.escape(key) + r'"\s*:\s*', re.DOTALL)
    m = pat.search(text)
    if not m:
        return None
    start = m.start()
    val_start = m.end()

    # Determine value type from first non-whitespace char after ":"
    # Scan forward past whitespace to find the value start
    in_str = False
    esc = False
    i = val_start
    while i < len(text) and text[i] in ' \t\n\r':
        i += 1
    if i >= len(text):
        return (start, len(text))

    first = text[i]
    if first == '{':
        # Object: track {} depth
        depth = 0
        for j in range(i, len(text)):
            c = text[j]
            if esc: esc = False; continue
            if c == '\\': esc = True; continue
            if c == '"': in_str = not in_str; continue
            if in_str: continue
            if c == '{': depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    return (start, j + 1)
    elif first == '[':
        # Array: track [] depth
        depth = 0
        for j in range(i, len(text)):
            c = text[j]
            if esc: esc = False; continue
            if c == '\\': esc = True; continue
            if c == '"': in_str = not in_str; continue
            if in_str: continue
            if c == '[': depth += 1
            elif c == ']':
                depth -= 1
                if depth == 0:
                    return (start, j + 1)
    elif first == '"':
        # String: find closing "
        for j in range(i + 1, len(text)):
            c = text[j]
            if esc: esc = False; continue
            if c == '\\': esc = True; continue
            if c == '"':
                return (start, j + 1)
    else:
        # Number, boolean, or null: scan until delimiter
        for j in range(i, len(text)):
            if text[j] in ',}\n\r\t ]':
                return (start, j)

    return (start, len(text))


def _indent_json(text: str, indent: int = 2) -> str:
    """Add *indent* spaces of extra indentation to every line of *text*
    except the first (which is assumed to be the opening bracket on the
    same line as the key)."""
    lines = text.split("\n")
    indented = [lines[0]]
    for line in lines[1:]:
        if line.strip():
            indented.append(" " * indent + line)
        else:
            indented.append(line)
    return "\n".join(indented)


def update_jsonc_key(file_path: Path, key: str, new_value: Any, *, indent_shift: int = 0) -> None:
    """Replace a top-level *key*'s value in a JSONC file, preserving comments.

    The replacement is done via targeted text substitution so that
    all comments and other keys around it remain intact.

    Args:
        file_path: Path to the JSONC file.
        key: Top-level key to replace (e.g. ``"tasks"``, ``"creators"``, ``"platform"``).
        new_value: The new value (will be ``json.dumps``-ed).
        indent_shift: Extra indentation to add to each line of the serialized value
                      (use 2 when the value is an array at 2-space indent but the file
                      expects 4-space indent like ``"key": [\\n    {...}\\n  ]``).
    """
    raw_text = file_path.read_text(encoding="utf-8")
    span = _find_jsonc_value_span(raw_text, key)

    raw_new = json.dumps(new_value, ensure_ascii=False, indent=2)
    if indent_shift:
        raw_new = _indent_json(raw_new, indent=indent_shift)

    replacement = f'"{key}": {raw_new}'

    if span is None:
        # Key not found — append before closing brace
        new_text = raw_text.rstrip().rstrip("}").rstrip() + ",\n" + replacement + "\n}"
    else:
        start, end = span
        new_text = raw_text[:start] + replacement + raw_text[end:]

    tmp = file_path.with_suffix(".jsonc.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(file_path)


_COMPACT_URL_PATTERN = re.compile(
    r'\{\s*\n\s*"url":\s*(".+?")\s*,\s*\n\s*"enabled":\s*(true|false)\s*\n\s*\}',
)


def dump_json(data: dict[str, Any]) -> str:
    formatted = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    # Compact {url, enabled} objects onto a single line.
    formatted = _COMPACT_URL_PATTERN.sub(r'{"url": \1, "enabled": \2}', formatted)
    return formatted
