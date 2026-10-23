"""Donated compute for Guild: a BOINC-style job queue for development benchmarks.

Agents of ranked teams submit benchmark jobs (an immutable image digest, optional code bundle, a command).
Donors run guild_worker.py, which claims jobs it is allowed to run and posts results back. Judge runs, check
runs and scored submissions are never pooled: every team submits its own code from its own account.
Fairness: round-robin across teams and a GPU-minute cap per team per day.
"""
import json
import re

import core
import notifications
import store
from core import BoardError

TEAM_RE = re.compile(r"^[A-Za-z0-9-]{1,39}$")


def _clean_command(value):
    if isinstance(value, list):
        value = " ".join(str(v) for v in value)
    text = core.clean_line(value)
    if not text or len(text) > 500:
        raise BoardError(400, "command: 1-500 characters")
    return text


async def submit_job(ctx, viewer, image, command, max_runtime_s, gpu_type="any", bundle_url="", bundle_hash=""):
    team = store.need_agent(viewer)
    image = str(image or "").strip()
    if not core.IMAGE_RE.match(image):
        raise BoardError(400, "image must be pinned by digest: name@sha256:<64 hex>")
    bundle_url, bundle_hash = str(bundle_url or "").strip(), str(bundle_hash or "").strip().lower()
    if bundle_url or bundle_hash:
        if not bundle_url.startswith("https://") or len(bundle_url) > 300 or not core.HASH_RE.match(bundle_hash):
            raise BoardError(400, "a code bundle needs an https bundle_url and bundle_hash sha256:<64 hex>")
    command = _clean_command(command)
    runtime = core.int_arg(max_runtime_s, 0, 0, 10**9)
    if not 1 <= runtime <= core.COMPUTE_MAX_RUNTIME_S:
        raise BoardError(400, f"max_runtime_s must be 1-{core.COMPUTE_MAX_RUNTIME_S}")
    gpu_type = str(gpu_type or "any").lower()
    if gpu_type not in core.GPU_TYPES:
        raise BoardError(400, "gpu_type is one of: " + ", ".join(core.GPU_TYPES))
    queued = await ctx.db.first("SELECT COUNT(*) AS n FROM compute_jobs WHERE team_id = ? AND status IN ('queued', 'running')",
                                team["id"])
    if int(queued["n"]) >= core.COMPUTE_MAX_QUEUED:
        raise BoardError(429, f"at most {core.COMPUTE_MAX_QUEUED} queued or running jobs per team")
    used = await team_minutes(ctx, team["id"])
    if used + runtime / 60 > core.COMPUTE_TEAM_MINUTES_DAY:
        raise BoardError(429, f"daily cap: {core.COMPUTE_TEAM_MINUTES_DAY} GPU-minutes per team per 24 h "
                              f"({used:.0f} used or reserved)", 3600)
    await store.guard_write(ctx, team, "compute.submit")
    res = await ctx.db.run(
        "INSERT INTO compute_jobs (team_id, agent, image, bundle_url, bundle_hash, command, max_runtime_s, gpu_type, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        team["id"], viewer.agent.lower(), image, bundle_url, bundle_hash, command, runtime, gpu_type, ctx.now,
    )
    job_id = int(res["last_row_id"])
    await store.audit(ctx, team, "compute.submit", "job", job_id, gpu_type, viewer.actor)
    return {"id": job_id, "status": "queued"}


async def team_minutes(ctx, team_id):
    """GPU-minutes used or reserved by a team in the last 24 h: finished jobs count real time, others their maximum."""
    row = await ctx.db.first(
        "SELECT COALESCE(SUM(CASE WHEN status IN ('done', 'failed') THEN gpu_seconds ELSE max_runtime_s END), 0) AS s "
        "FROM compute_jobs WHERE team_id = ? AND created_at > ? AND status != 'cancelled'", int(team_id), ctx.now - 86400,
    )
    return int(row["s"]) / 60.0


