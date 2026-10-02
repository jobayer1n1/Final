#!/usr/bin/env python3
"""
Generate safe_to_drop.csv by running the static analyzers on unpacked extensions.

An extension is considered safe to drop only when:
  - its manifest.json is readable, AND
  - no data-access signal was found (manifest or code), AND
  - no exfiltration signal was found (manifest or code), AND
  - no obfuscation signal was found, AND
  - every JS/TS file was parsed and analyzed.

All other extensions are kept for the dynamic-analysis pipeline.

--collect-data CSV
    Points to a CSV whose first column is extension_id. Those extensions
    are KNOWN to collect user data, which is out of scope for this project,
    so we do not want to spend dynamic-analysis budget on them. They are
    skipped entirely (never handed to the analyzer) and are emitted into
    safe_to_drop.csv with:
        files_scanned    = 0
        manifest_version = "unknown"
        source           = "collects_user_data"
    i.e. they are dropped from the pipeline because we are not interested
    in them, not because static analysis deemed them benign.

Rows produced by the analyzer are emitted with source = "analyzed".
"""
import argparse
import csv
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
ANALYZER = HERE / "helper" / "analyze_extension.js"

FIELDNAMES = ["extension_id", "files_scanned", "manifest_version", "source"]


def load_collect_data(csv_path: Path) -> set:
    """Read a CSV and return the set of extension_ids from the first column.

    Rows are treated as extensions that collect user data (out of scope).
    The first row is assumed to be a header and skipped. Blank rows and
    rows with an empty first cell are ignored.
    """
    ids = set()
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            next(reader)  # skip header
        except StopIteration:
            return ids
        for row in reader:
            if not row:
                continue
            ext_id = (row[0] or "").strip()
            if ext_id:
                ids.add(ext_id)
    return ids


