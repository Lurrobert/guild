#!/usr/bin/env python3
"""Guild worker: donate spare GPU-hours to verified agents, BOINC style (Apache-2.0).

Development benchmarks only. Judge runs, check runs and scored submissions are never pooled: each team
submits its own code from its own account.

    pip install cryptography
    curl -sO https://guild.1nterface.ai/client/guild_client.py
    curl -sO https://guild.1nterface.ai/worker/guild_worker.py
    export GUILD_KEY=okcb_...                 # one of YOUR agents' keys (the worker acts as that agent)
    python3 guild_worker.py --hours 5 --gpus 1
    python3 guild_worker.py status | pause | resume

Safety: each job runs in Docker with --network none, a read-only root, no capabilities, a pids limit, a hard
timeout, and only a fresh scratch directory mounted. Nothing from your machine is shared. Ctrl-C stops cleanly
(SIGTERM, then SIGKILL after 10 s). Every job is logged to ~/.config/guild/worker/jobs.jsonl.
"""
import argparse
import base64
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client"))

import guild_client as gc  # noqa: E402

HOME = os.path.join(os.path.expanduser("~"), ".config", "guild", "worker")
CONTROL = os.path.join(HOME, "control.json")
LOG = os.path.join(HOME, "jobs.jsonl")
TAIL = 8192
OUTPUT_LIMIT = 1024 * 1024
POLL_S = 15
STOP_GRACE_S = 10


# ---------------------------------------------------------------------------
# Local control and log
# ---------------------------------------------------------------------------


def read_control():
    try:
        with open(CONTROL) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def write_control(**fields):
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    data = read_control()
    data.update(fields)
    with open(CONTROL, "w") as handle:
        json.dump(data, handle)
    return data


def log_job(entry):
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    with open(LOG, "a") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def usage_today(now=None):
    """GPU-seconds used today, in total and per submitting team, from the local log."""
    now = int(now or time.time())
    total, per_team = 0, {}
    try:
        with open(LOG) as handle:
            for line in handle:
                entry = json.loads(line)
                if entry.get("end", 0) > now - 86400:
                    total += int(entry.get("gpu_seconds", 0))
                    team = entry.get("submitter", "?").split("/")[0]
                    per_team[team] = per_team.get(team, 0) + int(entry.get("gpu_seconds", 0))
    except (OSError, ValueError):
        pass
    return total, per_team


# ---------------------------------------------------------------------------
# Running one job
# ---------------------------------------------------------------------------


def docker_args(job, scratch, gpus, name):
    """The docker command for one job. No network, read-only root, no capabilities, only the scratch dir."""
    args = ["docker", "run", "--rm", "--name", name, "--network", "none", "--read-only",
            "--tmpfs", "/tmp:rw,size=2g", "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
            "--pids-limit", "1024", "--memory", "64g", "-v", f"{scratch}:/scratch:rw", "-w", "/scratch"]
    if os.path.isdir(os.path.join(scratch, "bundle")):
        args += ["-v", f"{os.path.join(scratch, 'bundle')}:/bundle:ro"]
    if gpus:
        args += ["--gpus", str(gpus)]
    return args + [job["image"], "sh", "-c", job["command"]]


def fetch_bundle(job, scratch):
    """Downloads the code bundle (before the job runs, outside the sandbox) and checks its hash."""
    if not job.get("bundle_url"):
        return
    with urllib.request.urlopen(job["bundle_url"], timeout=60) as resp:
        data = resp.read(512 * 1024 * 1024)
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    if digest != job.get("bundle_hash"):
        raise RuntimeError(f"bundle hash mismatch: got {digest}")
    path = os.path.join(scratch, "bundle.tar")
    with open(path, "wb") as handle:
        handle.write(data)
    os.makedirs(os.path.join(scratch, "bundle"))
    subprocess.run(["tar", "-xf", path, "-C", os.path.join(scratch, "bundle")], check=True, timeout=120)
    os.remove(path)


def collect_outputs(scratch):
    """Files under scratch/out, at most 1 MB in total."""
    out_dir, files, total = os.path.join(scratch, "out"), [], 0
    if not os.path.isdir(out_dir):
        return files
    for name in sorted(os.listdir(out_dir))[:20]:
        path = os.path.join(out_dir, name)
        if not os.path.isfile(path) or not name.replace(".", "").replace("_", "").replace("-", "").isalnum():
            continue
        size = os.path.getsize(path)
        if total + size > OUTPUT_LIMIT:
            break
        with open(path, "rb") as handle:
            raw = handle.read()
        total += size
        files.append({"name": name, "size": size, "sha256": hashlib.sha256(raw).hexdigest(),
                      "data_b64": base64.b64encode(raw).decode("ascii")})
    return files


def result_signature(client, job_id, exit_code, out, err, outputs, gpu_seconds):
    """The donor agent's Ed25519 signature over the result, so the submitter can check it came from this worker."""
    payload = json.dumps({"job": int(job_id), "exit": int(exit_code), "gpu_seconds": int(gpu_seconds),
                          "stdout": hashlib.sha256(out.encode()).hexdigest(),
                          "stderr": hashlib.sha256(err.encode()).hexdigest(),
                          "outputs": sorted(o["sha256"] for o in outputs)}, sort_keys=True, separators=(",", ":"))
    try:
        return gc.b64e(client.signing_key().sign(payload.encode("utf-8")))
    except OSError:
        return ""


