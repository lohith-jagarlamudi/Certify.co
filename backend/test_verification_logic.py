"""Deterministic regression tests for InternVerify field-level verification."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.main import _unavailable, compare_certificate_fields, classify_match, extract_certificate_fields

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
    # A real name containing "na" must not be treated as an unavailable sentinel.
    assert _unavailable("ADEPU NARESH") is False
    mismatch = compare_certificate_fields(
        {"student_name": "KAMUJU MOUNI SREETHI"},
        {"student_name": "ADEPU NARESH"},
    )
    assert mismatch["items"][0]["matched"] is False
    assert_status(
        "Unified Mentor real-name mismatch",
        {"student_name": "KAMUJU MOUNI SREETHI"},
        {"student_name": "ADEPU NARESH"},
        "not_verified",
    )

    certificate_text = """Issued Date: 08/08/2026
    CERTIFICATE OF INTERNSHIP
    KAMUJU MOUNI SREETHI
    For successfully completing 5 weeks internship as Data Analyst Intern at Unified Mentor Pvt. Ltd. Dated from 05/07/2026 to 08/08/2026"""
    fields = extract_certificate_fields(certificate_text)
    assert fields["student_name"] == "KAMUJU MOUNI SREETHI"
    assert fields["company_name"] == "Unified Mentor Pvt. Ltd"
    assert fields["internship_role"] == "Data Analyst Intern"
    assert fields["start_date"] == "05/07/2026"
    assert fields["end_date"] == "08/08/2026"
    assert fields["duration"] == "5 weeks"

    print("All InternVerify verification regression tests passed.")


if __name__ == "__main__":
    main()
