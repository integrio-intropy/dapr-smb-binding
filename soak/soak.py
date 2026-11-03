#!/usr/bin/env python3
"""Drive, disturb and record the file binding soak, SMB and FTP side by side (see README.md).

    soak.py run [--hours 24]   start the job driver, the chaos loop and the collector
    soak.py report             summarise results/ into results/report.md

Everything goes through kubectl with soak/.kubeconfig, so the script never touches another
cluster. Results are appended as CSV under soak/results/ while the run goes, so a report can
be made at any time, also mid-run.
"""

import argparse
import csv
import datetime as dt
import json
import os
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
NS = "soak"
NODE = "k3d-smb-soak-server-0"
ENV = {**os.environ, "KUBECONFIG": str(HERE / ".kubeconfig")}

CONCURRENT_JOBS = 2  # per binding

# Each binding under test: its Component, its Job and Deployment app ids, and the name of
# its injected container.
BINDINGS = {
    "smb": {
        "component": "soak-share",
        "job": "soak-job",
        "service": "soak-service",
        "container": "smb-binding",
    },
    "ftp": {
        "component": "soak-ftp",
        "job": "soak-ftp-job",
        "service": "soak-ftp-service",
        "container": "ftp-binding",
    },
}
FAIL_FIRST_RATE = 0.1
HANG_AFTER = dt.timedelta(minutes=10)
CHAOS_EVERY = (10 * 60, 20 * 60)

stop = threading.Event()
lock = threading.Lock()


def now():
    return dt.datetime.now(dt.UTC)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def kubectl(*args, input=None, check=True):
    p = subprocess.run(
        ["kubectl", "-n", NS, *args], env=ENV, input=input, capture_output=True, text=True, timeout=120
    )
    if check and p.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout


def kjson(*args):
    return json.loads(kubectl(*args, "-o", "json"))


def append(name, row):
    path = RESULTS / f"{name}.csv"
    with lock:
        new = not path.exists()
        with path.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(row))
            if new:
                w.writeheader()
            w.writerow(row)


def log(msg):
    line = f"{iso(now())} {msg}"
    print(line, flush=True)
    with lock, (RESULTS / "soak.log").open("a") as f:
        f.write(line + "\n")


def guarded(name, fn, every):
    """Runs fn every `every` seconds until stop; an error is logged, never fatal."""
    while not stop.is_set():
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - the soak must outlive its own hiccups
            log(f"{name}: {e}")
        stop.wait(every() if callable(every) else every)


# ---------------------------------------------------------------- job driver

template = (HERE / "k8s" / "job-template.yaml").read_text()
seq = [0]


def drive():
    for kind, b in BINDINGS.items():
        jobs = kjson("get", "jobs", "-l", f"app={b['job']}")["items"]
        active = [j for j in jobs if not finished(j)]
        for _ in range(CONCURRENT_JOBS - len(active)):
            seq[0] += 1
            name = f"soak-{kind}-{int(time.time())}-{seq[0]}"
            fail_first = "1" if random.random() < FAIL_FIRST_RATE else "0"
            kubectl(
                "apply",
                "-f",
                "-",
                input=template.replace("name: NAME", f"name: {name}")
                .replace("APP", b["job"])
                .replace("KIND", kind)
                .replace("COMPONENT", b["component"])
                .replace('"FAIL_FIRST"', f'"{fail_first}"'),
            )


def finished(job):
    return any(
        c["type"] in ("Complete", "Failed") and c["status"] == "True"
        for c in job["status"].get("conditions", [])
    )


# ---------------------------------------------------------------- collector

seen_rounds = set()


def collect():
    collect_jobs()
    collect_service()
    collect_memory()


def collect_jobs():
    for kind, b in BINDINGS.items():
        for job in kjson("get", "jobs", "-l", f"app={b['job']}")["items"]:
            collect_job(kind, b, job)


