"""Score extraction results against the answer key (ground_truth.json).

Usage, from the backend/ folder:

    python -m evals.score path/to/results.json

results.json is either
  - {"<pdf file name>": [run, run, ...]}, where each run is the extracted data
    in the API's camelCase shape, or null if the extraction failed; or
  - the diagnosis.json written by diagnostics/diagnose_extraction.py.
"""
import json
import sys
from pathlib import Path

GROUND_TRUTH = Path(__file__).with_name("ground_truth.json")

CHECKS = [
    "completed",     # the user got a result at all (no red banner)
    "vendor",        # the seller, not the customer
    "vat_id",        # the seller's VAT ID, or empty if the quote has none
    "department",    # empty unless the quote names one
    "lines",         # exactly the positions being bought (no alternatives)
    "alternatives",  # alternatives flagged as such
    "net_total",     # net, including shipping/fees
    "category",      # an acceptable commodity group
]


def _norm(text) -> str:
    """Compare text ignoring case and spaces: 'DE 198 570 491' == 'DE198570491'."""
    return "".join(str(text or "").split()).upper()


def _cents(lines) -> list[int]:
    """Line values as sorted whole cents. A line without a total (e.g. an
    alternative that only shows a unit price) is valued at quantity x unit price."""
    values = []
    for line in lines:
        total = line.get("totalPrice") or line.get("total")
        if not total:
            total = (line.get("amount") or line.get("quantity") or 0) * (
                line.get("unitPrice") or line.get("unit_price") or 0
            )
        values.append(round(float(total) * 100))
    return sorted(values)


def score_run(run: dict | None, expected: dict) -> dict[str, bool]:
    """Check one extraction result against one quote's answer key."""
    if run is None:  # the extraction failed: the user got nothing
        return {check: False for check in CHECKS}

    lines = run.get("orderLines") or []
    bought = [line for line in lines if not line.get("isAlternative")]
    alternatives = [line for line in lines if line.get("isAlternative")]
    # The old API only had one total ("totalCost"); the new one has "netTotal".
    net_total = run.get("netTotal", run.get("totalCost"))

    return {
        "completed": True,
        "vendor": any(
            name.lower() in (run.get("vendorName") or "").lower() for name in expected["vendor"]
        ),
        "vat_id": _norm(run.get("vatId")) == _norm(expected["vat_id"]),
        "department": _norm(run.get("department")) in {_norm(d) for d in expected["department"]},
        "lines": _cents(bought) == _cents(expected["lines"]),
        "alternatives": _cents(alternatives) == _cents(expected["alternatives"]),
        "net_total": net_total is not None
        and round(float(net_total) * 100) == round(expected["net_total"] * 100),
        "category": run.get("commodityGroupId") in expected["category_ids"],
    }


def load_results(path: str) -> dict[str, list]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "quotes" in data:  # diagnosis.json: keep a run's answer only if the app accepted it
        return {
            quote["file"]: [
                run.get("parsed_json") if run.get("app_result") == "OK" else None
                for run in quote.get("runs", [])
            ]
            for quote in data["quotes"]
        }
    return data


def main(path: str) -> None:
    answer_key = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))["quotes"]
    results = load_results(path)

    print(f"{'quote':<28}" + "".join(f"{check:>13}" for check in CHECKS))
    passed = total = 0
    for expected in answer_key:
        runs = results.get(expected["file"]) or [None]  # missing = failed
        scores = [score_run(run, expected) for run in runs]
        row = f"{expected['file'][:27]:<28}"
        for check in CHECKS:
            ok = sum(score[check] for score in scores)
            passed += ok
            total += len(scores)
            row += f"{f'{ok}/{len(scores)}':>13}"
        print(row)
    print(f"\nScore: {passed}/{total} checks passed ({100 * passed / total:.0f}%)")


if __name__ == "__main__":
    main(sys.argv[1])
