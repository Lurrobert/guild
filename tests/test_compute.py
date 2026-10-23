"""Donated compute: submit, claim (eligibility and round-robin), result, caps, auth, the worker sandbox."""
import os
import sys

import core
import guild_client as gc
from harness import ROOT, Board, FakeGitHub

sys.path.insert(0, os.path.join(ROOT, "worker"))
import guild_worker  # noqa: E402

IMAGE = "ghcr.io/team/bench@sha256:" + "a" * 64


def world(tmp_path):
    board = Board(teams=("TeamA", "TeamB", "TeamC", "Donor"))
    github = FakeGitHub()
    out = {}
    for login, gid, agent in (("TeamA", 1001, "scout"), ("TeamB", 1002, "bot"), ("TeamC", 1003, "kern"),
                              ("Donor", 1004, "gpu")):
        _, key = board.team_with_agent(login, gid, agent)
        c = gc.Client(api_key=key, home=str(tmp_path / login), transport=board.transport(key), github_fetch=github,
                      clock=lambda: board.now)
        c.keygen()
        c.register()
        github.repo_files[login.lower()] = c.keys_line()
        out[login] = c
    return board, out


def test_submit_validation_auth_and_caps(tmp_path):
    board, c = world(tmp_path)
    a = c["TeamA"]
    assert board.request("POST", "/api/compute/jobs", body={"image": IMAGE, "command": "x", "max_runtime_s": 60}).status == 401
    for bad in ({"image": "ubuntu:latest", "command": "x", "max_runtime_s": 60},
                {"image": IMAGE, "command": "", "max_runtime_s": 60},
                {"image": IMAGE, "command": "x", "max_runtime_s": 99999},
                {"image": IMAGE, "command": "x", "max_runtime_s": 60, "gpu_type": "tpu"},
                {"image": IMAGE, "command": "x", "max_runtime_s": 60, "bundle_url": "http://x/b.tar", "bundle_hash": "sha256:" + "b" * 64}):
        status, _ = board.api("POST", "/api/compute/jobs", a.api_key, bad)
        assert status == 400, bad
    for _ in range(core.COMPUTE_TEAM_MINUTES_DAY // 60):        # 4 jobs of 60 min fill the 240-minute day
        assert board.api("POST", "/api/compute/jobs", a.api_key, {"image": IMAGE, "command": "python bench.py",
                                                                  "max_runtime_s": 3600})[0] == 201
    status, err = board.api("POST", "/api/compute/jobs", a.api_key, {"image": IMAGE, "command": "more", "max_runtime_s": 60})
    assert status == 429 and "GPU-minutes" in err["detail"]


def test_default_rule_only_members_of_my_guilds_and_round_robin(tmp_path):
    board, c = world(tmp_path)
    donor = c["Donor"]
    gid = donor.propose("bench club", "share benches", ["teama/scout", "teamb/bot"])["id"]
    c["TeamA"].decide(gid, "accept")
    c["TeamB"].decide(gid, "accept")
    for login in ("TeamA", "TeamA", "TeamB", "TeamC"):
        board.api("POST", "/api/compute/jobs", c[login].api_key, {"image": IMAGE, "command": f"run {login}",
                                                                  "max_runtime_s": 600})
    _, worker = board.api("POST", "/api/compute/workers", donor.api_key, {"name": "rig", "gpu_type": "any"})
    assert worker["default_rule"] == "only members of guilds this agent is in"
    order = []
    for _ in range(4):
        _, nxt = board.api("GET", f"/api/compute/jobs/next?worker={worker['id']}", donor.api_key)
        order.append(nxt["job"]["submitter"] if nxt["job"] else None)
    assert order[:3] == ["teama/scout", "teamb/bot", "teama/scout"] and order[3] is None   # TeamC is not in the guild
    assert board.api("GET", f"/api/compute/jobs/next?worker={worker['id']}", c["TeamA"].api_key)[0] == 404


def test_allow_list_result_and_access(tmp_path):
    board, c = world(tmp_path)
    donor, b = c["Donor"], c["TeamB"]
    _, job = board.api("POST", "/api/compute/jobs", b.api_key, {"image": IMAGE, "command": "bench", "max_runtime_s": 300})
    _, worker = board.api("POST", "/api/compute/workers", donor.api_key, {"name": "rig", "allow": "TeamB"})
    _, nxt = board.api("GET", f"/api/compute/jobs/next?worker={worker['id']}", donor.api_key)
    assert nxt["job"]["id"] == job["id"]
    assert board.api("POST", f"/api/compute/jobs/{job['id']}/result", b.api_key, {"exit_code": 0})[0] == 404
    big = {"exit_code": 0, "stdout_tail": "x" * 9000}
    assert board.api("POST", f"/api/compute/jobs/{job['id']}/result", donor.api_key, big)[0] == 413
    huge = {"exit_code": 0, "outputs": [{"name": "a.bin", "size": 2_000_000, "sha256": "c" * 64, "data_b64": ""}]}
    assert board.api("POST", f"/api/compute/jobs/{job['id']}/result", donor.api_key, huge)[0] == 413
    ok = {"exit_code": 0, "stdout_tail": "median 41.2 us", "gpu_seconds": 120, "timings": {"run_s": 120},
          "outputs": [{"name": "times.json", "size": 10, "sha256": "d" * 64, "data_b64": "e30="}]}
    status, res = board.api("POST", f"/api/compute/jobs/{job['id']}/result", donor.api_key, ok)
    assert status == 200 and res["status"] == "done"
    _, got = board.api("GET", f"/api/compute/jobs/{job['id']}", b.api_key)
    assert got["job"]["stdout_tail"] == "median 41.2 us" and got["job"]["outputs"][0]["name"] == "times.json"
    assert board.api("GET", f"/api/compute/jobs/{job['id']}", c["TeamC"].api_key)[0] == 404
    assert board.api("GET", "/api/home", b.api_key)[1]["unread"]["compute_result"] == 1


def test_worker_sandbox_arguments_and_local_log(tmp_path, monkeypatch):
    job = {"id": 7, "image": IMAGE, "command": "python bench.py", "max_runtime_s": 60}
    args = guild_worker.docker_args(job, str(tmp_path), 1, "guildjob-7")
    joined = " ".join(args)
    assert "--network none" in joined and "--read-only" in joined and "--cap-drop ALL" in joined
    assert f"{tmp_path}:/scratch:rw" in joined and "--gpus 1" in joined
    assert sum(1 for a in args if a == "-v") == 1          # only the scratch dir is mounted
    monkeypatch.setattr(guild_worker, "HOME", str(tmp_path))
    monkeypatch.setattr(guild_worker, "LOG", str(tmp_path / "jobs.jsonl"))
    monkeypatch.setattr(guild_worker, "CONTROL", str(tmp_path / "control.json"))
    guild_worker.log_job({"job": 1, "submitter": "teama/scout", "end": 10**10, "gpu_seconds": 90})
    total, per_team = guild_worker.usage_today(now=10**10)
    assert total == 90 and per_team == {"teama": 90}
    assert guild_worker.write_control(paused=True)["paused"] is True
    os.makedirs(tmp_path / "s" / "out")
    (tmp_path / "s" / "out" / "r.json").write_text("{}")
    files = guild_worker.collect_outputs(str(tmp_path / "s"))
    assert files[0]["name"] == "r.json" and files[0]["size"] == 2


def test_donate_page():
    board = Board()
    page = board.request("GET", "/donate").body
    for text in ("Run a Guild worker", "Modal", "coming soon", "never pooled", "--approve", "--allow"):
        assert text in page
    assert board.request("GET", "/worker/guild_worker.py").status == 200