def collect_job(kind, b, job):
    name = job["metadata"]["name"]
    created = parse(job["metadata"]["creationTimestamp"])
    done = finished(job)
    hung = not done and now() - created > HANG_AFTER
    if not (done or hung):
        return
    pods = kjson("get", "pods", "-l", f"job-name={name}")["items"]
    restarts = {}
    for pod in pods:
        for c in pod["status"].get("containerStatuses", []):
            restarts[c["name"]] = restarts.get(c["name"], 0) + c["restartCount"]
    cond = next(
        (
            c
            for c in job["status"].get("conditions", [])
            if c["type"] in ("Complete", "Failed") and c["status"] == "True"
        ),
        {},
    )
    rounds = []
    for pod in pods:
        logs = kubectl(
            "logs",
            pod["metadata"]["name"],
            "-c",
            "soak-job",
            "--all-containers=false",
            "--previous=false",
            check=False,
        )
        prev = kubectl("logs", pod["metadata"]["name"], "-c", "soak-job", "--previous", check=False)
        rounds += [json.loads(line[5:]) for line in (prev + logs).splitlines() if line.startswith("SOAK ")]
    if hung:
        out = RESULTS / "hung"
        out.mkdir(exist_ok=True)
        for pod in pods:
            pn = pod["metadata"]["name"]
            (out / f"{pn}.txt").write_text(
                kubectl("describe", "pod", pn, check=False)
                + "\n\n"
                + kubectl("logs", pn, "--all-containers", check=False)
            )
    end = parse(cond["lastTransitionTime"]) if cond.get("lastTransitionTime") else now()
    append(
        "jobs",
        {
            "binding": kind,
            "job": name,
            "created": iso(created),
            "finished": iso(end),
            "seconds": int((end - created).total_seconds()),
            "outcome": "hung"
            if hung
            else ("complete" if cond.get("type") == "Complete" else cond.get("reason", "failed")),
            "fail_first": job["metadata"]["labels"].get("soak.intropy.io/fail-first"),
            "pods": len(pods),
            "app_restarts": restarts.get("soak-job", 0),
            "daprd_restarts": restarts.get("daprd", 0),
            "binding_restarts": restarts.get(b["container"], 0),
            "rounds": len(rounds),
            "round_errors": sum(1 for r in rounds if r["error"]),
            "mismatches": sum(r["mismatch"] for r in rounds),
            "errors": " | ".join(sorted({r["error"] for r in rounds if r["error"]}))[:500],
        },
    )
    kubectl("delete", "job", name, "--wait=false", check=False)


def collect_service():
    for kind, b in BINDINGS.items():
        for pod in kjson("get", "pods", "-l", f"app={b['service']}")["items"]:
            collect_service_pod(kind, b, pod)
    if len(seen_rounds) > 50_000:
        seen_rounds.clear()


def collect_service_pod(kind, b, pod):
    pn = pod["metadata"]["name"]
    status = {c["name"]: c["restartCount"] for c in pod["status"].get("containerStatuses", [])}
    for prev in (True, False):
        args = ["logs", pn, "-c", "soak-service", "--since=2m"] + (["--previous"] if prev else [])
        for line in kubectl(*args, check=False).splitlines():
            if not line.startswith("SOAK "):
                continue
            r = json.loads(line[5:])
            key = (r["pod"], r["ts"])
            if key in seen_rounds:
                continue
            seen_rounds.add(key)
            append(
                "service",
                {
                    "binding": kind,
                    "ts": r["ts"][:19] + "Z",
                    "pod": r["pod"],
                    "ok": r["ok"],
                    "mismatch": r["mismatch"],
                    "error": r["error"] or "",
                    "ms": r["ms"],
                    "daprd_restarts": status.get("daprd", 0),
                    "binding_restarts": status.get(b["container"], 0),
                    "app_restarts": status.get("soak-service", 0),
                },
            )


def collect_memory():
    out = kubectl("top", "pod", "--containers", "--no-headers", check=False)
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[1] in (
            "smb-binding",
            "ftp-binding",
            "daprd",
            "soak-service",
            "samba",
            "proftpd",
        ):
            append(
                "memory",
                {
                    "ts": iso(now()),
                    "pod": parts[0],
                    "container": parts[1],
                    "cpu": parts[2],
                    "memory": parts[3],
                },
            )


