from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from urllib.parse import urlsplit, urlunsplit

from tracecut.config import Settings
from tracecut.pipeline import TraceCutPipeline
from tracecut.providers.llm import make_provider


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tracecut",
        description="Evidence-grounded short-video planning and rendering.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Check local tools and provider configuration.")
    doctor.add_argument("--network", action="store_true", help="Call provider model-list endpoints.")

    run = subparsers.add_parser("run", help="Plan or render one short video.")
    run.add_argument("topic")
    run.add_argument("--source-url", action="append", default=[])
    run.add_argument("--duration", type=int, default=30)
    run.add_argument("--audience", default="general")
    run.add_argument("--style", default="factual-explainer")
    run.add_argument("--planner", choices=["siliconflow", "venus"], default="siliconflow")
    run.add_argument(
        "--reviewer",
        choices=["venus", "siliconflow"],
        default="venus",
        help="Preferred reviewer gateway; falls back to SiliconFlow if unavailable.",
    )
    run.add_argument("--no-media", action="store_true")
    run.add_argument("--render", action="store_true")
    run.add_argument(
        "--tts",
        choices=["cosyvoice", "say"],
        default=None,
        help="TTS backend (overrides TRACECUT_TTS). cosyvoice uses SiliconFlow API.",
    )
    run.add_argument(
        "--rubric-iters",
        type=int,
        default=0,
        help="Run rubric-based self-iteration: 0=off (default), N=max edit rounds.",
    )
    run.add_argument(
        "--vl",
        action="store_true",
        help="After render, run the VL visual-relevance judge on sampled frames.",
    )
    run.add_argument(
        "--vl-loop",
        type=int,
        default=0,
        help="VL reselection rounds: re-pick B-roll for low-relevance scenes (implies --vl).",
    )
    run.add_argument(
        "--director",
        action="store_true",
        help="Run the Director editing agent (multi-turn tool-calling polish). Requires --rubric-iters >= 1.",
    )

    judge = subparsers.add_parser(
        "judge", help="Judge a rumor: per-claim SUPPORT/REFUTE/UNVERIFIABLE + attribution."
    )
    judge.add_argument("rumor", help="The rumor text to judge.")
    judge.add_argument("--source-url", action="append", default=[], help="Optional seed source URLs.")
    judge.add_argument("--provider", choices=["siliconflow", "venus"], default="siliconflow")
    return parser


def sanitized_endpoint(value: str) -> str:
    if not value:
        return "missing"
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def doctor(settings: Settings, network: bool) -> int:
    import shutil

    report = {
        "ffmpeg": shutil.which("ffmpeg") or "missing",
        "ffprobe": shutil.which("ffprobe") or "missing",
        "siliconflow": {
            "configured": settings.provider_ready("siliconflow"),
            "base_url": sanitized_endpoint(settings.siliconflow_base_url),
            "model": settings.siliconflow_model,
        },
        "venus": {
            "configured": settings.provider_ready("venus"),
            "base_url": sanitized_endpoint(settings.venus_base_url),
            "model": settings.venus_model,
        },
        "pexels": {"configured": bool(settings.pexels_api_key)},
        "brave_search": {"configured": bool(settings.brave_api_key)},
        "tts": {
            "backend": settings.tts_backend,
            "cosyvoice_voice": settings.cosyvoice_voice,
            "say_voice": settings.tts_voice,
        },
        "os_proxy_inheritance": False,
    }
    if network:
        for name in ("siliconflow", "venus"):
            if not settings.provider_ready(name):
                continue
            provider = make_provider(settings, name)
            try:
                ok, detail = provider.health()
                report[name]["reachable"] = ok
                report[name]["detail"] = detail
            finally:
                provider.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    required_ok = settings.provider_ready("siliconflow") and bool(shutil.which("ffmpeg"))
    return 0 if required_ok else 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    if args.command == "doctor":
        return doctor(settings, args.network)
    if args.command == "run":
        if args.tts:
            settings = replace(settings, tts_backend=args.tts)
        pipeline = TraceCutPipeline(settings)
        manifest = pipeline.run(
            topic=args.topic,
            source_urls=args.source_url,
            duration_s=args.duration,
            target_audience=args.audience,
            style=args.style,
            planner_provider=args.planner,
            reviewer_provider=args.reviewer,
            download_media=not args.no_media,
            render=args.render,
            rubric_iters=args.rubric_iters,
            run_vl_judge=args.vl or args.vl_loop > 0,
            vl_loop=args.vl_loop,
            director=args.director,
        )
        print(manifest)
        return 0
    if args.command == "judge":
        from tracecut.judge import judge_rumor, write_report
        from tracecut.pipeline import safe_slug
        import uuid

        report = judge_rumor(settings, args.rumor, args.source_url, provider_name=args.provider)
        run_id = uuid.uuid4().hex[:12]
        run_dir = settings.output_dir / f"judge_{safe_slug(args.rumor)}_{run_id}"
        report_path = write_report(report, run_dir)
        # Print a human-readable summary.
        print(f"RUMOR: {report.rumor}")
        print(f"OVERALL: {report.overall}  (search={report.search_provider})")
        print(f"evidence: {len(report.evidence)} docs")
        for v in report.verdicts:
            print(f"  - {v.claim_id}: {v.verdict} | ev={v.evidence_ids}")
            print(f"      {v.reason[:90]}")
        print(f"\nreport: {report_path}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
