"""Export gold-standard annotation JSON from the SQLite database.

Usage examples:
    # export all cases into gold_all.json
    python export_gold.py

    # export a single case by case_id
    python export_gold.py --case-id c8f5df2c-5665-4674-95e6-1f74c516fd16

    # export all cases, one file per case, into a directory
    python export_gold.py --per-case --out-dir gold_output/

    # specify a custom output filename for all-cases export
    python export_gold.py --out gold_all_20260608.json
"""

import argparse
import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from service import annotation_service as svc  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Export gold annotation JSON")
    parser.add_argument("--case-id", help="Export a single case by case_id")
    parser.add_argument("--per-case", action="store_true",
                        help="Export one JSON file per case (into --out-dir)")
    parser.add_argument("--out-dir", default="gold_output",
                        help="Output directory when using --per-case (default: gold_output/)")
    parser.add_argument("--out", default="gold_all.json",
                        help="Output filename for all-cases export (default: gold_all.json)")
    args = parser.parse_args()

    if args.case_id:
        # single case
        data = svc.export_gold(args.case_id)
        if data is None:
            print(f"ERROR: case_id '{args.case_id}' not found in DB", file=sys.stderr)
            sys.exit(1)
        out_path = os.path.join(_REPO_ROOT, f"gold_{args.case_id}.json")
        _write(out_path, data)
        print(f"Exported: {out_path}")

    elif args.per_case:
        # one file per case
        out_dir = os.path.join(_REPO_ROOT, args.out_dir)
        os.makedirs(out_dir, exist_ok=True)
        cases = svc.export_gold()  # returns list
        for c in cases:
            fname = f"gold_{c['case_id']}.json"
            out_path = os.path.join(out_dir, fname)
            _write(out_path, c)
        print(f"Exported {len(cases)} files to {out_dir}/")

    else:
        # all cases in one file
        data = svc.export_gold()
        out_path = os.path.join(_REPO_ROOT, args.out)
        _write(out_path, data)
        n = len(data) if isinstance(data, list) else 1
        print(f"Exported {n} case(s) to {out_path}")


def _write(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
