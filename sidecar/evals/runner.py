"""Run the evaluation cases against a model (LEVEL-DESIGN-SPEC.md 15.5).

    uv run python -m evals.runner                          # every case, once, the .env model
    uv run python -m evals.runner --cases fix-wall --seeds 3
    uv run python -m evals.runner --provider anthropic --model claude-sonnet-5-5
    uv run python -m evals.runner --replay evals/recordings  # no model: replays recordings

Each case runs the real agent loop against the fake editor holding the case's fixture,
`--seeds` times. A run writes its transcript (replayable), its metrics and a schematic of
the section as the run left it. The report is `report.json` and `report.html` in
`evals/out/<timestamp>/` (gitignored). `--max-total-tokens` stops the run once the model
calls have used that many tokens; cost depends on the provider's prices, which the harness
does not know.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import html
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from gd_ai_assistant import catalog
from gd_ai_assistant.agent import History, ToolLoop, TurnResult
from gd_ai_assistant.config import SidecarConfig
from gd_ai_assistant.design.coords import Grid
from gd_ai_assistant.design.render import SchematicError, render_schematic
from gd_ai_assistant.providers import create_provider
from gd_ai_assistant.providers.base import Provider

from .cases import ROOT, Case, editor_for, load_cases
from .metrics import check, measure
from .recording import RecordingProvider, ReplayProvider

OUT = ROOT / "out"


def _provider_factory(args: argparse.Namespace) -> tuple[Any, str]:
    """(case, seed) -> provider, and a label for the report."""
    if args.replay:
        folder = Path(args.replay)

        def replay(case: Case, seed: int) -> Provider:
            path = folder / f"{case.id}.json"
            if not path.exists():
                raise SystemExit(f"no recording for {case.id} in {folder}")
            return ReplayProvider.from_file(path)
        return replay, f"replay of {folder}"

    overrides = {k: v for k, v in (("llm_provider", args.provider), ("model", args.model),
                                    ("base_url", args.base_url)) if v}
    config = SidecarConfig(**overrides)
    return (lambda case, seed: create_provider(config)), f"{config.llm_provider} {config.model}"


async def run_case(case: Case, provider: Provider, out: Path,
                   turn_timeout_s: float = 600.0) -> dict[str, Any]:
    editor = editor_for(case)
    before = {uid: (o.type_id, o.props["x"], o.props["y"]) for uid, o in editor.objects.items()}
    recorder = RecordingProvider(provider)
    history = History(token_budget=8000)
    results: list[TurnResult] = []
    began = time.perf_counter()
    for index, prompt in enumerate(case.prompts):
        loop = ToolLoop(link=editor, provider=recorder, catalog=catalog.load_catalog(),
                        history=history, max_tool_rounds=case.max_tool_rounds,
                        turn_timeout_s=turn_timeout_s, max_retries=2, retry_base_s=2.0)
        results.append(await loop.run(turn_id=f"t-{index + 1}", message_id=f"m-{index + 1}",
                                      user_text=prompt))
    elapsed = time.perf_counter() - began

    metrics = measure(case, editor, before, results, recorder.recording.tools, elapsed)
    verdicts = {}
    for kind, asserts in (("hard", case.hard), ("soft", case.soft)):
        for name, condition in asserts.items():
            outcome, value = check(metrics, name, condition)
            verdicts[f"{kind}:{name}"] = {"outcome": outcome, "value": value,
                                          "condition": condition}

    out.mkdir(parents=True, exist_ok=True)
    (out / "transcript.json").write_text(json.dumps(
        {"case": case.id, "turns": recorder.recording.turns,
         "tools": [vars(e) for e in recorder.recording.tools],
         "replies": [r.text for r in results]}, indent=1), encoding="utf-8")
    image = None
    if case.section is not None:
        try:
            schematic = render_schematic(editor.level_model(), Grid.from_constants(),
                                         {"x_range": list(case.section)})
            (out / "section.png").write_bytes(schematic.png)
            image = "section.png"
        except SchematicError as exc:
            metrics["schematic_error"] = str(exc)
    return {"case": case.id, "metrics": metrics, "verdicts": verdicts,
            "replies": [r.text for r in results], "image": image}


def _summary(cases: list[Case], runs: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for case in cases:
        mine = [r for r in runs if r["case"] == case.id]
        if not mine:
            continue
        rates = {}
        for key in mine[0]["verdicts"]:
            passed = sum(1 for r in mine if r["verdicts"][key]["outcome"] == "pass")
            rates[key] = f"{passed}/{len(mine)}"
        numbers = {}
        for key in ("tool_rounds", "input_tokens", "output_tokens", "elapsed_s",
                    "objects_created", "screenshots", "tool_errors", "min_window_ms"):
            values = [r["metrics"][key] for r in mine
                      if isinstance(r["metrics"].get(key), int | float)]
            if values:
                numbers[key] = statistics.median(values)
        hard = all(r["verdicts"][k]["outcome"] == "pass" for r in mine
                   for k in r["verdicts"] if k.startswith("hard:"))
        summary[case.id] = {"runs": len(mine), "all_hard_passed": hard, "pass_rates": rates,
                            "medians": numbers}
    return summary


def _html(meta: dict[str, Any], summary: dict[str, Any], runs: list[dict[str, Any]]) -> str:
    esc = html.escape
    rows = []
    for case_id, s in summary.items():
        rates = "<br>".join(f"{esc(k)}: {v}" for k, v in s["pass_rates"].items())
        medians = "<br>".join(f"{esc(k)}: {v}" for k, v in s["medians"].items())
        mark = "pass" if s["all_hard_passed"] else "fail"
        rows.append(f"<tr class={mark}><td>{esc(case_id)}</td><td>{s['runs']}</td>"
                    f"<td>{rates}</td><td>{medians}</td></tr>")
    details = []
    for run in runs:
        img = (f"<img src='{esc(run['folder'])}/{run['image']}'>" if run.get("image") else "")
        verdicts = "".join(
            f"<li class={v['outcome'].replace('/', '')}>{esc(k)} = {esc(str(v['value']))} "
            f"(want {esc(json.dumps(v['condition']))}): {v['outcome']}</li>"
            for k, v in run["verdicts"].items())
        reply = esc(run["replies"][-1] if run["replies"] else "")
        details.append(f"<section><h3>{esc(run['case'])} seed {run['seed']}</h3>{img}"
                       f"<ul>{verdicts}</ul><details><summary>Reply</summary>"
                       f"<pre>{reply}</pre></details>"
                       f"<a href='{esc(run['folder'])}/transcript.json'>transcript</a>"
                       "</section>")
    return f"""<!doctype html><meta charset=utf-8><title>Eval report</title>
