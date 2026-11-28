"""The security standards every review is also validated against - pull request or
document. An added layer: the review itself (its dimensions, persona, findings, verdict,
impact and scale) is unchanged, and these checks come on top of it.

    OWASP ASVS                       application code
    OWASP Top 10                     application code
    CIS AWS Foundations Benchmark /
      AWS Foundational Security
      Best Practices                 infrastructure
    NIST SSDF                        the software development life cycle

Each review states one conclusion per standard on a single "Standards" line: a pull
request's beside its Verdict, Impact and Scale lines, a document's as a Coverage bullet
(so it is written into the document with the review). PRISM reads the line back and
names any standard the review left out (see `missing`).

Stdlib only and free of Tkinter, so it is testable headlessly.
"""
import re

# (key, the name a report uses for it, what it covers, what to check)
STANDARDS = (
    ("asvs", "ASVS", "OWASP ASVS (application code)",
     "authentication, session management, access control, input validation and output "
     "encoding, cryptography, error handling and logging, data protection, API and "
     "configuration requirements"),
    ("top10", "Top 10", "OWASP Top 10 (application code)",
     "broken access control, cryptographic failures, injection, insecure design, "
     "security misconfiguration, vulnerable and outdated components, identification and "
     "authentication failures, software and data integrity failures, security logging "
     "and monitoring failures, server-side request forgery"),
    ("aws", "AWS", "CIS AWS Foundations Benchmark / AWS Foundational Security Best "
                   "Practices (infrastructure)",
     "IAM least privilege and no wildcard or root use, encryption at rest and in transit, "
     "no public S3 buckets, security groups or endpoints, CloudTrail, Config and "
     "logging kept on, secrets in a secrets manager, network isolation"),
    ("ssdf", "SSDF", "NIST SSDF (SDLC)",
     "secure defaults, tests for security-relevant behaviour, dependency and supply-chain "
     "hygiene (pinned, maintained, scanned), no secrets in the repository, review and "
     "traceability of the change, a way to respond to vulnerabilities"),
)

NAMES = tuple(s[1] for s in STANDARDS)
# The line every report gives, as the reviewer is asked to write it.
LINE_FORMAT = ("**Standards:** ASVS: <conclusion> · Top 10: <conclusion> · "
               "AWS: <conclusion> · SSDF: <conclusion>")

_GUIDE = "\n".join(f"- {full}: {what}." for _k, _n, full, what in STANDARDS)
_CONCLUSION = (
    "For each standard the conclusion is one of: `met` (you checked and found nothing), "
    "the ids of your findings that break it, or `n/a — <why>` when the change or "
    "document touches nothing it covers (for example AWS for a change with no "
    "infrastructure). Name the control in a finding that breaks one, e.g. "
    "\"(OWASP A01:2021)\", \"(ASVS V5.3.4)\", \"(CIS AWS 2.1.1)\", \"(AWS FSBP S3.8)\", "
    "\"(SSDF PW.7)\". Never claim `met` for something you did not look at.")

_ON_TOP = ("This is an ADDITIONAL layer of validation, on top of your full review: it "
           "replaces and narrows nothing. Do the complete review exactly as your "
           "instructions say - every dimension, finding, verdict, impact and scale - and "
           "also validate against all four standards:\n")

PR_TEXT = (
    "SECURITY STANDARDS - mandatory for every review. " + _ON_TOP + _GUIDE + "\n"
    + _CONCLUSION + "\n"
    "Add this line beside your Verdict, Impact score and Scale lines:\n" + LINE_FORMAT)

DOC_TEXT = (
    "SECURITY STANDARDS - mandatory for every review, whatever your persona. " + _ON_TOP
    + "(for a document: what it asks to build)\n" + _GUIDE + "\n" + _CONCLUSION + "\n"
    "Under Coverage add exactly one bullet in this shape:\n- " + LINE_FORMAT)

REASK_PROMPT = (
    "PRISM could not find your Standards line. Changing nothing else about your review, "
    "send exactly this one line and nothing else, with one conclusion per standard (`met`, "
    "the ids of your findings that break it, or `n/a — <why>`):\n" + LINE_FORMAT)

# The last "Standards:" line in a report (bold or not, as a bullet or not).
STANDARDS_RE = re.compile(r"^[\s>*_`-]*\**Standards\**\s*[:：]\**\s*(.+?)\s*$",
                          re.IGNORECASE | re.MULTILINE)
# A standard counts as answered only when its label is followed by its own conclusion
# ("ASVS: met") - a passing mention ("SSDF: n/a — no AWS code") is not an answer.
_SEP = r"[^·;:|]{0,24}[:：]"
_LABEL_RE = {
    "asvs": re.compile(r"\bASVS\b" + _SEP, re.IGNORECASE),
    "top10": re.compile(r"\bTop[\s-]*10\b" + _SEP, re.IGNORECASE),
    "aws": re.compile(r"\b(?:AWS|CIS|FSBP)\b" + _SEP, re.IGNORECASE),
    "ssdf": re.compile(r"\bSSDF\b" + _SEP, re.IGNORECASE),
}


def parse(text):
    """The report's Standards line (its text after the label), or "" without one."""
    found = list(STANDARDS_RE.finditer(text or ""))
    return " ".join(found[-1].group(1).split()).strip(" *_`") if found else ""


def missing(line):
    """The names of the standards a Standards line says nothing about (all four for an
    empty line)."""
    return [name for key, name, _f, _w in STANDARDS if not _LABEL_RE[key].search(line or "")]
