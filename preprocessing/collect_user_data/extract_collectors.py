#!/usr/bin/env python3
"""
extract_collectors.py

Reads the result.csv produced by privacy_policy_checker.py and writes a new
CSV containing only the extensions that collect user data. Each output row
keeps the full per-extension info (ext_id, policy_link, collect_user_data,
errors) so nothing is lost.

Usage
-----
    python3 extract_collectors.py privacy_policy_section.csv
    python3 extract_collectors.py privacy_policy_section.csv -o collectors.csv
    python3 extract_collectors.py privacy_policy_section.csv --include-unknown
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path


def filter_collectors(
    csv_path: Path, want_values: set[str]
) -> tuple[list[str], list[dict]]:
    """
    Read `csv_path` and return (fieldnames, matching_rows) where
    collect_user_data matches any value in `want_values` (case-insensitive).
    """
    rows: list[dict] = []

    with csv_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        if reader.fieldnames is None:
            raise ValueError(f"{csv_path} appears to be empty.")

        # Tolerate whitespace / case differences in headers.
        field_map = {name.strip().lower(): name for name in reader.fieldnames}
        ext_col = field_map.get("ext_id")
        cud_col = field_map.get("collect_user_data")

        if ext_col is None or cud_col is None:
            raise ValueError(
                f"{csv_path} is missing required columns. "
                f"Found: {reader.fieldnames}. "
                f"Expected at least 'ext_id' and 'collect_user_data'."
            )

        for row in reader:
            ext_id = (row.get(ext_col) or "").strip()
            value = (row.get(cud_col) or "").strip().lower()
            if ext_id and value in want_values:
                rows.append(row)

    # Preserve original column order.
    return list(reader.fieldnames), rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Read result.csv from privacy_policy_checker.py and write a new CSV "
            "listing extensions that collect user data."
        )
    )
    parser.add_argument(
        "csv",
        help="Path to result.csv (or a directory containing it).",
    )
    parser.add_argument(
        "-o",
        "--out",
        default=None,
        help="Output .csv path. Default: <csv_dir>/collect_user_data.csv",
    )
    parser.add_argument(
        "--include-unknown",
        action="store_true",
        help="Also include extensions whose collect_user_data is 'unknown'.",
    )
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if csv_path.is_dir():
        csv_path = csv_path / "result.csv"

    if not csv_path.is_file():
        print(f"Error: CSV file not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    want = {"yes"}
    if args.include_unknown:
        want.add("unknown")

    try:
        fieldnames, rows = filter_collectors(csv_path, want)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Sort by ext_id for stable output.
    ext_key = next(
        (n for n in fieldnames if n.strip().lower() == "ext_id"), fieldnames[0]
    )
    rows.sort(key=lambda r: r.get(ext_key, ""))

    out_path = (
        Path(args.out) if args.out else csv_path.with_name("collect_user_data.csv")
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    label = "yes" + (" + unknown" if args.include_unknown else "")
    print(f"Found {len(rows)} extension(s) with collect_user_data = {label}.")
    print(f"Written to {out_path}")


if __name__ == "__main__":
    main()