<style>body{{font:14px system-ui;margin:16px;max-width:1100px}}td,th{{border:1px solid #ccc;
padding:4px 8px;vertical-align:top}}table{{border-collapse:collapse}}tr.fail td:first-child,
li.fail,li.na{{color:#b00020}}tr.pass td:first-child,li.pass{{color:#1b5e20}}
img{{max-width:100%;border:1px solid #ccc}}section{{margin:24px 0}}</style>
<h1>Eval report</h1><p>{esc(json.dumps(meta))}</p>
<table><tr><th>case</th><th>runs</th><th>pass rates</th><th>medians</th></tr>
{''.join(rows)}</table>{''.join(details)}"""


def _markdown(meta: dict[str, Any], summary: dict[str, Any]) -> str:
    """Scores only, for `evals/BASELINE.md` (LEVEL-DESIGN-SPEC.md M3)."""
    lines = [f"# Eval baseline: {meta['model']}", "",
             f"{meta['when']}, {meta['seeds']} seed(s) per case, {meta['tokens']} tokens."
             + (f" Stopped early: {meta['stopped']}." if meta.get("stopped") else ""), "",
             "| case | hard | assertions passed | median rounds | median tokens |",
             "|---|---|---|---|---|"]
    for case_id, s in summary.items():
        failed = [f"{k} {v}" for k, v in s["pass_rates"].items()
                  if v.split("/")[0] != v.split("/")[1]]
        tokens = s["medians"].get("input_tokens", 0) + s["medians"].get("output_tokens", 0)
        lines.append(f"| {case_id} | {'pass' if s['all_hard_passed'] else 'FAIL'} | "
                     f"{'all' if not failed else ', '.join(failed)} | "
                     f"{s['medians'].get('tool_rounds', '')} | {tokens:.0f} |")
    return "\n".join(lines) + "\n"


async def main_async(args: argparse.Namespace) -> int:
    cases = load_cases(args.cases)
    factory, label = _provider_factory(args)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) if args.out else OUT / stamp
    seeds = 1 if args.replay else args.seeds
    runs: list[dict[str, Any]] = []
    used = 0
    stopped = None
    for case in cases:
        for seed in range(1, seeds + 1):
            if args.max_total_tokens and used >= args.max_total_tokens:
                stopped = f"token limit {args.max_total_tokens} reached"
                break
            folder = f"{case.id}-{seed}"
            print(f"{case.id} seed {seed}: running", flush=True)
            run = await run_case(case, factory(case, seed), out / folder)
            run.update(seed=seed, folder=folder)
            runs.append(run)
            m = run["metrics"]
            used += m["input_tokens"] + m["output_tokens"]
            failed = [k for k, v in run["verdicts"].items() if v["outcome"] != "pass"]
            print(f"  {m['status']}, {m['tool_rounds']} rounds, "
                  f"{m['input_tokens'] + m['output_tokens']} tokens, {m['elapsed_s']} s; "
                  + ("all assertions pass" if not failed else "failed: " + ", ".join(failed)),
                  flush=True)
        if stopped:
            break
    summary = _summary(cases, runs)
    meta = {"when": stamp, "model": label, "seeds": seeds, "tokens": used, "stopped": stopped}
    (out / "report.json").write_text(json.dumps(
        {"meta": meta, "summary": summary, "runs": runs}, indent=1), encoding="utf-8")
    (out / "report.html").write_text(_html(meta, summary, runs), encoding="utf-8")
    (out / "summary.md").write_text(_markdown(meta, summary), encoding="utf-8")
    print(f"report: {out / 'report.html'}")
    if args.record_to:
        target = Path(args.record_to)
        target.mkdir(parents=True, exist_ok=True)
        for run in runs:
            if run["seed"] == 1:
                src = json.loads((out / run["folder"] / "transcript.json").read_text("utf-8"))
                (target / f"{run['case']}.json").write_text(
                    json.dumps({"turns": src["turns"]}, indent=1), encoding="utf-8")
        print(f"recordings: {target}")
    return 0 if all(s["all_hard_passed"] for s in summary.values()) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cases", default="all", help="'all' or comma-separated case ids")
    parser.add_argument("--seeds", type=int, default=1, help="runs per case")
    parser.add_argument("--provider", help="override llm_provider from .env")
    parser.add_argument("--model", help="override model from .env")
    parser.add_argument("--base-url", help="override base_url from .env")
    parser.add_argument("--replay", help="folder of recordings to play instead of a model")
    parser.add_argument("--record-to", help="also save each case's first run as a recording")
    parser.add_argument("--max-total-tokens", type=int, default=0,
                        help="stop starting runs past this many tokens (0: no limit)")
    parser.add_argument("--out", help="report folder (default evals/out/<timestamp>)")
    return asyncio.run(main_async(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
