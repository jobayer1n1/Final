#!/usr/bin/env python3
"""
unzip.py --zipped-dir <input_directory> --out <output_directory> [--workers N] [--limit N] [--ignore-list-csv <path>]

Extracts Chrome (.crx), Firefox (.xpi), and generic .zip extension files
from the zipped directory into the output directory. Each archive is unpacked
into a separate folder named after the archive file (without extension).
"""

import argparse
import concurrent.futures
import csv
import io
import os
import sys
import zipfile
from pathlib import Path

# CRX format constants
CRX_MAGIC = b"Cr24"
CRX_VERSION_2 = 2
CRX_VERSION_3 = 3


def extract_crx(crx_path: Path, out_dir: Path) -> None:
    """
    Extract a Chrome CRX file.
    CRX format: 4-byte magic 'Cr24', 4-byte version, 4-byte pubkey length,
    4-byte signature length, then pubkey, signature, and finally the ZIP data.
    """
    with open(crx_path, "rb") as f:
        data = f.read()

    # Verify magic
    if data[:4] != CRX_MAGIC:
        raise ValueError(f"Not a valid CRX file: {crx_path}")

    version = int.from_bytes(data[4:8], byteorder="little")

    if version == 2:
        pubkey_len = int.from_bytes(data[8:12], byteorder="little")
        sig_len = int.from_bytes(data[12:16], byteorder="little")
        offset = 16 + pubkey_len + sig_len
    elif version == 3:
        header_size = int.from_bytes(data[8:12], byteorder="little")
        offset = 12 + header_size
    else:
        raise ValueError(f"Unsupported CRX version: {version}")

    # The rest is a ZIP archive
    zip_data = data[offset:]

    with io.BytesIO(zip_data) as zip_buffer:
        with zipfile.ZipFile(zip_buffer, "r") as zip_ref:
            zip_ref.extractall(out_dir)


def extract_zip(zip_path: Path, out_dir: Path) -> None:
    """Extract a standard ZIP archive (including .xpi)."""
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        zip_ref.extractall(out_dir)


def load_ignored_ids(csv_path: Path) -> set:
    """Read extension IDs from index 0 of each row in the CSV file."""
    ignored = set()
    if not csv_path.is_file():
        print(f"Warning: Ignore list CSV '{csv_path}' does not exist.", file=sys.stderr)
        return ignored

    try:
        with open(csv_path, mode="r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                if row and len(row) > 0:
                    ext_id = row[0].strip()
                    if ext_id:
                        ignored.add(ext_id)
    except Exception as e:
        print(f"Error reading ignore list CSV: {e}", file=sys.stderr)
    
    return ignored


def main():
    parser = argparse.ArgumentParser(
        description="Extract browser extension archives (.crx, .xpi, .zip) from a directory."
    )
    parser.add_argument(
        "-z", "--zipped-dir", required=True,
        help="Input directory containing extension files"
    )
    parser.add_argument(
        "-o", "--out", required=True,
        help="Output directory for extracted contents"
    )
    parser.add_argument(
        "-w", "--workers", type=int, default=1,
        help="Number of workers for unzipping (default: 1)"
    )
    parser.add_argument(
        "-l", "--limit", type=int, default=None,
        help="Maximum number of extension files to process"
    )
    parser.add_argument(
        "--ignore-list-csv", type=str, default=None,
        help="Path to a CSV file containing extension IDs to ignore at index 0"
    )
    args = parser.parse_args()

    input_dir = Path(args.zipped_dir)
    output_dir = Path(args.out)

    if not input_dir.is_dir():
        print(f"Error: Input directory '{input_dir}' does not exist.", file=sys.stderr)
        sys.exit(1)

    # Create output directory if it doesn't exist
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load ignore list if provided
    ignored_ids = set()
    if args.ignore_list_csv:
        ignored_ids = load_ignored_ids(Path(args.ignore_list_csv))
        if ignored_ids:
            print(f"Loaded {len(ignored_ids)} extension ID(s) to ignore from CSV.")

    # Collect files to process
    all_files = [f for f in input_dir.iterdir() if f.is_file()]

    if not all_files:
        print(f"No files found in '{input_dir}'.")
        return

    # Filter out ignored extensions based on file stem or full filename
    files = []
    for f in all_files:
        if f.stem in ignored_ids or f.name in ignored_ids:
            print(f"Skipping ignored extension: {f.name}")
            continue
        files.append(f)

    # Apply limit if specified
    if args.limit is not None and args.limit >= 0:
        files = files[:args.limit]

    if not files:
        print("No files remaining to process after applying filters and limits.")
        return

    total_files = len(files)
    done_count = 0

    def process_file(file_path):
        ext = file_path.suffix.lower()
        base_name = file_path.stem
        out_subdir = output_dir / base_name
        out_subdir.mkdir(exist_ok=True)

        try:
            if ext == ".crx":
                extract_crx(file_path, out_subdir)
                return True, file_path.name, out_subdir
            elif ext in (".xpi", ".zip"):
                extract_zip(file_path, out_subdir)
                return True, file_path.name, out_subdir
            else:
                return False, file_path.name, "Skipping unsupported file format"
        except Exception as e:
            return False, file_path.name, f"Failed to extract: {e}"

    print(f"\nStarting unzipping of {total_files} file(s) with {args.workers} worker(s)...")
    success_count = 0
    error_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_file, f): f for f in files}
        for future in concurrent.futures.as_completed(futures):
            done_count += 1
            success, fname, msg = future.result()
            if success:
                success_count += 1
                print(f"[{done_count}/{total_files}] Extracted {fname} to {msg}")
            else:
                error_count += 1
                print(f"[{done_count}/{total_files}] {msg} ({fname})", file=sys.stderr)

    print(f"\nProcessed: {total_files} | Success: {success_count} | Error: {error_count}")


if __name__ == "__main__":
    main()