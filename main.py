#!/usr/bin/env python3
"""
govt-notice-telegram-alerts (v4)
Production monitor for 9 verified official Indian government recruitment, examination,
and public-service commission websites with Telegram Bot API alerts, manual
live_test verification mode, and Git-tracked JSON state persistence.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import datetime
import hashlib
import html
import html.parser
import http.cookiejar
import json
import logging
import os
import random
import re
import socket
import ssl
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

# Optional third-party libraries (used when installed in GitHub Actions;
# full standard-library fallback included for hermetic environments).
try:
    import requests as _requests  # type: ignore
except ImportError:
    _requests = None

try:
    from bs4 import BeautifulSoup as _BeautifulSoup  # type: ignore
except ImportError:
    _BeautifulSoup = None


try:
    import fcntl as _fcntl  # type: ignore
except ImportError:
    _fcntl = None


SCHEMA_VERSION = 1
DEFAULT_STATE_PATH = os.path.join("data", "notice_state.json")
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = (8, 12)  # (connect_timeout, read_timeout) in seconds
MAX_HTTP_RETRIES = 2
MAX_TELEGRAM_RETRIES = 3
MAX_TELEGRAM_MESSAGE_LENGTH = 4000
MAX_NOTICE_DELIVERY_ATTEMPTS = 3
MAX_FAILED_RETRY_AGE_HOURS = 24
DEFAULT_MAX_NOTICE_AGE_DAYS = int(os.environ.get("MAX_NOTICE_AGE_DAYS", "30") or "30")
MAX_NEW_NOTICE_AGE_DAYS = DEFAULT_MAX_NOTICE_AGE_DAYS
MAX_PRODUCTION_ALERTS_PER_RUN = int(os.environ.get("MAX_ALERTS_PER_RUN", "10") or "10")
MAX_FULL_NOTICE_RECORDS = 2500
MAX_TOTAL_FINGERPRINTS = 15000

TRACKING_QUERY_PARAMS: Set[str] = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "fbclid",
    "gclid",
    "msclkid",
    "_ga",
    "_gl",
    "mc_cid",
    "mc_eid",
    "phpsessid",
    "jsessionid",
    "aspsessionid",
    "sessionid",
    "nocache",
    "cachebust",
    "timestamp",
    "rand",
    "random",
    "_",
    "cb",
}

REVIEW_REQUIRED_ERROR_CATEGORIES: Set[str] = {
    "UNRECOGNIZED_STRUCTURE",
    "PARSING_ERROR",
    "INVALID_API_RESPONSE",
    "INVALID_JSON",
    "ACCESS_DENIED_PAGE",
    "HTML_PARSE_ERROR",
    "HTTP_404_NOT_FOUND",
}


class StateValidationError(Exception):
    """Raised when the persistent JSON state file is missing, empty, or invalid."""


class SourceFetchError(Exception):
    """Raised when a target website cannot be fetched or its notice structure is invalid."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(f"[{category}] {message}")
        self.category = category
        self.detail = message


