"""Summarises a soak run's results/ into results/report.md and prints it."""

import csv
import datetime as dt
import json
import re
from collections import Counter

# Each binding: its name in the report, its service pods' prefix, its container and memory limit.
BINDINGS = {
    "smb": ("SMB binding", "soak-service-", "smb-binding", 256),
    "ftp": ("FTP binding", "soak-ftp-service-", "ftp-binding", 128),
}
FAULT_WINDOW = dt.timedelta(minutes=5)
RECOVERY_LIMIT = dt.timedelta(minutes=2)


def rows(results, name):
    path = results / f"{name}.csv"
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


def t(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def mib(s):
    m = re.fullmatch(r"(\d+)(Ki|Mi|Gi)", s)
    if not m:
        return None
    return int(m[1]) * {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024}[m[2]]


def report(results):
    run = json.loads((results / "run.json").read_text())
    jobs, service, chaos, memory = (rows(results, n) for n in ("jobs", "service", "chaos", "memory"))
    faults = [(t(c["ts"]), c["action"], c["target"]) for c in chaos]
    out = ["# File binding soak", "", f"Started {run['started']}, planned until {run['until']}.", ""]
    for kind in BINDINGS:
        # A fault on the other binding's workloads cannot explain this one's failures; a
        # shared one (CPU stress) can.
        own = [
            f
            for f in faults
            if f[1].startswith(f"{kind}_") or not f[1].startswith(tuple(f"{k}_" for k in BINDINGS))
        ]
        out += section(
            kind,
            [j for j in jobs if j.get("binding", "smb") == kind],
            [r for r in service if r.get("binding", "smb") == kind],
            own,
            memory,
        )
    out += (
        ["# Faults injected", ""]
        + [f"- {n}× {a}" for a, n in Counter(f[1] for f in faults).most_common()]
        + [""]
    )

    text = "\n".join(out)
    (results / "report.md").write_text(text)
    print(text)


def section(kind, jobs, service, faults, memory):
    title, service_prefix, container, limit = BINDINGS[kind]
    out = [f"# {title}", ""]

    # ---- jobs: a job overlapping a fault (from its start to FAULT_WINDOW after its end) may
    # fail for the fault's sake; one that did not overlap one must always complete.
    def disturbed(j):
        start, end = t(j["created"]), t(j["finished"])
        return any(start - FAULT_WINDOW <= ft <= end + FAULT_WINDOW for ft, _, _ in faults)

    outcome = Counter(j["outcome"] for j in jobs)
    clean = [j for j in jobs if not disturbed(j)]
    clean_bad = [j for j in clean if j["outcome"] != "complete"]
    out += [
        "## Jobs (CronJob pod spec: OnFailure, backoffLimit 5, OnSuccess)",
        "",
        f"- {len(jobs)} jobs: " + ", ".join(f"{n} {k}" for k, n in outcome.most_common()),
        f"- Without a fault nearby: {len(clean)} jobs, **{len(clean_bad)} not complete**",
        f"- daprd startup restarts: {sum(int(j['daprd_restarts']) > 0 for j in jobs)} of {len(jobs)} jobs "
        f"(max {max((int(j['daprd_restarts']) for j in jobs), default=0)} in one job)",
        f"- Injected first-attempt failures: "
        f"{sum(j['fail_first'] == '1' for j in jobs)}, of which "
        f"{sum(j['fail_first'] == '1' and j['outcome'] == 'complete' for j in jobs)} completed on the retry",
        f"- File mismatches: **{sum(int(j['mismatches']) for j in jobs)}**",
        f"- Duration: median {median(int(j['seconds']) for j in jobs)} s, "
        f"max {max((int(j['seconds']) for j in jobs), default=0)} s",
        "",
    ]
    if clean_bad:
        out += ["Jobs that failed without a fault nearby:", ""]
        out += [
            f"- {j['job']} {j['outcome']} daprd:{j['daprd_restarts']} binding:{j['binding_restarts']} "
            f"app:{j['app_restarts']} {j['errors']}"
            for j in clean_bad[:30]
        ]
        out.append("")
    errs = Counter(e for j in jobs for e in j["errors"].split(" | ") if e)
    if errs:
        out += ["Round errors in jobs:", ""] + [f"- {n}× {e}" for e, n in errs.most_common(10)] + [""]

    # ---- service: every failing stretch of rounds per pod, matched to the fault before it.
    by_pod = {}
    for r in service:
        by_pod.setdefault(r["pod"], []).append(r)
    outages = []
    for pod, rs in by_pod.items():
        rs.sort(key=lambda r: r["ts"])
        start = None
        for r in rs:
            bad = r["error"] or int(r["mismatch"])
            if bad and start is None:
                start = r
            elif not bad and start is not None:
                outages.append((pod, t(start["ts"]), t(r["ts"]), start["error"]))
                start = None
        if start is not None:
            outages.append((pod, t(start["ts"]), None, start["error"]))
    ok = sum(1 for r in service if not r["error"] and not int(r["mismatch"]))
    unexplained, slow = [], []
    for pod, begin, end, err in outages:
        cause = max(
            (f for f in faults if f[0] <= begin + dt.timedelta(seconds=30)), default=None, key=lambda f: f[0]
        )
        if cause is None or begin - cause[0] > FAULT_WINDOW:
            unexplained.append((pod, begin, end, err))
        elif end is None or end - cause[0] > RECOVERY_LIMIT:
            slow.append((pod, cause, begin, end, err))
    out += [
        "## Deployment (restartPolicy Always, exit-when-idle off)",
        "",
        f"- {len(service)} rounds: {ok} ok, {len(service) - ok} failed",
        f"- File mismatches: **{sum(int(r['mismatch']) for r in service)}**",
        f"- Failing stretches: {len(outages)}; **{len(unexplained)} without a fault before them**, "
        f"**{len(slow)} took over {int(RECOVERY_LIMIT.total_seconds())} s to recover**",
        f"- Round time: median {median(int(r['ms']) for r in service)} ms",
        "",
    ]
    for pod, begin, end, err in unexplained[:20]:
        out.append(f"- unexplained: {pod} {begin:%H:%M:%S}–{end and f'{end:%H:%M:%S}'} {err}")
    for pod, cause, _begin, end, err in slow[:20]:
        out.append(
            f"- slow: {pod} after {cause[1]} at {cause[0]:%H:%M:%S}, "
            f"recovered {end and f'{end:%H:%M:%S}'} {err}"
        )
    out.append("")

    # ---- memory: the binding in the long-lived service pods, first hour against last hour.
    mem = [
        (t(m["ts"]), mib(m["memory"]))
        for m in memory
        if m["container"] == container and m["pod"].startswith(service_prefix) and mib(m["memory"])
    ]
    if mem:
        first, last = mem[0][0], mem[-1][0]
        early = [v for ts, v in mem if ts - first < dt.timedelta(hours=1)]
        late = [v for ts, v in mem if last - ts < dt.timedelta(hours=1)]
        out += [
            "## Binding memory (service pods)",
            "",
            f"- First hour: avg {sum(early) / len(early):.0f} MiB; last hour: avg {sum(late) / len(late):.0f} MiB; "
            f"max {max(v for _, v in mem):.0f} MiB (limit {limit} MiB)",
            "",
        ]
    return out


def median(values):
    v = sorted(values)
    return v[len(v) // 2] if v else 0