def parse(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------------------------------------------------------------- chaos


def node(*cmd):
    p = subprocess.run(["docker", "exec", NODE, *cmd], capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {p.stderr.strip()}")
    return p.stdout


def kill_container(selector, container):
    pods = [p for p in kjson("get", "pods", "-l", selector)["items"] if p["status"]["phase"] == "Running"]
    if not pods:
        return None
    pod = random.choice(pods)
    pn = pod["metadata"]["name"]
    ids = node(
        "crictl", "ps", "-q", "--name", f"^{container}$", "--label", f"io.kubernetes.pod.name={pn}"
    ).split()
    for cid in ids:
        node("crictl", "stop", "--timeout", "0", cid)
    return f"{pn}/{container}" if ids else None


def per_binding(kind, b):
    """The faults that target one binding's workloads, named <kind>_<fault>."""

    def daprd_service():
        return kill_container(f"app={b['service']}", "daprd")

    def binding_service():
        return kill_container(f"app={b['service']}", b["container"])

    def daprd_job():
        return kill_container(f"app={b['job']}", "daprd")

    def binding_job():
        return kill_container(f"app={b['job']}", b["container"])

    def rollout():
        kubectl("rollout", "restart", f"deploy/{b['service']}")
        return f"deploy/{b['service']}"

    faults = [daprd_service, binding_service, daprd_job, binding_job, rollout]
    for f in faults:
        f.__name__ = f"chaos_{kind}_{f.__name__}"
    return faults


def chaos_smb_server_restart():
    kubectl("delete", "pod", "samba-dc-0", "--wait=false")
    return "samba-dc-0"


def chaos_ftp_server_restart():
    kubectl("delete", "pod", "-l", "app=ftp-server", "--wait=false")
    return "ftp-server"


def chaos_cpu():
    # Busy loops on every core for 3 minutes, so containers start slowly, as on a loaded node.
    name = f"cpu-stress-{int(time.time())}"
    kubectl(
        "run",
        name,
        "--image=soak-samba-dc:dev",
        "--image-pull-policy=Never",
        "--restart=Never",
        "--labels=app=cpu-stress",
        "--command",
        "--",
        "sh",
        "-c",
        "for i in $(seq $(nproc)); do (while :; do :; done) & done; sleep 180",
    )
    return name


CHAOS = [
    *(f for kind, b in BINDINGS.items() for f in per_binding(kind, b)),
    chaos_smb_server_restart,
    chaos_ftp_server_restart,
    chaos_cpu,
]


def chaos():
    kubectl(
        "delete",
        "pod",
        "-l",
        "app=cpu-stress",
        "--field-selector=status.phase!=Running",
        "--wait=false",
        check=False,
    )
    action = random.choice(CHAOS)
    target = action()
    name = action.__name__.removeprefix("chaos_")
    append("chaos", {"ts": iso(now()), "action": name, "target": target or ""})
    log(f"chaos {name} {target or '(no target)'}")


# ---------------------------------------------------------------- run


def run(hours):
    RESULTS.mkdir(exist_ok=True)
    until = now() + dt.timedelta(hours=hours)
    (RESULTS / "run.json").write_text(json.dumps({"started": iso(now()), "until": iso(until)}))
    log(f"soak started, until {iso(until)}")
    threads = [
        threading.Thread(target=guarded, args=("driver", drive, 5), daemon=True),
        threading.Thread(target=guarded, args=("collector", collect, 30), daemon=True),
        # The first fault comes after a quiet first interval, so there is a baseline.
        threading.Thread(
            target=lambda: (
                stop.wait(random.uniform(*CHAOS_EVERY)),
                guarded("chaos", chaos, lambda: random.uniform(*CHAOS_EVERY)),
            ),
            daemon=True,
        ),
    ]
    for t in threads:
        t.start()
    try:
        while now() < until and not stop.is_set():
            stop.wait(30)
    except KeyboardInterrupt:
        pass
    stop.set()
    log("soak stopping; collecting the last results")
    time.sleep(5)
    try:
        collect()
    except Exception as e:  # noqa: BLE001
        log(f"final collect: {e}")
    log("soak stopped")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--hours", type=float, default=24)
    r.add_argument(
        "--chaos-minutes",
        type=float,
        nargs=2,
        default=[10, 20],
        help="the shortest and longest pause between two faults",
    )
    sub.add_parser("report")
    a = ap.parse_args()
    if a.cmd == "run":
        global CHAOS_EVERY
        CHAOS_EVERY = (a.chaos_minutes[0] * 60, a.chaos_minutes[1] * 60)
        run(a.hours)
    else:
        from report import report

        report(RESULTS)


if __name__ == "__main__":
    sys.exit(main())
