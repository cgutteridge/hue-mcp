"""
The pattern library: named patterns with a short description, stored one JSON file each.

Two folders, searched in this order:
- my-patterns/   your own patterns. Git ignores this folder; everything you save goes here.
                 Set HUE_MY_PATTERNS to keep them somewhere else (e.g. a synced folder).
- patterns/      the built-in patterns that ship with the project. Read-only to the tools.

A pattern of yours with the same name as a built-in one takes its place; delete yours and the
built-in one is back. Like pattern.py, this file knows nothing about MCP or Bluetooth.

File format (the same for both folders; the file name is the pattern's name):

    {
      "name": "forest-railway",
      "description": "One line saying what it looks like.",
      "pattern": [ ...steps, see pattern.py... ]
    }
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pattern as patterns

log = logging.getLogger("hue-mcp")

HERE = Path(__file__).parent
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_NAME, MAX_DESCRIPTION = 40, 200
BUILT_IN, YOURS = "built-in", "yours"


class LibraryError(ValueError):
    """Something the caller can fix: a bad name, a missing pattern, a clash."""


@dataclass
class Entry:
    name: str
    description: str
    source: str                      # BUILT_IN or YOURS
    pattern: list[Any] = field(repr=False)
    overrides_built_in: bool = False


class Library:
    def __init__(self, built_in_dir: Path | None = None, user_dir: Path | None = None):
        self.built_in_dir = built_in_dir or HERE / "patterns"
        self.user_dir = user_dir or Path(os.environ.get("HUE_MY_PATTERNS", HERE / "my-patterns")).expanduser()

    # ── reading ────────────────────────────────────────────────────────────────

    def _read(self, path: Path, source: str) -> Entry | None:
        try:
            data = json.loads(path.read_text())
            return Entry(path.stem, str(data.get("description", "")), source, data["pattern"])
        except (OSError, ValueError, KeyError, AttributeError) as err:
            log.warning("skipping unreadable pattern file %s: %s", path, err)
            return None

    def _entries(self, folder: Path, source: str) -> dict[str, Entry]:
        if not folder.is_dir():
            return {}
        found = (self._read(p, source) for p in sorted(folder.glob("*.json")) if NAME_PATTERN.match(p.stem))
        return {e.name: e for e in found if e}

    def list(self) -> list[Entry]:
        """Every pattern, yours taking the place of built-in ones with the same name."""
        built_in = self._entries(self.built_in_dir, BUILT_IN)
        yours = self._entries(self.user_dir, YOURS)
        for name, entry in yours.items():
            entry.overrides_built_in = name in built_in
        return sorted({**built_in, **yours}.values(), key=lambda e: e.name)

    def get(self, name: str) -> Entry:
        check_name(name)
        for folder, source in ((self.user_dir, YOURS), (self.built_in_dir, BUILT_IN)):
            path = folder / f"{name}.json"
            if path.is_file():
                entry = self._read(path, source)
                if entry is None:
                    raise LibraryError(f"the file for {name!r} can't be read: {path}")
                entry.overrides_built_in = source == YOURS and (self.built_in_dir / f"{name}.json").is_file()
                return entry
        names = [e.name for e in self.list()]
        hint = difflib.get_close_matches(name, names, n=1)
        raise LibraryError(f"there's no pattern called {name!r}" + (f" (did you mean {hint[0]!r}?)" if hint else "")
                           + f". Known: {', '.join(names) or 'none'}")

    # ── writing (only ever to your folder) ─────────────────────────────────────

    def save(self, name: str, description: str, steps: Any, replace: bool = False,
             check_color: Callable[[str], Any] | None = None) -> str:
        """Check and save a pattern. Returns a sentence saying what happened."""
        check_name(name)
        description = check_description(description)
        parsed = patterns.parse(steps, check_color)  # raises PatternError, which says which step is wrong
        path = self.user_dir / f"{name}.json"
        is_built_in = (self.built_in_dir / f"{name}.json").is_file()
        if path.exists() and not replace:
            raise LibraryError(f"you already have a pattern called {name!r}; pass replace=true to update it")
        if is_built_in and not path.exists() and not replace:
            raise LibraryError(f"{name!r} is a built-in pattern; pass replace=true to save your own version "
                               "in its place (deleting yours later brings the built-in one back)")
        existed = path.exists()
        self.user_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(format_file(name, description, steps))
        tmp.replace(path)  # atomic: a reader never sees half a file
        what = "Updated" if existed else ("Saved your own version of the built-in" if is_built_in else "Saved")
        return f"{what} {name!r}: it {patterns.describe(parsed)}."

    def delete(self, name: str) -> str:
        check_name(name)
        path = self.user_dir / f"{name}.json"
        built_in = (self.built_in_dir / f"{name}.json").is_file()
        if not path.is_file():
            if built_in:
                raise LibraryError(f"{name!r} is a built-in pattern and can't be deleted from here")
            self.get(name)  # raises a helpful "no pattern called…"
        path.unlink()
        return f"Deleted {name!r}." + (" The built-in version is back." if built_in else "")


def check_name(name: Any) -> None:
    if not isinstance(name, str) or not NAME_PATTERN.match(name) or len(name) > MAX_NAME:
        raise LibraryError(f"{name!r} isn't a valid pattern name: use lowercase letters, digits and single "
                           f"hyphens, up to {MAX_NAME} characters (e.g. \"forest-railway\")")


def check_description(description: Any) -> str:
    if not isinstance(description, str) or not description.strip():
        raise LibraryError("a pattern needs a short description: one line saying what it looks like")
    description = description.strip()
    if "\n" in description or len(description) > MAX_DESCRIPTION:
        raise LibraryError(f"the description should be one line of at most {MAX_DESCRIPTION} characters")
    return description


KEY_ORDER = ["on", "color", "brightness", "fade_ms", "wait_ms", "loop", "times", "for_ms", "min", "max"]


def _ordered(obj: Any) -> Any:
    """Keys in a fixed, readable order (on, color, brightness, fade_ms...; min before max),
    whatever order they arrived in: some clients send them sorted alphabetically."""
    if isinstance(obj, list):
        return [_ordered(item) for item in obj]
    if isinstance(obj, dict):
        rank = {k: i for i, k in enumerate(KEY_ORDER)}
        return {k: _ordered(obj[k]) for k in sorted(obj, key=lambda k: (rank.get(k, len(rank)), k))}
    return obj


def format_file(name: str, description: str, steps: list[Any]) -> str:
    """Pretty JSON for humans: one step per line, loops indented."""
    def fmt(obj: Any, depth: int) -> str:
        pad = "  " * depth
        if isinstance(obj, list):
            return "[\n" + ",\n".join(pad + "  " + fmt(item, depth + 1) for item in obj) + "\n" + pad + "]"
        if isinstance(obj, dict) and "loop" in obj:
            rest = {k: v for k, v in obj.items() if k != "loop"}
            return '{"loop": ' + fmt(obj["loop"], depth) + (", " + json.dumps(rest)[1:-1] if rest else "") + "}"
        return json.dumps(obj)
    steps = _ordered(steps)
    return ('{\n  "name": %s,\n  "description": %s,\n  "pattern": %s\n}\n'
            % (json.dumps(name), json.dumps(description), fmt(steps, 1)))
