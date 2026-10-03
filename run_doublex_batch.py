import json
import os
import subprocess
import sys
from pathlib import Path


script_dir = Path(__file__).resolve().parent

UNZIPPED_DIR = str(script_dir / "unzipped")
DOUBLEX       = str(script_dir / "DoubleX" / "src" / "doublex.py")
RESULTS_DIR   = str(script_dir / "doublex_results")

os.makedirs(RESULTS_DIR, exist_ok=True)

def get_scripts(manifest, ext_dir):
    """Extract content and background script paths from a manifest."""
    content, background = [], []

    for cs in manifest.get("content_scripts", []):
        for js in cs.get("js", []):
            p = os.path.join(ext_dir, js)
            if os.path.isfile(p):
                content.append(p)

    bg = manifest.get("background", {})
    for js in bg.get("scripts", []):
        p = os.path.join(ext_dir, js)
        if os.path.isfile(p):
            background.append(p)
    sw = bg.get("service_worker")
    if sw:
        p = os.path.join(ext_dir, sw)
        if os.path.isfile(p):
            background.append(p)

    return content, background

def concat(files, out_path):
    """Concatenate multiple JS files into one, preserving separators."""
    with open(out_path, "w", encoding="utf-8", errors="ignore") as out:
        for f in files:
            out.write(f"\n// ==== {os.path.basename(f)} ====\n")
            with open(f, "r", encoding="utf-8", errors="ignore") as src:
                out.write(src.read())
    return out_path

summary = []

for ext_id in sorted(os.listdir(UNZIPPED_DIR)):
    ext_dir = os.path.join(UNZIPPED_DIR, ext_id)
    if not os.path.isdir(ext_dir):
        continue

    manifest_path = os.path.join(ext_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        summary.append((ext_id, "SKIP", "no manifest.json"))
        continue

    try:
        with open(manifest_path, encoding="utf-8", errors="ignore") as f:
            manifest = json.load(f)
    except Exception as e:
        summary.append((ext_id, "SKIP", f"manifest parse error: {e}"))
        continue

    content, background = get_scripts(manifest, ext_dir)

    if not content or not background:
        summary.append((ext_id, "SKIP", f"cs={len(content)} bg={len(background)}"))
        continue

    cs_path = concat(content, os.path.join(ext_dir, "_combined_cs.js"))
    bp_path = concat(background, os.path.join(ext_dir, "_combined_bp.js"))

    cmd = [
        "python3", DOUBLEX,
        "-cs", cs_path,
        "-bp", bp_path,
        "--manifest", manifest_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(script_dir))

    log_path = os.path.join(RESULTS_DIR, f"{ext_id}.log")
    with open(log_path, "w") as log:
        log.write("STDOUT:\n" + result.stdout + "\n\nSTDERR:\n" + result.stderr)

    # Detect esprima parse failures in output
    combined = result.stdout + result.stderr
    if "Esprima parsing error" in combined:
        status = "PARSE_FAIL"
    elif result.returncode == 0:
        status = "ANALYZED"
    else:
        status = f"EXIT_{result.returncode}"

    summary.append((ext_id, status, log_path))
    print(f"[{status}] {ext_id}")

# Write summary
with open(os.path.join(RESULTS_DIR, "summary.tsv"), "w") as f:
    for ext_id, status, detail in summary:
        f.write(f"{ext_id}\t{status}\t{detail}\n")

print(f"\nDone. {len(summary)} extensions processed. Summary at {RESULTS_DIR}/summary.tsv")