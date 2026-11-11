"""Native Pi usage reader; shared ledger, launch state and row contract in usage_log.

Follow parentId to the selected leaf. Compaction does not erase raw usage and
abandoned branches must never be added to the active turn's totals.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import time

import hook_space
import usage_log as ledger


def branch(path: str, leaf: str | None) -> list[dict]:
    if not path or not leaf:
        return []
    entries = {}
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(entry, dict) and entry.get("id"):
                    entries[entry["id"]] = entry
    except OSError:
        return []
    out, seen = [], set()
    while leaf in entries and leaf not in seen:
        seen.add(leaf)
        entry = entries[leaf]
        out.append(entry)
        leaf = entry.get("parentId")
    return list(reversed(out))


def stamp(entry: dict) -> float | None:
    value = entry.get("timestamp")
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def messages(entries):
    return [e for e in entries if e.get("type") == "message" and isinstance(e.get("message"), dict)]


def skill_call(block: dict, cwd: str):
    args = block.get("arguments") or {}
    if block.get("name") == "read":
        path = args.get("path")
        if isinstance(path, str):
            return ledger.resolve_skill("file", os.path.abspath(os.path.join(cwd, path)))
    if block.get("name") in {"bash", "powershell"}:
        return ledger.resolve_skill("command", args.get("command"))
    return None


def measure(entries: list[dict], run: dict) -> dict | None:
    start = next((i for i, e in enumerate(entries) if e.get("id") == run.get("pi_start")), None)
    if start is None:
        return None
    part = []
    # Several skills may start in one assistant entry. Earlier peers end at
    # that same entry; only the last segment owns the following tools/children.
    cut = run.get("cut_entry") == run.get("pi_start")
    tail = [] if cut else entries[start + (1 if run.get("from_call") else 0):]
    for entry in tail:
        if entry.get("id") == run.get("cut_entry"):
            part.append(entry)  # choosing the next skill still belongs to the previous segment
            cut = True
            break
        msg = entry.get("message") or {}
        if msg.get("role") == "user" and entry.get("id") != run.get("prompt_id"):
            break
        part.append(entry)
    answers = [e["message"] for e in messages(part) if e["message"].get("role") == "assistant"]
    calls = [b for m in answers for b in m.get("content", []) if b.get("type") == "toolCall"]
    usage = [m.get("usage") for m in answers]
    def total(key):
        if run.get("shared_turn"):
            return None
        if any(not isinstance(u, dict) or key not in u for u in usage):
            return None
        return sum(u[key] for u in usage)
    agents, child_ids = [], set()
    for entry in messages(part):
        msg = entry["message"]
        child = (msg.get("details") or {}).get("svaibScout") if msg.get("role") == "toolResult" else None
        if isinstance(child, dict):
            agents.append(child)
        # The structured delegation terminal response owns partial usage.
        # Child policy runs without writing a second skill ledger.
        details = msg.get("details") or {}
        if msg.get("role") != "toolResult":
            continue
        for receipt in details.get("svaibAgents", []):
            if isinstance(receipt, dict) and receipt.get("id") and receipt["id"] not in child_ids:
                child_ids.add(receipt["id"])
                agents.append(receipt)
    return {"input": total("input"), "output": total("output"),
            "cache_read": total("cacheRead"), "cache_write": total("cacheWrite"),
            "models": list(dict.fromkeys(m.get("model", "") for m in answers if m.get("model"))),
            "version": run.get("pi_version", ""), "turns": len(answers), "tools": len(calls),
            "agents": agents, "pending": False, "cut": cut,
            "error": any(m.get("stopReason") in {"error", "aborted"} for m in answers),
            "last": stamp(part[-1]) if part else run.get("start")}


def close(ev: dict, entries: list[dict], *, all_runs: bool = False) -> None:
    if not ledger.has_state(ev["session_id"]):
        return
    with ledger.state_of(ev["session_id"]) as state:
        for run in list(state["runs"]):
            if not all_runs and run.get("prompt_id") == ev.get("prompt_id"):
                continue
            if not ledger.usage_root_ready(run):
                state["runs"].remove(run)
                state.save()
                continue
            # A resumed/forked branch may no longer include an abandoned run.
            m = measure(entries, run)
            if m is None:
                continue
            if ledger.append(run["root"], ledger.rows(run, m, ev["session_id"])):
                state["runs"].remove(run)
                state.save()
        if all_runs:
            state["launch"] = None


def run(mode: str, ev: dict) -> None:
    root = hook_space.ready_usage_root(ev, use_claude_env=False)
    sid, prompt = ev.get("session_id"), ev.get("prompt_id")
    if not root or not sid or not prompt:
        return
    snapshot = ev.get("native_entries")
    entries = snapshot if isinstance(snapshot, list) else branch(ev.get("transcript_path"), ev.get("leaf_id"))
    if mode in {"prompt", "stop", "end"}:
        close(ev, entries, all_runs=mode == "end")
    if mode == "end":
        return
    own = ledger.plugin_name()
    selected = []
    if mode == "prompt":
        match = re.match(r"\s*/(?:skill:|svaib:)([\w.-]+)(?:\s|$)", ev.get("prompt") or "")
        skill = ledger.plugin_skill(f"{own}:{match[1]}") if match else None
        if skill is None:
            expanded = re.match(r'^<skill name="[^"]+" location="([^"]+)">\n', ev.get("prompt") or "")
            skill = ledger.resolve_skill("file", expanded[1]) if expanded else None
        if skill:
            selected.append((prompt, None, skill, ledger.COMMAND, time.time()))
    else:
        current = next((i for i, e in enumerate(entries) if e.get("id") == prompt), len(entries))
        for entry in entries[current:]:
            msg = entry.get("message") or {}
            if msg.get("role") != "assistant":
                continue
            for block in msg.get("content", []):
                if block.get("type") == "toolCall":
                    skill = skill_call(block, ev["cwd"])
                    if skill:
                        selected.append((entry["id"], block.get("id"), skill, ledger.MODEL, stamp(entry) or time.time()))
    with ledger.state_of(sid) as state:
        runs = state["runs"]
        launch = ledger.live_launch(state, time.time())
        if mode == "prompt" and not selected:
            if re.match(r"\s*/[\w]", ev.get("prompt") or ""):
                state["launch"] = None
                return
            if launch and launch.get("root_real") == os.path.realpath(root):
                if not any(r.get("prompt_id") == prompt for r in runs):
                    launch["turn"] += 1
                    runs.append(ledger.segment_of(launch, ev, f"pi:{prompt}", pi_start=prompt,
                                                 pi_version=ev.get("pi_version")))
        for entry_id, call_id, skill, method, started in selected:
            if any(r.get("prompt_id") == prompt and r.get("skill") == skill[0] for r in runs):
                continue
            key = f"pi:{prompt}:{call_id or skill[0]}"
            if launch is None or method == ledger.COMMAND:
                launch = ledger.new_launch(state, ev, key, skill, own, root, method, harness="pi")
            for old in runs:
                if old.get("prompt_id") == prompt:
                    old.setdefault("cut_entry", entry_id)
            runs.append(ledger.segment_of(launch, ev, key, skill, method, pi_start=entry_id,
                                         from_call=call_id, pi_version=ev.get("pi_version"), start=started))
            peers = [r for r in runs if r.get("prompt_id") == prompt and r.get("pi_start") == entry_id]
            if len(peers) > 1:
                for peer in peers:
                    peer["shared_turn"] = True  # native usage is per response, not per parallel call
        if mode == "stop":
            for item in runs:
                if item.get("prompt_id") == prompt:
                    item["stopped_at"] = time.time()
            if launch:
                launch["last"] = time.time()
