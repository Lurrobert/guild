import os

import docs
import gen_docs
from harness import ROOT


def test_docs_module_is_in_sync():
    with open(os.path.join(ROOT, "src", "docs.py"), encoding="utf-8") as handle:
        assert handle.read() == gen_docs.render(), "run: python3 tools/gen_docs.py"


def test_agents_md_has_the_required_rules():
    text = docs.SKILL_MD
    for phrase in ("Work in groups with other teams' agents and their humans", "No judge or harness exploits",
                   "No gaming the workload", "hidden-test data", "Each agent decides what is fair",
                   "Never share secrets, tokens", "no hidden instructions"):
        assert phrase.lower() in text.lower(), phrase


def test_security_states_the_metadata_limit():
    assert "the server sees metadata" in docs.SECURITY_MD
    assert "who sent a copy to whom" in docs.SECURITY_MD


def test_no_duplicate_top_level_names_in_server_modules():
    """A second def with the same name silently replaces the first (this broke j_job once)."""
    import ast
    for name in os.listdir(os.path.join(ROOT, "src")):
        if not name.endswith(".py") or name in ("docs.py", "version.py"):
            continue
        with open(os.path.join(ROOT, "src", name)) as handle:
            tree = ast.parse(handle.read())
        seen = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.name not in seen, f"{name}: {node.name} defined twice (lines {seen.get(node.name)}, {node.lineno})"
                seen[node.name] = node.lineno
