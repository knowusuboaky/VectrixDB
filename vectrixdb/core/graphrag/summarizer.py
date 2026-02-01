"""
Community Summarization for VectrixDB GraphRAG.

Generates summaries for communities using LLM or template-based approaches.
Summaries are generated bottom-up: Level 0 first, then aggregated for higher levels.
"""

import os
from typing import List, Optional, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

from .graph.knowledge_graph import KnowledgeGraph
from .graph.community import Community, CommunityHierarchy
from .config import GraphRAGConfig, LLMProvider


class CommunitySummarizer:
    """
    Generate summaries for communities in the knowledge graph.

    Strategies:
    - LLM-based: High-quality summaries using GPT/Claude/Llama
    - Template-based: Fast, free summaries using templates

    Summaries are generated bottom-up:
    1. Level 0: Summarize from entities and relationships
    2. Level 1+: Aggregate child community summaries
    """

    SUMMARY_PROMPT = """Summarize the following entities and their relationships into a coherent description.
Focus on the main themes, key connections, and important insights.

Entities:
{entities}

Relationships:
{relationships}

Write a clear, concise summary (2-3 sentences) that captures the essence of this community:"""

    AGGREGATE_PROMPT = """Synthesize the following community summaries into a higher-level overview.
Identify common themes and overarching patterns.

Community summaries:
{summaries}

Write a unified summary (2-3 sentences) that captures the broader theme:"""

    def __init__(
        self,
        config: Optional[GraphRAGConfig] = None,
        use_llm: bool = True,
        max_entities_per_summary: int = 20,
        max_relationships_per_summary: int = 30,
    ):
        """
        Initialize the summarizer.

        Args:
            config: GraphRAGConfig for LLM settings.
            use_llm: Whether to use LLM for summaries.
            max_entities_per_summary: Max entities to include in prompt.
            max_relationships_per_summary: Max relationships to include.
        """
        self.config = config or GraphRAGConfig()
        self.use_llm = use_llm
        self.max_entities = max_entities_per_summary
        self.max_relationships = max_relationships_per_summary

        self._llm_client = None
        if use_llm:
            self._init_llm()

    def _init_llm(self):
        """Initialize LLM client."""
        provider = self.config.llm_provider
        if isinstance(provider, LLMProvider):
            provider = provider.value

        if provider == "openai":
            self._init_openai()
        elif provider == "ollama":
            self._init_ollama()
        else:
            self.use_llm = False

    def _init_openai(self):
        """Initialize OpenAI client."""
        try:
            from openai import OpenAI
            api_key = self.config.llm_api_key or os.environ.get("OPENAI_API_KEY")
            if api_key:
                self._llm_client = OpenAI(api_key=api_key)
                self._call_llm = self._call_openai
            else:
                self.use_llm = False
        except ImportError:
            self.use_llm = False

    def _init_ollama(self):
        """Initialize Ollama client."""
        try:
            import ollama
            self._llm_client = ollama
            self._call_llm = self._call_ollama
        except ImportError:
            try:
                import requests
                self._llm_client = requests
                self._endpoint = self.config.llm_endpoint or "http://localhost:11434"
                self._call_llm = self._call_ollama_requests
            except Exception:
                self.use_llm = False

    def _call_openai(self, prompt: str) -> str:
        """Call OpenAI API."""
        response = self._llm_client.chat.completions.create(
            model=self.config.llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=300,
        )
        return response.choices[0].message.content.strip()

    def _call_ollama(self, prompt: str) -> str:
        """Call Ollama API."""
        response = self._llm_client.generate(
            model=self.config.llm_model,
            prompt=prompt,
            options={"temperature": 0.3}
        )
        return response['response'].strip()

    def _call_ollama_requests(self, prompt: str) -> str:
        """Call Ollama via requests."""
        response = self._llm_client.post(
            f"{self._endpoint}/api/generate",
            json={
                "model": self.config.llm_model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.3}
            }
        )
        response.raise_for_status()
        return response.json()['response'].strip()

    def _format_entities_for_prompt(
        self,
        graph: KnowledgeGraph,
        entity_ids: List[str]
    ) -> str:
        """Format entities for the prompt."""
        lines = []
        for eid in entity_ids[:self.max_entities]:
            entity = graph.get_entity(eid)
            if entity:
                lines.append(f"- {entity.name} ({entity.type}): {entity.description}")
        return "\n".join(lines) if lines else "No entities"

    def _format_relationships_for_prompt(
        self,
        graph: KnowledgeGraph,
        entity_ids: List[str]
    ) -> str:
        """Format relationships for the prompt."""
        entity_set = set(entity_ids)
        lines = []

        for eid in entity_ids:
            for rel in graph.get_relationships_for_entity(eid):
                if len(lines) >= self.max_relationships:
                    break
                if rel.source_id in entity_set and rel.target_id in entity_set:
                    source = graph.get_entity(rel.source_id)
                    target = graph.get_entity(rel.target_id)
                    if source and target:
                        lines.append(
                            f"- {source.name} --[{rel.type}]--> {target.name}: {rel.description}"
                        )

        return "\n".join(lines) if lines else "No relationships"

    def _generate_template_summary(
        self,
        graph: KnowledgeGraph,
        entity_ids: List[str]
    ) -> str:
        """Generate a template-based summary (no LLM)."""
        entities = [graph.get_entity(eid) for eid in entity_ids if graph.get_entity(eid)]

        if not entities:
            return "Empty community"

        # Group by type
        type_counts: Dict[str, int] = {}
        names: List[str] = []
        for entity in entities[:10]:
            type_counts[entity.type] = type_counts.get(entity.type, 0) + 1
            names.append(entity.name)

        # Build summary
        type_summary = ", ".join(f"{count} {t}s" for t, count in sorted(type_counts.items(), key=lambda x: -x[1]))
        name_sample = ", ".join(names[:5])
        if len(names) > 5:
            name_sample += f", and {len(names) - 5} more"

        return f"Community of {len(entities)} entities ({type_summary}) including {name_sample}."

    def summarize_community(
        self,
        graph: KnowledgeGraph,
        community: Community,
        hierarchy: Optional[CommunityHierarchy] = None
    ) -> str:
        """
        Generate a summary for a single community.

        Args:
            graph: The knowledge graph.
            community: The community to summarize.
            hierarchy: Optional hierarchy for aggregating child summaries.

        Returns:
            Summary string.
        """
        # For Level 0, summarize from entities
        if community.level == 0 or not hierarchy:
            return self._summarize_from_entities(graph, community)

        # For higher levels, aggregate child summaries
        return self._summarize_from_children(community, hierarchy)

    def _summarize_from_entities(
        self,
        graph: KnowledgeGraph,
        community: Community
    ) -> str:
        """Summarize from entities and relationships."""
        if not self.use_llm or not self._llm_client:
            return self._generate_template_summary(graph, community.entity_ids)

        try:
            entities_text = self._format_entities_for_prompt(graph, community.entity_ids)
            relationships_text = self._format_relationships_for_prompt(graph, community.entity_ids)

            prompt = self.SUMMARY_PROMPT.format(
                entities=entities_text,
                relationships=relationships_text
            )

            return self._call_llm(prompt)
        except Exception as e:
            # Fallback to template
            return self._generate_template_summary(graph, community.entity_ids)

    def _summarize_from_children(
        self,
        community: Community,
        hierarchy: CommunityHierarchy
    ) -> str:
        """Summarize by aggregating child community summaries."""
        # Get child summaries
        child_summaries = []
        for child_id in community.children_ids:
            child = hierarchy.get_community(child_id)
            if child and child.summary:
                child_summaries.append(child.summary)

        if not child_summaries:
            return f"High-level community containing {len(community.entity_ids)} entities."

        if not self.use_llm or not self._llm_client:
            # Template aggregation
            return f"Aggregated community ({len(child_summaries)} subcommunities): " + " ".join(child_summaries[:3])

        try:
            summaries_text = "\n".join(f"- {s}" for s in child_summaries)
            prompt = self.AGGREGATE_PROMPT.format(summaries=summaries_text)
            return self._call_llm(prompt)
        except Exception:
            return f"Aggregated community ({len(child_summaries)} subcommunities)."

    def summarize_hierarchy(
        self,
        graph: KnowledgeGraph,
        hierarchy: CommunityHierarchy,
        max_workers: int = 4
    ) -> CommunityHierarchy:
        """
        Generate summaries for all communities in the hierarchy.

        Processes bottom-up: Level 0 first, then higher levels.

        Args:
            graph: The knowledge graph.
            hierarchy: The community hierarchy.
            max_workers: Parallel workers for summarization.

        Returns:
            The hierarchy with summaries populated.
        """
        # Process levels in order (bottom-up)
        for level in sorted(hierarchy.levels.keys()):
            communities = hierarchy.get_level(level)

            if max_workers > 1 and len(communities) > 1:
                # Parallel summarization
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {
                        executor.submit(
                            self.summarize_community, graph, c, hierarchy
                        ): c
                        for c in communities
                    }

                    for future in as_completed(futures):
                        community = futures[future]
                        try:
                            community.summary = future.result()
                        except Exception:
                            community.summary = f"Community with {len(community.entity_ids)} entities."
            else:
                # Sequential summarization
                for community in communities:
                    try:
                        community.summary = self.summarize_community(graph, community, hierarchy)
                    except Exception:
                        community.summary = f"Community with {len(community.entity_ids)} entities."

        return hierarchy


def summarize_communities(
    graph: KnowledgeGraph,
    hierarchy: CommunityHierarchy,
    config: Optional[GraphRAGConfig] = None,
    use_llm: bool = True
) -> CommunityHierarchy:
    """
    Factory function to summarize all communities.

    Args:
        graph: Knowledge graph.
        hierarchy: Community hierarchy.
        config: Optional GraphRAG config.
        use_llm: Whether to use LLM for summaries.

    Returns:
        Hierarchy with summaries populated.
    """
    summarizer = CommunitySummarizer(config=config, use_llm=use_llm)
    return summarizer.summarize_hierarchy(graph, hierarchy)
