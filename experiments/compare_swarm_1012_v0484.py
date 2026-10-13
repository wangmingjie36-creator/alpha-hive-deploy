import json
import sys
prod = json.load(open(sys.argv[1])); new = json.load(open(sys.argv[2])); label = sys.argv[3] if len(sys.argv) > 3 else "new"
full = "--full" in sys.argv
mism = {"final": [], "dir": [], "w": [], "dims": [], "agents": {}}
rows = []
for tk in sorted(prod):
    a, b = prod[tk], new.get(tk)
    if b is None:
        print("MISSING", tk); continue
    if round(a["final_score"], 2) != round(b["final_score"], 2): mism["final"].append(tk)
    if a["direction"] != b["direction"]: mism["dir"].append(tk)
    if a.get("dimension_weights") != b.get("dimension_weights"): mism["w"].append(tk)
    if a.get("dimension_scores") != b.get("dimension_scores"): mism["dims"].append(tk)
    for ag in a["agent_details"]:
        x, y = a["agent_details"][ag], b["agent_details"].get(ag, {})
        if (x.get("score"), x.get("direction")) != (y.get("score"), y.get("direction")):
            mism["agents"].setdefault(ag, []).append(f"{tk}:{x.get('score')}/{x.get('direction')}→{y.get('score')}/{y.get('direction')}")
    rows.append((tk, a["final_score"], b["final_score"], a["direction"], b["direction"]))
print(f"== prod vs {label}: final≠ {len(mism['final'])}/30 {mism['final']}; dir≠ {len(mism['dir'])} {mism['dir']}; weights≠ {len(mism['w'])}; dims≠ {len(mism['dims'])}")
for ag, v in mism["agents"].items():
    print(f"  {ag}: {len(v)} 只不同  " + ("; ".join(v) if full else "; ".join(v[:6])))
if full:
    for r in rows: print("  %-6s %5.2f → %5.2f  %s → %s" % r)
