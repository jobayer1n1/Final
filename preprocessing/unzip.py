#!/usr/bin/env python3
"""
unzip.py --zipped-dir <input_directory> --out <output_directory> [--workers N]

Extracts Chrome (.crx), Firefox (.xpi), and generic .zip extension files
from the zipped directory into the output directory. Each archive is unpacked
into a separate folder named after the archive file (without extension).
"""

import argparse
import concurrent.futures
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
    args = parser.parse_args()

    input_dir = Path(args.zipped_dir)
    output_dir = Path(args.out)

    if not input_dir.is_dir():
        print(f"Error: Input directory '{input_dir}' does not exist.", file=sys.stderr)
        sys.exit(1)

    # Create output directory if it doesn't exist
    output_dir.mkdir(parents=True, exist_ok=True)

    # Collect files to process
    files = [f for f in input_dir.iterdir() if f.is_file()]

    if not files:
        print(f"No files found in '{input_dir}'.")
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
                return False, file_path.name, "Skipping unsupported file"
        except Exception as e:
            return False, file_path.name, f"Failed to extract: {e}"

    print(f"Starting unzipping with {args.workers} workers...")
    success_count = 0
    error_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_file, f): f for f in files}
        for future in concurrent.futures.as_completed(futures):
            done_count += 1
            success, fname, msg = future.result()
            if success:
                success_count += 1
                print(f"[{done_count}/{total_files}] {fname} to {msg}")
            else:
                error_count += 1
                print(f"[{done_count}/{total_files}] {msg} ({fname})", file=sys.stderr)

    print(f"\nProcessed:{total_files} Success:{success_count} Error:{error_count}")


if __name__ == "__main__":
    main()