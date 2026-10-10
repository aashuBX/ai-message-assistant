"""Message triage service.

Library entry point for embedding in a queue worker or HTTP handler:

    from triage import triage_records
    results = asyncio.run(triage_records(records, classifier))
"""

from .pipeline import triage_message, triage_records

__all__ = ["triage_message", "triage_records"]
