"""The numbers in the docs, and the answers in the evaluation, against the tree.

This repository has no CI, so this test is the only thing that ever runs these checks.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_doc_numbers as numbers  # noqa: E402


def test_document_numbers_match_the_tree():
    drift = numbers.check()
    assert not drift, "the docs disagree with the tree:\n" + "\n".join(drift)


def test_every_evaluation_answer_is_re_derived_from_the_tree():
    """An answer nothing derives is an answer that goes stale unnoticed — one did."""
    answers = numbers.eval_answers()
    assert len(answers) == 10
    wrong = [(q, said, want) for q, said, want in answers if said != want]
    assert not wrong, "\n".join(
        f"evaluation.xml says {said!r}, the tree derives {want!r}\n  {q}" for q, said, want in wrong
    )


def test_the_ten_evaluation_pairs_are_the_ten_derivations():
    """A pair with no derivation must fail rather than pass unexamined."""
    underived = [q for q, _, want in numbers.eval_answers() if want == numbers.NO_DERIVATION]
    assert not underived, f"nothing derives these evaluation pairs: {underived}"