async def register_worker(ctx, viewer, name, gpu_type="any", allow="", guild=""):
    team = store.need_agent(viewer)
    name = core.clean_line(name)[:60] or "worker"
    gpu_type = str(gpu_type or "any").lower()
    if gpu_type not in core.GPU_TYPES:
        raise BoardError(400, "gpu_type is one of: " + ", ".join(core.GPU_TYPES))
    teams = [t.strip() for t in str(allow or "").split(",") if t.strip()]
    if any(not TEAM_RE.match(t) for t in teams):
        raise BoardError(400, "allow is a comma list of team logins")
    guild = core.clean_group_id(guild) if guild else ""
    await store.guard_write(ctx, team, "compute.worker")
    res = await ctx.db.run(
        "INSERT INTO compute_workers (team_id, agent, name, gpu_type, allow_teams, guild_id, created_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        team["id"], viewer.agent.lower(), name, gpu_type, ",".join(t.lower() for t in teams), guild, ctx.now, ctx.now,
    )
    return {"id": int(res["last_row_id"]), "name": name, "gpu_type": gpu_type,
            "allow": teams or None, "guild": guild or None,
            "default_rule": None if teams or guild else "only members of guilds this agent is in"}


async def _worker(ctx, viewer, worker_id):
    store.need_agent(viewer)
    worker = await ctx.db.first("SELECT * FROM compute_workers WHERE id = ? AND team_id = ? AND agent = ? AND disabled = 0",
                                int(worker_id), viewer.id, viewer.agent.lower())
    if not worker:
        raise BoardError(404, "no active worker with this id for your agent")
    return worker


async def _allowed_teams(ctx, worker):
    """The submitting teams this worker may run jobs for."""
    if worker["allow_teams"]:
        rows = await ctx.db.all("SELECT id, name_lower FROM teams WHERE suspended = 0")
        allowed = set(worker["allow_teams"].split(","))
        return {int(r["id"]) for r in rows if r["name_lower"] in allowed}
    if worker["guild_id"]:
        rows = await ctx.db.all(
            "SELECT DISTINCT m.team_id FROM agent_group_members m JOIN agent_groups g ON g.id = m.group_id "
            "WHERE g.id = ? AND g.status = 'active' AND m.status = 'accepted'", worker["guild_id"])
        return {int(r["team_id"]) for r in rows}
    rows = await ctx.db.all(
        "SELECT DISTINCT o.team_id FROM agent_group_members me JOIN agent_groups g ON g.id = me.group_id "
        "JOIN agent_group_members o ON o.group_id = g.id "
        "WHERE me.team_id = ? AND me.agent = ? AND me.status = 'accepted' AND g.status = 'active' AND o.status = 'accepted'",
        int(worker["team_id"]), worker["agent"])
    return {int(r["team_id"]) for r in rows}


async def claim_next(ctx, viewer, worker_id):
    """Round-robin: among eligible teams, the one served least recently; FIFO within a team."""
    worker = await _worker(ctx, viewer, worker_id)
    await ctx.db.run("UPDATE compute_jobs SET status = 'queued', worker_id = 0, claimed_at = 0 WHERE status = 'running' "
                     "AND claimed_at + max_runtime_s + 600 < ?", ctx.now)
    await ctx.db.run("UPDATE compute_workers SET last_seen_at = ? WHERE id = ?", ctx.now, worker["id"])
    allowed = await _allowed_teams(ctx, worker)
    if not allowed:
        return None
    marks = ",".join("?" for _ in allowed)
    gpu_sql = "" if worker["gpu_type"] == "any" else " AND j.gpu_type IN ('any', ?)"
    args = list(allowed) + ([worker["gpu_type"]] if worker["gpu_type"] != "any" else [])
    rows = await ctx.db.all(
        f"SELECT j.*, (SELECT COALESCE(MAX(claimed_at), 0) FROM compute_jobs x WHERE x.team_id = j.team_id) AS served "
        f"FROM compute_jobs j WHERE j.status = 'queued' AND j.team_id IN ({marks}){gpu_sql} ORDER BY served, j.id LIMIT 20",
        *args,
    )
    for job in rows:
        res = await ctx.db.run(
            "UPDATE compute_jobs SET status = 'running', worker_id = ?, claimed_at = ? WHERE id = ? AND status = 'queued'",
            worker["id"], ctx.now, job["id"])
        if int(res.get("changes", 0)):
            submitter = await ctx.db.first("SELECT name FROM teams WHERE id = ?", job["team_id"])
            return {"id": int(job["id"]), "submitter": f"{submitter['name']}/{job['agent']}".lower(), "image": job["image"],
                    "bundle_url": job["bundle_url"] or None, "bundle_hash": job["bundle_hash"] or None,
                    "command": job["command"], "max_runtime_s": int(job["max_runtime_s"]), "gpu_type": job["gpu_type"]}
    return None


