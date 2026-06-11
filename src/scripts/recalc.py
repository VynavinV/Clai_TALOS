#!/usr/bin/env python3
"""
Recalculate an XLSX workbook by forcing a LibreOffice Calc open/save cycle in headless mode.

Usage:
    python src/scripts/recalc.py --input model.xlsx [--output model.xlsx] [--timeout 180]
"""

import argparse
import json
import os
import shutil
import subprocess
import tempfile


def _find_soffice() -> str | None:
    env_bin = os.getenv("LIBREOFFICE_BIN", "").strip()
    if env_bin:
        return env_bin

    candidates = [
        "soffice",
        "libreoffice",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "C:/Program Files/LibreOffice/program/soffice.exe",
        "C:/Program Files (x86)/LibreOffice/program/soffice.exe",
    ]

    for candidate in candidates:
        if os.path.isabs(candidate) and os.path.isfile(candidate):
            return candidate
        resolved = shutil.which(candidate)
        if resolved:
            return resolved

    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Input XLSX path")
    parser.add_argument("--output", default="", help="Output XLSX path (default: overwrite input)")
    parser.add_argument("--timeout", type=int, default=180, help="Timeout in seconds")
    args = parser.parse_args()

    input_path = os.path.abspath(args.input)
    output_path = os.path.abspath(args.output) if args.output else input_path

    if not os.path.isfile(input_path):
        print(json.dumps({"ok": False, "error": f"Input file not found: {input_path}"}))
        return 1

    soffice = _find_soffice()
    if not soffice:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "LibreOffice (soffice) not found",
                    "hint": "Install LibreOffice and/or set LIBREOFFICE_BIN",
                }
            )
        )
        return 1

    with tempfile.TemporaryDirectory(prefix="talos_recalc_") as temp_dir:
        work_in = os.path.join(temp_dir, os.path.basename(input_path))
        out_dir = os.path.join(temp_dir, "out")
        os.makedirs(out_dir, exist_ok=True)
        shutil.copy2(input_path, work_in)

        cmd = [
            soffice,
            "--headless",
            "--nologo",
            "--norestore",
            "--convert-to",
            "xlsx",
            "--outdir",
            out_dir,
            work_in,
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=max(30, int(args.timeout)),
            )
        except subprocess.TimeoutExpired:
            print(json.dumps({"ok": False, "error": "LibreOffice conversion timed out"}))
            return 1

        if proc.returncode != 0:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "LibreOffice conversion failed",
                        "stderr": (proc.stderr or "").strip(),
                        "stdout": (proc.stdout or "").strip(),
                    }
                )
            )
            return 1

        recalculated = os.path.join(out_dir, os.path.basename(input_path))
        if not os.path.isfile(recalculated):
            # Some LibreOffice versions keep extension as provided in convert target.
            alt = os.path.splitext(os.path.basename(input_path))[0] + ".xlsx"
            recalculated = os.path.join(out_dir, alt)

        if not os.path.isfile(recalculated):
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "LibreOffice did not produce an output workbook",
                        "stderr": (proc.stderr or "").strip(),
                        "stdout": (proc.stdout or "").strip(),
                    }
                )
            )
            return 1

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        shutil.copy2(recalculated, output_path)

    print(
        json.dumps(
            {
                "ok": True,
                "input_path": input_path,
                "output_path": output_path,
                "recalculated": True,
                "engine": "libreoffice",
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
