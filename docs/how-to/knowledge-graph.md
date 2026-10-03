# Work with the knowledge graph

Graph mode extracts entities and relationships from what you add, groups them into communities, and uses both at search time. This page is the part of that you steer: what counts as the same entity, what happens when a fact changes, how much work an add does, and the two questions you can ask the graph directly.

## Entity types have rules

Every entity type has a schema: how close two names must be to merge, whether a shorter name may fold into a longer one, and whether an entity may absorb a same-named entity of another type.

```python
from vectrixdb import Vectrix
from vectrixdb.core.graphrag.config import EntitySchema, GraphRAGConfig, default_entity_schemas

schemas = default_entity_schemas()
schemas["PERSON"].threshold = 0.8                      # looser typo tolerance for names
schemas["PRODUCT"] = EntitySchema("PRODUCT", threshold=0.95, allow_subset=False)

config = GraphRAGConfig(enabled=True, entity_schemas=schemas)
db = Vectrix("kb", mode="graph", graphrag_config=config)
```

The defaults encode what usually holds: people merge on initials and surnames ("M. Curie" is "Marie Curie"), organizations merge on subsets, and concepts, locations, events and products merge only on near-exact names, so "Learning" is not "Machine Learning" and "Paris" the city is not "Paris Agreement". Types never merge across each other unless a schema says `merge_across_types=True`, so "Apple" the company and "Apple" the fruit stay two nodes. Without schemas the graph behaves as before: one rule for every type.

## Facts change: supersede, do not delete

When a relationship stops being true, the old edge is not deleted. It is marked superseded by the new one, stamped with when, and kept for the record:

```python
pipeline = db.graph
old = next(r for r in pipeline.graph.get_all_relationships() if r.type == "WORKS_FOR").id
from vectrixdb.core.graphrag.extractor.base import Relationship
new = pipeline.graph.add_relationship(
    Relationship.create(source_id, new_employer_id, "WORKS_FOR"), supersedes=old
)
pipeline.supersede(old, new)   # persists; add_relationship(supersedes=) already marked it
```

A superseded edge drops out of traversal, search and community detection. `get_all_relationships(include_superseded=True)` and `graph_explain()` still show it, with `valid_from`, `valid_to` and `superseded_by`. The dashboard draws it dashed.

## Adds only redo what changed

Community detection and summarisation used to run over the whole graph after every add. Now a batch re-detects only the connected components it touched: the communities elsewhere keep their ids and their summaries, so under an LLM extractor an add costs one summary per changed community rather than one per community. The stats on an add say how many were recomputed and how many kept. `GraphRAGConfig(incremental_communities=False)` restores the full re-detection.

## Ask the graph

```python
db.graph_path("Marie Curie", "Nobel Prize")
# {'found': True, 'entities': ['Marie Curie', 'Pierre Curie', 'Nobel Prize'],
#  'relationships': [{'type': 'MARRIED_TO', ...}, {'type': 'WON', ...}], 'depth': 2}

db.graph_explain("Marie Curie")
# entity, its communities with summaries, its relationships strongest first, and the superseded ones

db.graph_explain("Marie Curie", "Nobel Prize")
# plus: direct relationships between the two, shared neighbours, whether they share a community, the path
```

Names resolve through the graph's own matching, so "Curie" finds "Marie Curie"; an unknown name comes back as `found: False` with `missing` naming it, which is different from `found: False` with no path.

## See it

The dashboard's Graph tab draws the collection's graph with a force-directed layout. Nodes are coloured by their community when the graph has communities, by entity type otherwise, and superseded relationships are dashed. `vectrixdb serve` and open the collection.

**Where the graph is kept.** Extracting writes one object a collection, `<collection>/graph.json`, to the graph store: `VECTRIXDB_GRAPH_STORE`, a folder, `s3://bucket/prefix` or a Blob address, the same three addresses evaluation runs take, and `<path>/graph` beside the server when unset. Every instance reads the same object, so a graph extracted on one server shows on all of them. The object records the index build the chunks carried when it was read; when the collection has been written to since, the tab says the graph is older than the index, how many builds and chunks came after it, and its button reads **Extract again**. Deleting the collection deletes its graph. A collection not made for graph search has no graph and the tab says so; nothing here applies to it.