class TelegramDeliveryError(Exception):
    """Raised when Telegram message delivery fails."""

    def __init__(
        self,
        message: str,
        permanent: bool = False,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.permanent = permanent
        self.ambiguous = ambiguous


@dataclasses.dataclass(frozen=True)
class SourceConfig:
    """Configuration for a monitored official government website."""

    source_id: str
    organization: str
    homepage_url: str
    notice_urls: Tuple[str, ...]
    adapter_name: str
    notes: str = ""


@dataclasses.dataclass
class DiscoveredNotice:
    """Normalized representation of a single discovered official notice."""

    source_id: str
    organization: str
    title: str
    url: str
    publication_date: Optional[str] = None
    category: Optional[str] = None
    source_identifier: Optional[str] = None
    application_date: Optional[str] = None
    exam_date: Optional[str] = None
    raw_context: str = ""
    normalized_url: str = ""
    normalized_title: str = ""
    fingerprint: str = ""

    def __post_init__(self) -> None:
        self.title = clean_display_title(self.title)
        if not self.normalized_url:
            self.normalized_url = normalize_url(self.url)
        if not self.normalized_title:
            self.normalized_title = normalize_title(self.title)
        if not self.fingerprint:
            self.fingerprint = compute_fingerprint(
                self.normalized_url, self.normalized_title
            )
        if not self.category:
            self.category = classify_notice_category(self.title, self.url)
        if not self.application_date:
            self.application_date = extract_application_date(self.raw_context or self.title)
        if not self.exam_date:
            self.exam_date = extract_exam_date(self.raw_context or self.title)



# ============================================================================
# 9 Verified Official Websites Configuration (v4)
# ============================================================================

SOURCES: Tuple[SourceConfig, ...] = (
    SourceConfig(
        source_id="ssc",
        organization="Staff Selection Commission (SSC)",
        homepage_url="https://ssc.gov.in/",
        notice_urls=(
            "https://ssc.gov.in/api/general-website/portal/notice-boards?page=1&limit=50&contentType=notice-boards&key=createdAt&order=DESC&isPaginationRequired=true&isAttachment=true&language=english&attributes=id,headline,examId,contentType,redirectUrl,startDate,endDate,language,createdAt",
            "https://ssc.gov.in/",
        ),
        adapter_name="adapter_ssc",
        notes="Uses official SSC portal JSON API (/api/general-website/portal/notice-boards) with HTML fallback.",
    ),
    SourceConfig(
        source_id="upsc",
        organization="Union Public Service Commission (UPSC)",
        homepage_url="https://www.upsc.gov.in/",
        notice_urls=(
            "https://www.upsc.gov.in/whats-new",
            "https://www.upsc.gov.in/recruitment/recruitment-advertisement",
            "https://www.upsc.gov.in/examinations/active-exams",
            "https://www.upsc.gov.in/",
        ),
        adapter_name="adapter_upsc",
        notes="Parses UPSC What's New, Recruitment Advertisements, Active Exams, and homepage PDF notices.",
    ),
    SourceConfig(
        source_id="uppsc",
        organization="Uttar Pradesh Public Service Commission (UPPSC)",
        homepage_url="https://uppsc.up.nic.in/",
        notice_urls=(
            "https://uppsc.up.nic.in/",
            "https://uppsc.up.nic.in/FooterPages/Latest-News",
        ),
        adapter_name="adapter_uppsc",
        notes="Parses UPPSC Open_PDF_DB.aspx and Open_PDF.aspx notice board items and full title attributes.",
    ),
    SourceConfig(
        source_id="hssc",
        organization="Haryana Staff Selection Commission (HSSC)",
        homepage_url="https://www.hssc.gov.in/",
        notice_urls=(
            "https://www.hssc.gov.in/publicNotice",
            "https://www.hssc.gov.in/advertisement",
            "https://www.hssc.gov.in/result",
            "https://www.hssc.gov.in/",
        ),
        adapter_name="adapter_hssc",
        notes="Parses HSSC Public Notices, Advertisements, Results tables, and header notification links.",
    ),
    SourceConfig(
        source_id="rssb",
        organization="Rajasthan Staff Selection Board (RSSB)",
        homepage_url="https://rssb.rajasthan.gov.in/",
        notice_urls=(
            "https://recruitment.rajasthan.gov.in/",
            "https://rssb.rajasthan.gov.in/news",
            "https://rssb.rajasthan.gov.in/advertisements",
            "https://rssb.rajasthan.gov.in/results",
            "https://rssb.rajasthan.gov.in/",
        ),
        adapter_name="adapter_rssb",
        notes="Parses Rajasthan State Recruitment Portal (recruitment.rajasthan.gov.in) notifications/ongoing recruitments and RSSB notice boards with legacy TLS renegotiation enabled.",
    ),
    SourceConfig(
        source_id="mpesb",
        organization="Madhya Pradesh Employees Selection Board (MPESB)",
        homepage_url="https://esb.mp.gov.in/",
        notice_urls=(
            "https://esb.mp.gov.in/e_default.html",
            "https://esb.mp.gov.in/",
            "https://esb.mponline.gov.in/",
        ),
        adapter_name="adapter_mpesb",
        notes="Parses MPESB Latest Updates box (Rulebooks, Online Form, Admit Card, Results) and MPOnline portal.",
    ),
    SourceConfig(
        source_id="dsssb",
        organization="Delhi Subordinate Services Selection Board (DSSSB)",
        homepage_url="https://dsssb.delhi.gov.in/",
        notice_urls=(
            "https://dsssbonline.nic.in/",
            "https://dsssb.delhi.gov.in/High-Light",
            "https://dsssb.delhi.gov.in/latest-vacancies",
            "https://dsssb.delhi.gov.in/latest-result",
            "https://dsssb.delhi.gov.in/",
        ),
        adapter_name="adapter_dsssb",
        notes="Parses DSSSB Online Application Portal (dsssbonline.nic.in Current Vacancies & eAdmit Cards) and dsssb.delhi.gov.in notice boards.",
    ),
    SourceConfig(
        source_id="upprpb",
        organization="Uttar Pradesh Police Recruitment and Promotion Board (UPPRPB)",
        homepage_url="https://uppbpb.gov.in/",
        notice_urls=(
            "https://uppbpb.gov.in/Home/Notice",
            "https://uppbpb.gov.in/Home/DirectRecruitment",
            "https://uppbpb.gov.in/",
        ),
        adapter_name="adapter_upprpb",
        notes="Parses UPPRPB /Home/Notice, /Home/DirectRecruitment, and /FilesUploaded/Notice PDFs.",
    ),
    SourceConfig(
        source_id="ukpsc",
        organization="Uttarakhand Public Service Commission (UKPSC)",
        homepage_url="https://psc.uk.gov.in/",
        notice_urls=(
            "https://ukpsc.net.in/",
            "https://psc.uk.gov.in/public/announcement",
            "https://psc.uk.gov.in/candidate-corner/recruitment",
            "https://psc.uk.gov.in/",
        ),
        adapter_name="adapter_ukpsc",
        notes="Parses official UKPSC Online Examination Portal (ukpsc.net.in React bundle notifications/admit cards/applications) and psc.uk.gov.in notice boards.",
    ),
)

SOURCE_BY_ID: Dict[str, SourceConfig] = {s.source_id: s for s in SOURCES}

# Websites removed from active monitoring (7 removed earlier + 3 removed in 9-site reduction: crpf, cisf, ssb):
REMOVED_SOURCE_IDS: frozenset[str] = frozenset(
    {
        "upsssc",       # UPSSSC — Geo-fenced by UP State Data Center (drops TCP SYN)
        "bpsc",         # BPSC — NIC Bihar firewall sends immediate TCP RST (errno 104)
        "bssc",         # BSSC — NIC Bihar firewall sends immediate TCP RST (errno 104)
        "hppsc",        # HPPSC — Authoritative DNS SERVFAIL / geo-fenced
        "bsf",          # BSF — NIC WAF 403 Forbidden / timeout
        "itbp",         # ITBP — Geo-fenced by NIC (drops TCP SYN from cloud IPs)
        "assam_rifles", # Assam Rifles — Geo-fenced by NIC (drops TCP SYN from cloud IPs)
        "crpf",         # CRPF — Excluded from active 9-site monitoring configuration
        "cisf",         # CISF — Excluded from active 9-site monitoring configuration
        "ssb",          # SSB — Excluded from active 9-site monitoring configuration
    }
)


# ============================================================================
# Secret Sanitization & Logging
# ============================================================================

class SecretRedactingFilter(logging.Filter):
    """Ensures Telegram bot tokens and chat IDs are never printed in logs."""

    def __init__(self, secrets: Sequence[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 4]

    def sanitize_text(self, text: str) -> str:
        if not text:
            return text
        out = text
        for secret in self._secrets:
            if secret in out:
                out = out.replace(secret, "[REDACTED]")
        # Also redact any bot<id>:<token> pattern in URLs
        out = re.sub(r"bot\d{5,}:[A-Za-z0-9_-]{15,}", "bot[REDACTED]", out)
        return out

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self.sanitize_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: self.sanitize_text(str(v)) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self.sanitize_text(str(a)) if isinstance(a, str) else a
                    for a in record.args
                )
        return True


def configure_logging(secrets: Sequence[str] = ()) -> Tuple[logging.Logger, SecretRedactingFilter]:
    logger = logging.getLogger("govt_notice_monitor")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    redactor = SecretRedactingFilter(secrets)
    handler.addFilter(redactor)
    logger.addHandler(handler)
    logger.propagate = False
    return logger, redactor


# ============================================================================
# Normalization & Deduplication Fingerprinting (Section 7)
# ============================================================================

def normalize_url(url: str, base_url: Optional[str] = None) -> str:
    """
    Normalizes a notice URL conservatively:
    - Resolves relative paths against base_url when provided
    - Normalizes Unicode (NFKC) and trims whitespace
    - Removes URL fragments (#...) and path session parameters (;jsessionid=...)
    - Converts backslashes in paths to forward slashes
    - Lowercases scheme and netloc, strips default ports (:80, :443)
    - Removes known tracking/session parameters while preserving document-identifying parameters
    """
    if not url:
        return ""
    cleaned = unicodedata.normalize("NFKC", html.unescape(url)).strip()
    cleaned = cleaned.replace("\\", "/")
    cleaned = re.sub(r"[\r\n\t]+", "", cleaned)

    if base_url:
        cleaned = urllib.parse.urljoin(base_url, cleaned)

    parsed = urllib.parse.urlsplit(cleaned)
    scheme = (parsed.scheme or "https").lower()
    if scheme not in ("http", "https"):
        return ""

    netloc = parsed.netloc.lower()
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    # Normalize http -> https for the monitored official domains so equivalent links match
    if scheme == "http" and (
        netloc.endswith(".gov.in") or netloc.endswith(".nic.in")
    ):
        scheme = "https"

    # Strip path session tokens like ;jsessionid=XYZ
    raw_path = re.sub(r";jsessionid=[^/?#]*", "", parsed.path or "/", flags=re.IGNORECASE)
    # Normalize path: collapse multiple slashes, strip trailing slash on non-root paths
    path = re.sub(r"/{2,}", "/", raw_path)
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    # Encode spaces in path safely without double-encoding existing % sequences
    path = urllib.parse.quote(urllib.parse.unquote(path), safe="/:@!$&'()*+,;=.-_~")

    # Filter out purely tracking parameters while preserving notice-identifying params
    query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    filtered_pairs = [
        (k, v)
        for (k, v) in query_pairs
        if k.lower() not in TRACKING_QUERY_PARAMS
    ]
    filtered_pairs.sort(key=lambda kv: (kv[0], kv[1]))
    normalized_query = urllib.parse.urlencode(filtered_pairs, doseq=True)

    # Preserve SPA hash-router paths starting with '#/' (e.g. ukpscnet.in/#/pg0zx1/admit-card-login)
    # while stripping ordinary HTML section fragments (#main-content, #top, etc.)
    raw_frag = (parsed.fragment or "").strip()
    preserved_fragment = raw_frag if raw_frag.startswith("/") else ""

    return urllib.parse.urlunsplit((scheme, netloc, path, normalized_query, preserved_fragment))


LEADING_SERIAL_RE = re.compile(
    r"^(?:(?:s\.?\s*no\.?|sr\.?\s*no\.?|sl\.?\s*no\.?)\s*[:.-]?\s*)?"
    r"(?:"
    r"\(\d{1,4}\)\s+|"
    r"\d{1,4}[:.)]\s+|"
    r"\d{1,4}\s+(?=(?:\d{1,2}[-/.]\d{1,2}[-/.]20\d{2}|20\d{2}-\d{2}-\d{2}|"
    r"(?:public|notice|advt|advertisement|result|final|provisional|list|corrigendum|"
    r"schedule|syllabus|admit|answer|important|press|recruitment|vacancy|online|"
    r"notification|examination|exam|regarding|subject|cancellation|extension|addendum|"
    r"विज्ञापन|सूचना|भर्ती|परिणाम|प्रवेश|उत्तर|अधिसूचना|विज्ञप्ति)\b))"
    r")",
    re.IGNORECASE,
)


TRAILING_NEW_BADGE_RE = re.compile(
    r"(?:\s*(?:\[\s*new\s*!?\s*\]|\(\s*new\s*!?\s*\)|(?<=\s)new!?))+\s*$",
    re.IGNORECASE,
)


def clean_display_title(title: str) -> str:
    """Cleans a notice title for human-readable display in Telegram messages."""
    if not title:
        return ""
    text = html.unescape(title)
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\[\s*Notice\s*Board\s*\]", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    # Strip leading table serial numbers so shifting row numbers never alter titles
    text = LEADING_SERIAL_RE.sub("", text).strip()
    # Strip trailing UI badges such as 'New', '[New]', or '(New)'
    text = TRAILING_NEW_BADGE_RE.sub("", text).strip()
    return text


def normalize_title(title: str) -> str:
    """
    Normalizes a notice title conservatively without merging distinct notices:
    - Unescapes HTML entities and normalizes Unicode (NFKC)
    - Removes UI badge clutter like '[ Notice Board ]', trailing 'New' badges,
      and leading table serial numbers
    - Normalizes dash characters and collapses internal whitespace
    """
    cleaned = clean_display_title(title)
    if not cleaned:
        return ""
    # Normalize typographic dashes to standard hyphen-minus
    text = re.sub(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]", "-", cleaned)
    # Remove trailing file size badges like (1.2 MB) or [245 KB]
    text = re.sub(r"\s*[\[(]\s*\d+(?:\.\d+)?\s*(?:kb|mb|bytes)\s*[\])]\s*$", "", text, flags=re.IGNORECASE)
    text = TRAILING_NEW_BADGE_RE.sub("", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text.casefold()


def compute_fingerprint(normalized_url: str, normalized_title: str) -> str:
    """Computes stable SHA-256 hex fingerprint from normalized URL + normalized title."""
    u = normalized_url.strip()
    t = normalized_title.strip()
    if not u and not t:
        raise ValueError("Cannot compute fingerprint from empty URL and empty title")
    payload = f"{u}\n{t}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def canonical_url_key(url: str) -> str:
    """
    Produces a canonical URL comparison key that normalizes 'www.' prefixes on
    official government domains, query parameter ordering, and URL percent-encoding
    so identical notices across mirror URLs or query variations match reliably.
    """
    norm = normalize_url(url)
    if not norm:
        return ""
    parsed = urllib.parse.urlsplit(norm)
    host = parsed.netloc.lower()
    if host.startswith("www.") and (
        host.endswith(".gov.in") or host.endswith(".nic.in") or host.endswith(".net.in")
    ):
        host = host[4:]
    path = urllib.parse.unquote(parsed.path).lower()
    path = re.sub(r"/{2,}", "/", path)
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urllib.parse.urlunsplit((parsed.scheme, host, path, parsed.query, parsed.fragment))


def canonical_title_key(title: str) -> str:
    """
    Produces a canonical title comparison key that strips leading/trailing dates,
    file size/window annotations, and punctuation/spacing variations ('cut-off' vs
    'cutoff', 'answer-key' vs 'answerkey') to detect cross-page duplicate notices.
    """
    norm = normalize_title(title)
    if not norm:
        return ""
    # Strip leading or trailing explicit dates
    text = re.sub(
        r"^(?:dated?\s*[:.-]?\s*)?(?:20\d{2}-\d{2}-\d{2}|\d{1,2}[-/.]\d{1,2}[-/.]20\d{2}|\d{1,2}[-\s/.]+[a-z]{3,9}[-,.\s/]+20\d{2})\s*[:|-]?\s*",
        "",
        norm,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\s*[\[(]?(?:dated?\s*[:.-]?\s*)?(?:20\d{2}-\d{2}-\d{2}|\d{1,2}[-/.]\d{1,2}[-/.]20\d{2}|\d{1,2}[-\s/.]+[a-z]{3,9}[-,.\s/]+20\d{2})[\])]?\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s*\(?(?:pdf|doc|docx)\s*,?\s*\d+(?:\.\d+)?\s*(?:kb|mb|bytes)\)?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\(opens in a new window\)", "", text, flags=re.IGNORECASE)
    text = text.replace("&", " and ")
    text = re.sub(r"\bcut[-\s]+off\b", "cutoff", text)
    text = re.sub(r"\banswer[-\s]+key\b", "answerkey", text)
    text = re.sub(r"\bscore[-\s]+card\b", "scorecard", text)
    text = re.sub(r"\be[-\s]*admit\s*card\b", "admit card", text)
    text = re.sub(r"[^a-z0-9\u0900-\u097f]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ============================================================================
# Strict Configurable Keyword Filtering & Category Classification (Section 4)
# ============================================================================

DEFAULT_KEYWORD_CATEGORIES: Dict[str, Tuple[str, ...]] = {
    "Recruitment": (
        "new vacancy",
        "new vacancies",
        "recruitment",
        "notification",
        "advertisement",
        "apply online",
        "application form",
        "posts",
        "vacancy",
        "vacancies",
        "advt",
        "online application",
        "post code",
        "सीधी भर्ती",
        "भर्ती",
        "विज्ञप्ति",
        "विज्ञापन",
    ),
    "Examination": (
        "exam date",
        "examination date",
        "date of examination",
        "date of exam",
        "exam schedule",
        "examination schedule",
        "schedule of examination",
        "schedule of written examination",
        "written examination",
        "paper-ii schedule",
        "paper-i schedule",
        "cbt schedule",
        "admit card",
        "e-admit card",
        "eadmit card",
        "hall ticket",
        "city intimation",
        "exam city",
        "call letter",
        "time table",
        "timetable",
        "परीक्षा तिथि",
        "परीक्षा कार्यक्रम",
        "प्रवेश पत्र",
    ),
    "Results": (
        "result",
        "results",
        "final result",
        "scorecard",
        "score card",
        "merit list",
        "cut off",
        "cutoff",
        "cut-off",
        "selection list",
        "reserve list",
        "परिणाम",
        "चयन सूची",
        "मेधा सूची",
    ),
    "Answer Keys": (
        "answer key",
        "answerkey",
        "answer-key",
        "provisional answer key",
        "final answer key",
        "response sheet",
        "उत्तर कुंजी",
    ),
    "Next Stages": (
        "dv",
        "document verification",
        "verification of documents",
        "skill test",
        "typing test",
        "stenography test",
        "interview",
        "personality test",
        "pet",
        "pst",
        "pet/pst",
        "medical examination",
        "dme",
        "rme",
        "physical efficiency test",
        "physical standard test",
        "साक्षात्कार",
        "दस्तावेज सत्यापन",
        "अभिलेखों की संवीक्षा",
    ),
}

# Category evaluation precedence so specific stage notices (Answer Keys, Results,
# Next Stages, Examination) take precedence over generic "Recruitment / Notification" words
CATEGORY_PRECEDENCE: Tuple[str, ...] = (
    "Answer Keys",
    "Results",
    "Next Stages",
    "Examination",
    "Recruitment",
)

# Generic/ambiguous single-token keywords that require positive recruitment/exam context
# and strict exclusion of unrelated administrative/commercial/media contexts
AMBIGUOUS_KEYWORDS: Set[str] = {
    "posts",
    "notification",
    "dv",
    "interview",
    "pet",
    "pst",
}

IRRELEVANT_NOTICE_RE = re.compile(
    r"\b(?:"
    r"e?[-\s]*tender(?:s|ing)?|notice\s+inviting\s+tender|nit\s+no|rfp|rfq|gem\s+bid|"
    r"bid\s+document|sealed\s+quotation|quotation\s+for|procurement\s+of|auction\s+notice|"
    r"public\s+auction|e[-\s]*auction|empanelment\s+of\s+(?:firm|vendor|agency|hospital|hotel|lab)|"
    r"annual\s+maintenance\s+contract|supply\s+and\s+installation|purchase\s+of\s+equipment|"
    r"earnest\s+money|security\s+deposit|"
    r"transfer\s+(?:and|&)\s+posting|posting\s+and\s+transfer|transfer\s+order|posting\s+order|"
    r"routine\s+transfer|inter[-\s]*commissionerate\s+transfer|deputation\s+to\s+the\s+post\s+of\s+chief\s+vigilance|"
    r"seniority\s+list|gradation\s+list|civil\s+list|disposition\s+list|"
    r"pension\s+adalat|family\s+pension|compassionate\s+appointment|"
    r"immovable\s+property\s+return|annual\s+property\s+return|apar\s+grading|"
    r"medical\s+reimbursement|cghs\s+empanelment|ltc\s+claim|"
    r"gazetted\s+holiday|list\s+of\s+holidays|holiday\s+calendar|"
    r"flag\s+code\s+of\s+india|citizen'?s?\s+charter|annual\s+report\s+20\d{2}|"
    r"suo\s+motu\s+disclosure|right\s+to\s+information\s+manual|"
    r"memorandum\s+of\s+understanding|signing\s+of\s+mou"
    r")\b",
    re.IGNORECASE,
)

IRRELEVANT_URL_RE = re.compile(
    r"(?:/tender/|/tenders/|tender_notice|e[-_]?tender|/auction/|/quotation/|/procurement/|"
    r"flag_code|citizen_charter|annual_report|seniority_list|gradation_list|transfer_order)",
    re.IGNORECASE,
)

AMBIGUOUS_POSTS_NEGATIVE_RE = re.compile(
    r"\b(?:"
    r"speed\s+post|registered\s+post|by\s+post|ordinary\s+post|india\s+post|post\s+office|"
    r"post\s+box|postal\s+order|ex[-\s]*post\s+facto|post[-\s]*mortem|social\s+media\s+posts?|"
    r"blog\s+posts?|abolition\s+of\s+posts?|continuance\s+of\s+posts?|upgradation\s+of\s+posts?|"
    r"redesignation\s+of\s+posts?|sanctioned\s+strength\s+of\s+posts?"
    r")\b",
    re.IGNORECASE,
)

AMBIGUOUS_NOTIFICATION_NEGATIVE_RE = re.compile(
    r"\b(?:"
    r"tariff\s+notification|land\s+acquisition\s+notification|holiday\s+notification|"
    r"privacy\s+notification|maintenance\s+notification|cookie\s+notification|"
    r"gazette\s+notification\s+(?:regarding|on|for)\s+(?:amendment|rules|act|regulation|holiday|tariff)"
    r")\b",
    re.IGNORECASE,
)

AMBIGUOUS_DV_NEGATIVE_RE = re.compile(
    r"\b(?:"
    r"disbursement\s+voucher|dv\s+voucher|dv\s+no|domestic\s+violence|dv\s+act|official\s+car\s+dv"
    r")\b",
    re.IGNORECASE,
)

AMBIGUOUS_INTERVIEW_NEGATIVE_RE = re.compile(
    r"\b(?:"
    r"media\s+interview|press\s+interview|tv\s+interview|television\s+interview|"
    r"radio\s+interview|podcast\s+interview|newspaper\s+interview|exclusive\s+interview"
    r")\b",
    re.IGNORECASE,
)

POSITIVE_JOB_CONTEXT_RE = re.compile(
    r"\b(?:"
    r"exam(?:ination)?s?|recruitment|vacancy|vacancies|advt|advertisement|"
    r"candidate(?:s)?|applicant(?:s)?|application(?:s)?|apply|online\s+form|"
    r"roll\s*no|roll\s+number|merit|result(?:s)?|score\s*card|cut[-\s]*off|shortlist(?:ed|ing)?|"
    r"admit\s*card|hall\s*ticket|call\s*letter|answer[-\s]*key|response\s+sheet|"
    r"schedule|time\s*table|date\s+of|shift|tier|phase|stage|paper|cbt|ldce|omr|"
    r"document\s+verification|skill\s+test|typing\s+test|medical|pet|pst|dme|rme|"
    r"post\s+of|posts\s+of|\d+\s+posts|post\s+code|group[-\s]*[abcd]|grade[-\s]*[i1234v]+|"
    r"constable|inspector|sub[-\s]*inspector|asi|head\s+constable|rifleman|"
    r"officer|assistant|engineer|je|ae|lecturer|teacher|professor|scientist|"
    r"clerk|stenographer|steno|translator|auditor|accountant|mts|chsl|cgl|cpo|gd|nda|cds|cms|ese|"
    r"civil\s+services|subordinate\s+services|selection\s+post|deputation|"
    r"ssc|upsc|uppsc|hssc|rssb|rsmssb|mpesb|dsssb|upprpb|uppbpb|ukpsc|crpf|cisf|ssb|"
    r"भर्ती|परीक्षा|परिणाम|विज्ञप्ति|विज्ञापन|अभ्यर्थी|पद|आरक्षी|उपनिरीक्षक"
    r")\b",
    re.IGNORECASE,
)


def _normalize_for_keyword_search(text: str) -> str:
    """Normalizes punctuation and spacing variations for reliable keyword matching."""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", html.unescape(text)).lower()
    s = re.sub(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212_/]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _compile_keyword_pattern(keyword: str) -> re.Pattern[str]:
    """
    Compiles a word-boundary regex for a keyword that handles common spacing,
    hyphenation, and punctuation variations (e.g. 'cut off' / 'cut-off' / 'cutoff',
    'answer key' / 'answer-key' / 'answerkey', 'e-admit card' / 'eadmit card').
    """
    kw = _normalize_for_keyword_search(keyword)
    if kw in ("cut off", "cutoff", "cut-off"):
        return re.compile(r"\bcut[\s_-]*off\b", re.IGNORECASE)
    if kw in ("answer key", "answerkey", "answer-key"):
        return re.compile(r"\banswer[\s_-]*keys?\b", re.IGNORECASE)
    if kw == "provisional answer key":
        return re.compile(r"\bprovisional\s+answer[\s_-]*keys?\b", re.IGNORECASE)
    if kw == "final answer key":
        return re.compile(r"\bfinal\s+answer[\s_-]*keys?\b", re.IGNORECASE)
    if kw in ("scorecard", "score card"):
        return re.compile(r"\bscore[\s_-]*cards?\b", re.IGNORECASE)
    if kw in ("admit card", "e-admit card", "eadmit card"):
        return re.compile(r"\b(?:e[\s_-]*)?admit[\s_-]*cards?\b", re.IGNORECASE)
    if kw == "hall ticket":
        return re.compile(r"\bhall[\s_-]*tickets?\b", re.IGNORECASE)
    if kw == "posts":
        return re.compile(r"\bposts?\b", re.IGNORECASE)
    if kw in ("time table", "timetable"):
        return re.compile(r"\btime[\s_-]*table\b", re.IGNORECASE)

    tokens = [re.escape(tok) for tok in kw.split() if tok]
    if not tokens:
        return re.compile(r"^\b$", re.IGNORECASE)
    joined = r"[\s_-]+".join(tokens)
    # Use word boundaries for ASCII alphanumeric keywords; for Devanagari use whitespace/punctuation boundaries
    if re.match(r"^[a-z0-9]", kw) and re.search(r"[a-z0-9]$", kw):
        return re.compile(rf"\b{joined}\b", re.IGNORECASE)
    return re.compile(rf"(?:^|(?<=[\s,.;:()\[\]/-])){joined}(?=$|[\s,.;:()\[\]/-])", re.IGNORECASE)


def load_keyword_categories(
    custom_categories: Optional[Dict[str, Sequence[str]]] = None,
    config_path: Optional[str] = None,
) -> Dict[str, Tuple[str, ...]]:
    """
    Loads keyword categories from custom_categories, config_path JSON file,
    NOTICE_KEYWORDS_JSON, or NOTICE_EXTRA_KEYWORDS environment variable,
    falling back to DEFAULT_KEYWORD_CATEGORIES.
    """
    if custom_categories is not None:
        return {k: tuple(v) for k, v in custom_categories.items()}

    merged: Dict[str, List[str]] = {
        cat: list(kws) for cat, kws in DEFAULT_KEYWORD_CATEGORIES.items()
    }
    if config_path and os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                parsed_file = json.load(f)
            if isinstance(parsed_file, dict):
                for cat, kws in parsed_file.items():
                    if isinstance(kws, list):
                        merged[str(cat)] = [str(x).strip() for x in kws if str(x).strip()]
        except (OSError, json.JSONDecodeError):
            pass

    env_json = os.environ.get("NOTICE_KEYWORDS_JSON", "").strip()
    if env_json:
        try:
            parsed = json.loads(env_json)
            if isinstance(parsed, dict):
                for cat, kws in parsed.items():
                    if isinstance(kws, list):
                        merged[str(cat)] = [str(x).strip() for x in kws if str(x).strip()]
        except json.JSONDecodeError:
            pass

    extra_kws = os.environ.get("NOTICE_EXTRA_KEYWORDS", "").strip()
    if extra_kws:
        extras = [k.strip() for k in extra_kws.split(",") if k.strip()]
        if extras:
            merged.setdefault("Recruitment", []).extend(extras)

    return {cat: tuple(kws) for cat, kws in merged.items()}


def is_irrelevant_notice_context(title: str, content: str = "", url: str = "") -> bool:
    """
    Returns True when a notice is an administrative, procurement, tender, auction,
    staff transfer/posting, seniority list, pension, holiday, RTI, or media notice
    that must never trigger a job notification.
    """
    combined_text = f"{title} {content}".strip()
    if not combined_text:
        return True
    if IRRELEVANT_NOTICE_RE.search(combined_text):
        return True
    if url and IRRELEVANT_URL_RE.search(url):
        return True
    return False


def _passes_ambiguous_keyword_context(keyword: str, combined_text: str) -> bool:
    """
    Validates that ambiguous keywords ('posts', 'notification', 'dv', 'interview', 'pet', 'pst')
    appear in a genuine recruitment/examination context and not an unrelated phrase.
    """
    kw = keyword.lower().strip()
    if kw == "posts":
        cleaned = AMBIGUOUS_POSTS_NEGATIVE_RE.sub(" ", combined_text)
        if not re.search(r"\bposts?\b", cleaned, re.IGNORECASE):
            return False
        return bool(
            re.search(
                r"\b(?:\d+\s+posts?|posts?\s+of|post\s+code|vacant\s+posts?|various\s+posts?|"
                r"recruitment|vacancy|vacancies|exam(?:ination)?|advt|advertisement|selection|candidate|apply)\b",
                cleaned,
                re.IGNORECASE,
            )
        )
    if kw == "notification":
        if AMBIGUOUS_NOTIFICATION_NEGATIVE_RE.search(combined_text):
            return False
        # Strip the word 'notification' itself before checking for positive job/exam context
        without_notif = re.sub(r"\bnotifications?\b", " ", combined_text, flags=re.IGNORECASE)
        return bool(POSITIVE_JOB_CONTEXT_RE.search(without_notif))
    if kw == "dv":
        if AMBIGUOUS_DV_NEGATIVE_RE.search(combined_text):
            return False
        without_dv = re.sub(r"\bdv\b", " ", combined_text, flags=re.IGNORECASE)
        return bool(POSITIVE_JOB_CONTEXT_RE.search(without_dv))
    if kw == "interview":
        if AMBIGUOUS_INTERVIEW_NEGATIVE_RE.search(combined_text):
            return False
        without_int = re.sub(r"\binterviews?\b", " ", combined_text, flags=re.IGNORECASE)
        return bool(POSITIVE_JOB_CONTEXT_RE.search(without_int))
    if kw in ("pet", "pst"):
        without_pet = re.sub(r"\b(?:pet|pst)\b", " ", combined_text, flags=re.IGNORECASE)
        return bool(POSITIVE_JOB_CONTEXT_RE.search(without_pet))
    return True


def match_notice_keywords(
    title: str,
    content: str = "",
    url: str = "",
    keyword_categories: Optional[Dict[str, Sequence[str]]] = None,
    categories: Optional[Dict[str, Sequence[str]]] = None,
) -> Tuple[bool, Optional[str], List[str]]:
    """
    Evaluates whether a notice matches at least one configured keyword from the 5
    official categories (Recruitment, Examination, Results, Answer Keys, Next Stages)
    using word boundaries and contextual false-positive checks.

    Returns: (is_relevant, matched_category, matched_keywords)
    """
    if is_irrelevant_notice_context(title, content=content, url=url):
        return False, None, []

    combined_raw = f"{title} {content}".strip()
    norm_text = _normalize_for_keyword_search(combined_raw)
    if not norm_text:
        return False, None, []

    effective_cats = load_keyword_categories(
        custom_categories=keyword_categories if keyword_categories is not None else categories
    )
    ordered_cats = list(CATEGORY_PRECEDENCE) + [
        c for c in effective_cats.keys() if c not in CATEGORY_PRECEDENCE
    ]

    matched_by_cat: Dict[str, List[str]] = {}
    all_matched_kws: List[str] = []

    for cat in ordered_cats:
        kws = effective_cats.get(cat, ())
        for kw in kws:
            pattern = _compile_keyword_pattern(kw)
            if pattern.search(norm_text) or pattern.search(combined_raw):
                norm_kw = kw.lower().strip()
                if norm_kw in AMBIGUOUS_KEYWORDS:
                    if not _passes_ambiguous_keyword_context(norm_kw, combined_raw):
                        continue
                matched_by_cat.setdefault(cat, []).append(kw)
                if kw not in all_matched_kws:
                    all_matched_kws.append(kw)

    if not matched_by_cat:
        return False, None, []

    # Choose the most specific category according to CATEGORY_PRECEDENCE
    # Special case: if 'Recruitment' matched an explicit recruitment/vacancy/advertisement phrase
    # and 'Examination' only matched generic 'examination', prefer 'Recruitment' when title starts with Advt/Recruitment/Current Vacancy/Ongoing Recruitment
    chosen_category = next(c for c in ordered_cats if c in matched_by_cat)
    if (
        chosen_category == "Examination"
        and "Recruitment" in matched_by_cat
        and re.search(
            r"\b(?:new\s+vacancy|current\s+vacancy|ongoing\s+recruitment|recruitment\s+for|advertisement\s+for|recruitment\s+advertisement|direct\s+recruitment)\b",
            combined_raw,
            re.IGNORECASE,
        )
        and not re.search(
            r"\b(?:admit\s*card|hall\s*ticket|city\s+intimation|call\s+letter|exam(?:ination)?\s+schedule|schedule\s+of|time\s*table|exam(?:ination)?\s+date)\b",
            combined_raw,
            re.IGNORECASE,
        )
    ):
        chosen_category = "Recruitment"

    return True, chosen_category, all_matched_kws


def is_relevant_job_notice(
    title: str,
    content: str = "",
    url: str = "",
    keyword_categories: Optional[Dict[str, Sequence[str]]] = None,
) -> bool:
    """Returns True if the notice matches strict job/exam keyword filtering."""
    matched, _, _ = match_notice_keywords(
        title=title,
        content=content,
        url=url,
        keyword_categories=keyword_categories,
    )
    return matched


def classify_notice_category(
    title: str,
    url: str = "",
    keyword_categories: Optional[Dict[str, Sequence[str]]] = None,
) -> str:
    """Determines the canonical notice category from title/URL keywords."""
    matched, cat, _ = match_notice_keywords(
        title=title,
        content="",
        url=url,
        keyword_categories=keyword_categories,
    )
    if matched and cat:
        return cat
    return "Official Notice"


# ============================================================================
# Date Extraction & Mandatory Publication-Date Validation (Section 4)
# ============================================================================

SingleDateTokenPattern = (
    r"(?:20\d{2}-(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01])|"
    r"(?:0?[1-9]|[12]\d|3[01])[-/.](?:0?[1-9]|1[0-2])[-/.]20\d{2}|"
    r"(?:0?[1-9]|[12]\d|3[01])[-\s/.]+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)[-,.\s/]+20\d{2}|"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)[-\s/.]+(?:0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?[-,.\s/]+20\d{2})"
)

DATE_PATTERNS: Tuple[re.Pattern[str], ...] = (
    # ISO YYYY-MM-DD (including ISO-8601 timestamps like 2026-10-12T13:17:07.360Z)
    re.compile(r"\b(20\d{2}-(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01]))(?:(?=T)|\b)"),
    # DD-MM-YYYY or DD/MM/YYYY or DD.MM.YYYY
    re.compile(r"\b((?:0?[1-9]|[12]\d|3[01])[-/.](?:0?[1-9]|1[0-2])[-/.]20\d{2})\b"),
    # DD Mon YYYY or DD-Mon-YYYY or DD Month YYYY
    re.compile(
        r"\b((?:0?[1-9]|[12]\d|3[01])[-\s/.]+"
        r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"[-,.\s/]+20\d{2})\b",
        re.IGNORECASE,
    ),
)

# Loose date token finder used to detect and validate calendar dates
ANY_DATE_LIKE_TOKEN_RE = re.compile(
    r"\b("
    r"20\d{2}-\d{1,2}-\d{1,2}(?=[T\s]|$|\b)|"
    r"\d{1,2}[-/.]\d{1,2}[-/.]20\d{2}\b|"
    r"\d{1,2}[-\s/.]+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)[-,.\s/]+20\d{2}\b|"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)[-\s/.]+\d{1,2}(?:st|nd|rd|th)?[-,.\s/]+20\d{2}\b"
    r")",
    re.IGNORECASE,
)

MONTH_NAME_MAP: Dict[str, int] = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

# Clauses that represent expiry/application deadlines rather than publication dates
EXPIRY_DATE_CLAUSE_RE = re.compile(
    rf"\b(?:visible\s+up\s*to|valid\s+up\s*to|valid\s+till|last\s+date(?:\s+(?:for|of|to)\s+(?:apply|online\s+application|submission|registration|fee\s+payment))?|closing\s+date(?:\s+of\s+application)?|end\s+date|archive\s+on|apply\s+(?:online\s+)?(?:up\s*to|till|before|by))\s*[:.-]?\s*({SingleDateTokenPattern})",
    re.IGNORECASE,
)

# Clauses that represent application windows ("Apply Online from X to Y")
APPLICATION_WINDOW_CLAUSE_RE = re.compile(
    rf"\b(?:apply\s+online(?:\s+from|\s+between)?|online\s+application(?:s)?(?:\s+from|\s+starts?\s+on|\s+between)?|application\s+form(?:\s+from|\s+starts?\s+on)?|registration\s+(?:from|starts?\s+on|between))\s*[:.-]?\s*({SingleDateTokenPattern})(?:\s*(?:to|till|up\s*to|-)\s*({SingleDateTokenPattern}))?",
    re.IGNORECASE,
)

# Clauses that represent modified/updated/scraped timestamps rather than official publication dates
MODIFIED_DATE_CLAUSE_RE = re.compile(
    rf"\b(?:last\s+modified(?:\s+date|\s+on)?|modified\s+(?:date|on)|last\s+updated(?:\s+on)?|updated\s+on|page\s+updated\s+on|website\s+updated\s+on|scraped\s+on|fetched\s+on)\s*[:.-]?\s*({SingleDateTokenPattern})",
    re.IGNORECASE,
)

# Clauses that represent examination / interview / PET / PST / DV event dates rather than publication dates
EXAM_DATE_CLAUSE_RE = re.compile(
    rf"\b(?:exam(?:ination)?\s+date|date\s+of\s+(?:written\s+|cbt\s+|main\s+|preliminary\s+|skill\s+|typing\s+)?(?:exam(?:ination)?|test|interview|dv|verification)|(?:written\s+|cbt\s+)?exam(?:ination)?\s+(?:will\s+be\s+|to\s+be\s+|is\s+)?(?:held|conducted|scheduled)\s+(?:on|from)|(?:to\s+be\s+|will\s+be\s+)(?:held|conducted)\s+(?:on|from)|scheduled\s+(?:to\s+be\s+held\s+)?(?:on|from)|(?:interview|skill\s+test|typing\s+test|dv|document\s+verification|pet\s*/?\s*pst|medical\s+examination)\s+(?:date|on|from|scheduled\s+on))\s*[:.-]?\s*({SingleDateTokenPattern})(?:\s*(?:to|till|-)\s*({SingleDateTokenPattern}))?",
    re.IGNORECASE,
)


def extract_expiry_date(text: str) -> Optional[str]:
    """
    Extracts an explicit expiry/closing/visibility-end date if present in text
    (e.g. 'Visible upto : 29/04/2026', 'Valid till 2025-12-31').
    """
    if not text:
        return None
    cleaned = re.sub(r"\s+", " ", text).strip()
    match = EXPIRY_DATE_CLAUSE_RE.search(cleaned)
    if match:
        candidate = match.group(1).strip()
        if parse_publication_date_sort_key(candidate) is not None:
            return candidate
    return None


def extract_application_date(text: str) -> Optional[str]:
    """
    Extracts explicit application start/closing dates when present in text
    (e.g. 'Apply Online from 20-10-2026 to 15-11-2026' or 'Last Date: 15-11-2026').
    """
    if not text:
        return None
    cleaned = re.sub(r"\s+", " ", text).strip()
    win_match = APPLICATION_WINDOW_CLAUSE_RE.search(cleaned)
    if win_match:
        d1 = win_match.group(1).strip()
        d2 = (win_match.group(2) or "").strip()
        if parse_publication_date_sort_key(d1) is not None:
            if d2 and parse_publication_date_sort_key(d2) is not None:
                return f"{d1} to {d2}"
            return d1
    exp_match = re.search(
        rf"\b(?:last\s+date(?:\s+(?:for|of|to)\s+(?:apply|online\s+application|submission|registration|fee\s+payment))?|closing\s+date(?:\s+of\s+application)?|apply\s+(?:online\s+)?(?:up\s*to|till|before|by))\s*[:.-]?\s*({SingleDateTokenPattern})",
        cleaned,
        re.IGNORECASE,
    )
    if exp_match:
        cand = exp_match.group(1).strip()
        if parse_publication_date_sort_key(cand) is not None:
            return cand
    return None


def extract_exam_date(text: str) -> Optional[str]:
    """
    Extracts explicit examination / test / interview / DV schedule dates when present
    (e.g. 'Exam Date: 15-12-2026' or 'to be held on 10-11-2026 to 12-11-2026').
    """
    if not text:
        return None
    cleaned = re.sub(r"\s+", " ", text).strip()
    match = EXAM_DATE_CLAUSE_RE.search(cleaned)
    if match:
        d1 = match.group(1).strip()
        d2 = (match.group(2) or "").strip()
        if parse_publication_date_sort_key(d1) is not None:
            if d2 and parse_publication_date_sort_key(d2) is not None:
                return f"{d1} to {d2}"
            return d1
    return None


def is_notice_expired(
    text: str = "",
    end_date: Optional[str] = None,
    reference_date: Optional[datetime.date] = None,
) -> bool:
    """
    Returns True if the notice has an explicit expiry/closing date that is
    strictly in the past relative to reference_date (defaults to today in UTC).
    """
    raw_exp = end_date or extract_expiry_date(text)
    if not raw_exp:
        return False
    exp_key = parse_publication_date_sort_key(raw_exp)
    if exp_key is None:
        return False
    ref = reference_date or datetime.datetime.now(datetime.timezone.utc).date()
    return datetime.date(*exp_key) < ref


def extract_explicit_date(text: str) -> Optional[str]:
    """
    Extracts an explicit official publication date if present in text, strictly
    ignoring modified dates ('Last modified ...', 'Updated on ...'), application
    deadlines/windows ('Visible upto ...', 'Last date ...', 'Closing date ...',
    'Apply online from ...'), and examination event dates ('Exam Date: ...',
    'to be held on ...').
    Never invents or infers dates from the current scrape timestamp.
    """
    if not text:
        return None
    cleaned = re.sub(r"\s+", " ", text).strip()
    # Strip modified dates, application windows, expiry/closing dates, and exam dates
    cleaned = MODIFIED_DATE_CLAUSE_RE.sub(" ", cleaned)
    cleaned = APPLICATION_WINDOW_CLAUSE_RE.sub(" ", cleaned)
    cleaned = EXPIRY_DATE_CLAUSE_RE.sub(" ", cleaned)
    cleaned = EXAM_DATE_CLAUSE_RE.sub(" ", cleaned)

    for m in ANY_DATE_LIKE_TOKEN_RE.finditer(cleaned):
        candidate = m.group(1).strip()
        if parse_publication_date_sort_key(candidate) is not None:
            return candidate
    return None


def parse_publication_date_sort_key(date_str: Optional[str]) -> Optional[Tuple[int, int, int]]:
    """
    Parses an explicit date string into a validated (year, month, day) tuple.
    Returns None if missing, unparseable, or an impossible calendar date (e.g. 31-02-2026).
    Never substitutes current time.
    """
    if not date_str:
        return None
    s = date_str.strip()
    # 1. ISO YYYY-MM-DD (including trailing ISO timestamp e.g. 2026-10-18T10:00:00Z)
    m_iso = re.match(r"^(20\d{2})-(0?[1-9]|1[0-2])-(0?[1-9]|[12]\d|3[01])(?:[T\s].*)?$", s)
    if m_iso:
        y, m, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        try:
            datetime.date(y, m, d)
            return (y, m, d)
        except ValueError:
            return None

    # 2. DD-MM-YYYY, DD/MM/YYYY, DD.MM.YYYY
    m_dmy = re.match(r"^(0?[1-9]|[12]\d|3[01])[-/.](0?[1-9]|1[0-2])[-/.](20\d{2})$", s)
    if m_dmy:
        d, m, y = int(m_dmy.group(1)), int(m_dmy.group(2)), int(m_dmy.group(3))
        try:
            datetime.date(y, m, d)
            return (y, m, d)
        except ValueError:
            return None

    # 3. DD Mon YYYY / DD-Mon-YYYY / DD Month YYYY
    m_named = re.match(
        r"^(0?[1-9]|[12]\d|3[01])[-\s/.]+([A-Za-z]{3,9})[-,.\s/]+(20\d{2})$", s
    )
    if m_named:
        d = int(m_named.group(1))
        mon_prefix = m_named.group(2)[:3].lower()
        y = int(m_named.group(3))
        m = MONTH_NAME_MAP.get(mon_prefix)
        if m is not None:
            try:
                datetime.date(y, m, d)
                return (y, m, d)
            except ValueError:
                return None

    # 4. Mon DD YYYY / Month DD, YYYY
    m_mdy_named = re.match(
        r"^([A-Za-z]{3,9})[-\s/.]+(0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?[-,.\s/]+(20\d{2})$",
        s,
    )
    if m_mdy_named:
        mon_prefix = m_mdy_named.group(1)[:3].lower()
        d = int(m_mdy_named.group(2))
        y = int(m_mdy_named.group(3))
        m = MONTH_NAME_MAP.get(mon_prefix)
        if m is not None:
            try:
                datetime.date(y, m, d)
                return (y, m, d)
            except ValueError:
                return None

    return None


def format_canonical_date(date_str: Optional[str]) -> Optional[str]:
    """Formats a valid publication date string as YYYY-MM-DD, or returns None if invalid."""
    key = parse_publication_date_sort_key(date_str)
    if key is None:
        return None
    return f"{key[0]:04d}-{key[1]:02d}-{key[2]:02d}"


def validate_publication_date(
    pub_date_str: Optional[str],
    reference_date: Optional[datetime.date] = None,
    max_age_days: int = DEFAULT_MAX_NOTICE_AGE_DAYS,
    allow_future_validated: bool = False,
    context_text: str = "",
) -> Tuple[bool, Optional[str], str]:
    """
    Mandatory publication-date validator for production alerts:
    - Rejects missing or empty publication dates ('MISSING_PUBLICATION_DATE')
    - Rejects invalid or impossible calendar dates like 31-02-2026 ('INVALID_PUBLICATION_DATE')
    - Rejects future publication dates unless explicitly validated ('FUTURE_PUBLICATION_DATE')
    - Rejects notices older than `max_age_days` ('STALE_PUBLICATION_DATE')

    Returns: (is_valid, canonical_iso_date, status_reason)
    """
    if not pub_date_str or not str(pub_date_str).strip():
        return False, None, "MISSING_PUBLICATION_DATE"

    raw = str(pub_date_str).strip()
    # Ensure the string isn't actually a labelled deadline/modified/exam date string
    if (
        MODIFIED_DATE_CLAUSE_RE.search(raw)
        or EXPIRY_DATE_CLAUSE_RE.search(raw)
        or EXAM_DATE_CLAUSE_RE.search(raw)
    ):
        return False, None, "NON_PUBLICATION_DATE_TYPE"

    sort_key = parse_publication_date_sort_key(raw)
    if sort_key is None:
        return False, None, "INVALID_PUBLICATION_DATE"

    try:
        pub_date = datetime.date(*sort_key)
    except ValueError:
        return False, None, "INVALID_PUBLICATION_DATE"

    canonical_iso = f"{pub_date.year:04d}-{pub_date.month:02d}-{pub_date.day:02d}"

    if reference_date is not None:
        ref = reference_date
    else:
        # Use Indian Standard Time (UTC+05:30) date when checking Indian government portals
        ref = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=5, minutes=30)
        ).date()

    age_days = (ref - pub_date).days
    if age_days < 0 and not allow_future_validated:
        return False, canonical_iso, "FUTURE_PUBLICATION_DATE"

    if age_days > max_age_days:
        return False, canonical_iso, "STALE_PUBLICATION_DATE"

    return True, canonical_iso, "VALID_PUBLICATION_DATE"


