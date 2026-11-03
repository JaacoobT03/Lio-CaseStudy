"""Run the app's real extraction on the sample quotes and score the results.

Runs inside the backend container, which has the database and OPENAI_API_KEY:

    python -m evals.run_eval /tmp/quotes --runs 3

Each run's extracted data (or null if the extraction failed) is written to
--out, then scored against the answer key with evals/score.py.
"""
import argparse
import json
from pathlib import Path

from app.db.session import SessionLocal
from app.services.extraction import extract_vendor_data
from app.services.pdf import extract_text
from evals.score import GROUND_TRUTH, main as print_scores


def run(quotes_dir: Path, runs: int) -> dict[str, list]:
    files = [quote["file"] for quote in json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))["quotes"]]
    results: dict[str, list] = {}
    with SessionLocal() as db:
        for file in files:
            # The same two steps the upload performs: PDF -> text -> extraction.
            pdf_text = extract_text((quotes_dir / file).read_bytes())
            results[file] = []
            for i in range(runs):
                response = extract_vendor_data(pdf_text, db)
                status = "ok" if response.success else f"FAILED: {response.error}"
                print(f"{file[:45]:<46} run {i + 1}: {status[:90]} | missing: {response.missing_fields}")
                results[file].append(response.data.model_dump(by_alias=True) if response.success else None)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("quotes_dir", type=Path, help="folder containing the quote PDFs")
    parser.add_argument("--runs", type=int, default=1, help="runs per quote (the model varies)")
    parser.add_argument("--out", default="/tmp/eval-results.json", help="where to write the raw results")
    args = parser.parse_args()

    results = run(args.quotes_dir, args.runs)
    Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print()
    print_scores(args.out)
