"""Tests for library.py, using temporary folders. Run with:   uv run --with pytest pytest"""

import json

import pytest

from library import BUILT_IN, YOURS, Library, LibraryError, format_file
from pattern import PatternError

STEPS = [{"color": "red"}, {"wait_ms": 1000}]
LOOP = [{"loop": [{"brightness": 50, "fade_ms": 500}, {"wait_ms": 200}], "times": 3}]


@pytest.fixture
def lib(tmp_path):
    built_in = tmp_path / "patterns"
    built_in.mkdir()
    (built_in / "storm.json").write_text(format_file("storm", "A built-in storm.", STEPS))
    return Library(built_in, tmp_path / "my-patterns")


def test_list_and_get_built_in(lib):
    [entry] = lib.list()
    assert (entry.name, entry.description, entry.source) == ("storm", "A built-in storm.", BUILT_IN)
    assert lib.get("storm").pattern == STEPS


def test_save_creates_your_folder_and_lists_both(lib):
    message = lib.save("calm", "Calm blue.", LOOP)
    assert message.startswith("Saved 'calm'") and "takes about" in message
    assert lib.user_dir.is_dir()
    assert [(e.name, e.source) for e in lib.list()] == [("calm", YOURS), ("storm", BUILT_IN)]
    assert lib.get("calm").pattern == LOOP


def test_saved_file_is_readable_json_with_one_step_per_line(lib):
    lib.save("calm", "Calm blue.", LOOP)
    text = (lib.user_dir / "calm.json").read_text()
    assert json.loads(text) == {"name": "calm", "description": "Calm blue.", "pattern": LOOP}
    assert '      {"brightness": 50, "fade_ms": 500},\n' in text


def test_saving_again_needs_replace(lib):
    lib.save("calm", "Calm blue.", LOOP)
    with pytest.raises(LibraryError, match="replace=true"):
        lib.save("calm", "Other.", STEPS)
    assert lib.save("calm", "Other.", STEPS, replace=True).startswith("Updated 'calm'")
    assert lib.get("calm").description == "Other."


def test_your_version_overrides_built_in_until_deleted(lib):
    with pytest.raises(LibraryError, match="built-in pattern; pass replace=true"):
        lib.save("storm", "My storm.", LOOP)
    assert "own version of the built-in" in lib.save("storm", "My storm.", LOOP, replace=True)
    entry = lib.get("storm")
    assert (entry.source, entry.overrides_built_in, entry.pattern) == (YOURS, True, LOOP)
    assert [e.source for e in lib.list()] == [YOURS]
    assert lib.delete("storm") == "Deleted 'storm'. The built-in version is back."
    assert lib.get("storm").source == BUILT_IN


def test_built_in_cannot_be_deleted(lib):
    with pytest.raises(LibraryError, match="can't be deleted"):
        lib.delete("storm")


def test_missing_pattern_suggests_close_name(lib):
    with pytest.raises(LibraryError, match="no pattern called 'strom' .did you mean 'storm'"):
        lib.get("strom")
    with pytest.raises(LibraryError, match="no pattern called"):
        lib.delete("nothing-here")


@pytest.mark.parametrize("name", ["Storm", "my storm", "a--b", "-a", "x" * 41, "../etc", ""])
def test_bad_names_rejected(lib, name):
    with pytest.raises(LibraryError, match="isn't a valid pattern name"):
        lib.save(name, "Desc.", STEPS)


@pytest.mark.parametrize("description", ["", "   ", "two\nlines", "x" * 201, None])
def test_bad_descriptions_rejected(lib, description):
    with pytest.raises(LibraryError, match="description"):
        lib.save("ok", description, STEPS)


def test_invalid_pattern_is_not_saved(lib):
    with pytest.raises(PatternError, match=r"pattern\[0\]: unknown key 'colour'"):
        lib.save("bad", "Bad.", [{"colour": "red"}])
    assert not (lib.user_dir / "bad.json").exists()


def test_unreadable_files_are_skipped(lib):
    lib.user_dir.mkdir()
    (lib.user_dir / "broken.json").write_text("{not json")
    assert [e.name for e in lib.list()] == ["storm"]
