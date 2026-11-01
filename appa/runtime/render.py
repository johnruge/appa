"""Run events -> terminal lines. The web UI (M5) renders the same events from JSON."""

from __future__ import annotations

from appa import db

ICON = {
    "candidate": "✅ to apply",
    "manual": "⭐ apply myself",
    "review": "🟡 review",
    "reject": "❌ reject",
    "duplicate": "↩️  duplicate",
}


def format_event(ev: db.RunEvent) -> list[str]:
    """Format a run event as terminal lines, one style per event kind, in local time."""
    p = ev.payload or {}
    t = ev.ts.astimezone().strftime("%H:%M:%S") if ev.ts else ""  # your local time
    k = ev.kind
    if k == "status":
        extra = ""
        if p.get("status") in ("succeeded", "failed", "cancelled"):
            extra = f"  ${p.get('cost_usd', 0):.4f} · {p.get('steps', 0)} steps · {p.get('model_calls', 0)} model calls"
            if p.get("error"):
                extra += f"\n{'':>9}  {p['error']}"
        elif p.get("model"):
            extra = f"  model: {p['model']}"
        return [f"{t} ● {p.get('status')}{extra}"]
    if k == "log":
        return [f"{t}   {p.get('message')}"]
    if k == "tool_call":
        mark = "→" if p.get("ok") else "✗"
        args = " ".join(str(v) for v in (p.get("args") or {}).values())[:120]
        tail = p.get("result") if p.get("ok") else p.get("error")
        return [f"{t}   {mark} {p.get('tool')}({args})" + (f"  {tail}" if tail else "")]
    if k == "model_call":
        if p.get("ok") is False:
            return [f"{t}   ✗ model {p.get('purpose', '')}: {p.get('error')}"]
        return [
            f"{t}   ◇ {p.get('purpose', '')} [{p.get('model')}] "
            f"{p.get('input_tokens', 0)}+{p.get('output_tokens', 0)} tok ${p.get('cost_usd', 0):.5f}"
        ]
    if k == "decision":
        lines = [f"{t}   {ICON.get(p.get('verdict'), p.get('verdict')):<16} {p.get('company')} | {p.get('title')}"]
        lines += [f"{'':>28}- {r[:200]}" for r in (p.get("reasons") or [])[:3]]
        return lines
    if k == "question":
        return [f"{t}   ? question #{p.get('question_id')}: {p.get('prompt')}"]
    if k == "result":
        counts = p.get("counts") or p.get("waiting_on_you") or {}
        return [f"{t} ■ result: " + (", ".join(f"{k2} {v}" for k2, v in counts.items()) or str(p)[:200])]
    return [f"{t}   {k}: {str(p)[:200]}"]