def select_latest_notice(notices: Sequence[DiscoveredNotice]) -> Optional[DiscoveredNotice]:
    """
    Selects the latest available notice from a list of discovered notices:
    - When the notice board provides explicit publication dates (either on the top
      item or across at least half of the discovered items), selects the notice
      with the newest explicit calendar date, breaking ties by source ordering.
    - When the notice board does not provide a date column (so the top item has no
      date and only isolated older titles mention dates), preserves source ordering
      and returns notices[0].
    - Never substitutes the current scrape timestamp for a missing publication date.
    """
    if not notices:
        return None

    dated_candidates: List[Tuple[Tuple[int, int, int], int, DiscoveredNotice]] = []
    for idx, notice in enumerate(notices):
        sort_key = parse_publication_date_sort_key(notice.publication_date)
        if sort_key is not None:
            dated_candidates.append((sort_key, -idx, notice))

    first_has_date = parse_publication_date_sort_key(notices[0].publication_date) is not None
    if dated_candidates and (first_has_date or len(dated_candidates) * 2 >= len(notices)):
        dated_candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return dated_candidates[0][2]

    return notices[0]


# ============================================================================
# Navigation, Boilerplate & Page Structure Validation (Section 4)
# ============================================================================

IGNORED_EXACT_TITLES: Set[str] = {
    "home",
    "about",
    "about us",
    "contact",
    "contact us",
    "skip to main content",
    "screen reader",
    "screen reader access",
    "sitemap",
    "site map",
    "feedback",
    "rti",
    "right to information",
    "disclaimer",
    "privacy policy",
    "terms and conditions",
    "terms of use",
    "copyright policy",
    "hyperlink policy",
    "help",
    "faq",
    "faqs",
    "photo gallery",
    "video gallery",
    "gallery",
    "tenders",
    "tender",
    "archive",
    "archives",
    "view all",
    "read more",
    "more",
    "more >>",
    "click here",
    "download",
    "pdf",
    "login",
    "sign in",
    "register",
    "citizen charter",
    "citizen's charter",
    "organisation structure",
    "organization structure",
    "the commission",
    "historical perspective",
    "constitutional provisions",
    "secretariat",
    "functions",
    "overview",
    "branch information",
    "process",
    "instructions to applicants",
    "recruitment rules",
    "direct recruitment",
    "government orders",
    "notices",
    "advertisements",
    "results",
    "public notice",
    "notifications",
    "what's new",
    "whats new",
    "latest news",
    "active examinations",
    "forthcoming examinations",
    "previous question papers",
    "cut-off marks",
    "answer keys",
    "revised syllabus and scheme",
    "demo files",
    "forms for certificates",
    "recruitment tests",
    "recruitment requisition",
    "calendar",
    "english",
    "hindi",
    "हिन्दी",
    "हिंदी",
    "मुख्य पृष्ठ",
}

IGNORED_URL_SUBSTRINGS: Tuple[str, ...] = (
    "javascript:",
    "mailto:",
    "tel:",
    "facebook.com",
    "twitter.com",
    "x.com/",
    "youtube.com",
    "instagram.com",
    "linkedin.com",
    "/about-us",
    "/contact",
    "/photo-gallery",
    "/photogallery",
    "/video-gallery",
    "/citizen-charter",
    "/citizens-charter",
    "/citizencharter",
    "/right-to-information",
    "/righttoinfo",
    "/organisation-structure",
    "/organisationstructure",
    "/branchinfo",
    "/overview",
    "/function",
    "/commission",
    "/process",
    "/applicantinstruction",
    "/home/notice",
    "/home/directrecruitment",
    "/home/governmentorders",
    "/home/manual",
    "/examinations/exam-calendar",
    "/examinations/active-exams",
    "/examinations/forthcoming-exams",
    "/examinations/previous-question-papers",
    "/examinations/cutoff-marks",
    "/examinations/answer-key",
    "/examinations/revised-syllabus-scheme",
    "/examinations/representation-question-papers",
    "/recruitment/recruitment-advertisement",
    "/recruitment/recruitment-performas",
    "/recruitment/recruitment-requisition",
    "/recruitment/recruitment-test",
    "/recruitment/time-frame-representation",
    "/sitemap",
    "/feedback",
    "/screen-reader",
)

NOTICE_SIGNAL_KEYWORDS: Tuple[str, ...] = (
    "notice",
    "notification",
    "advt",
    "advertisement",
    "recruitment",
    "vacancy",
    "vacancies",
    "exam",
    "examination",
    "result",
    "merit",
    "admit card",
    "admit-card",
    "call letter",
    "hall ticket",
    "answer key",
    "answer-key",
    "syllabus",
    "corrigendum",
    "amendment",
    "addendum",
    "errata",
    "cancellation",
    "cancelled",
    "postponed",
    "postponement",
    "extension",
    "extended",
    "revised",
    "schedule",
    "calendar",
    "time table",
    "timetable",
    "cut-off",
    "cut off",
    "cutoff",
    "marks",
    "interview",
    "shortlist",
    "shortlisting",
    "selection",
    "selected",
    "post of",
    "posts of",
    "constable",
    "inspector",
    "officer",
    "assistant",
    "clerk",
    "scientist",
    "professor",
    "teacher",
    "apply",
    "registration",
    "objection",
    "representation",
    "provisional",
    "final",
    "document verification",
    "verification",
    "scrutiny",
    "counselling",
    "counseling",
    "skill test",
    "typing test",
    "medical",
    "joining",
    "allocation",
    "debarred",
    "roll no",
    "roll number",
    "candidate",
    "applicant",
    "announcement",
    "press note",
    "press release",
    "circular",
    "memorandum",
    "important",
    "list of",
    "date of",
    "pet",
    "pst",
    "cbt",
    "omr",
    "भर्ती",
    "विज्ञापन",
    "परिणाम",
    "सूचना",
    "परीक्षा",
    "प्रवेश पत्र",
    "उत्तर कुंजी",
    "चयन",
    "पद",
    "आरक्षी",
    "उपनिरीक्षक",
    "अधिसूचना",
    "विज्ञप्ति",
    "रद्द",
    "स्थगित",
    "संशोधित",
    "अभ्यर्थी",
    "सत्यापन",
    "सूची",
)


def is_valid_notice_candidate(title: str, href: str, base_url: str) -> bool:
    """
    Conservative check to prevent navigation links, boilerplate, tenders, transfer
    orders, or template placeholders from being mistaken for official recruitment/exam
    notices while supporting official notice boards.
    """
    cleaned_title = clean_display_title(title)
    if not cleaned_title or len(cleaned_title) < 10:
        return False

    # Reject unrendered Angular/mustache template placeholders like {{'AboutUs_HM' | translate}}
    if "{{" in cleaned_title or "}}" in cleaned_title:
        return False

    lower_title = cleaned_title.casefold()
    if lower_title in IGNORED_EXACT_TITLES:
        return False

    if not href or href.strip().startswith("#"):
        return False

    lower_href = href.strip().lower()
    if any(ignored in lower_href for ignored in IGNORED_URL_SUBSTRINGS):
        return False

    resolved = normalize_url(href, base_url)
    if not resolved:
        return False

    lower_resolved = resolved.lower()
    if any(ignored in lower_resolved for ignored in IGNORED_URL_SUBSTRINGS):
        return False

    # Reject tenders, auctions, transfer orders, seniority lists, RTI, and non-job noise
    if is_irrelevant_notice_context(cleaned_title, url=resolved):
        return False

    parsed_resolved = urllib.parse.urlsplit(resolved)
    parsed_base = urllib.parse.urlsplit(base_url)

    # Do not accept bare root homepage URLs or top-level section landing paths as individual notices
    if parsed_resolved.path in (
        "",
        "/",
        "/home",
        "/Home/Index",
        "/index.html",
        "/Default.aspx",
        "/advertisement",
        "/result",
        "/publicNotice",
        "/notifications",
        "/news",
        "/advertisements",
        "/results",
    ) and not parsed_resolved.query:
        return False

    # Check if URL or title has strong notice signals
    is_pdf_or_doc = any(
        ext in lower_resolved
        for ext in (
            ".pdf",
            ".doc",
            ".docx",
            "open_pdf",
            "view_enclosure",
            "view_notice",
            "viewnotice",
            "notificationdocview",
            "candipostdetailsservlet",
            "/file/",
            "filesuploaded",
            "whats-new/",
            "/notice/",
            "/notices/",
            "/announcement/",
            "/circular/",
            "/upload",
            "/assets/document/",
        )
    )
    has_keyword = is_relevant_job_notice(cleaned_title, url=resolved) or any(
        kw in lower_title for kw in NOTICE_SIGNAL_KEYWORDS
    )

    if is_pdf_or_doc or (has_keyword and len(cleaned_title) >= 15):
        # Ensure external links are still official Indian government or exam portals
        host = parsed_resolved.netloc.lower()
        base_host = parsed_base.netloc.lower()
        if (
            host == base_host
            or host.endswith(".gov.in")
            or host.endswith(".nic.in")
            or "hryssc.com" in host
            or "upcons" in host
            or "hgupexam" in host
            or "applyssb.com" in host
            or "ukpsc.net.in" in host
            or "ukpscnet.in" in host
            or "mponline.gov.in" in host
        ):
            return True

    return False


ACCESS_DENIED_PATTERNS: Tuple[str, ...] = (
    "access denied",
    "403 forbidden",
    "error 403",
    "error 404",
    "404 not found",
    "attention required! | cloudflare",
    "just a moment...",
    "request blocked",
    "your ip has been blocked",
    "security challenge",
    "server error in '/' application",
    "runtime error",
)

NOTICE_BOARD_CONTEXT_MARKERS: Tuple[str, ...] = (
    "notice board",
    "notice-board",
    "noticeboard",
    "public notice",
    "latest news",
    "what's new",
    "whats new",
    "advertisement",
    "recruitment",
    "notifications",
    "announcements",
    "press note",
    "circulars",
    "latest updates",
    "सूचना पट्ट",
    "विज्ञापन",
    "भर्ती",
)

EMPTY_BOARD_TEXT_MARKERS: Tuple[str, ...] = (
    "no notice",
    "no notices",
    "no record found",
    "no records found",
    "no record available",
    "no records available",
    "no data available",
    "no data found",
    "no new notice",
    "no active notice",
    "no active advertisement",
    "no active recruitment",
    "no result found",
    "no results available",
    "no circular",
    "no announcement",
    "currently no",
    "presently no",
    "nothing to display",
    "0 records",
    "0 notices",
    "कोई सूचना उपलब्ध नहीं",
    "कोई रिकॉर्ड नहीं",
)


def is_access_denied_or_error_page(html_text: str) -> bool:
    """Detects WAF blocks, access-denied pages, or server error pages masquerading as HTTP 200."""
    if not html_text:
        return True
    lower = html_text.lower()
    # If the page has many links and looks like a full portal, don't false-positive on minor text
    if lower.count("<a ") > 15 and "access denied" not in lower[:1500]:
        return False
    return any(pat in lower for pat in ACCESS_DENIED_PATTERNS)


def is_unrendered_spa_shell(html_text: str) -> bool:
    """Detects client-side SPA shells (<app-root></app-root> or <div id='root'></div>) with no rendered DOM."""
    if not html_text:
        return True
    stripped = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", html_text, flags=re.IGNORECASE | re.DOTALL)
    visible_text = re.sub(r"<[^>]+>", " ", stripped)
    visible_text = re.sub(r"\s+", " ", visible_text).strip()
    lower_html = html_text.lower()
    if ("<app-root" in lower_html or '<div id="root"></div>' in lower_html) and len(visible_text) < 80:
        return True
    return False


def is_recognized_empty_notice_board(html_text: str) -> bool:
    """
    Determines whether an HTML page with 0 extracted notices is a genuinely
    recognized empty notice board rather than a broken parser or unexpected page.
    """
    if not html_text or is_access_denied_or_error_page(html_text) or is_unrendered_spa_shell(html_text):
        return False

    lower = html_text.lower()
    has_board_context = any(marker in lower for marker in NOTICE_BOARD_CONTEXT_MARKERS)
    if not has_board_context:
        return False

    # Case 1: Explicit empty-board message inside a notice board page
    if any(marker in lower for marker in EMPTY_BOARD_TEXT_MARKERS):
        return True

    # Case 2: Recognizable notice table header (e.g., Date / Subject / Title / Download) with 0 notice links
    if "<table" in lower and ("<th" in lower or "<thead" in lower):
        header_tokens = ("subject", "title", "description", "notice", "date", "download", "advt", "विवरण", "तिथि")
        matched_headers = sum(1 for tok in header_tokens if tok in lower)
        if matched_headers >= 2:
            return True

    return False


# ============================================================================
# HTTP Client with Timeouts, Bounded Retries, Backoff & Retry-After (Section 9)
# ============================================================================