def analyze_one(ext_dir: Path, timeout: int) -> dict:
    try:
        proc = subprocess.run(
            ["node", str(ANALYZER), "--dir", str(ext_dir), "--compact"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "extension_id": ext_dir.name,
            "error": "timeout",
            "summary": {"safe_to_drop": False},
        }
    if proc.returncode != 0:
        return {
            "extension_id": ext_dir.name,
            "error": f"node exit {proc.returncode}: {proc.stderr.strip()[:500]}",
            "summary": {"safe_to_drop": False},
        }
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        return {
            "extension_id": ext_dir.name,
            "error": f"bad JSON from analyzer: {e}",
            "summary": {"safe_to_drop": False},
        }


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate safe_to_drop.csv")
    ap.add_argument("--unzipped-dir", required=True,
                    help="Directory containing unpacked extension folders")
    ap.add_argument("--out", required=True,
                    help="Output directory for safe_to_drop.csv and full report")
    ap.add_argument("--workers", type=int, default=4,
                    help="Parallel node processes (default: 4)")
    ap.add_argument("--timeout", type=int, default=120,
                    help="Timeout per extension in seconds (default: 120)")
    ap.add_argument("--collect-data", metavar="CSV", default=None,
                    help="CSV of extensions that are known to collect user "
                         "data (out of scope, we are not interested in them). "
                         "Their extension_id must be in the first column; the "
                         "first row is treated as a header. These extensions "
                         "are skipped from static analysis and emitted into "
                         "safe_to_drop.csv with files_scanned=0, "
                         "manifest_version=unknown and "
                         "source=collects_user_data.")
    args = ap.parse_args()

    unpacked = Path(args.unzipped_dir).resolve()
    out_dir = Path(args.out).resolve()

    if not unpacked.is_dir():
        print(f"[ERROR] unzipped dir not found: {unpacked}", file=sys.stderr)
        return 1
    if not ANALYZER.exists():
        print(f"[ERROR] analyzer not found: {ANALYZER}", file=sys.stderr)
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)

    # --collect-data: extension ids we deliberately exclude because they
    # collect user data. These never reach the analyzer.
    collect_ids = set()
    if args.collect_data:
        cd_path = Path(args.collect_data).resolve()
        if not cd_path.is_file():
            print(f"[ERROR] collect-data CSV not found: {cd_path}",
                  file=sys.stderr)
            return 1
        try:
            collect_ids = load_collect_data(cd_path)
        except (OSError, csv.Error) as e:
            print(f"[ERROR] failed to read collect-data CSV: {e}",
                  file=sys.stderr)
            return 1
        print(f"Loaded {len(collect_ids)} extension ids that collect user "
              f"data from {cd_path}")

    all_ext_dirs = sorted(p for p in unpacked.iterdir() if p.is_dir())

    excluded_dirs = [d for d in all_ext_dirs if d.name in collect_ids]
    ext_dirs = [d for d in all_ext_dirs if d.name not in collect_ids]

    print(f"Found {len(all_ext_dirs)} extension folders under {unpacked}")
    print(f"Excluding {len(excluded_dirs)} data-collecting extensions "
          f"(out of scope)")
    print(f"Analyzing {len(ext_dirs)} extensions")
    print(f"Using {args.workers} workers, timeout {args.timeout}s "
          f"per extension\n")

    results = []
    completed = 0
    t0 = time.time()
    if ext_dirs:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(analyze_one, d, args.timeout): d
                       for d in ext_dirs}
            for fut in as_completed(futures):
                d = futures[fut]
                results.append(fut.result())
                completed += 1
                if completed % 25 == 0 or completed == len(ext_dirs):
                    print(f"[{completed:5d}/{len(ext_dirs)}]  last: {d.name}")

    results.sort(key=lambda r: r.get("extension_id", ""))

    safe_rows = []
    errored = 0
    for r in results:
        s = r.get("summary") or {}
        if r.get("error"):
            errored += 1
            continue
        if s.get("safe_to_drop") is True:
            safe_rows.append({
                "extension_id": r.get("extension_id", ""),
                "files_scanned": s.get("files_scanned", 0),
                "manifest_version": (r.get("manifest") or {}).get(
                    "manifest_version", ""),
                "source": "analyzed",
            })

    # Out-of-scope data-collecting extensions: skip analysis entirely and
    # emit them as safe to drop so they are excluded from the dynamic pipeline.
    excluded_rows = [
        {
            "extension_id": ext_id,
            "files_scanned": 0,
            "manifest_version": "unknown",
            "source": "collects_user_data",
        }
        for ext_id in sorted(collect_ids)
    ]

    # Merge and dedupe by extension_id (analyzed results take precedence).
    seen = {row["extension_id"] for row in safe_rows}
    for row in excluded_rows:
        if row["extension_id"] not in seen:
            safe_rows.append(row)
            seen.add(row["extension_id"])

    safe_rows.sort(key=lambda r: r["extension_id"])

    safe_csv = out_dir / "safe_to_drop.csv"
    with safe_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        w.writerows(safe_rows)

    full_json = out_dir / "triage_full_report.json"
    with full_json.open("w", encoding="utf-8") as f:
        json.dump({
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "unzipped_dir": str(unpacked),
            "collect_data_csv": str(args.collect_data) if args.collect_data else None,
            "total": len(results) + len(excluded_dirs),
            "analyzed": len(results),
            "excluded_collects_user_data": len(excluded_dirs),
            "safe_to_drop": len(safe_rows),
            "errored": errored,
            "results": results,
        }, f, indent=2)

    analyzed_safe = sum(1 for r in safe_rows if r["source"] == "analyzed")
    excluded_safe = sum(1 for r in safe_rows
                        if r["source"] == "collects_user_data")

    elapsed = time.time() - t0
    print(f"\n{'=' * 60}")
    print(f"Total extensions              : {len(results) + len(excluded_dirs)}")
    print(f"Analyzed                      : {len(results)}")
    print(f"Excluded (collect user data)  : {len(excluded_dirs)}")
    print(f"Safe to drop                  : {len(safe_rows)}")
    print(f"  - via analyzer              : {analyzed_safe}")
    print(f"  - excluded (collect data)   : {excluded_safe}")
    print(f"Keep for dynamic analysis     : {len(results) - analyzed_safe}")
    print(f"Analyzer errors (kept)        : {errored}")
    print(f"Elapsed                       : {elapsed:.1f}s")
    print(f"{'=' * 60}")
    print(f"[OK] safe_to_drop.csv        -> {safe_csv}")
    print(f"[OK] triage_full_report.json -> {full_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())