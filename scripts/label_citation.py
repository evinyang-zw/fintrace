"""Interactive helper for the 30-claim citation pack.

Run it in a real terminal (not CI): it prints one claim + its evidence, takes
s/p/u, and writes the CSV in place so you can stop and resume.
"""

from __future__ import annotations

import csv
from pathlib import Path

PATH = Path("data/annotation/citation_labels.csv")
CHOICES = {"s": "supported", "p": "partial", "u": "unsupported"}


def main() -> None:
    rows = list(csv.DictReader(PATH.open(encoding="utf-8")))
    for row in rows:
        if row.get("label", "").strip():
            continue
        print("\n" + "=" * 78)
        print(f"{row['sample_id']}  {row['question_id']}  [{row['category']}]")
        print("\nCLAIM:\n" + row["claim"])
        print("\nEVIDENCE:\n" + row["spans"][:1200])
        while True:
            answer = input("\n[s]upported / [p]artial / [u]nsupported / [k]skip > ").strip().lower()
            if answer in CHOICES:
                row["label"] = CHOICES[answer]
                break
            if answer == "k":
                break
        if row.get("label"):
            row["reason"] = input("reason (optional) > ").strip()
        with PATH.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    remaining = sum(1 for row in rows if not row.get("label", "").strip())
    print(f"\ndone; {remaining} rows still unlabelled")
    if remaining == 0:
        print("now run: make citation-kappa")


if __name__ == "__main__":
    main()