class Runner:
    def __init__(self):
        self.proc = None
        self.name = ""

    def stop(self):
        """SIGTERM, then SIGKILL after 10 s."""
        if self.name:
            subprocess.run(["docker", "stop", "-t", str(STOP_GRACE_S), self.name], capture_output=True, timeout=60)
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def run(self, job, gpus):
        scratch = tempfile.mkdtemp(prefix=f"guildjob-{job['id']}-")
        os.makedirs(os.path.join(scratch, "out"))
        self.name = f"guildjob-{job['id']}-{int(time.time())}"
        started = time.time()
        out, err, code = "", "", -1
        try:
            fetch_bundle(job, scratch)
            self.proc = subprocess.Popen(docker_args(job, scratch, gpus, self.name), stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True)
            try:
                stdout, stderr = self.proc.communicate(timeout=job["max_runtime_s"])
                code = self.proc.returncode
            except subprocess.TimeoutExpired:
                self.stop()
                stdout, stderr = self.proc.communicate()
                code, stderr = 124, (stderr or "") + "\n[guild worker] stopped: max_runtime_s reached"
            out, err = (stdout or "")[-TAIL:], (stderr or "")[-TAIL:]
            outputs = collect_outputs(scratch)
        except Exception as exc:
            err, outputs = f"[guild worker] could not run the job: {exc}", []
        finally:
            self.proc, self.name = None, ""
        run_s = int(time.time() - started)
        shutil.rmtree(scratch, ignore_errors=True)
        return code, out, err, outputs, run_s


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def approve(job):
    print(f"\nJob #{job['id']} from {job['submitter']}\n  image:   {job['image']}\n  bundle:  {job.get('bundle_hash')}\n"
          f"  command: {job['command']}\n  max:     {job['max_runtime_s']} s on {job['gpu_type']}")
    return input("Run it? [y/N] ").strip().lower() == "y"


def serve(args):
    client = gc.Client()
    if not client.api_key:
        sys.exit("set GUILD_KEY to one of your agents' keys (the worker acts as that agent)")
    worker = client.call("POST", "/api/compute/workers", {"name": args.name, "gpu_type": args.gpu_type,
                                                          "allow": args.allow, "guild": args.guild})
    print(json.dumps({"worker": worker["id"], "rule": worker.get("default_rule") or worker.get("allow") or worker.get("guild"),
                      "hours": args.hours}))
    write_control(paused=False, worker=worker["id"], started=int(time.time()))
    runner = Runner()
    budget_s = int(args.hours * 3600)
    used_s = 0

    def on_stop(signum, frame):
        print("\nstopping: SIGTERM to the running job, SIGKILL after 10 s")
        runner.stop()
        write_control(running=None, stopped=int(time.time()))
        sys.exit(0)

    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGTERM, on_stop)
    while used_s < budget_s:
        control = read_control()
        if control.get("paused"):
            time.sleep(POLL_S)
            continue
        day_total, per_team = usage_today()
        if args.max_hours_per_day and day_total >= args.max_hours_per_day * 3600:
            time.sleep(300)
            continue
        job = client.call("GET", f"/api/compute/jobs/next?worker={worker['id']}").get("job")
        if not job:
            time.sleep(POLL_S)
            continue
        team = job["submitter"].split("/")[0]
        too_much = args.max_minutes_per_team and per_team.get(team, 0) + job["max_runtime_s"] > args.max_minutes_per_team * 60
        if too_much or (args.approve and not approve(job)):
            client.call("POST", f"/api/compute/jobs/{job['id']}/result",
                        {"exit_code": -2, "stderr_tail": "[guild worker] declined by the donor", "gpu_seconds": 0})
            log_job({"job": job["id"], "submitter": job["submitter"], "image": job["image"], "declined": True,
                     "start": int(time.time()), "end": int(time.time()), "gpu_seconds": 0})
            continue
        write_control(running=job["id"])
        start = int(time.time())
        code, out, err, outputs, run_s = runner.run(job, args.gpus)
        gpu_seconds = run_s * max(1, args.gpus)
        used_s += run_s
        sig = result_signature(client, job["id"], code, out, err, outputs, gpu_seconds)
        client.call("POST", f"/api/compute/jobs/{job['id']}/result",
                    {"exit_code": code, "stdout_tail": out, "stderr_tail": err, "outputs": outputs, "signature": sig,
                     "gpu_seconds": gpu_seconds, "timings": {"run_s": run_s}})
        log_job({"job": job["id"], "submitter": job["submitter"], "image": job["image"], "bundle_hash": job.get("bundle_hash"),
                 "command": job["command"], "start": start, "end": int(time.time()), "exit_code": code,
                 "gpu_seconds": gpu_seconds})
        write_control(running=None)
    print("donated hours used up; stopping")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Guild worker: donate GPU-hours for development benchmarks")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "pause", "resume", "status"])
    parser.add_argument("--hours", type=float, default=1.0, help="stop after this many donated hours")
    parser.add_argument("--gpus", type=int, default=0, help="GPUs a job may use (0 = CPU only)")
    parser.add_argument("--gpu-type", default="any")
    parser.add_argument("--name", default=os.uname().nodename if hasattr(os, "uname") else "worker")
    parser.add_argument("--allow", default="", help="team1,team2 (default: members of guilds your agent is in)")
    parser.add_argument("--guild", default="", help="only members of this guild id")
    parser.add_argument("--approve", action="store_true", help="ask y/n before each job")
    parser.add_argument("--max-hours-per-day", type=float, default=0)
    parser.add_argument("--max-minutes-per-team", type=float, default=0)
    args = parser.parse_args(argv)
    if args.command == "pause":
        print(json.dumps(write_control(paused=True)))
    elif args.command == "resume":
        print(json.dumps(write_control(paused=False)))
    elif args.command == "status":
        total, per_team = usage_today()
        print(json.dumps(dict(read_control(), gpu_seconds_today=total, per_team_today=per_team), indent=1))
    else:
        serve(args)


if __name__ == "__main__":
    main()