class HttpClient:
    """
    Resilient HTTP fetcher with per-source error classification, Retry-After
    support, bounded exponential backoff with jitter, strict TLS verification
    with legacy server renegotiation support, CookieJar persistence, DNS-over-HTTPS
    fallback for flaky .gov.in/.nic.in authoritative nameservers, and host-level
    circuit breaking for unreachable endpoints.
    """

    def __init__(
        self,
        timeout: Tuple[int, int] = DEFAULT_TIMEOUT,
        max_retries: int = MAX_HTTP_RETRIES,
        sleep_fn: Callable[[float], None] = time.sleep,
        enable_doh_fallback: bool = True,
    ) -> None:
        self.connect_timeout, self.read_timeout = timeout
        self.max_retries = max_retries
        self.sleep_fn = sleep_fn
        self.enable_doh_fallback = enable_doh_fallback
        self._cookie_jar = http.cookiejar.CookieJar()
        self._doh_cache: Dict[str, List[str]] = {}
        self._dead_hosts: Dict[str, SourceFetchError] = {}

    @staticmethod
    def _create_ssl_context() -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        # Allow RFC 5746 legacy server renegotiation required by official portals
        # such as rsmssb.rajasthan.gov.in / recruitment.rajasthan.gov.in under OpenSSL 3.0+
        # while keeping strict certificate and hostname verification active.
        ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
        return ctx

    def _resolve_via_doh(self, host: str) -> List[str]:
        """
        Resolves a .gov.in or .nic.in hostname via Google/Cloudflare DNS-over-HTTPS
        when the local stub resolver returns SERVFAIL/gaierror (e.g. hppsc.hp.gov.in).
        Also checks 'www.<host>' if bare '<host>' returns no A records.
        """
        if not self.enable_doh_fallback or not host:
            return []
        lower_host = host.lower()
        if not (lower_host.endswith(".gov.in") or lower_host.endswith(".nic.in") or lower_host.endswith(".net.in")):
            return []
        if lower_host in self._doh_cache:
            return self._doh_cache[lower_host]

        candidates = [lower_host]
        if not lower_host.startswith("www."):
            candidates.append("www." + lower_host)

        ctx = self._create_ssl_context()
        for qname in candidates:
            for doh_url in (
                f"https://cloudflare-dns.com/dns-query?name={urllib.parse.quote(qname)}&type=A",
                f"https://dns.google/resolve?name={urllib.parse.quote(qname)}&type=A",
            ):
                try:
                    req = urllib.request.Request(
                        doh_url,
                        headers={"Accept": "application/dns-json", "User-Agent": USER_AGENT},
                    )
                    with urllib.request.urlopen(req, timeout=4, context=ctx) as resp:
                        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                        ips = [
                            str(ans.get("data")).strip()
                            for ans in payload.get("Answer", [])
                            if isinstance(ans, dict) and ans.get("type") == 1 and ans.get("data")
                        ]
                        if ips:
                            self._doh_cache[lower_host] = ips
                            return ips
                except Exception:
                    continue

        self._doh_cache[lower_host] = []
        return []

    def request(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
        data: Optional[bytes] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Dict[str, str], bytes, str]:
        parsed_target = urllib.parse.urlsplit(url)
        target_host = (parsed_target.hostname or "").lower()
        dead_key = f"{parsed_target.scheme.lower()}://{target_host}" if target_host else ""
        if dead_key and dead_key in self._dead_hosts:
            cached_err = self._dead_hosts[dead_key]
            raise SourceFetchError(cached_err.category, f"{cached_err.detail} (cached unreachable host {target_host})")

        req_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
            "Connection": "keep-alive",
        }
        if headers:
            req_headers.update(headers)
        if extra_headers:
            req_headers.update(extra_headers)

        last_exc: Optional[SourceFetchError] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                status, resp_headers, body, final_url = self._perform_single_request(
                    url=url,
                    method=method,
                    headers=req_headers,
                    data=data,
                )

                if status == 429 or status in (500, 502, 503, 504):
                    category = "HTTP_429_RATE_LIMITED" if status == 429 else f"HTTP_{status}_SERVER_ERROR"
                    last_exc = SourceFetchError(
                        category, f"HTTP {status} from {url}"
                    )
                    if attempt < self.max_retries:
                        delay = self._compute_backoff(attempt, resp_headers.get("Retry-After"))
                        self.sleep_fn(delay)
                        continue
                    raise last_exc

                if status == 403:
                    raise SourceFetchError("HTTP_403_FORBIDDEN", f"HTTP 403 Forbidden for {url}")
                if status == 404:
                    raise SourceFetchError("HTTP_404_NOT_FOUND", f"HTTP 404 Not Found for {url}")
                if status >= 400:
                    raise SourceFetchError(f"HTTP_{status}_ERROR", f"HTTP {status} for {url}")

                if not body or not body.strip():
                    raise SourceFetchError("EMPTY_RESPONSE", f"Empty response body from {url}")

                return status, resp_headers, body, final_url

            except SourceFetchError as exc:
                last_exc = exc
                # Do not retry permanent HTTP client errors (403, 404, 400) or TLS errors
                if exc.category in (
                    "HTTP_403_FORBIDDEN",
                    "HTTP_404_NOT_FOUND",
                    "TLS_SSL_ERROR",
                ) or (exc.category.startswith("HTTP_4") and exc.category != "HTTP_429_RATE_LIMITED"):
                    raise
                if attempt < self.max_retries:
                    delay = self._compute_backoff(attempt, None)
                    self.sleep_fn(delay)
                else:
                    if dead_key and exc.category in ("CONNECTION_TIMEOUT", "CONNECTION_RESET", "DNS_ERROR"):
                        self._dead_hosts[dead_key] = exc
                    raise

        assert last_exc is not None
        raise last_exc

    def _compute_backoff(self, attempt: int, retry_after_header: Optional[str]) -> float:
        if retry_after_header:
            try:
                val = float(retry_after_header.strip())
                if 0 < val <= 60:
                    return val
            except ValueError:
                pass
        base = min(15.0, (2 ** (attempt - 1)) * 1.0)
        jitter = random.uniform(0.1, 0.5)
        return base + jitter

    def _perform_single_request(
        self,
        url: str,
        method: str,
        headers: Dict[str, str],
        data: Optional[bytes],
    ) -> Tuple[int, Dict[str, str], bytes, str]:
        total_timeout = max(self.connect_timeout, self.read_timeout)
        parsed = urllib.parse.urlsplit(url)
        target_host = (parsed.hostname or "").lower()
        ctx = self._create_ssl_context()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookie_jar),
            urllib.request.HTTPSHandler(context=ctx),
        )

        def _do_open() -> Tuple[int, Dict[str, str], bytes, str]:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with opener.open(req, timeout=total_timeout) as resp:
                status = getattr(resp, "status", resp.getcode())
                resp_headers = {k: v for k, v in resp.headers.items()}
                body = resp.read()
                final_url = resp.geturl()
                return status, resp_headers, body, final_url

        try:
            try:
                return _do_open()
            except (socket.gaierror, urllib.error.URLError) as first_exc:
                is_dns_err = isinstance(first_exc, socket.gaierror) or (
                    isinstance(first_exc, urllib.error.URLError)
                    and isinstance(first_exc.reason, socket.gaierror)
                )
                if not is_dns_err or not target_host:
                    raise
                doh_ips = self._resolve_via_doh(target_host)
                if not doh_ips:
                    raise
                bare_host = target_host[4:] if target_host.startswith("www.") else target_host
                matching_hosts = {target_host, bare_host, f"www.{bare_host}"}
                orig_getaddrinfo = socket.getaddrinfo
                def _patched_gai(h: Any, p: Any, *args: Any, **kwargs: Any) -> Any:
                    if str(h).lower() in matching_hosts:
                        port_num = int(p) if p else (443 if parsed.scheme == "https" else 80)
                        return [
                            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port_num))
                            for ip in doh_ips
                        ]
                    return orig_getaddrinfo(h, p, *args, **kwargs)
                socket.getaddrinfo = _patched_gai
                try:
                    return _do_open()
                finally:
                    socket.getaddrinfo = orig_getaddrinfo
        except urllib.error.HTTPError as exc:
            resp_headers = {k: v for k, v in exc.headers.items()} if exc.headers else {}
            try:
                err_body = exc.read()
            except Exception:
                err_body = b""
            return exc.code, resp_headers, err_body, url
        except ssl.SSLError as exc:
            raise SourceFetchError("TLS_SSL_ERROR", f"TLS/SSL verification failed for {url}: {exc}") from exc
        except ConnectionResetError as exc:
            raise SourceFetchError("CONNECTION_RESET", f"Connection reset by peer for {url}: {exc}") from exc
        except socket.gaierror as exc:
            raise SourceFetchError("DNS_ERROR", f"DNS resolution failed for {url}: {exc}") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise SourceFetchError("CONNECTION_TIMEOUT", f"Connection timed out for {url}: {exc}") from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, ssl.SSLError):
                raise SourceFetchError("TLS_SSL_ERROR", f"TLS/SSL error for {url}: {reason}") from exc
            if isinstance(reason, ConnectionResetError) or "connection reset" in str(reason).lower() or "errno 104" in str(reason).lower():
                raise SourceFetchError("CONNECTION_RESET", f"Connection reset by peer for {url}: {reason}") from exc
            if isinstance(reason, socket.gaierror):
                raise SourceFetchError("DNS_ERROR", f"DNS resolution failed for {url}: {reason}") from exc
            if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason).lower():
                raise SourceFetchError("CONNECTION_TIMEOUT", f"Timed out connecting to {url}: {reason}") from exc
            raise SourceFetchError("NETWORK_ERROR", f"Network error for {url}: {reason}") from exc
        except Exception as exc:
            if "connection reset" in str(exc).lower() or "errno 104" in str(exc).lower():
                raise SourceFetchError("CONNECTION_RESET", f"Connection reset by peer for {url}: {exc}") from exc
            raise SourceFetchError("NETWORK_ERROR", f"Unexpected HTTP error for {url}: {exc}") from exc

    def fetch_text(
        self, url: str, extra_headers: Optional[Dict[str, str]] = None
    ) -> Tuple[str, str]:
        _, headers, body, final_url = self.request(url, extra_headers=extra_headers)
        content_type = headers.get("Content-Type", "")
        encoding = "utf-8"
        m = re.search(r"charset=([A-Za-z0-9_-]+)", content_type, re.IGNORECASE)
        if m:
            encoding = m.group(1)
        try:
            return body.decode(encoding, errors="replace"), final_url
        except LookupError:
            return body.decode("utf-8", errors="replace"), final_url

    def fetch_json(
        self,
        url: str,
        extra_headers: Optional[Dict[str, str]] = None,
        method: str = "GET",
        data: Optional[bytes] = None,
    ) -> Any:
        hdrs = {"Accept": "application/json, text/plain, */*"}
        if extra_headers:
            hdrs.update(extra_headers)
        _, resp_headers, body, _ = self.request(url, method=method, data=data, extra_headers=hdrs)
        content_type = resp_headers.get("Content-Type", "")
        encoding = "utf-8"
        m = re.search(r"charset=([A-Za-z0-9_-]+)", content_type, re.IGNORECASE)
        if m:
            encoding = m.group(1)
        text = body.decode(encoding, errors="replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise SourceFetchError("INVALID_JSON", f"Invalid JSON response from {url}: {exc}") from exc


# ============================================================================
# HTML Parsing Engine (BeautifulSoup4 with stdlib HTMLParser fallback)
# ============================================================================

@dataclasses.dataclass
class ExtractedAnchor:
    href: str
    text: str
    title_attr: str
    row_text: str


SERIAL_CELL_RE = re.compile(
    r"^(?:(?:s|sr|sl)\.?\s*no\.?\s*[:.-]?\s*)?\(?#?\d{1,4}[.)]?$",
    re.IGNORECASE,
)


class _StdlibNoticeHTMLParser(html.parser.HTMLParser):
    """Fallback HTML parser extracting <a> elements and enclosing <tr> context."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: List[ExtractedAnchor] = []
        self._in_anchor = False
        self._current_href = ""
        self._current_title = ""
        self._anchor_text_parts: List[str] = []
        self._in_tr = 0
        self._tr_cells: List[str] = []
        self._in_td = 0
        self._td_text_parts: List[str] = []
        self._tr_anchors: List[Tuple[str, str, str]] = []
        self._in_ignored_tag = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        if t in ("script", "style", "noscript", "nav"):
            self._in_ignored_tag += 1
            return
        if self._in_ignored_tag > 0:
            return

        if t == "tr":
            self._in_tr += 1
            if self._in_tr == 1:
                self._tr_cells = []
                self._tr_anchors = []

        if t in ("td", "th") and self._in_tr > 0:
            self._in_td += 1
            if self._in_td == 1:
                self._td_text_parts = []

        if t == "a":
            attr_dict = {k.lower(): (v or "") for k, v in attrs}
            href = attr_dict.get("href", "").strip()
            if href:
                self._in_anchor = True
                self._current_href = href
                self._current_title = attr_dict.get("title", "").strip()
                self._anchor_text_parts = []

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ("script", "style", "noscript", "nav"):
            if self._in_ignored_tag > 0:
                self._in_ignored_tag -= 1
            return
        if self._in_ignored_tag > 0:
            return

        if t == "a" and self._in_anchor:
            self._in_anchor = False
            anchor_text = " ".join(self._anchor_text_parts).strip()
            if self._in_tr > 0:
                self._tr_anchors.append(
                    (self._current_href, anchor_text, self._current_title)
                )
            else:
                self.anchors.append(
                    ExtractedAnchor(
                        href=self._current_href,
                        text=anchor_text,
                        title_attr=self._current_title,
                        row_text=anchor_text,
                    )
                )

        if t in ("td", "th") and self._in_td > 0:
            if self._in_td == 1:
                cell_text = re.sub(r"\s+", " ", " ".join(self._td_text_parts)).strip()
                if cell_text:
                    self._tr_cells.append(cell_text)
                self._td_text_parts = []
            self._in_td -= 1

        if t == "tr" and self._in_tr > 0:
            if self._in_tr == 1:
                cells = list(self._tr_cells)
                if len(cells) >= 2 and SERIAL_CELL_RE.match(cells[0]):
                    cells = cells[1:]
                row_text = re.sub(r"\s+", " ", " ".join(cells)).strip()
                for href, a_text, a_title in self._tr_anchors:
                    self.anchors.append(
                        ExtractedAnchor(
                            href=href,
                            text=a_text,
                            title_attr=a_title,
                            row_text=row_text or a_text,
                        )
                    )
                self._tr_cells = []
                self._tr_anchors = []
            self._in_tr -= 1

    def handle_data(self, data: str) -> None:
        if self._in_ignored_tag > 0:
            return
        if self._in_anchor:
            self._anchor_text_parts.append(data)
        if self._in_td > 0:
            self._td_text_parts.append(data)
        elif self._in_tr > 0:
            self._tr_cells.append(data)


def extract_anchors_with_context(html_text: str) -> List[ExtractedAnchor]:
    """
    Extracts anchors and their enclosing table-row (<tr>) context while ignoring <nav>.
    Strips leading serial-number <td> cells so table row shifts never change notice titles.
    Pre-normalizes unquoted href=... attributes (e.g. UPPSC Open_PDF_DB.aspx).
    """
    if not html_text or not html_text.strip():
        raise SourceFetchError("EMPTY_RESPONSE", "Empty HTML document")

    # Normalize unquoted href=Open_PDF... attributes found in legacy ASP.NET pages (e.g. UPPSC)
    patched_html = re.sub(
        r"\bhref=([^\s\"\'>]+)",
        r'href="\1"',
        html_text,
        flags=re.IGNORECASE,
    )

    if _BeautifulSoup is not None:
        soup = _BeautifulSoup(patched_html, "html.parser")
        for tag in soup(["script", "style", "noscript", "nav"]):
            tag.decompose()
        results: List[ExtractedAnchor] = []
        for a_tag in soup.find_all("a", href=True):
            href = str(a_tag.get("href", "")).strip()
            if not href:
                continue
            text = a_tag.get_text(" ", strip=True)
            title_attr = str(a_tag.get("title", "")).strip()
            parent_tr = a_tag.find_parent("tr")
            if parent_tr is not None:
                cells = [
                    c.get_text(" ", strip=True)
                    for c in parent_tr.find_all(["td", "th"], recursive=False)
                ]
                cells = [c for c in cells if c]
                if len(cells) >= 2 and SERIAL_CELL_RE.match(cells[0]):
                    cells = cells[1:]
                row_text = " ".join(cells).strip() if cells else parent_tr.get_text(" ", strip=True)
            else:
                row_text = text
            results.append(
                ExtractedAnchor(
                    href=href,
                    text=text,
                    title_attr=title_attr,
                    row_text=row_text,
                )
            )
        return results

    parser = _StdlibNoticeHTMLParser()
    try:
        parser.feed(patched_html)
        parser.close()
    except Exception as exc:
        raise SourceFetchError("HTML_PARSE_ERROR", f"Failed to parse HTML: {exc}") from exc
    return parser.anchors


def _is_direct_document_url(url: str) -> bool:
    lower = url.lower()
    return any(
        tok in lower
        for tok in (".pdf", ".doc", ".docx", "open_pdf", "view_enclosure", "/api/attachment/", "/assets/document/")
    )


def _extract_document_token(normalized_url: str) -> Optional[str]:
    """
    Returns a canonical document identifier (filename or query document token)
    when the URL points to a specific document file, else None.
    """
    if not normalized_url:
        return None
    parsed = urllib.parse.urlsplit(normalized_url)
    path_lower = parsed.path.lower()
    if any(path_lower.endswith(ext) for ext in (".pdf", ".doc", ".docx")):
        basename = os.path.basename(path_lower)
        if basename:
            return f"file:{basename}"
    if "open_pdf" in path_lower and parsed.query:
        return f"query:{parsed.query}"
    return None


def deduplicate_source_notices(
    notices: Sequence[DiscoveredNotice],
    source: Optional[SourceConfig] = None,
) -> List[DiscoveredNotice]:
    """
    Deduplicates notices discovered across multiple pages or URLs for the same source
    while preserving distinct notices that have different official documents or
    meaningful titles.
    """
    deduped: List[DiscoveredNotice] = []
    by_fp: Dict[str, int] = {}
    by_source_id_only: Dict[Tuple[str, str], int] = {}
    by_source_id_token: Dict[Tuple[str, str], int] = {}
    by_canonical_url: Dict[Tuple[str, str], List[int]] = {}
    by_norm_title: Dict[str, List[int]] = {}
    by_canon_title: Dict[str, List[int]] = {}

    def _merge_metadata(existing: DiscoveredNotice, incoming: DiscoveredNotice, idx: int) -> None:
        if incoming.publication_date and not existing.publication_date:
            existing.publication_date = incoming.publication_date
        if incoming.application_date and not existing.application_date:
            existing.application_date = incoming.application_date
        if incoming.exam_date and not existing.exam_date:
            existing.exam_date = incoming.exam_date
        if incoming.source_identifier and not existing.source_identifier:
            existing.source_identifier = incoming.source_identifier
        if incoming.raw_context and not existing.raw_context:
            existing.raw_context = incoming.raw_context
        if _is_direct_document_url(incoming.normalized_url) and not _is_direct_document_url(existing.normalized_url):
            existing.url = incoming.url
            existing.normalized_url = incoming.normalized_url
            existing.fingerprint = compute_fingerprint(existing.normalized_url, existing.normalized_title)
            by_fp[existing.fingerprint] = idx

    for item in notices:
        if item.fingerprint in by_fp:
            idx = by_fp[item.fingerprint]
            _merge_metadata(deduped[idx], item, idx)
            continue

        # 1. Same source_id + same non-empty source_identifier
        if item.source_identifier:
            sid_only_key = (item.source_id, item.source_identifier.strip())
            if sid_only_key in by_source_id_only:
                idx = by_source_id_only[sid_only_key]
                _merge_metadata(deduped[idx], item, idx)
                continue

        # 2. Same source_identifier + same normalized_title
        if item.source_identifier and item.normalized_title:
            sid_key = (item.source_identifier.strip(), item.normalized_title)
            if sid_key in by_source_id_token:
                idx = by_source_id_token[sid_key]
                _merge_metadata(deduped[idx], item, idx)
                continue

        # 3. Same source_id + identical canonical direct document URL
        canon_url = canonical_url_key(item.normalized_url)
        if _is_direct_document_url(item.normalized_url) and canon_url:
            url_key = (item.source_id, canon_url)
            if url_key in by_canonical_url:
                merged_url = False
                item_ct = canonical_title_key(item.title)
                for idx in by_canonical_url[url_key]:
                    existing = deduped[idx]
                    ex_ct = canonical_title_key(existing.title)
                    if not item_ct or not ex_ct or item_ct == ex_ct or item_ct in ex_ct or ex_ct in item_ct:
                        _merge_metadata(existing, item, idx)
                        merged_url = True
                        break
                if merged_url:
                    continue

        # 4. Identical normalized_title or canonical_title_key across multiple pages/mirrors of this source
        merged_into_existing = False
        candidate_indices: List[int] = []
        if len(item.normalized_title) >= 12 and item.normalized_title in by_norm_title:
            candidate_indices.extend(by_norm_title[item.normalized_title])
        item_canon_title = canonical_title_key(item.title)
        if len(item_canon_title) >= 12 and item_canon_title in by_canon_title:
            for c_idx in by_canon_title[item_canon_title]:
                if c_idx not in candidate_indices:
                    candidate_indices.append(c_idx)

        if candidate_indices:
            item_doc_tok = _extract_document_token(item.normalized_url)
            for idx in candidate_indices:
                existing = deduped[idx]
                existing_doc_tok = _extract_document_token(existing.normalized_url)
                if (
                    item_doc_tok is not None
                    and existing_doc_tok is not None
                    and item_doc_tok != existing_doc_tok
                ):
                    continue
                _merge_metadata(existing, item, idx)
                merged_into_existing = True
                break

        if merged_into_existing:
            continue

        new_idx = len(deduped)
        deduped.append(item)
        by_fp[item.fingerprint] = new_idx
        if item.source_identifier:
            by_source_id_only[(item.source_id, item.source_identifier.strip())] = new_idx
        if item.source_identifier and item.normalized_title:
            by_source_id_token[(item.source_identifier.strip(), item.normalized_title)] = new_idx
        if canon_url:
            by_canonical_url.setdefault((item.source_id, canon_url), []).append(new_idx)
        by_norm_title.setdefault(item.normalized_title, []).append(new_idx)
        if item_canon_title:
            by_canon_title.setdefault(item_canon_title, []).append(new_idx)

    return deduped


def extract_notices_from_html(
    source: SourceConfig,
    html_text: str,
    page_url: str,
    prefer_row_text: bool = False,
) -> List[DiscoveredNotice]:
    """
    Extracts validated official notices from an HTML page with deduplication.
    """
    anchors = extract_anchors_with_context(html_text)
    notices: List[DiscoveredNotice] = []
    seen_fp: Set[str] = set()

    for item in anchors:
        # Decide the best title: if anchor text is generic ("Download", "Click here", "View")
        # or if prefer_row_text is True, use enclosing row text or title attribute.
        raw_title = item.text
        if item.title_attr and len(item.title_attr) > len(raw_title):
            raw_title = f"{item.title_attr} - {raw_title}" if raw_title and raw_title not in item.title_attr else item.title_attr

        generic_link_labels = {
            "download", "view", "click here", "pdf", "link", "open", "read more", "notice", "file"
        }
        if (prefer_row_text or raw_title.strip().casefold() in generic_link_labels or len(raw_title.strip()) < 12) and item.row_text:
            raw_title = item.row_text

        cleaned_title = clean_display_title(raw_title)
        if not is_valid_notice_candidate(cleaned_title, item.href, page_url):
            continue

        resolved_url = normalize_url(item.href, page_url)
        if not resolved_url:
            continue

        pub_date = extract_explicit_date(item.row_text) or extract_explicit_date(cleaned_title)
        app_date = extract_application_date(item.row_text) or extract_application_date(cleaned_title)
        ex_date = extract_exam_date(item.row_text) or extract_exam_date(cleaned_title)
        notice = DiscoveredNotice(
            source_id=source.source_id,
            organization=source.organization,
            title=cleaned_title,
            url=resolved_url,
            publication_date=pub_date,
            application_date=app_date,
            exam_date=ex_date,
            raw_context=item.row_text or cleaned_title,
        )
        if notice.fingerprint not in seen_fp:
            seen_fp.add(notice.fingerprint)
            notices.append(notice)

    return deduplicate_source_notices(notices, source)


def validate_and_extract_html_notices(
    source: SourceConfig,
    html_text: str,
    page_url: str,
    prefer_row_text: bool = False,
) -> Tuple[List[DiscoveredNotice], bool]:
    """
    Validates an HTML response and extracts notices.
    Returns (notices, board_recognized).
    Raises SourceFetchError('ACCESS_DENIED_PAGE', ...) if WAF/error block page is detected.
    """
    if is_access_denied_or_error_page(html_text):
        raise SourceFetchError(
            "ACCESS_DENIED_PAGE",
            f"Access-denied or error page returned from {page_url}",
        )
    notices = extract_notices_from_html(
        source, html_text, page_url, prefer_row_text=prefer_row_text
    )
    if notices:
        return notices, True
    if is_recognized_empty_notice_board(html_text):
        return [], True
    return [], False


# ============================================================================
# Per-Website Adapters for All 12 Active Official Organizations (Section 3)
# ============================================================================

def adapter_ssc(source: SourceConfig, client: HttpClient) -> List[DiscoveredNotice]:
    """
    1. Staff Selection Commission (SSC) — https://ssc.gov.in/
    Queries the official SSC JSON API (/api/general-website/portal/notice-boards)
    verified against live ssc.gov.in, with fallback to HTML parsing.
    """
    notices: List[DiscoveredNotice] = []
    seen_fp: Set[str] = set()
    api_error: Optional[SourceFetchError] = None

    api_url = source.notice_urls[0]
    try:
        payload = client.fetch_json(
            api_url,
            extra_headers={"Referer": "https://ssc.gov.in/"},
        )
        if not isinstance(payload, dict):
            raise SourceFetchError("INVALID_API_RESPONSE", "SSC API payload is not a JSON object")

        status_code = str(payload.get("statusCode", "200")).strip()
        records = payload.get("data")
        if status_code not in ("200", "OK", "True", "true") or not isinstance(records, list):
            raise SourceFetchError(
                "INVALID_API_RESPONSE",
                f"SSC API returned unexpected schema or statusCode={status_code!r}",
            )

        if len(records) == 0:
            # Valid, recognized empty notice board response from official SSC JSON API
            return []

        for item in records:
            if not isinstance(item, dict):
                continue
            headline = clean_display_title(str(item.get("headline") or ""))
            if not headline:
                continue
            created_at = str(item.get("createdAt") or "")
            pub_date = extract_explicit_date(created_at) or extract_explicit_date(headline)
            source_id_val = str(item.get("id") or "") or None

            # Determine official PDF link from attachments or redirectUrl
            pdf_url = ""
            attachments = item.get("attachments")
            if isinstance(attachments, list) and attachments:
                first_att = attachments[0]
                if isinstance(first_att, dict) and first_att.get("path"):
                    att_path = str(first_att["path"]).replace("\\", "/").lstrip("/")
                    pdf_url = f"https://ssc.gov.in/api/attachment/{att_path}"
            if not pdf_url and item.get("redirectUrl"):
                pdf_url = normalize_url(str(item["redirectUrl"]), source.homepage_url)
            if not pdf_url:
                pdf_url = source.homepage_url

            notice = DiscoveredNotice(
                source_id=source.source_id,
                organization=source.organization,
                title=headline,
                url=pdf_url,
                publication_date=pub_date,
                source_identifier=source_id_val,
            )
            if notice.fingerprint not in seen_fp:
                seen_fp.add(notice.fingerprint)
                notices.append(notice)

        if notices:
            return deduplicate_source_notices(notices, source)
        raise SourceFetchError("PARSING_ERROR", "SSC API data array contained no parseable notice items")
    except SourceFetchError as exc:
        api_error = exc

    # Fallback to HTML if API request failed
    if len(source.notice_urls) > 1:
        try:
            html_text, final_url = client.fetch_text(source.homepage_url)
            found, recognized = validate_and_extract_html_notices(source, html_text, final_url)
            if found or recognized:
                return found
        except SourceFetchError:
            pass

    if api_error is not None:
        raise api_error
    raise SourceFetchError("UNRECOGNIZED_STRUCTURE", "No notices found from SSC API or homepage HTML")


def adapter_upsc(source: SourceConfig, client: HttpClient) -> List[DiscoveredNotice]:
    """
    2. Union Public Service Commission (UPSC) — https://www.upsc.gov.in/
    Inspects UPSC What's New, Recruitment Advertisements, Active Exams, and homepage.
    Prioritizes chronological /whats-new/ feed items ahead of static header PDFs.
    """
    notices = _multi_page_html_adapter(source, client, prefer_row_text=False)
    whats_new_items = [n for n in notices if "/whats-new/" in n.normalized_url.lower()]
    other_items = [n for n in notices if "/whats-new/" not in n.normalized_url.lower()]
    if whats_new_items:
        return whats_new_items + other_items
    return notices


def adapter_uppsc(source: SourceConfig, client: HttpClient) -> List[DiscoveredNotice]:
    """
    3. Uttar Pradesh Public Service Commission (UPPSC) — https://uppsc.up.nic.in/
    Parses UPPSC Open_PDF_DB.aspx and Open_PDF.aspx notice board items,
    preferring full title attributes over truncated link text.
    """
    notices: List[DiscoveredNotice] = []
    seen_fp: Set[str] = set()
    primary_error: Optional[SourceFetchError] = None
    last_error: Optional[SourceFetchError] = None
    any_page_fetched = False
    any_board_recognized = False

    for idx, url in enumerate(source.notice_urls):
        try:
            html_text, final_url = client.fetch_text(url)
            any_page_fetched = True
            if is_access_denied_or_error_page(html_text):
                err = SourceFetchError("ACCESS_DENIED_PAGE", f"Access denied page from {url}")
                if idx == 0 or primary_error is None:
                    primary_error = err
                last_error = err
                continue
            anchors = extract_anchors_with_context(html_text)
            page_notices: List[DiscoveredNotice] = []
            for item in anchors:
                if "open_pdf" not in item.href.lower() and not is_valid_notice_candidate(item.text, item.href, final_url):
                    continue
                # UPPSC puts the complete, non-truncated title in the title attribute
                full_title = item.title_attr if len(item.title_attr) >= len(item.text) else item.text
                if not full_title or len(full_title.strip()) < 8:
                    full_title = item.row_text
                cleaned_title = clean_display_title(full_title)
                if not cleaned_title or len(cleaned_title) < 8:
                    continue
                resolved_url = normalize_url(item.href, final_url)
                if not resolved_url:
                    continue
                pub_date = extract_explicit_date(item.row_text) or extract_explicit_date(cleaned_title)
                app_date = extract_application_date(item.row_text) or extract_application_date(cleaned_title)
                ex_date = extract_exam_date(item.row_text) or extract_exam_date(cleaned_title)
                notice = DiscoveredNotice(
                    source_id=source.source_id,
                    organization=source.organization,
                    title=cleaned_title,
                    url=resolved_url,
                    publication_date=pub_date,
                    application_date=app_date,
                    exam_date=ex_date,
                    raw_context=item.row_text or cleaned_title,
                )
                page_notices.append(notice)
                if notice.fingerprint not in seen_fp:
                    seen_fp.add(notice.fingerprint)
                    notices.append(notice)
            if page_notices or is_recognized_empty_notice_board(html_text):
                any_board_recognized = True
        except SourceFetchError as exc:
            if idx == 0 or (primary_error is None and exc.category != "HTTP_404_NOT_FOUND"):
                primary_error = exc
            last_error = exc

    if notices:
        return deduplicate_source_notices(notices, source)
    if any_board_recognized:
        return []
    chosen_error = primary_error or last_error
    if chosen_error and not any_page_fetched:
        raise chosen_error
    if chosen_error and chosen_error.category == "ACCESS_DENIED_PAGE":
        raise chosen_error
    raise SourceFetchError("UNRECOGNIZED_STRUCTURE", "No notices extracted and UPPSC notice board structure was not recognized")


def adapter_hssc(source: SourceConfig, client: HttpClient) -> List[DiscoveredNotice]:
    """
    4. Haryana Staff Selection Commission (HSSC) — https://www.hssc.gov.in/
    Parses HSSC Public Notices, Advertisements, Results tables (where links live
    in separate <td> columns with PDF icons), and header notifications.
    """
    return _multi_page_html_adapter(source, client, prefer_row_text=True)


def adapter_upprpb(source: SourceConfig, client: HttpClient) -> List[DiscoveredNotice]:
    """
    8. Uttar Pradesh Police Recruitment and Promotion Board (UPPRPB) — https://uppbpb.gov.in/
    Parses /Home/Notice, /Home/DirectRecruitment, and homepage notices.
    """
    return _multi_page_html_adapter(source, client, prefer_row_text=False)


def _multi_page_html_adapter(
    source: SourceConfig,
    client: HttpClient,
    prefer_row_text: bool = False,
) -> List[DiscoveredNotice]:
    """
    Shared multi-URL official HTML notice board parser for portals that expose
    tabular or list-based notice boards across one or more official subpages.
    Checks all configured notice URLs consistently (without arbitrary early breaks)
    and distinguishes valid empty notice boards from unrecognized/broken pages.
    """
    notices: List[DiscoveredNotice] = []
    seen_fp: Set[str] = set()
    primary_error: Optional[SourceFetchError] = None
    last_error: Optional[SourceFetchError] = None
    access_error: Optional[SourceFetchError] = None
    any_page_fetched = False
    any_board_recognized = False

    for idx, url in enumerate(source.notice_urls):
        try:
            html_text, final_url = client.fetch_text(
                url, extra_headers={"Referer": source.homepage_url}
            )
            any_page_fetched = True
            found, recognized = validate_and_extract_html_notices(
                source, html_text, final_url, prefer_row_text=prefer_row_text
            )
            if recognized:
                any_board_recognized = True
            for n in found:
                if n.fingerprint not in seen_fp:
                    seen_fp.add(n.fingerprint)
                    notices.append(n)
        except SourceFetchError as exc:
            if exc.category == "ACCESS_DENIED_PAGE":
                access_error = exc
            if idx == 0 or (primary_error is None and exc.category != "HTTP_404_NOT_FOUND"):
                primary_error = exc
            last_error = exc

    if notices:
        return deduplicate_source_notices(notices, source)

    if any_board_recognized:
        return []

    if access_error is not None:
        raise access_error

    chosen_error = primary_error or last_error
    if chosen_error and not any_page_fetched:
        raise chosen_error

    raise SourceFetchError(
        "UNRECOGNIZED_STRUCTURE",
        f"Fetched {source.organization} but found 0 valid notice items and could not recognize notice-board structure.",
    )


def _extract_rajasthan_portal_notices(
    source: SourceConfig, html_text: str, page_url: str
) -> Tuple[List[DiscoveredNotice], bool]:
    """
    Extracts official notices and ongoing recruitment openings from the Government of
    Rajasthan State Recruitment Portal (https://recruitment.rajasthan.gov.in/):
    1. <div class="notice-item"><p>Title <a href="notificationdocview?ID=...">View Document</a></p></div>
    2. <div class="job-card">...<h6 onclick="postDetailsClick(4686)">POST NAME</h6>...<small>28-Oct-2026</small></div>
    """
    notices: List[DiscoveredNotice] = []
    seen_fp: Set[str] = set()
    recognized = False

    # 1. Extract <div class="notice-item"> blocks with notificationdocview?ID=... links
    for block_match in re.finditer(
        r'<div[^>]+class=["\'][^"\']*notice-item[^"\']*["\'][^>]*>(.*?)</div>',
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        recognized = True
        block_html = block_match.group(1)
        href_match = re.search(
            r'href=["\']?\s*(notificationdocview\?ID=[^\s"\'<>]+)',
            block_html,
            flags=re.IGNORECASE,
        )
        if not href_match:
            continue
        href = href_match.group(1).strip()
        raw_text = re.sub(r"<a[^>]*>.*?</a>", " ", block_html, flags=re.IGNORECASE | re.DOTALL)
        raw_text = re.sub(r"<[^>]+>", " ", raw_text)
        title = clean_display_title(raw_text)
        if not title or len(title) < 8:
            continue
        resolved_url = normalize_url(href, page_url)
        pub_date = extract_explicit_date(title)
        notice = DiscoveredNotice(
            source_id=source.source_id,
            organization=source.organization,
            title=title,
            url=resolved_url,
            publication_date=pub_date,
        )
        if notice.fingerprint not in seen_fp:
            seen_fp.add(notice.fingerprint)
            notices.append(notice)

    # 2. Extract <div class="job-card"> ongoing recruitment blocks
    for card_match in re.finditer(
        r'<div[^>]+class=["\'][^"\']*job-card[^"\']*["\'][^>]*>(.*?)</li>',
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        recognized = True
        card_html = card_match.group(1)
        id_match = re.search(r"postDetailsClick\(\s*['\"]?(\d+)['\"]?\s*\)", card_html)
        h6_match = re.search(r"<h6[^>]*>(.*?)</h6>", card_html, flags=re.IGNORECASE | re.DOTALL)
        if not h6_match:
            continue
        post_title = clean_display_title(h6_match.group(1))
        if not post_title:
            continue
        badge_match = re.search(
            r'<span[^>]+class=["\'][^"\']*badge[^"\']*["\'][^>]*>(.*?)</span>',
            card_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        dept_badge = clean_display_title(badge_match.group(1)) if badge_match else ""
        date_match = re.search(r"<small[^>]*>(.*?)</small>", card_html, flags=re.IGNORECASE | re.DOTALL)
        closing_date = extract_explicit_date(date_match.group(1)) if date_match else None
        rect_id = id_match.group(1) if id_match else ""
        full_title = f"Ongoing Recruitment: {post_title}"
        if dept_badge and dept_badge not in full_title:
            full_title = f"{full_title} {dept_badge}"
        if closing_date:
            full_title = f"{full_title} (Last Date: {closing_date})"
        notice_url = (
            f"https://recruitment.rajasthan.gov.in/candipostdetailsservlet?rect_id={rect_id}"
            if rect_id
            else normalize_url(page_url)
        )
        notice = DiscoveredNotice(
            source_id=source.source_id,
            organization=source.organization,
            title=full_title,
            url=notice_url,
            publication_date=None,
            application_date=f"Last Date: {closing_date}" if closing_date else None,
            category="Recruitment",
            source_identifier=f"rect_id:{rect_id}" if rect_id else None,
        )
        if notice.fingerprint not in seen_fp:
            seen_fp.add(notice.fingerprint)
            notices.append(notice)

    return notices, recognized


def adapter_rssb(source: SourceConfig, client: HttpClient) -> List[DiscoveredNotice]:
    """
    5. Rajasthan Staff Selection Board (RSSB) — https://rssb.rajasthan.gov.in/
    Parses the official Rajasthan State Recruitment Portal (https://recruitment.rajasthan.gov.in/)
    and RSSB notice pages (/news, /advertisements, /results, /) with legacy TLS renegotiation enabled.
    """
    notices: List[DiscoveredNotice] = []
    seen_fp: Set[str] = set()
    primary_error: Optional[SourceFetchError] = None
    last_error: Optional[SourceFetchError] = None
    any_page_fetched = False
    any_board_recognized = False

    for idx, url in enumerate(source.notice_urls):
        try:
            html_text, final_url = client.fetch_text(
                url, extra_headers={"Referer": source.homepage_url}
            )
            any_page_fetched = True
            if "recruitment.rajasthan.gov.in" in final_url.lower() or "notificationdocview" in html_text.lower() or "postdetailsclick" in html_text.lower():
                portal_notices, portal_rec = _extract_rajasthan_portal_notices(source, html_text, final_url)
                if portal_rec:
                    any_board_recognized = True
                for n in portal_notices:
                    if n.fingerprint not in seen_fp:
                        seen_fp.add(n.fingerprint)
                        notices.append(n)
            found, recognized = validate_and_extract_html_notices(
                source, html_text, final_url, prefer_row_text=False
            )
            if recognized:
                any_board_recognized = True
            for n in found:
                if n.fingerprint not in seen_fp:
                    seen_fp.add(n.fingerprint)
                    notices.append(n)
        except SourceFetchError as exc:
            if idx == 0 or (primary_error is None and exc.category != "HTTP_404_NOT_FOUND"):
                primary_error = exc
            last_error = exc

    if notices:
        return deduplicate_source_notices(notices, source)
    if any_board_recognized:
        return []
    chosen_error = primary_error or last_error
    if chosen_error and not any_page_fetched:
        raise chosen_error
    raise SourceFetchError(
        "UNRECOGNIZED_STRUCTURE",
        "Fetched RSSB / Rajasthan Recruitment Portal but found 0 valid notices and could not recognize board structure.",
    )


def adapter_mpesb(source: SourceConfig, client: HttpClient) -> List[DiscoveredNotice]:
    """
    6. Madhya Pradesh Employees Selection Board (MPESB) — https://esb.mp.gov.in/
    Parses MPESB Latest Updates box (e_default.html, homepage, and esb.mponline.gov.in).
    """
    return _multi_page_html_adapter(source, client, prefer_row_text=True)


DSSSB_POST_CODE_RE = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9\s/&(),.-]{3,120}?)\s*::\s*Post\s*Code\s*:\s*([0-9]{1,4}/[0-9]{2,4})",
    re.IGNORECASE,
)


def _extract_dsssbonline_notices(
    source: SourceConfig, html_text: str, page_url: str
) -> Tuple[List[DiscoveredNotice], bool]:
    """
    Extracts active DSSSB vacancy notifications (Post Name :: Post Code:XX/YY) and
    eAdmit Card links from DSSSB's official Online Application Registration System
    (https://dsssbonline.nic.in/).
    """
    notices: List[DiscoveredNotice] = []
    seen_fp: Set[str] = set()
    recognized = "current vacancies" in html_text.lower() or "online application registration system" in html_text.lower()

    stripped = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", html_text, flags=re.IGNORECASE | re.DOTALL)
    leaf_cells = re.findall(
        r"<td[^>]*>((?:(?!</?td\b).)*?)</td>",
        stripped,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for cell_html in leaf_cells:
        cell_text = clean_display_title(re.sub(r"<[^>]+>", " ", cell_html))
        cell_text = re.sub(r"^Current\s+Vacancies\s*", "", cell_text, flags=re.IGNORECASE).strip()
        for m in DSSSB_POST_CODE_RE.finditer(cell_text):
            post_name = clean_display_title(m.group(1))
            post_code = m.group(2).strip()
            if not post_name or not post_code:
                continue
            title = f"Current Vacancy: {post_name} (Post Code: {post_code})"
            notice_url = f"https://dsssbonline.nic.in/?post_code={ urllib.parse.quote(post_code, safe='/') }"
            notice = DiscoveredNotice(
                source_id=source.source_id,
                organization=source.organization,
                title=title,
                url=notice_url,
                category="Recruitment",
                source_identifier=f"post_code:{post_code}",
            )
            if notice.fingerprint not in seen_fp:
                seen_fp.add(notice.fingerprint)
                notices.append(notice)
                recognized = True

    # Also check AdmitCardEntry.aspx link on dsssbonline.nic.in
    for a in extract_anchors_with_context(html_text):
        if "admitcardentry.aspx" in a.href.lower():
            resolved = normalize_url(a.href, page_url)
            title = clean_display_title(a.text) or "Generate/Print DSSSB eAdmit Card"
            if len(title) < 25:
                title = f"{title} - DSSSB Online Portal"
            notice = DiscoveredNotice(
                source_id=source.source_id,
                organization=source.organization,
                title=title,
                url=resolved,
                category="Examination",
            )
            if notice.fingerprint not in seen_fp:
                seen_fp.add(notice.fingerprint)
                notices.append(notice)
                recognized = True

    return notices, recognized


def adapter_dsssb(source: SourceConfig, client: HttpClient) -> List[DiscoveredNotice]:
    """
    7. Delhi Subordinate Services Selection Board (DSSSB) — https://dsssb.delhi.gov.in/
    Parses DSSSB's official Online Application Portal (https://dsssbonline.nic.in/) Current Vacancies
    and eAdmit Cards as well as dsssb.delhi.gov.in (/High-Light, /latest-vacancies, /latest-result).
    """
    notices: List[DiscoveredNotice] = []
    seen_fp: Set[str] = set()
    primary_error: Optional[SourceFetchError] = None
    last_error: Optional[SourceFetchError] = None
    any_page_fetched = False
    any_board_recognized = False

    for idx, url in enumerate(source.notice_urls):
        try:
            html_text, final_url = client.fetch_text(
                url, extra_headers={"Referer": source.homepage_url}
            )
            any_page_fetched = True
            if "dsssbonline.nic.in" in final_url.lower() or "post code:" in html_text.lower():
                d_notices, d_rec = _extract_dsssbonline_notices(source, html_text, final_url)
                if d_rec:
                    any_board_recognized = True
                for n in d_notices:
                    if n.fingerprint not in seen_fp:
                        seen_fp.add(n.fingerprint)
                        notices.append(n)
            found, recognized = validate_and_extract_html_notices(
                source, html_text, final_url, prefer_row_text=True
            )
            if recognized:
                any_board_recognized = True
            for n in found:
                if n.fingerprint not in seen_fp:
                    seen_fp.add(n.fingerprint)
                    notices.append(n)
        except SourceFetchError as exc:
            if idx == 0 or (primary_error is None and exc.category != "HTTP_404_NOT_FOUND"):
                primary_error = exc
            last_error = exc

    if notices:
        return deduplicate_source_notices(notices, source)
    if any_board_recognized:
        return []
    chosen_error = primary_error or last_error
    if chosen_error and not any_page_fetched:
        raise chosen_error
    raise SourceFetchError(
        "UNRECOGNIZED_STRUCTURE",
        "Fetched DSSSB but found 0 valid notices and could not recognize board structure.",
    )


UKPSC_JS_ENTRY_RE = re.compile(
    r'\{[^{}]*?id\s*:\s*(\d+)\s*,\s*exam_name\s*:\s*"((?:\\.|[^"\\])*)"'
    r'[^{}]*?exam_apply_url\s*:\s*"((?:\\.|[^"\\])*)"'
    r'(?:[^{}]*?start_date_format\s*:\s*"((?:\\.|[^"\\])*)")?'
    r'(?:[^{}]*?end_date_format\s*:\s*"((?:\\.|[^"\\])*)")?[^{}]*?\}',
    re.DOTALL,
)


def _extract_ukpsc_js_bundle_notices(
    source: SourceConfig, js_text: str, base_url: str
) -> List[DiscoveredNotice]:
    """
    Parses the structured exam notification/admit-card/application array embedded in
    UKPSC's official online examination portal React bundle (https://ukpsc.net.in/static/js/main.*.js).
    """
    notices: List[DiscoveredNotice] = []
    seen_fp: Set[str] = set()
    for m in UKPSC_JS_ENTRY_RE.finditer(js_text):
        item_id = m.group(1).strip()
        raw_exam_name = m.group(2).encode("utf-8").decode("unicode_escape", errors="replace")
        raw_apply_url = m.group(3).strip()
        start_date = (m.group(4) or "").strip()
        end_date = (m.group(5) or "").strip()

        title = clean_display_title(raw_exam_name)
        if not title or len(title) < 10:
            continue
        resolved_url = normalize_url(raw_apply_url, base_url) or normalize_url(base_url)
        pub_date = extract_explicit_date(start_date) or extract_explicit_date(title)
        end_date_parsed = extract_explicit_date(end_date)
        app_date = f"{pub_date} to {end_date_parsed}" if (pub_date and end_date_parsed) else (f"Last Date: {end_date_parsed}" if end_date_parsed else None)
        # Attach unique id query param when multiple exam subjects share the same login portal URL
        if "#" in resolved_url:
            base_part, frag_part = resolved_url.split("#", 1)
            sep = "&" if "?" in base_part else "?"
            canonical_url = f"{base_part}{sep}notice_id={item_id}#{frag_part}"
        else:
            sep = "&" if "?" in resolved_url else "?"
            canonical_url = f"{resolved_url}{sep}notice_id={item_id}"

        notice = DiscoveredNotice(
            source_id=source.source_id,
            organization=source.organization,
            title=title,
            url=canonical_url,
            publication_date=pub_date,
            application_date=app_date,
            source_identifier=f"ukpsc_net:{item_id}:{normalize_title(title)[:40]}",
        )
        if notice.fingerprint not in seen_fp:
            seen_fp.add(notice.fingerprint)
            notices.append(notice)
    return notices


def adapter_ukpsc(source: SourceConfig, client: HttpClient) -> List[DiscoveredNotice]:
    """
    9. Uttarakhand Public Service Commission (UKPSC) — https://psc.uk.gov.in/
    Inspects UKPSC's official Online Examination Portal (https://ukpsc.net.in/ and its React bundle)
    as well as psc.uk.gov.in notice boards (/public/announcement, /candidate-corner/recruitment).
    """
    notices: List[DiscoveredNotice] = []
    seen_fp: Set[str] = set()
    primary_error: Optional[SourceFetchError] = None
    last_error: Optional[SourceFetchError] = None
    any_page_fetched = False
    any_board_recognized = False

    for idx, url in enumerate(source.notice_urls):
        try:
            html_text, final_url = client.fetch_text(
                url, extra_headers={"Referer": source.homepage_url}
            )
            any_page_fetched = True

            # Check if this is the ukpsc.net.in React portal with static/js/main.*.js bundle
            js_matches = re.findall(r'src=["\']([^"\']*static/js/main\.[^"\']+\.js)["\']', html_text)
            if js_matches:
                for js_rel in js_matches:
                    js_url = urllib.parse.urljoin(final_url, js_rel)
                    js_text, _ = client.fetch_text(js_url, extra_headers={"Referer": final_url})
                    bundle_notices = _extract_ukpsc_js_bundle_notices(source, js_text, final_url)
                    if bundle_notices:
                        any_board_recognized = True
                        for n in bundle_notices:
                            if n.fingerprint not in seen_fp:
                                seen_fp.add(n.fingerprint)
                                notices.append(n)
                continue

            found, recognized = validate_and_extract_html_notices(
                source, html_text, final_url, prefer_row_text=True
            )
            if recognized:
                any_board_recognized = True
            for n in found:
                if n.fingerprint not in seen_fp:
                    seen_fp.add(n.fingerprint)
                    notices.append(n)
        except SourceFetchError as exc:
            if idx == 0 or (primary_error is None and exc.category != "HTTP_404_NOT_FOUND"):
                primary_error = exc
            last_error = exc

    if notices:
        return deduplicate_source_notices(notices, source)
    if any_board_recognized:
        return []
    chosen_error = primary_error or last_error
    if chosen_error and not any_page_fetched:
        raise chosen_error
    raise SourceFetchError(
        "UNRECOGNIZED_STRUCTURE",
        "Fetched UKPSC but found 0 valid notices and could not recognize board structure.",
    )


ADAPTER_REGISTRY: Dict[str, Callable[[SourceConfig, HttpClient], List[DiscoveredNotice]]] = {
    "adapter_ssc": adapter_ssc,
    "adapter_upsc": adapter_upsc,
    "adapter_uppsc": adapter_uppsc,
    "adapter_hssc": adapter_hssc,
    "adapter_rssb": adapter_rssb,
    "adapter_mpesb": adapter_mpesb,
    "adapter_dsssb": adapter_dsssb,
    "adapter_upprpb": adapter_upprpb,
    "adapter_ukpsc": adapter_ukpsc,
}


# ============================================================================
# Persistent State Management, Schema Migration & Merging (Sections 5, 6, 7)
# ============================================================================

def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_initial_state() -> Dict[str, Any]:
    """Creates a clean versioned state structure for a newly initialized repository."""
    now = utc_now_iso()
    sources_map: Dict[str, Any] = {}
    for src in SOURCES:
        sources_map[src.source_id] = {
            "organization": src.organization,
            "homepage_url": src.homepage_url,
            "baseline_completed": False,
            "baseline_completed_at": None,
            "last_checked_at": None,
            "last_success_at": None,
            "last_status": "uninitialized",
            "last_error": None,
            "notice_count": 0,
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "initialized": False,
        "created_at": now,
        "updated_at": now,
        "sources": sources_map,
        "notices": {},
    }


def validate_state_schema(state: Any) -> Dict[str, Any]:
    """
    Validates that the loaded JSON state conforms to SCHEMA_VERSION.
    Raises StateValidationError if corrupt, missing required fields, or wrong version.
    """
    if not isinstance(state, dict):
        raise StateValidationError("State root must be a JSON object")

    version = state.get("schema_version")
    if version != SCHEMA_VERSION:
        raise StateValidationError(
            f"Unsupported state schema_version: {version!r} (expected {SCHEMA_VERSION})"
        )

    if not isinstance(state.get("initialized"), bool):
        raise StateValidationError("State 'initialized' flag must be a boolean")

    sources = state.get("sources")
    if not isinstance(sources, dict):
        raise StateValidationError("State 'sources' must be a JSON object")

    notices = state.get("notices")
    if not isinstance(notices, dict):
        raise StateValidationError("State 'notices' must be a JSON object")

    for fp, record in notices.items():
        if not isinstance(record, dict):
            raise StateValidationError(f"Notice entry {fp!r} must be an object")
        for req_key in (
            "fingerprint",
            "source_id",
            "organization",
            "title",
            "normalized_url",
            "first_seen_at",
            "delivery_status",
        ):
            if req_key not in record:
                raise StateValidationError(
                    f"Notice entry {fp!r} missing required key {req_key!r}"
                )
        # Normalize delivery_status aliases ('sent' -> 'delivered', 'known' -> 'baselined')
        raw_status = record["delivery_status"]
        if raw_status == "sent":
            record["delivery_status"] = "delivered"
        elif raw_status == "known":
            record["delivery_status"] = "baselined"

        if record["delivery_status"] not in ("baselined", "pending", "delivered", "failed"):
            raise StateValidationError(
                f"Notice entry {fp!r} has invalid delivery_status {record['delivery_status']!r}"
            )
        test_status = record.get("test_delivery_status")
        if test_status is not None and test_status not in ("delivered", "failed"):
            raise StateValidationError(
                f"Notice entry {fp!r} has invalid test_delivery_status {test_status!r}"
            )

    # v4 State Migration & Backward Compatibility:
    # Safely prune removed sites from active monitoring
    # while strictly preserving all 9 retained sites, their notification history,
    # fingerprints, and existing baseline_completed flags.
    for rem_id in REMOVED_SOURCE_IDS:
        sources.pop(rem_id, None)

    # Prune any historical notice entries belonging to removed sources if present
    pruned_notice_fps = [
        fp for fp, rec in notices.items()
        if rec.get("source_id") in REMOVED_SOURCE_IDS
    ]
    for fp in pruned_notice_fps:
        notices.pop(fp, None)

    # Ensure all 9 configured sources exist in state['sources']
    for src in SOURCES:
        if src.source_id not in sources:
            sources[src.source_id] = {
                "organization": src.organization,
                "homepage_url": src.homepage_url,
                "baseline_completed": False,
                "baseline_completed_at": None,
                "last_checked_at": None,
                "last_success_at": None,
                "last_status": "uninitialized",
                "last_error": None,
                "notice_count": 0,
            }
        else:
            src_entry = sources[src.source_id]
            if not isinstance(src_entry, dict) or not isinstance(
                src_entry.get("baseline_completed"), bool
            ):
                raise StateValidationError(
                    f"Invalid source state entry for {src.source_id!r}"
                )

    return state


def apply_state_retention_policy(
    state: Dict[str, Any],
    max_full_records: int = MAX_FULL_NOTICE_RECORDS,
    max_total_fingerprints: int = MAX_TOTAL_FINGERPRINTS,
) -> Dict[str, Any]:
    """
    Applies explicit retention limits to state storage without discarding
    fingerprints needed for duplicate protection:
    - All 'pending' and 'failed' notices and the most recent `max_full_records`
      notices retain all verbose metadata fields.
    - Older 'delivered'/'baselined' records beyond `max_full_records` are compacted
      in-place (retaining fingerprint, source_id, organization, title, normalized_title,
      normalized_url, first_seen_at, delivery_status) so duplicate protection never
      forgets previously processed notices.
    """
    notices = state.get("notices")
    if not isinstance(notices, dict) or len(notices) <= max_full_records:
        return state

    sorted_fps = sorted(
        notices.keys(),
        key=lambda k: (
            1 if notices[k].get("delivery_status") in ("pending", "failed") else 0,
            str(notices[k].get("last_seen_at") or notices[k].get("first_seen_at") or ""),
            k,
        ),
        reverse=True,
    )

    # Compact records beyond max_full_records while preserving all deduplication keys
    for fp in sorted_fps[max_full_records:max_total_fingerprints]:
        rec = notices[fp]
        if rec.get("delivery_status") in ("delivered", "baselined"):
            for verbose_key in (
                "last_delivery_error",
                "last_test_error",
                "test_delivered_at",
                "test_delivery_attempts",
            ):
                rec.pop(verbose_key, None)

    if len(sorted_fps) > max_total_fingerprints:
        for fp in sorted_fps[max_total_fingerprints:]:
            if notices[fp].get("delivery_status") not in ("pending", "failed"):
                notices.pop(fp, None)

    return state


def load_state(state_path: str, allow_init_if_missing: bool = False) -> Dict[str, Any]:
    """
    Loads and validates the JSON state file.
    Fails safely on missing, empty, or corrupt files rather than silently
    recreating an empty baseline.
    """
    if not os.path.exists(state_path):
        if allow_init_if_missing:
            return create_initial_state()
        raise StateValidationError(
            f"State file {state_path!r} does not exist. Run with --init-state to initialize safely."
        )

    try:
        with open(state_path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        raise StateValidationError(f"Failed to read state file {state_path!r}: {exc}") from exc

    if not raw or not raw.strip():
        raise StateValidationError(
            f"State file {state_path!r} is empty. Refusing to recreate baseline automatically."
        )

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateValidationError(
            f"State file {state_path!r} contains corrupt JSON: {exc}"
        ) from exc

    return validate_state_schema(parsed)


def save_state_atomically(state: Dict[str, Any], state_path: str) -> None:
    """
    Validates, applies retention compaction, and writes the state file atomically
    via a temporary file in the same directory so an interrupted write never
    corrupts data/notice_state.json.
    """
    apply_state_retention_policy(state)
    validate_state_schema(state)
    state["updated_at"] = utc_now_iso()

    parent_dir = os.path.dirname(os.path.abspath(state_path))
    os.makedirs(parent_dir, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(prefix=".notice_state_", suffix=".tmp", dir=parent_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, ensure_ascii=False, sort_keys=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, state_path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def merge_states(base_state: Dict[str, Any], incoming_state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Losslessly merges two valid state dictionaries during a Git push/rebase conflict:
    - Never discards any notice fingerprint from either state
    - Preserves 'delivered' and 'baselined' statuses over 'failed'/'pending' so a conflict
      or stale state file never overwrites newer sent records or triggers duplicate alerts
    - Promotes stale 'pending' records that predate a source's completed baseline to 'baselined'
    - Preserves completed baselines across all 12 active sources
    """
    validate_state_schema(base_state)
    validate_state_schema(incoming_state)

    merged = copy.deepcopy(base_state)
    merged["initialized"] = bool(
        base_state.get("initialized", False) or incoming_state.get("initialized", False)
    )
    merged["updated_at"] = utc_now_iso()

    # Merge per-source metadata
    for src in SOURCES:
        sid = src.source_id
        s_base = merged["sources"].get(sid, {})
        s_inc = incoming_state["sources"].get(sid, {})

        baseline_done = bool(
            s_base.get("baseline_completed", False) or s_inc.get("baseline_completed", False)
        )
        b_at_base = s_base.get("baseline_completed_at")
        b_at_inc = s_inc.get("baseline_completed_at")
        if b_at_base and b_at_inc:
            baseline_at = min(str(b_at_base), str(b_at_inc))
        else:
            baseline_at = b_at_base or b_at_inc

        # Prefer latest check metadata from whichever state ran more recently
        chk_base = str(s_base.get("last_checked_at") or "")
        chk_inc = str(s_inc.get("last_checked_at") or "")
        newer = s_inc if chk_inc >= chk_base else s_base

        suc_base = str(s_base.get("last_success_at") or "")
        suc_inc = str(s_inc.get("last_success_at") or "")
        last_success_at = max(suc_base, suc_inc) or None

        merged["sources"][sid] = {
            "organization": src.organization,
            "homepage_url": src.homepage_url,
            "baseline_completed": baseline_done,
            "baseline_completed_at": baseline_at,
            "last_checked_at": max(chk_base, chk_inc) or None,
            "last_success_at": last_success_at,
            "last_status": newer.get("last_status", "uninitialized"),
            "last_error": newer.get("last_error"),
            "notice_count": newer.get("notice_count", 0),
        }

    # Merge notices without losing any fingerprint or downgrading delivered/baselined status
    status_priority = {
        "delivered": 4,
        "sent": 4,
        "baselined": 3,
        "known": 3,
        "failed": 2,
        "pending": 1,
    }
    merged_notices: Dict[str, Any] = merged["notices"]
    for fp, inc_rec in incoming_state["notices"].items():
        if fp not in merged_notices:
            merged_notices[fp] = copy.deepcopy(inc_rec)
            continue

        base_rec = merged_notices[fp]
        b_status = str(base_rec.get("delivery_status", "pending"))
        i_status = str(inc_rec.get("delivery_status", "pending"))
        winning_status = (
            i_status
            if status_priority.get(i_status, 0) >= status_priority.get(b_status, 0)
            else b_status
        )
        base_rec["delivery_status"] = winning_status
        base_rec["first_seen_at"] = min(
            str(base_rec.get("first_seen_at") or utc_now_iso()),
            str(inc_rec.get("first_seen_at") or utc_now_iso()),
        )
        base_rec["last_seen_at"] = max(
            str(base_rec.get("last_seen_at") or ""),
            str(inc_rec.get("last_seen_at") or ""),
        ) or base_rec["first_seen_at"]

        for opt_field in (
            "publication_date",
            "application_date",
            "exam_date",
            "category",
            "source_identifier",
            "delivered_at",
        ):
            if not base_rec.get(opt_field) and inc_rec.get(opt_field):
                base_rec[opt_field] = inc_rec[opt_field]

        base_rec["delivery_attempts"] = max(
            int(base_rec.get("delivery_attempts") or 0),
            int(inc_rec.get("delivery_attempts") or 0),
        )
        if winning_status in ("delivered", "baselined"):
            base_rec["last_delivery_error"] = None
        elif inc_rec.get("last_delivery_error"):
            base_rec["last_delivery_error"] = inc_rec["last_delivery_error"]

        # Merge live_test tracking fields if present
        if inc_rec.get("test_delivery_status") == "delivered" or (
            not base_rec.get("test_delivery_status") and inc_rec.get("test_delivery_status")
        ):
            base_rec["test_delivery_status"] = inc_rec.get("test_delivery_status")
            base_rec["test_delivered_at"] = inc_rec.get("test_delivered_at") or base_rec.get("test_delivered_at")
            base_rec["last_test_error"] = inc_rec.get("last_test_error")

    # Prevent a stale state from reintroducing old 'pending' notifications that predate
    # a source's baseline_completed_at timestamp
    for fp, rec in merged_notices.items():
        if rec.get("delivery_status") == "pending":
            sid = str(rec.get("source_id") or "")
            src_meta = merged["sources"].get(sid, {})
            b_at = str(src_meta.get("baseline_completed_at") or "")
            first_seen = str(rec.get("first_seen_at") or "")
            if src_meta.get("baseline_completed") and b_at and first_seen and first_seen <= b_at:
                rec["delivery_status"] = "baselined"
                rec["last_delivery_error"] = None

    apply_state_retention_policy(merged)
    return validate_state_schema(merged)


# ============================================================================
# Telegram Delivery Client (Section 8 & Live Test Mode)
# ============================================================================

def resolve_notice_or_board_url(notice_record: Dict[str, Any]) -> str:
    """
    Returns the direct official notice/PDF URL if valid; otherwise falls back to
    the source's official notice-board URL or homepage URL.
    """
    url = str(notice_record.get("url") or notice_record.get("normalized_url") or "").strip()
    parsed = urllib.parse.urlsplit(url)
    source_id = str(notice_record.get("source_id") or "")
    src_cfg = SOURCE_BY_ID.get(source_id)

    if parsed.scheme in ("http", "https") and parsed.netloc:
        # If the URL is bare homepage root and the source has a dedicated HTML notice board URL, prefer it
        if src_cfg and url.rstrip("/") == src_cfg.homepage_url.rstrip("/"):
            for cand in src_cfg.notice_urls:
                if "/api/" not in cand.lower():
                    return cand
        return url

    if src_cfg:
        for cand in src_cfg.notice_urls:
            if "/api/" not in cand.lower():
                return cand
        return src_cfg.homepage_url
    return ""


def format_telegram_message(
    notice_record: Dict[str, Any],
    is_live_test: bool = False,
) -> str:
    """
    Formats a concise, HTML-escaped Telegram message for a single notice.
    When is_live_test=True, prefixes the message clearly with '[LIVE TEST]'.
    Includes Organization, Notice Title, Category, Official Publication Date,
    Application / Examination Dates (when available), and the official Notice/PDF link.
    """
    org = html.escape(str(notice_record.get("organization") or "Official Government Source"))
    raw_title = str(notice_record.get("title") or "Official Notice")
    if len(raw_title) > 1200:
        raw_title = raw_title[:1197] + "..."
    title = html.escape(raw_title)

    category = html.escape(str(notice_record.get("category") or "Official Notice"))
    pub_date = notice_record.get("publication_date")
    app_date = notice_record.get("application_date") or extract_application_date(raw_title)
    exam_date = notice_record.get("exam_date") or extract_exam_date(raw_title)

    url = resolve_notice_or_board_url(notice_record)
    escaped_url = html.escape(url, quote=True)

    lines: List[str] = []
    if is_live_test:
        lines.append(f"[LIVE TEST] 🏛 <b>{org}</b>")
    else:
        lines.append(f"🏛 <b>{org}</b>")

    lines.extend(
        [
            "",
            f"📌 <b>Notice:</b> {title}",
            f"📂 <b>Category:</b> {category}",
        ]
    )
    if pub_date:
        lines.append(f"📅 <b>Publication Date:</b> {html.escape(str(pub_date))}")
    if app_date:
        lines.append(f"🗓 <b>Application Date:</b> {html.escape(str(app_date))}")
    if exam_date:
        lines.append(f"📝 <b>Examination Date:</b> {html.escape(str(exam_date))}")
    if escaped_url:
        lines.append(f"🔗 <b>Official Link:</b> <a href=\"{escaped_url}\">{escaped_url}</a>")

    message = "\n".join(lines)
    if len(message) > MAX_TELEGRAM_MESSAGE_LENGTH:
        message = message[: MAX_TELEGRAM_MESSAGE_LENGTH - 3] + "..."
    return message


def format_live_test_summary_message(summary: "RunSummary") -> str:
    """
    Formats the final Telegram test summary message required in live_test mode.
    """
    lines = [
        "[LIVE TEST] 📊 <b>Government Notice Monitor — Live Test Summary</b>",
        "",
        f"• <b>Total websites:</b> {summary.total_websites}",
        f"• <b>Successful website fetches and parsing:</b> {summary.succeeded_websites}",
        f"• <b>Failed websites:</b> {summary.failed_websites}",
        f"• <b>Websites with notices found:</b> {summary.websites_with_notices}",
        f"• <b>Test messages delivered successfully:</b> {summary.test_messages_delivered}",
        f"• <b>Telegram delivery failures:</b> {summary.telegram_delivery_failures}",
        f"• <b>Websites requiring adapter review:</b> {summary.websites_requiring_review}",
        "",
        "<b>Per-Website Live Test Status:</b>",
    ]

    for src in SOURCES:
        res = summary.per_source_results.get(src.source_id)
        if not res:
            continue
        outcome = str(res.get("test_outcome") or res.get("status") or "UNKNOWN")
        disc = int(res.get("discovered") or 0)
        err_cat = res.get("error_category")
        if err_cat:
            lines.append(
                f"• <code>{html.escape(src.source_id)}</code>: {html.escape(outcome)} ({html.escape(str(err_cat))})"
            )
        else:
            lines.append(
                f"• <code>{html.escape(src.source_id)}</code>: {html.escape(outcome)} (notices={disc})"
            )

    message = "\n".join(lines)
    if len(message) > MAX_TELEGRAM_MESSAGE_LENGTH:
        message = message[: MAX_TELEGRAM_MESSAGE_LENGTH - 3] + "..."
    return message


class TelegramNotifier:
    """
    Delivers individual notice alerts and live-test summaries via the Telegram Bot API over HTTPS.
    Never logs credentials, respects Retry-After rate limits, and avoids
    retrying permanent errors (400, 401, 403, 404).
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        max_retries: int = MAX_TELEGRAM_RETRIES,
        timeout: int = 15,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        if not bot_token or not bot_token.strip():
            raise ValueError("TELEGRAM_BOT_TOKEN is missing or empty")
        if not chat_id or not chat_id.strip():
            raise ValueError("TELEGRAM_CHAT_ID is missing or empty")
        self._bot_token = bot_token.strip()
        self._chat_id = chat_id.strip()
        self.max_retries = max_retries
        self.timeout = timeout
        self.sleep_fn = sleep_fn

    def send_notice(
        self,
        notice_record: Dict[str, Any],
        is_live_test: bool = False,
    ) -> str:
        message_html = format_telegram_message(notice_record, is_live_test=is_live_test)
        self._send_html_message(message_html)
        return message_html

    def send_live_test_summary(self, summary: "RunSummary") -> str:
        message_html = format_live_test_summary_message(summary)
        self._send_html_message(message_html)
        return message_html

    def _send_html_message(self, message_html: str) -> None:
        api_url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload = json.dumps(
            {
                "chat_id": self._chat_id,
                "text": message_html,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            }
        ).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

        last_err: Optional[TelegramDeliveryError] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                status, resp_headers, body = self._post_json(api_url, payload, headers)
                parsed = self._parse_response(body)

                if status == 200 and parsed.get("ok") is True:
                    # Brief politeness delay to avoid hitting Telegram per-chat rate limits
                    self.sleep_fn(0.35)
                    return

                description = str(parsed.get("description") or f"HTTP {status}")
                description = self._redact(description)

                if status == 429:
                    retry_after = 5.0
                    params = parsed.get("parameters")
                    if isinstance(params, dict) and params.get("retry_after"):
                        try:
                            retry_after = float(params["retry_after"])
                        except (TypeError, ValueError):
                            pass
                    elif resp_headers.get("Retry-After"):
                        try:
                            retry_after = float(resp_headers["Retry-After"])
                        except ValueError:
                            pass
                    last_err = TelegramDeliveryError(
                        f"Telegram rate limit (429): {description}", permanent=False
                    )
                    if attempt < self.max_retries:
                        self.sleep_fn(min(retry_after + 0.5, 60.0))
                        continue
                    raise last_err

                if status in (400, 401, 403, 404):
                    raise TelegramDeliveryError(
                        f"Permanent Telegram API error (HTTP {status}): {description}",
                        permanent=True,
                    )

                last_err = TelegramDeliveryError(
                    f"Transient Telegram API error (HTTP {status}): {description}",
                    permanent=False,
                )
                if attempt < self.max_retries:
                    self.sleep_fn((2 ** (attempt - 1)) + random.uniform(0.1, 0.4))
                    continue
                raise last_err

            except TelegramDeliveryError as exc:
                if exc.permanent or exc.ambiguous or attempt >= self.max_retries:
                    raise
                last_err = exc
                self.sleep_fn((2 ** (attempt - 1)) + random.uniform(0.1, 0.4))

        if last_err:
            raise last_err

    def _redact(self, text: str) -> str:
        out = text.replace(self._bot_token, "[REDACTED]")
        out = out.replace(self._chat_id, "[REDACTED_CHAT]")
        return out

    def _post_json(
        self, url: str, payload: bytes, headers: Dict[str, str]
    ) -> Tuple[int, Dict[str, str], bytes]:
        try:
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                status = getattr(resp, "status", resp.getcode())
                resp_headers = {k: v for k, v in resp.headers.items()}
                return status, resp_headers, resp.read()
        except urllib.error.HTTPError as exc:
            resp_headers = {k: v for k, v in exc.headers.items()} if exc.headers else {}
            try:
                err_body = exc.read()
            except Exception:
                err_body = b""
            return exc.code, resp_headers, err_body
        except (socket.timeout, TimeoutError) as exc:
            safe_msg = self._redact(str(exc))
            raise TelegramDeliveryError(
                f"Ambiguous post-send timeout (Telegram may have accepted message): {safe_msg}",
                permanent=False,
                ambiguous=True,
            ) from exc
        except Exception as exc:
            safe_msg = self._redact(str(exc))
            raise TelegramDeliveryError(
                f"Telegram network/timeout error: {safe_msg}", permanent=False
            ) from exc

    @staticmethod
    def _parse_response(body: bytes) -> Dict[str, Any]:
        if not body:
            return {}
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}


# ============================================================================
# Core Monitoring, Baseline & Live-Test Engine (Sections 2, 3, 4, 5, 6, 8, 9)
# ============================================================================

@dataclasses.dataclass
class RunSummary:
    mode: str = "monitor"
    total_websites: int = 0
    succeeded_websites: int = 0
    failed_websites: int = 0
    websites_with_notices: int = 0
    websites_with_no_notices: int = 0
    websites_requiring_review: int = 0
    notices_discovered: int = 0
    genuinely_new_notices: int = 0
    baselined_notices: int = 0
    filtered_irrelevant_notices: int = 0
    filtered_date_notices: int = 0
    rejected_missing_or_invalid_date_notices: int = 0
    rejected_stale_notices: int = 0
    capped_pending_notices: int = 0
    capped_deferred_notices: int = 0
    test_messages_delivered: int = 0
    telegram_deliveries_confirmed: int = 0
    telegram_delivery_failures: int = 0
    summary_message_delivered: bool = False
    state_persisted: bool = False
    per_source_results: Dict[str, Dict[str, Any]] = dataclasses.field(default_factory=dict)


def _find_existing_notice_record(
    notices_store: Dict[str, Any],
    item: DiscoveredNotice,
) -> Optional[Dict[str, Any]]:
    """
    Finds an existing record in notices_store matching item:
    1. Exact SHA-256 fingerprint match
    2. Same source_id + same non-empty source_identifier
    3. Same source_id + identical normalized or canonical direct document URL
    4. Same source_id + identical normalized_title or canonical_title_key where neither has a conflicting document filename
    """
    direct = notices_store.get(item.fingerprint)
    if direct is not None:
        return direct

    item_doc_tok = _extract_document_token(item.normalized_url)
    item_is_direct_doc = _is_direct_document_url(item.normalized_url)
    item_canon_url = canonical_url_key(item.normalized_url)
    item_canon_title = canonical_title_key(item.title)

    for rec in notices_store.values():
        if rec.get("source_id") != item.source_id:
            continue
        rec_sid = rec.get("source_identifier")
        if item.source_identifier and rec_sid:
            if str(rec_sid).strip() == str(item.source_identifier).strip():
                return rec
            continue

        rec_norm_url = str(rec.get("normalized_url") or "")
        if item_is_direct_doc and item.normalized_url:
            if rec_norm_url == item.normalized_url:
                return rec
            if item_canon_url and canonical_url_key(rec_norm_url) == item_canon_url:
                return rec

        rec_title = str(rec.get("title") or "")
        rec_norm_title = str(rec.get("normalized_title") or normalize_title(rec_title))
        rec_canon_title = canonical_title_key(rec_title)
        title_matches = (
            (len(item.normalized_title) >= 12 and rec_norm_title == item.normalized_title)
            or (len(item_canon_title) >= 12 and rec_canon_title == item_canon_title)
        )
        if title_matches:
            rec_doc_tok = _extract_document_token(rec_norm_url)
            if (
                item_doc_tok is not None
                and rec_doc_tok is not None
                and item_doc_tok != rec_doc_tok
            ):
                continue
            return rec
    return None


def _deliver_notice_with_notifier(
    notifier: Any,
    notice_record: Dict[str, Any],
    is_live_test: bool = False,
) -> None:
    """Calls notifier.send_notice with backward compatibility for 1-arg test mocks."""
    if is_live_test:
        try:
            notifier.send_notice(notice_record, is_live_test=True)
        except TypeError:
            notifier.send_notice(notice_record)
    else:
        notifier.send_notice(notice_record)


def _is_historical_or_expired_for_board(
    item: DiscoveredNotice,
    discovered_board: Sequence[DiscoveredNotice],
    reference_date: Optional[datetime.date] = None,
    max_age_days: int = DEFAULT_MAX_NOTICE_AGE_DAYS,
) -> bool:
    """
    Determines whether a newly encountered notice on an already-baselined source
    is actually an expired notice or an old historical notice (> max_age_days
    older than the board's newest dated notice or reference_date).
    """
    board_dates: List[datetime.date] = []
    for n in discovered_board:
        k = parse_publication_date_sort_key(n.publication_date)
        if k is not None:
            try:
                board_dates.append(datetime.date(*k))
            except ValueError:
                pass
    newest_board_date = max(board_dates) if board_dates else None
    effective_ref = reference_date or newest_board_date

    # 1. Explicit expiry date check ('Visible upto ...', 'Valid till ...')
    exp_str = extract_expiry_date(item.raw_context or item.title) or extract_expiry_date(item.title)
    if exp_str:
        exp_key = parse_publication_date_sort_key(exp_str)
        if exp_key is not None and effective_ref is not None:
            if datetime.date(*exp_key) < effective_ref:
                return True

    # 2. Publication date significantly older than the board's current notices / reference_date
    if item.publication_date and effective_ref is not None:
        pub_key = parse_publication_date_sort_key(item.publication_date)
        if pub_key is not None:
            pub_date = datetime.date(*pub_key)
            if (effective_ref - pub_date).days > max_age_days:
                return True

    return False


def _is_failed_record_stale(rec: Dict[str, Any]) -> bool:
    """
    Returns True if a previously failed notice has exceeded MAX_NOTICE_DELIVERY_ATTEMPTS
    or is older than MAX_FAILED_RETRY_AGE_HOURS, preventing old failed notices from being
    resent when they are no longer new.
    """
    attempts = int(rec.get("delivery_attempts") or 0)
    if attempts >= MAX_NOTICE_DELIVERY_ATTEMPTS:
        return True
    first_seen_str = str(rec.get("first_seen_at") or "").strip()
    if first_seen_str:
        try:
            dt = datetime.datetime.strptime(first_seen_str, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc
            )
            age_hours = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds() / 3600.0
            if age_hours > MAX_FAILED_RETRY_AGE_HOURS:
                return True
        except ValueError:
            pass
    return False


def run_monitor(
    state_path: str = DEFAULT_STATE_PATH,
    mode: str = "auto",
    sources: Sequence[SourceConfig] = SOURCES,
    http_client: Optional[HttpClient] = None,
    notifier: Optional[TelegramNotifier] = None,
    logger: Optional[logging.Logger] = None,
    allow_init_if_missing: bool = False,
    reference_date: Optional[datetime.date] = None,
    max_age_days: int = DEFAULT_MAX_NOTICE_AGE_DAYS,
    max_alerts_per_run: int = MAX_PRODUCTION_ALERTS_PER_RUN,
    keyword_categories: Optional[Dict[str, Tuple[str, ...]]] = None,
) -> Tuple[RunSummary, Dict[str, Any]]:
    """
    Executes a complete monitoring or live_test pass across configured websites:
    - Isolates per-website failures so one broken site never stops others
    - Distinguishes valid empty notice boards from unrecognized/broken pages
    - Performs explicit per-source baseline on first successful check (including valid empty boards)
    - In 'live_test' mode:
      * Baselines all discovered notices for every successfully validated source
      * Sends 1 '[LIVE TEST]' Telegram message for the latest notice from each source with notices
      * Keeps test-message delivery status separate from normal notification delivery status
      * Sends a final Telegram test summary message
    - In 'monitor'/'auto' mode:
      * Enforces strict keyword filtering across the 5 required job-notice categories
      * Enforces mandatory publication-date validation (rejects missing, invalid, future, or stale dates)
      * Deduplicates across URLs, titles, organization IDs, and notice identifiers
      * Enforces a maximum of `max_alerts_per_run` (default 10) production notifications per run
      * Marks a notice as delivered ('sent') only after Telegram confirms successful delivery
    - Saves updated state atomically under an advisory file lock
    """
    if logger is None:
        logger, _ = configure_logging()
    if http_client is None:
        http_client = HttpClient()

    if mode not in ("auto", "baseline", "monitor", "live_test"):
        raise ValueError(f"Unsupported execution mode: {mode!r}")

    active_keywords = keyword_categories if keyword_categories is not None else load_keyword_categories()

    # Acquire advisory process lock to prevent concurrent runs on the same runner from racing
    lock_file_handle = None
    try:
        import fcntl
        lock_dir = os.path.dirname(os.path.abspath(state_path))
        if os.path.isdir(lock_dir):
            lock_path = f"{os.path.abspath(state_path)}.lock"
            lock_file_handle = open(lock_path, "a+", encoding="utf-8")
            fcntl.flock(lock_file_handle.fileno(), fcntl.LOCK_EX)
    except Exception:
        lock_file_handle = None

    try:
        state = load_state(state_path, allow_init_if_missing=allow_init_if_missing)
        is_live_test = mode == "live_test"
        force_baseline = (mode == "baseline") or (mode == "auto" and not state.get("initialized", False))

        effective_mode = "live_test" if is_live_test else ("baseline" if force_baseline else "monitor")
        summary = RunSummary(mode=effective_mode, total_websites=len(sources))

        if is_live_test:
            logger.info("Running in LIVE_TEST mode (live scraping + [LIVE TEST] Telegram verification + baseline recording).")
        elif force_baseline:
            logger.info("Running in BASELINE mode (or initial uninitialized state).")
        else:
            logger.info(
                "Running in MONITOR mode (incremental detection, strict keyword & date validation, max_age_days=%d, max_alerts_per_run=%d).",
                max_age_days,
                max_alerts_per_run,
            )

        notices_store: Dict[str, Any] = state["notices"]
        sources_store: Dict[str, Any] = state["sources"]

        # Step 1: Query each website independently with strict failure isolation
        for src in sources:
            src_state = sources_store.setdefault(
                src.source_id,
                {
                    "organization": src.organization,
                    "homepage_url": src.homepage_url,
                    "baseline_completed": False,
                    "baseline_completed_at": None,
                    "last_checked_at": None,
                    "last_success_at": None,
                    "last_status": "uninitialized",
                    "last_error": None,
                    "notice_count": 0,
                },
            )
            src_state["last_checked_at"] = utc_now_iso()
            adapter_fn = ADAPTER_REGISTRY.get(src.adapter_name)
            if adapter_fn is None:
                err_msg = f"Missing adapter function {src.adapter_name}"
                logger.error("[%s] %s", src.source_id, err_msg)
                src_state["last_status"] = "error"
                src_state["last_error"] = err_msg
                summary.failed_websites += 1
                summary.websites_requiring_review += 1
                summary.per_source_results[src.source_id] = {
                    "status": "error",
                    "test_outcome": "REVIEW_REQUIRED",
                    "error_category": "CONFIGURATION_ERROR",
                    "error": err_msg,
                    "discovered": 0,
                    "new": 0,
                    "baselined": 0,
                }
                continue

            try:
                raw_discovered = adapter_fn(src, http_client)
                discovered = deduplicate_source_notices(raw_discovered, src)
            except SourceFetchError as exc:
                needs_review = exc.category in REVIEW_REQUIRED_ERROR_CATEGORIES
                outcome = "REVIEW_REQUIRED" if needs_review else "FETCH_FAILED"
                logger.warning(
                    "[%s] %s failed: outcome=%s category=%s detail=%s",
                    src.source_id,
                    src.organization,
                    outcome,
                    exc.category,
                    exc.detail,
                )
                src_state["last_status"] = "error"
                src_state["last_error"] = f"{exc.category}: {exc.detail}"
                summary.failed_websites += 1
                if needs_review:
                    summary.websites_requiring_review += 1
                summary.per_source_results[src.source_id] = {
                    "status": "error",
                    "test_outcome": outcome,
                    "error_category": exc.category,
                    "error": exc.detail,
                    "discovered": 0,
                    "new": 0,
                    "baselined": 0,
                }
                continue
            except Exception as exc:
                logger.exception(
                    "[%s] Unexpected adapter error for %s: %s",
                    src.source_id,
                    src.organization,
                    exc,
                )
                src_state["last_status"] = "error"
                src_state["last_error"] = f"UNEXPECTED_ERROR: {exc}"
                summary.failed_websites += 1
                summary.websites_requiring_review += 1
                summary.per_source_results[src.source_id] = {
                    "status": "error",
                    "test_outcome": "REVIEW_REQUIRED",
                    "error_category": "UNEXPECTED_ERROR",
                    "error": str(exc),
                    "discovered": 0,
                    "new": 0,
                    "baselined": 0,
                }
                continue

            # Website fetch and structure validation succeeded!
            summary.succeeded_websites += 1
            summary.notices_discovered += len(discovered)
            src_state["last_status"] = "ok"
            src_state["last_error"] = None
            src_state["last_success_at"] = utc_now_iso()
            src_state["notice_count"] = len(discovered)

            # Determine whether this source should baseline discovered notices
            source_needs_baseline = (
                is_live_test
                or force_baseline
                or (not src_state.get("baseline_completed", False))
            )
            src_new_count = 0
            src_baselined_count = 0

            for item in discovered:
                existing = _find_existing_notice_record(notices_store, item)
                if existing is not None:
                    existing["last_seen_at"] = utc_now_iso()
                    if item.publication_date and not existing.get("publication_date"):
                        existing["publication_date"] = item.publication_date
                    if item.application_date and not existing.get("application_date"):
                        existing["application_date"] = item.application_date
                    if item.exam_date and not existing.get("exam_date"):
                        existing["exam_date"] = item.exam_date
                    if item.category and (
                        not existing.get("category") or existing.get("category") == "Official Notice"
                    ):
                        existing["category"] = item.category
                    if item.source_identifier and not existing.get("source_identifier"):
                        existing["source_identifier"] = item.source_identifier
                    # If we are baselining or live-testing, ensure any previously pending item
                    # currently on the board is marked baselined so it won't fire later as a normal alert.
                    if source_needs_baseline and existing.get("delivery_status") == "pending":
                        existing["delivery_status"] = "baselined"
                        src_baselined_count += 1
                        summary.baselined_notices += 1
                    continue

                filter_reason: Optional[str] = None
                if source_needs_baseline:
                    status_value = "baselined"
                else:
                    # 1. Check board-level expiry / historical age
                    is_expired_or_old = _is_historical_or_expired_for_board(
                        item,
                        discovered,
                        reference_date=reference_date,
                        max_age_days=max_age_days,
                    )
                    # 2. Check strict keyword filtering across the 5 required job-notice categories
                    is_relevant, matched_cat, matched_kw = match_notice_keywords(
                        item.title,
                        item.raw_context or "",
                        categories=active_keywords,
                    )
                    # 3. Check mandatory publication date validation
                    date_valid, canonical_pub_date, date_reason = validate_publication_date(
                        item.publication_date,
                        reference_date=reference_date,
                        max_age_days=max_age_days,
                        context_text=item.raw_context or item.title,
                    )

                    if not is_relevant:
                        status_value = "baselined"
                        filter_reason = f"IRRELEVANT_KEYWORD:{matched_kw}"
                        summary.filtered_irrelevant_notices += 1
                        logger.warning(
                            "[%s] Filtered irrelevant notice (reason=%s): %s",
                            src.source_id,
                            filter_reason,
                            item.title[:100],
                        )
                    elif not date_valid or is_expired_or_old:
                        status_value = "baselined"
                        filter_reason = f"INVALID_PUBLICATION_DATE:{date_reason if not date_valid else 'EXPIRED_OR_HISTORICAL'}"
                        summary.filtered_date_notices += 1
                        if not date_valid and date_reason in (
                            "MISSING_PUBLICATION_DATE",
                            "INVALID_PUBLICATION_DATE",
                            "FUTURE_PUBLICATION_DATE",
                            "NON_PUBLICATION_DATE_TYPE",
                        ):
                            summary.rejected_missing_or_invalid_date_notices += 1
                        else:
                            summary.rejected_stale_notices += 1
                        logger.warning(
                            "[%s] Filtered notice due to publication-date validation (reason=%s, pub_date=%r): %s",
                            src.source_id,
                            filter_reason,
                            item.publication_date,
                            item.title[:100],
                        )
                    else:
                        status_value = "pending"
                        if canonical_pub_date:
                            item.publication_date = canonical_pub_date
                        if matched_cat:
                            item.category = matched_cat

                rec_entry: Dict[str, Any] = {
                    "fingerprint": item.fingerprint,
                    "source_id": item.source_id,
                    "organization": item.organization,
                    "title": item.title,
                    "normalized_title": item.normalized_title,
                    "url": item.url,
                    "normalized_url": item.normalized_url,
                    "publication_date": item.publication_date,
                    "category": item.category,
                    "source_identifier": item.source_identifier,
                    "first_seen_at": utc_now_iso(),
                    "last_seen_at": utc_now_iso(),
                    "delivery_status": status_value,
                    "delivered_at": None,
                    "delivery_attempts": 0,
                    "last_delivery_error": None,
                }
                if item.application_date:
                    rec_entry["application_date"] = item.application_date
                if item.exam_date:
                    rec_entry["exam_date"] = item.exam_date
                if filter_reason:
                    rec_entry["filter_reason"] = filter_reason

                notices_store[item.fingerprint] = rec_entry
                if status_value == "baselined":
                    src_baselined_count += 1
                    summary.baselined_notices += 1
                else:
                    src_new_count += 1
                    summary.genuinely_new_notices += 1

            # Complete source baseline even if a validated notice board genuinely has 0 notices
            if source_needs_baseline and not src_state.get("baseline_completed", False):
                src_state["baseline_completed"] = True
                src_state["baseline_completed_at"] = utc_now_iso()
                logger.info(
                    "[%s] Baseline completed for %s: recorded %d existing notices.",
                    src.source_id,
                    src.organization,
                    len(discovered),
                )
            else:
                logger.info(
                    "[%s] Checked %s: discovered=%d new=%d baselined=%d",
                    src.source_id,
                    src.organization,
                    len(discovered),
                    src_new_count,
                    src_baselined_count,
                )

            if len(discovered) > 0:
                summary.websites_with_notices += 1
                test_outcome = "OK_WITH_NOTICES"
            else:
                summary.websites_with_no_notices += 1
                test_outcome = "NO_NOTICE_FOUND"
                logger.info(
                    "[%s] %s returned a recognized empty notice board (NO_NOTICE_FOUND).",
                    src.source_id,
                    src.organization,
                )

            # In live_test mode, send 1 [LIVE TEST] Telegram alert for the latest notice of this website
            latest_notice = select_latest_notice(discovered) if is_live_test else None
            if is_live_test and latest_notice is not None:
                latest_rec = _find_existing_notice_record(notices_store, latest_notice)
                if latest_rec is None:
                    latest_rec = notices_store[latest_notice.fingerprint]

                # Ensure normal delivery_status remains 'baselined' or 'delivered' (never 'pending' or 'failed')
                if latest_rec.get("delivery_status") not in ("baselined", "delivered"):
                    latest_rec["delivery_status"] = "baselined"

                latest_rec["test_delivery_attempts"] = int(latest_rec.get("test_delivery_attempts") or 0) + 1
                if notifier is None:
                    latest_rec["test_delivery_status"] = "failed"
                    latest_rec["last_test_error"] = "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"
                    summary.telegram_delivery_failures += 1
                    test_outcome = "LIVE_TEST_DELIVERY_FAILED"
                    logger.error(
                        "[%s] Cannot send [LIVE TEST] message for %s: TelegramNotifier not configured.",
                        src.source_id,
                        src.organization,
                    )
                else:
                    try:
                        _deliver_notice_with_notifier(notifier, latest_rec, is_live_test=True)
                        latest_rec["test_delivery_status"] = "delivered"
                        latest_rec["test_delivered_at"] = utc_now_iso()
                        latest_rec["last_test_error"] = None
                        summary.test_messages_delivered += 1
                        summary.telegram_deliveries_confirmed += 1
                        test_outcome = "LIVE_TEST_DELIVERED"
                        logger.info(
                            "[%s] Delivered [LIVE TEST] Telegram message for latest notice: %s (date=%s)",
                            src.source_id,
                            latest_rec.get("title", "")[:80],
                            latest_rec.get("publication_date") or "N/A",
                        )
                    except TelegramDeliveryError as exc:
                        latest_rec["test_delivery_status"] = "failed"
                        latest_rec["last_test_error"] = str(exc)
                        summary.telegram_delivery_failures += 1
                        test_outcome = "LIVE_TEST_DELIVERY_FAILED"
                        logger.error(
                            "[%s] Failed [LIVE TEST] Telegram delivery for %s: %s (notice remains baselined and will NOT be queued as normal alert).",
                            src.source_id,
                            latest_rec.get("title", "")[:80],
                            exc,
                        )

            summary.per_source_results[src.source_id] = {
                "status": "ok",
                "test_outcome": test_outcome,
                "error_category": None,
                "error": None,
                "discovered": len(discovered),
                "new": src_new_count,
                "baselined": src_baselined_count,
                "latest_title": latest_notice.title if latest_notice else (discovered[0].title if discovered else None),
                "latest_date": latest_notice.publication_date if latest_notice else (discovered[0].publication_date if discovered else None),
            }

        # Mark repository state initialized if at least one source succeeded or already initialized
        if summary.succeeded_websites > 0 or state.get("initialized", False):
            state["initialized"] = True

        # Step 2: Handle mode-specific Telegram deliveries
        if is_live_test:
            # Persist baselined state before sending final summary so state is safe on disk
            save_state_atomically(state, state_path)
            summary.state_persisted = True

            if notifier is not None and hasattr(notifier, "send_live_test_summary"):
                try:
                    notifier.send_live_test_summary(summary)
                    summary.summary_message_delivered = True
                    logger.info("Delivered final [LIVE TEST] summary message to Telegram.")
                except TelegramDeliveryError as exc:
                    summary.telegram_delivery_failures += 1
                    logger.error("Failed to deliver final [LIVE TEST] summary message: %s", exc)
        else:
            # First, expire any stale 'failed' records that exceeded retry count or age window
            for rec in notices_store.values():
                if rec.get("delivery_status") == "failed" and _is_failed_record_stale(rec):
                    rec["delivery_status"] = "baselined"
                    rec["last_delivery_error"] = "Suppressed stale failed notice (retry limit or age window exceeded)"

            # Validate any pre-existing pending/failed records against strict keyword & publication-date rules
            # and deduplicate across the delivery queue so retries or pre-existing state entries cannot send invalid/duplicate alerts.
            candidate_records = [
                rec
                for rec in notices_store.values()
                if rec.get("delivery_status") in ("pending", "failed")
            ]
            candidate_records.sort(key=lambda r: (r.get("first_seen_at") or "", r.get("fingerprint") or ""))

            # Collect already-delivered canonical keys per source to block cross-URL/cross-title duplicate sends
            delivered_canon_urls: Set[Tuple[str, str]] = set()
            delivered_canon_titles: Set[Tuple[str, str]] = set()
            delivered_source_ids: Set[Tuple[str, str]] = set()
            for rec in notices_store.values():
                if rec.get("delivery_status") == "delivered":
                    sid = str(rec.get("source_id") or "")
                    c_url = canonical_url_key(str(rec.get("normalized_url") or rec.get("url") or ""))
                    if _is_direct_document_url(str(rec.get("normalized_url") or "")) and c_url:
                        delivered_canon_urls.add((sid, c_url))
                    c_title = canonical_title_key(str(rec.get("title") or ""))
                    if len(c_title) >= 12:
                        delivered_canon_titles.add((sid, c_title))
                    s_ident = str(rec.get("source_identifier") or "").strip()
                    if s_ident:
                        delivered_source_ids.add((sid, s_ident))

            pending_records: List[Dict[str, Any]] = []
            for rec in candidate_records:
                if force_baseline:
                    rec["delivery_status"] = "baselined"
                    continue
                sid = str(rec.get("source_id") or "")
                rec_title = str(rec.get("title") or "")
                rec_pub_date = rec.get("publication_date")
                rec_norm_url = str(rec.get("normalized_url") or rec.get("url") or "")
                c_url = canonical_url_key(rec_norm_url)
                c_title = canonical_title_key(rec_title)
                s_ident = str(rec.get("source_identifier") or "").strip()

                # Check duplicate against already-delivered notices
                if (
                    (s_ident and (sid, s_ident) in delivered_source_ids)
                    or (_is_direct_document_url(rec_norm_url) and c_url and (sid, c_url) in delivered_canon_urls)
                    or (len(c_title) >= 12 and (sid, c_title) in delivered_canon_titles)
                ):
                    rec["delivery_status"] = "baselined"
                    rec["filter_reason"] = "DUPLICATE_OF_DELIVERED_NOTICE"
                    logger.info("[%s] Suppressed duplicate pending notice: %s", sid, rec_title[:80])
                    continue

                is_rel, matched_cat, matched_kw = match_notice_keywords(
                    rec_title,
                    rec_title,
                    categories=active_keywords,
                )
                d_valid, canon_date, d_reason = validate_publication_date(
                    rec_pub_date,
                    reference_date=reference_date,
                    max_age_days=max_age_days,
                    context_text=rec_title,
                )
                if not is_rel:
                    rec["delivery_status"] = "baselined"
                    rec["filter_reason"] = f"IRRELEVANT_KEYWORD:{matched_kw}"
                    summary.filtered_irrelevant_notices += 1
                    logger.warning("[%s] Demoted pending notice failing keyword filter (%s): %s", sid, matched_kw, rec_title[:80])
                    continue
                if not d_valid:
                    rec["delivery_status"] = "baselined"
                    rec["filter_reason"] = f"INVALID_PUBLICATION_DATE:{d_reason}"
                    summary.filtered_date_notices += 1
                    logger.warning("[%s] Demoted pending notice failing publication-date validation (%s): %s", sid, d_reason, rec_title[:80])
                    continue

                if canon_date:
                    rec["publication_date"] = canon_date
                if matched_cat and (not rec.get("category") or rec.get("category") == "Official Notice"):
                    rec["category"] = matched_cat

                # Reserve in delivered sets so two identical pending notices in the same batch don't both fire
                if s_ident:
                    delivered_source_ids.add((sid, s_ident))
                if _is_direct_document_url(rec_norm_url) and c_url:
                    delivered_canon_urls.add((sid, c_url))
                if len(c_title) >= 12:
                    delivered_canon_titles.add((sid, c_title))
                pending_records.append(rec)

            # Enforce maximum production alerts per run (default: 10)
            if max_alerts_per_run > 0 and len(pending_records) > max_alerts_per_run:
                summary.capped_pending_notices = len(pending_records) - max_alerts_per_run
                summary.capped_deferred_notices = summary.capped_pending_notices
                logger.info(
                    "Enforcing max_alerts_per_run=%d: delivering %d alerts now and deferring %d remaining pending notices.",
                    max_alerts_per_run,
                    max_alerts_per_run,
                    summary.capped_pending_notices,
                )
                pending_records = pending_records[:max_alerts_per_run]

            if pending_records and not force_baseline:
                if notifier is None:
                    logger.error(
                        "%d notices are pending Telegram delivery, but TelegramNotifier is not configured.",
                        len(pending_records),
                    )
                    summary.telegram_delivery_failures += len(pending_records)
                    for rec in pending_records:
                        rec["delivery_status"] = "failed"
                        rec["delivery_attempts"] = int(rec.get("delivery_attempts", 0)) + 1
                        rec["last_delivery_error"] = "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"
                else:
                    for rec in pending_records:
                        rec["delivery_attempts"] = int(rec.get("delivery_attempts", 0)) + 1
                        try:
                            _deliver_notice_with_notifier(notifier, rec, is_live_test=False)
                            rec["delivery_status"] = "delivered"
                            rec["delivered_at"] = utc_now_iso()
                            rec["last_delivery_error"] = None
                            summary.telegram_deliveries_confirmed += 1
                            logger.info(
                                "Delivered Telegram alert for [%s] %s",
                                rec.get("source_id"),
                                rec.get("title", "")[:80],
                            )
                        except TelegramDeliveryError as exc:
                            if getattr(exc, "ambiguous", False):
                                # Connection timed out after sending POST; Telegram may have accepted the message.
                                # Do not retry to prevent duplicate alerts.
                                rec["delivery_status"] = "delivered"
                                rec["delivered_at"] = utc_now_iso()
                                rec["last_delivery_error"] = str(exc)
                                summary.telegram_delivery_failures += 1
                                logger.warning(
                                    "Ambiguous post-send timeout for [%s] %s: %s (marked delivered to prevent duplicate resend).",
                                    rec.get("source_id"),
                                    rec.get("title", "")[:80],
                                    exc,
                                )
                            elif getattr(exc, "permanent", False) or int(rec.get("delivery_attempts", 0)) >= MAX_NOTICE_DELIVERY_ATTEMPTS:
                                rec["delivery_status"] = "baselined"
                                rec["last_delivery_error"] = str(exc)
                                summary.telegram_delivery_failures += 1
                                logger.error(
                                    "Permanent/exhausted Telegram delivery failure for [%s] %s: %s (suppressed from further retries).",
                                    rec.get("source_id"),
                                    rec.get("title", "")[:80],
                                    exc,
                                )
                            else:
                                rec["delivery_status"] = "failed"
                                rec["last_delivery_error"] = str(exc)
                                summary.telegram_delivery_failures += 1
                                logger.error(
                                    "Failed Telegram delivery for [%s] %s: %s",
                                    rec.get("source_id"),
                                    rec.get("title", "")[:80],
                                    exc,
                                )

            # Step 3: Persist state atomically
            save_state_atomically(state, state_path)
            summary.state_persisted = True

        return summary, state
    finally:
        if lock_file_handle is not None:
            try:
                import fcntl
                fcntl.flock(lock_file_handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                lock_file_handle.close()
            except Exception:
                pass


def print_run_summary(summary: RunSummary) -> None:
    """Prints the mandatory end-of-run summary to stdout (GitHub Actions logs only)."""
    lines = [
        "",
        "============================================================",
        f"     GOVERNMENT NOTICE MONITOR RUN SUMMARY ({summary.mode.upper()})",
        "============================================================",
        f"Total websites                         : {summary.total_websites}",
        f"Successful website fetches and parsing : {summary.succeeded_websites}",
        f"Failed websites                        : {summary.failed_websites}",
        f"Websites with notices found            : {summary.websites_with_notices}",
        f"Websites with no notices found         : {summary.websites_with_no_notices}",
        f"Websites requiring adapter review      : {summary.websites_requiring_review}",
        f"Number of notices discovered           : {summary.notices_discovered}",
        f"Number of baselined notices            : {summary.baselined_notices}",
        f"Filtered irrelevant notices            : {summary.filtered_irrelevant_notices}",
        f"Filtered missing/stale date notices    : {summary.filtered_date_notices}",
        f"Number of genuinely new notices        : {summary.genuinely_new_notices}",
        f"Test messages delivered successfully   : {summary.test_messages_delivered}",
        f"Telegram deliveries confirmed          : {summary.telegram_deliveries_confirmed}",
        f"Telegram delivery failures             : {summary.telegram_delivery_failures}",
        f"State persistence succeeded            : {summary.state_persisted}",
        "------------------------------------------------------------",
        "Per-Source Breakdown:",
    ]
    for src in SOURCES:
        res = summary.per_source_results.get(src.source_id, {})
        st = res.get("status", "skipped")
        outcome = res.get("test_outcome") or st.upper()
        if st == "ok":
            lines.append(
                f"  [OK]   {src.source_id:<13} | outcome={outcome:<20} "
                f"discovered={res.get('discovered', 0):<3} new={res.get('new', 0):<3} "
                f"baselined={res.get('baselined', 0):<3} | {src.organization}"
            )
        else:
            lines.append(
                f"  [FAIL] {src.source_id:<13} | outcome={outcome:<20} "
                f"{res.get('error_category', 'ERROR')} | {src.organization}"
            )
    lines.append("============================================================")
    print("\n".join(lines), flush=True)


# ============================================================================
# CLI Entry Point
# ============================================================================

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor 9 official Indian government notice boards and send Telegram alerts."
    )
    parser.add_argument(
        "--state-file",
        default=DEFAULT_STATE_PATH,
        help=f"Path to persistent JSON state file (default: {DEFAULT_STATE_PATH})",
    )
    parser.add_argument(
        "--mode",
        choices=("auto", "baseline", "monitor", "live_test"),
        default="auto",
        help=(
            "Execution mode: 'baseline' (record existing notices without alerts), "
            "'monitor' (alert on new notices), 'live_test' (send [LIVE TEST] for latest "
            "notice per site, baseline all discovered notices, and send test summary), "
            "or 'auto' (default)."
        ),
    )
    parser.add_argument(
        "--init-state",
        action="store_true",
        help="Initialize a clean state file if missing and exit (or combine with --mode baseline).",
    )
    parser.add_argument(
        "--merge-state",
        default="",
        help="Merge incoming JSON state file into --state-file losslessly without discarding fingerprints.",
    )
    parser.add_argument(
        "--allow-partial-source-failures",
        action="store_true",
        help="Exit 0 if at least one website succeeded and state was persisted, while still logging all failed sources clearly.",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=int(os.environ.get("MAX_NOTICE_AGE_DAYS", str(DEFAULT_MAX_NOTICE_AGE_DAYS))),
        help=f"Maximum publication age in days for a production alert (default: {DEFAULT_MAX_NOTICE_AGE_DAYS}).",
    )
    parser.add_argument(
        "--max-alerts-per-run",
        type=int,
        default=int(os.environ.get("MAX_ALERTS_PER_RUN", str(MAX_PRODUCTION_ALERTS_PER_RUN))),
        help=f"Maximum number of production Telegram notifications per run (default: {MAX_PRODUCTION_ALERTS_PER_RUN}).",
    )
    parser.add_argument(
        "--keywords-file",
        default=os.environ.get("NOTICE_KEYWORDS_FILE", ""),
        help="Optional path to JSON file overriding or extending keyword categories.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    logger, _ = configure_logging(secrets=(bot_token, chat_id))

    if args.merge_state:
        try:
            base_state = load_state(args.state_file, allow_init_if_missing=False)
            incoming_state = load_state(args.merge_state, allow_init_if_missing=False)
            merged = merge_states(base_state, incoming_state)
            save_state_atomically(merged, args.state_file)
            logger.info(
                "Successfully merged state from %s into %s (%d total notice fingerprints).",
                args.merge_state,
                args.state_file,
                len(merged["notices"]),
            )
            return 0
        except Exception as exc:
            logger.error("Failed to merge state files: %s", exc)
            return 2

    if args.init_state and not os.path.exists(args.state_file):
        initial_state = create_initial_state()
        save_state_atomically(initial_state, args.state_file)
        logger.info("Initialized clean state file at %s", args.state_file)
        if args.mode == "auto":
            return 0

    try:
        state_preview = load_state(args.state_file, allow_init_if_missing=args.init_state)
    except StateValidationError as exc:
        logger.error("CRITICAL STATE ERROR: %s", exc)
        return 2

    effective_mode = args.mode
    if effective_mode == "auto":
        effective_mode = "baseline" if not state_preview.get("initialized", False) else "monitor"

    notifier: Optional[TelegramNotifier] = None
    if effective_mode in ("monitor", "live_test"):
        if not bot_token or not chat_id:
            logger.error(
                "Missing required environment variables TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID for %s mode.",
                effective_mode,
            )
            return 3
        notifier = TelegramNotifier(bot_token=bot_token, chat_id=chat_id)
    elif bot_token and chat_id:
        notifier = TelegramNotifier(bot_token=bot_token, chat_id=chat_id)

    keyword_categories = load_keyword_categories(config_path=args.keywords_file or None)

    try:
        summary, _ = run_monitor(
            state_path=args.state_file,
            mode=effective_mode,
            sources=SOURCES,
            notifier=notifier,
            logger=logger,
            allow_init_if_missing=args.init_state,
            max_age_days=args.max_age_days,
            max_alerts_per_run=args.max_alerts_per_run,
            keyword_categories=keyword_categories,
        )
    except StateValidationError as exc:
        logger.error("State validation/persistence failed: %s", exc)
        return 2
    except Exception as exc:
        logger.exception("Unhandled fatal error during monitor run: %s", exc)
        return 1

    print_run_summary(summary)

    if not summary.state_persisted:
        logger.error("State persistence did not complete.")
        return 2

    if summary.telegram_delivery_failures > 0:
        logger.error(
            "%d Telegram alert deliveries failed.",
            summary.telegram_delivery_failures,
        )
        return 4

    if summary.succeeded_websites == 0:
        logger.error("All %d configured websites failed during this run.", summary.total_websites)
        return 5

    if summary.failed_websites > 0:
        logger.warning(
            "Partial website failures occurred: %d/%d websites failed (see summary above).",
            summary.failed_websites,
            summary.total_websites,
        )
        if not args.allow_partial_source_failures:
            return 6

    return 0


if __name__ == "__main__":
    sys.exit(main())
