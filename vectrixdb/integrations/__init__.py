"""
VectrixDB Integrations.

Optional integrations with external services.
Users can implement their own RRF fusion with external search systems.

Example (user-side RRF with any external system):

    from vectrixdb import V

    # Query VectrixDB
    db = V("my_data")
    vectrix_results = db.search("query", mode="ultimate")

    # Query your external system (Azure, Elasticsearch, etc.)
    external_results = your_external_search("query")

    # Simple RRF fusion
    def rrf_fusion(results_list, k=60):
        scores = {}
        for results in results_list:
            for rank, r in enumerate(results):
                rid = r.id if hasattr(r, 'id') else r['id']
                scores[rid] = scores.get(rid, 0) + 1 / (k + rank + 1)
        return sorted(scores.items(), key=lambda x: -x[1])

    final = rrf_fusion([vectrix_results, external_results])
"""

__all__ = []
