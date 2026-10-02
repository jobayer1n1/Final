#!/usr/bin/env python3
"""
run_preprocessing.py

Three-stage preprocessing pipeline driver. Runs, in order:

    Stage 1 -- collect_user_data/privacy_policy_section_info.py
        Fetches Chrome Web Store pages for each extension ID and records
        privacy-policy info. Writes:
            results/privacy_policy_section.json
            results/privacy_policy_section.csv

    Stage 2 -- collect_user_data/extract_collectors.py
        Reads results/privacy_policy_section.csv and writes
        results/collect_user_data.csv, listing only extensions whose
        collect_user_data == "yes" (and "unknown" if --include-unknown).

    Stage 3 -- safe_to_drop/generate_safe_to_drop.py
        Runs the static analyzer on the unpacked extensions and writes
        results/safe_to_drop.csv and results/triage_full_report.json.
        Extensions from results/collect_user_data.csv are treated as
        out-of-scope and short-circuited into safe_to_drop.csv with
        source=collects_user_data (no analyzer budget spent on them).

Layout (this script lives at preprocessing/run_preprocessing.py):

    preprocessing/
        run_preprocessing.py                   <-- this file
        collect_user_data/
            privacy_policy_section_info.py     (stage 1)
            extract_collectors.py              (stage 2)
        safe_to_drop/
            generate_safe_to_drop.py           (stage 3)
            helper/
                analyze_extension.js

Every input read and every output written by this script lives under
preprocessing/results/:

    preprocessing/results/
        privacy_policy_section.json     (stage 1 output)
        privacy_policy_section.csv      (stage 1 output, stage 2 input)
        collect_user_data.csv           (stage 2 output, stage 3 input)
        safe_to_drop.csv                (stage 3 output)
        triage_full_report.json         (stage 3 output)

Skipping stages
---------------
--skip takes a comma-separated list of stage numbers (1, 2, 3). Any stage
listed there is not run; the pipeline instead reads the corresponding
input artifact that already sits in results/.

    --skip 1        reuse results/privacy_policy_section.csv, run 2 and 3
    --skip 2        reuse results/collect_user_data.csv, run 1 and 3
    --skip 1,2      run only stage 3
    --skip 3        run only stages 1 and 2
    --skip 1,2,3    no-op

If a skipped stage's input file is not present in results/, the pipeline
errors out with a clear message rather than silently skipping ahead.

Examples
--------
Full run from an unpacked extensions directory:

    cd preprocessing
    python3 run_preprocessing.py --unzipped-dir /data/unpacked --workers 8

Reuse an earlier stage-1 output, rerun stages 2 and 3:

    python3 run_preprocessing.py --unzipped-dir /data/unpacked --skip 1

Only rerun the static triage stage against an existing collect list:

    python3 run_preprocessing.py --unzipped-dir /data/unpacked --skip 1,2
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent          # preprocessing/
RESULTS_DIR = HERE / "results"                  # preprocessing/results/

PRIVACY_SCRIPT = HERE / "collect_user_data" / "privacy_policy_section_info.py"
EXTRACT_SCRIPT = HERE / "collect_user_data" / "extract_collectors.py"
TRIAGE_SCRIPT = HERE / "safe_to_drop" / "generate_safe_to_drop.py"
ANALYZER_JS = HERE / "safe_to_drop" / "helper" / "analyze_extension.js"

PRIVACY_CSV = RESULTS_DIR / "privacy_policy_section.csv"
COLLECTORS_CSV = RESULTS_DIR / "collect_user_data.csv"

ALL_STAGES = (1, 2, 3)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_skip(value: str) -> set[int]:
    """Parse '--skip 1,2' or '--skip 1 2' into {1, 2}. Only stages 1-3 are valid."""
    stages: set[int] = set()
    for token in value.replace(",", " ").split():
        if not token.isdigit() or int(token) not in ALL_STAGES:
            raise argparse.ArgumentTypeError(
                f"invalid stage number {token!r}; expected 1, 2, or 3"
            )
        stages.add(int(token))
    return stages


def fmt_stages(stages) -> str:
    s = sorted(stages)
    return ", ".join(str(x) for x in s) if s else "(none)"


def run_step(title: str, cmd: list[str]) -> None:
    """Run a subprocess, aborting the pipeline on non-zero exit."""
    print(f"\n{'=' * 72}")
    print(f"[STEP] {title}")
    print(f"       $ {' '.join(cmd)}")
    print(f"{'=' * 72}\n", flush=True)

    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        print(
            f"\n[ERROR] step failed (exit {proc.returncode}): {title}",
            file=sys.stderr,
        )
        sys.exit(proc.returncode)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=(
            "Run privacy_policy_section_info.py, extract_collectors.py, and "
            "generate_safe_to_drop.py in sequence, writing every output into "
            "<this_script_dir>/results/. Use --skip to select which stages "
            "actually run; skipped stages read their input from results/."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # ---- Shared / control ----
    ap.add_argument("--workers", type=int, default=4,
                    help="Concurrent workers, forwarded to both the CWS "
                         "checker (stage 1) and the static analyzer (stage 3).")
    ap.add_argument("--skip", type=parse_skip, default=None,
                    metavar="STAGES",
                    help="Comma-separated stage numbers to skip (1, 2, 3). "
                         "Example: --skip 1,3 runs only stage 2. Skipped "
                         "stages read their input from the results/ directory.")

    # ---- Stage 1 inputs (forwarded to privacy_policy_section_info.py) ----
    g1 = ap.add_argument_group("stage 1 inputs (privacy_policy_section_info.py)")
    g1.add_argument("--unzipped-dir", dest="unzipped_dir", default=None,
                    help="Directory of unpacked extensions (subfolders = extension IDs). "
                         "Also reused as the input for stage 3 unless --triage-dir is given.")
    g1.add_argument("--zipped-dir", dest="zipped_dir", default=None,
                    help="Directory of .crx files named {ext_id}.crx.")
    g1.add_argument("--list", default=None,
                    help="Text file with one extension ID per line.")
    g1.add_argument("--id", dest="single_id", default=None,
                    help="A single extension ID to check.")
    g1.add_argument("--delay", type=float, default=1.0,
                    help="Per-worker delay between CWS requests (seconds).")
    g1.add_argument("--limit", type=int, default=None,
                    help="Only process this many extension IDs (smoke-test helper).")
    g1.add_argument("--no-color", action="store_true",
                    help="Disable ANSI color in the CWS checker output.")

    # ---- Stage 2 options ----
    g2 = ap.add_argument_group("stage 2 options (extract_collectors.py)")
    g2.add_argument("--include-unknown", action="store_true",
                    help="Also treat collect_user_data == 'unknown' as out-of-scope.")

    # ---- Stage 3 inputs (forwarded to generate_safe_to_drop.py) ----
    g3 = ap.add_argument_group("stage 3 inputs (generate_safe_to_drop.py)")
    g3.add_argument("--triage-dir", dest="triage_dir", default=None,
                    help="Directory of unpacked extensions for static analysis. "
                         "Defaults to the value of --unzipped-dir if set.")
    g3.add_argument("--timeout", type=int, default=120,
                    help="Timeout per extension for static analysis (seconds).")

    return ap


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = build_parser()
    args = ap.parse_args()

    if args.workers < 1:
        ap.error("--workers must be at least 1")

    # ---- Decide which stages actually run --------------------------------
    skip: set[int] = set(args.skip) if args.skip else set()
    run1 = 1 not in skip
    run2 = 2 not in skip
    run3 = 3 not in skip

    if not (run1 or run2 or run3):
        print("Nothing to do — all stages skipped.")
        return 0

    print(f"Stages to run : {fmt_stages({s for s in ALL_STAGES if s not in skip})}")
    if skip:
        print(f"Stages skipped: {fmt_stages(skip)}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # ---- Verify sibling scripts we'll actually need ----------------------
    required: list[Path] = []
    if run1:
        required.append(PRIVACY_SCRIPT)
    if run2:
        required.append(EXTRACT_SCRIPT)
    if run3:
        required.extend([TRIAGE_SCRIPT, ANALYZER_JS])

    missing = [p for p in required if not p.is_file()]
    if missing:
        for p in missing:
            print(f"[ERROR] required file not found: {p}", file=sys.stderr)
        print(
            "\nExpected layout:\n"
            "    preprocessing/\n"
            "        run_preprocessing.py\n"
            "        collect_user_data/privacy_policy_section_info.py\n"
            "        collect_user_data/extract_collectors.py\n"
            "        safe_to_drop/generate_safe_to_drop.py\n"
            "        safe_to_drop/helper/analyze_extension.js",
            file=sys.stderr,
        )
        return 1

    # ---- Verify inputs a skipped upstream stage would have produced ------
    if run2 and not run1 and not PRIVACY_CSV.is_file():
        print(
            f"[ERROR] stage 2 needs {PRIVACY_CSV} but stage 1 is skipped and "
            f"that file does not exist. Run stage 1 first, or remove '1' from "
            f"--skip.",
            file=sys.stderr,
        )
        return 1

    if run3 and not run2 and not COLLECTORS_CSV.is_file():
        print(
            f"[ERROR] stage 3 needs {COLLECTORS_CSV} but stage 2 is skipped "
            f"and that file does not exist. Run stage 2 first, or remove '2' "
            f"from --skip.",
            file=sys.stderr,
        )
        return 1

    # ---- Resolve stage 3's triage directory ------------------------------
    triage_dir = args.triage_dir or args.unzipped_dir
    if run3:
        if not triage_dir:
            ap.error(
                "stage 3 needs an unpacked extensions directory. Pass "
                "--triage-dir (or --unzipped-dir, which is reused for both "
                "stages). Use --skip 3 to skip stage 3 entirely."
            )
        if not Path(triage_dir).is_dir():
            print(f"[ERROR] triage directory not found: {triage_dir}",
                  file=sys.stderr)
            return 1

    # ---- Stage 1: CWS privacy policy info --------------------------------
    if run1:
        if not any((args.unzipped_dir, args.zipped_dir, args.list, args.single_id)):
            ap.error(
                "stage 1 needs one of --unzipped-dir / --zipped-dir / --list / --id. "
                "Use --skip 1 to skip stage 1 and reuse results/privacy_policy_section.csv."
            )

        privacy_cmd = [
            sys.executable, str(PRIVACY_SCRIPT),
            "--out", str(RESULTS_DIR),
            "--workers", str(args.workers),
            "--delay", str(args.delay),
        ]
        if args.unzipped_dir:
            privacy_cmd += ["--unzipped-dir", args.unzipped_dir]
        if args.zipped_dir:
            privacy_cmd += ["--zipped-dir", args.zipped_dir]
        if args.list:
            privacy_cmd += ["--list", args.list]
        if args.single_id:
            privacy_cmd += ["--id", args.single_id]
        if args.limit is not None:
            privacy_cmd += ["--limit", str(args.limit)]
        if args.no_color:
            privacy_cmd += ["--no-color"]

        run_step(
            "collect_user_data/privacy_policy_section_info.py -> "
            "results/privacy_policy_section.{json,csv}",
            privacy_cmd,
        )

        if not PRIVACY_CSV.is_file():
            print(f"[ERROR] stage 1 did not produce {PRIVACY_CSV}",
                  file=sys.stderr)
            return 1
    elif run2:
        print(f"\n[SKIP] stage 1 — reusing {PRIVACY_CSV}")

    # ---- Stage 2: extract extensions that collect user data --------------
    if run2:
        extract_cmd = [
            sys.executable, str(EXTRACT_SCRIPT),
            str(PRIVACY_CSV),
            "-o", str(COLLECTORS_CSV),
        ]
        if args.include_unknown:
            extract_cmd.append("--include-unknown")

        run_step(
            "collect_user_data/extract_collectors.py -> "
            "results/collect_user_data.csv",
            extract_cmd,
        )

        if not COLLECTORS_CSV.is_file():
            print(f"[ERROR] stage 2 did not produce {COLLECTORS_CSV}",
                  file=sys.stderr)
            return 1
    elif run3:
        print(f"\n[SKIP] stage 2 — reusing {COLLECTORS_CSV}")

    # ---- Stage 3: static triage, using collect_user_data.csv ------------
    if run3:
        triage_cmd = [
            sys.executable, str(TRIAGE_SCRIPT),
            "--unzipped-dir", str(triage_dir),
            "--out", str(RESULTS_DIR),
            "--workers", str(args.workers),
            "--timeout", str(args.timeout),
            "--collect-data", str(COLLECTORS_CSV),
        ]
        run_step(
            "safe_to_drop/generate_safe_to_drop.py -> "
            "results/safe_to_drop.csv + results/triage_full_report.json",
            triage_cmd,
        )
    else:
        print("\n[SKIP] stage 3 — safe_to_drop.csv not regenerated")

    # ---- Summary ---------------------------------------------------------
    print(f"\n{'=' * 72}")
    print("Pipeline complete. Outputs:")
    print(f"{'=' * 72}")
    for name in (
        "privacy_policy_section.json",
        "privacy_policy_section.csv",
        "collect_user_data.csv",
        "safe_to_drop.csv",
        "triage_full_report.json",
    ):
        path = RESULTS_DIR / name
        marker = "OK " if path.is_file() else "-- "
        print(f"  [{marker}] {path}")
    print(f"{'=' * 72}")

    return 0


if __name__ == "__main__":
    sys.exit(main())