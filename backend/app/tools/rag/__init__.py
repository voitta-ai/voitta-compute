"""Local hybrid RAG (Chroma + bm25s) over the ``docs/`` corpus.

Public entry points re-exported here so callers don't need to know
about the index/search split:

* ``query(query_text, top_k, dense_weight)`` — hybrid search returning
  ranked chunk records.
* ``get_range(file, first, last, max_bytes)`` — stitch contiguous chunks
  of one file (overlap de-duplicated).
* ``index_status()`` — diagnostic for ``/health``-style routes.
* ``RagNotBuilt`` — raised when ``scripts/build_rag.py`` hasn't been
  run yet.
"""

from app.tools.rag.index import (
    CORPORA,
    DEFAULT_CORPUS,
    RagNotBuilt,
    UnknownCorpus,
    index_status,
)
from app.tools.rag.search import get_range, query

__all__ = [
    "CORPORA",
    "DEFAULT_CORPUS",
    "RagNotBuilt",
    "UnknownCorpus",
    "get_range",
    "index_status",
    "query",
]
