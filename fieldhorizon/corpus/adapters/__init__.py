"""
Source adapters.

Each module turns one institution's official catalogue interface into
`Candidate` objects. Import `registry` to have every adapter register
itself; importing an individual adapter module directly also works and
registers only that one.

There is deliberately no generic web-scraper adapter. A source that
cannot be reached through an official, documented interface is not added
here -- it is recorded as a disabled candidate source instead.
"""

from __future__ import annotations
