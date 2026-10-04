"""Deterministic regression tests for InternVerify field-level verification."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.main import compare_certificate_fields, classify_match

UNAVAILABLE = "Not enough data to compare"
ID = "50a6ae97c74442fab269cfb02591f9ad"


def assert_status(name, uploaded, browser, expected):
    status, reason = classify_match(uploaded, browser, "verification_unavailable")
    assert status == expected, f"{name}: expected {expected}, got {status}: {reason}"


def main():
    assert_status(
        "exact match",
        {"student_name": "JAGARLAMUDI LOHITH CHOWDARY", "certificate_number": ID},
        {"student_name": "JAGARLAMUDI LOHITH CHOWDARY", "certificate_number": ID},
        "verified",
    )
    assert_status(
        "one-sided missing name",
        {"student_name": UNAVAILABLE, "certificate_number": ID},
        {"student_name": "JAGARLAMUDI LOHITH CHOWDARY", "certificate_number": ID},
        "needs_review",
    )
    assert_status(
        "name mismatch",
        {"student_name": "JAGARLAMUDI CHOWDARY", "certificate_number": ID},
        {"student_name": "JAGARLAMUDI LOHITH CHOWDARY", "certificate_number": ID},
        "not_verified",
    )
    report = compare_certificate_fields(
        {"student_name": UNAVAILABLE, "certificate_number": ID},
        {"student_name": UNAVAILABLE, "certificate_number": ID},
    )
    assert all(item["field"] != "student_name" for item in report["items"])
    assert_status(
        "both unavailable ignored",
        {"student_name": UNAVAILABLE, "certificate_number": ID},
        {"student_name": UNAVAILABLE, "certificate_number": ID},
        "verified",
    )
    assert_status(
        "mismatch survives unavailable field",
        {"student_name": "A", "company_name": UNAVAILABLE},
        {"student_name": "B", "company_name": UNAVAILABLE},
        "not_verified",
    )
    assert_status(
        "match plus missing field",
        {"student_name": "Daniels", "certificate_number": UNAVAILABLE},
        {"student_name": "Daniels", "certificate_number": ID},
        "needs_review",
    )
    assert_status(
        "browser status ignored on exact field match",
        {"student_name": "Daniels"},
        {"student_name": "Daniels"},
        "verified",
    )
    print("All InternVerify verification regression tests passed.")


if __name__ == "__main__":
    main()