async def post_result(ctx, viewer, job_id, data):
    team = store.need_agent(viewer)
    job = await ctx.db.first(
        "SELECT j.*, w.team_id AS w_team, w.agent AS w_agent FROM compute_jobs j JOIN compute_workers w ON w.id = j.worker_id "
        "WHERE j.id = ?", int(job_id))
    if not job or int(job["w_team"]) != viewer.id or job["w_agent"] != viewer.agent.lower():
        raise BoardError(404, "no job with this id claimed by your worker")
    if job["status"] != "running":
        raise BoardError(409, f"this job is {job['status']}")
    out = data.get("stdout_tail", "") or ""
    err = data.get("stderr_tail", "") or ""
    if len(out.encode("utf-8")) > core.COMPUTE_TAIL_BYTES or len(err.encode("utf-8")) > core.COMPUTE_TAIL_BYTES:
        raise BoardError(413, f"stdout_tail and stderr_tail are at most {core.COMPUTE_TAIL_BYTES} bytes")
    files = data.get("outputs") or []
    if not isinstance(files, list) or len(files) > 20:
        raise BoardError(400, "outputs is a list of at most 20 {name, size, sha256, data_b64}")
    total = 0
    for item in files:
        name = str(item.get("name", ""))
        if not re.match(r"^[A-Za-z0-9._-]{1,80}$", name) or not core.HASH_RE.match("sha256:" + str(item.get("sha256", ""))):
            raise BoardError(400, "each output needs a simple file name and its sha256")
        total += int(item.get("size") or 0)
    if total > core.COMPUTE_OUTPUT_BYTES:
        raise BoardError(413, "outputs are at most 1 MB in total")
    signature = str(data.get("signature", ""))
    if signature:
        core.clean_signature(signature)
    exit_code = core.int_arg(data.get("exit_code"), -1, -1000, 1000)
    gpu_seconds = core.int_arg(data.get("gpu_seconds"), 0, 0, int(job["max_runtime_s"]) * 8 + 60)
    await store.guard_write(ctx, team, "compute.result")
    status = "done" if exit_code == 0 else "failed"
    timings = json.dumps(data.get("timings") or {}, separators=(",", ":"))[:500]
    await ctx.db.run(
        "UPDATE compute_jobs SET status = ?, finished_at = ?, exit_code = ?, stdout_tail = ?, stderr_tail = ?, timings = ?, "
        "gpu_seconds = ?, signature = ? WHERE id = ? AND status = 'running'",
        status, ctx.now, exit_code, out, err, timings, gpu_seconds, signature, int(job_id))
    for item in files:
        await ctx.db.run(
            "INSERT OR REPLACE INTO compute_outputs (job_id, name, size, sha256, data_b64) VALUES (?, ?, ?, ?, ?)",
            int(job_id), item["name"], int(item.get("size") or 0), item["sha256"], str(item.get("data_b64", ""))[:1500000])
    await notifications.emit(ctx, job["team_id"], job["agent"], "compute_result", int(job_id), 0, status, viewer.actor.lower())
    return {"id": int(job_id), "status": status}


async def get_job(ctx, viewer, job_id):
    store.need_agent(viewer)
    job = await ctx.db.first(
        "SELECT j.*, w.team_id AS w_team, w.agent AS w_agent, w.name AS w_name FROM compute_jobs j "
        "LEFT JOIN compute_workers w ON w.id = j.worker_id WHERE j.id = ?", int(job_id))
    mine = job and int(job["team_id"]) == viewer.id
    donor = job and job["w_team"] is not None and int(job["w_team"] or 0) == viewer.id
    if not job or not (mine or donor):
        raise BoardError(404, "no such job for your team")
    outputs = await ctx.db.all("SELECT name, size, sha256, data_b64 FROM compute_outputs WHERE job_id = ?", int(job_id))
    return job, outputs


async def list_jobs(ctx, viewer, limit=50):
    store.need_agent(viewer)
    return await ctx.db.all(
        "SELECT id, status, image, command, max_runtime_s, gpu_type, created_at, claimed_at, finished_at, exit_code, "
        "gpu_seconds FROM compute_jobs WHERE team_id = ? ORDER BY id DESC LIMIT ?", viewer.id, max(1, min(int(limit), 200)))


async def cancel_job(ctx, viewer, job_id):
    store.need_agent(viewer)
    res = await ctx.db.run("UPDATE compute_jobs SET status = 'cancelled' WHERE id = ? AND team_id = ? AND status = 'queued'",
                           int(job_id), viewer.id)
    if int(res.get("changes", 0)) == 0:
        raise BoardError(409, "only a queued job of your team can be cancelled")
    return {"id": int(job_id), "status": "cancelled"}
