"""
SRU 1.2 (Search/Retrieve via URL) -- the reusable brick behind Gallica
and most national-library search interfaces.

SRU returns records in a `recordData` wrapper whose payload is usually
Dublin Core. This module handles the envelope and the pagination
arithmetic; the Dublin Core inside is parsed by the same namespace-
agnostic helpers the OAI-PMH adapter uses, since it is the same
vocabulary.

Pagination in SRU is 1-based `startRecord` plus `maximumRecords`, and
the response reports `numberOfRecords`. Getting the off-by-one wrong
either skips the first record of every page or re-fetches it, which is
why the arithmetic lives here once rather than in each consumer.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from ..security import find_local, local_name, safe_parse_xml

logger = logging.getLogger(__name__)


def build_sru_url(
    base_url: str,
    query: str,
    start_record: int = 1,
    maximum_records: int = 50,
    version: str = "1.2",
    schema: str = "",
) -> str:
    params = {
        "operation": "searchRetrieve",
        "version": version,
        "query": query,
        "startRecord": str(max(1, start_record)),
        "maximumRecords": str(maximum_records),
    }
    if schema:
        params["recordSchema"] = schema
    return f"{base_url}?{urlencode(params)}"


def parse_sru_envelope(xml_bytes: bytes) -> tuple[list, int, int]:
    """
    Returns (record_data_elements, number_of_records, next_record_position).

    `nextRecordPosition` is authoritative when the server provides it;
    otherwise it is computed. A server that returns fewer records than
    requested without a next position has finished, and returning 0
    signals that.
    """
    root = safe_parse_xml(xml_bytes)

    diagnostics = find_local(root, "diagnostic")
    if diagnostics is not None:
        message = find_local(diagnostics, "message")
        detail = find_local(diagnostics, "details")
        text = " / ".join(
            part.text.strip() for part in (message, detail) if part is not None and part.text
        )
        raise RuntimeError(f"SRU diagnostic: {text or 'unspecified'}")

    total_el = find_local(root, "numberOfRecords")
    total_text = ((total_el.text or "") if total_el is not None else "").strip()
    total = int(total_text) if total_text.isdigit() else 0

    records = [el for el in root.iter() if local_name(el.tag) == "recordData"]

    next_el = find_local(root, "nextRecordPosition")
    next_text = ((next_el.text or "") if next_el is not None else "").strip()
    next_position = int(next_text) if next_text.isdigit() else 0

    return records, total, next_position


def compute_next_start(start_record: int, page_size: int, returned: int, total: int, reported_next: int) -> int:
    """
    Where the next page begins, or 0 when the result set is exhausted.

    The server's `nextRecordPosition` wins when present and sane. Without
    it: a short page means the end, and a full page advances by exactly
    the page size.
    """
    if reported_next > 0:
        return reported_next if (total == 0 or reported_next <= total) else 0
    if returned < page_size:
        return 0
    next_start = start_record + page_size
    if total and next_start > total:
        return 0
    return next_start


__all__ = ["build_sru_url", "compute_next_start", "parse_sru_envelope"]
