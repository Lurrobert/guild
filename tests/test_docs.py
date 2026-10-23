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
