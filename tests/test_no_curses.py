import ast
from pathlib import Path

_AGENT_TOOLS = Path(__file__).resolve().parent.parent / "agent_tools"


def _imports_curses(source: str) -> bool:
    """True when any `import curses` or `from curses ...` appears, at any depth; mentions in text do not count."""
    return any(
        (isinstance(node, ast.Import) and any(a.name.split(".")[0] == "curses" for a in node.names))
        or (isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").split(".")[0] == "curses")
        for node in ast.walk(ast.parse(source))
    )


def test_no_agent_tools_module_imports_curses():
    modules = sorted(_AGENT_TOOLS.glob("*.py"))
    assert modules
    offenders = [m.name for m in modules if _imports_curses(m.read_text(encoding="utf-8"))]
    assert offenders == []


def test_the_check_sees_top_level_and_function_level_imports():
    assert _imports_curses("import curses\n")
    assert _imports_curses("from curses import wrapper\n")
    assert _imports_curses("def f():\n    import curses\n")
    assert not _imports_curses('"""curses is gone"""\n# import curses\n')
