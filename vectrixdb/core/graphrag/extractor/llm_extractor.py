"""
LLM-based Entity Extractor for GraphRAG.

Uses Large Language Models for high-quality entity and relationship extraction.
Supports multiple providers: OpenAI, Ollama, AWS Bedrock, Azure OpenAI.
"""

import os
import json
import time
import re
from typing import List, Dict, Optional, Any, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from .base import (
    BaseExtractor,
    Entity,
    Relationship,
    ExtractionResult,
    EntityType,
    RelationshipType,
)
from ..chunker import TextUnit
from ..config import GraphRAGConfig, LLMProvider


class LLMExtractor(BaseExtractor):
    """
    High-quality entity extraction using LLM.

    Supports multiple LLM providers:
    - OpenAI (gpt-4o-mini, gpt-4o, gpt-4-turbo)
    - Ollama (llama3.2, mistral, phi3)
    - AWS Bedrock (claude-3, titan, llama)
    - Azure OpenAI

    Example:
        >>> extractor = LLMExtractor(provider="openai", model="gpt-4o-mini")
        >>> result = extractor.extract_single("Apple Inc. was founded by Steve Jobs in 1976.")
    """

    EXTRACTION_PROMPT = """Extract all entities and relationships from the following text.

For each ENTITY, provide:
- name: The exact name as it appears in text
- type: One of [PERSON, ORGANIZATION, LOCATION, CONCEPT, EVENT, OBJECT, TECHNOLOGY, PRODUCT]
- description: Brief description based on the context (1 sentence)

For each RELATIONSHIP between entities, provide:
- source: Source entity name (must match an entity name above)
- target: Target entity name (must match an entity name above)
- type: One of [RELATED_TO, WORKS_FOR, LOCATED_IN, PART_OF, CREATED_BY, USED_BY, CAUSED_BY, MENTIONS]
- description: How they are related (1 sentence)
- strength: 0.0 to 1.0 indicating importance (1.0 = central to text, 0.3 = minor mention)

Return ONLY valid JSON in this exact format:
{{
    "entities": [
        {{"name": "...", "type": "...", "description": "..."}}
    ],
    "relationships": [
        {{"source": "...", "target": "...", "type": "...", "description": "...", "strength": 0.8}}
    ]
}}

TEXT:
{text}

JSON:"""

    def __init__(
        self,
        provider: str = "openai",
        model: str = "gpt-4o-mini",
        api_key: Optional[str] = None,
        endpoint: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        config: Optional[GraphRAGConfig] = None,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ):
        """
        Initialize the LLM extractor.

        Args:
            provider: LLM provider (openai, ollama, aws_bedrock, azure_openai).
            model: Model name.
            api_key: API key (uses environment variable if not provided).
            endpoint: Custom endpoint URL.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens in response.
            config: Optional GraphRAGConfig for settings.
            max_retries: Maximum retry attempts on failure.
            retry_delay: Delay between retries in seconds.
        """
        if config:
            self.provider = config.llm_provider.value if isinstance(config.llm_provider, LLMProvider) else config.llm_provider
            self.model = config.llm_model
            self.api_key = config.llm_api_key
            self.endpoint = config.llm_endpoint
            self.temperature = config.llm_temperature
            self.max_tokens = config.llm_max_tokens
        else:
            self.provider = provider
            self.model = model
            self.api_key = api_key
            self.endpoint = endpoint
            self.temperature = temperature
            self.max_tokens = max_tokens

        self.max_retries = max_retries
        self.retry_delay = retry_delay

        # Initialize the appropriate client
        self._client = None
        self._init_client()

    def _init_client(self):
        """Initialize the LLM client based on provider."""
        if self.provider == "openai":
            self._init_openai()
        elif self.provider == "ollama":
            self._init_ollama()
        elif self.provider == "aws_bedrock":
            self._init_bedrock()
        elif self.provider == "azure_openai":
            self._init_azure()
        else:
            raise ValueError(f"Unsupported provider: {self.provider}")

    def _init_openai(self):
        """Initialize OpenAI client."""
        try:
            from openai import OpenAI
            api_key = self.api_key or os.environ.get("OPENAI_API_KEY")
            if not api_key:
                raise ValueError("OpenAI API key not found. Set OPENAI_API_KEY environment variable or pass api_key.")
            self._client = OpenAI(api_key=api_key)
            self._call_fn = self._call_openai
        except ImportError:
            raise ImportError("openai package not installed. Run: pip install openai")

    def _init_ollama(self):
        """Initialize Ollama client."""
        try:
            import ollama
            self._client = ollama
            self._endpoint = self.endpoint or "http://localhost:11434"
            self._call_fn = self._call_ollama
        except ImportError:
            # Fallback to requests
            import requests
            self._client = requests
            self._endpoint = self.endpoint or "http://localhost:11434"
            self._call_fn = self._call_ollama_requests

    def _init_bedrock(self):
        """Initialize AWS Bedrock client."""
        try:
            import boto3
            self._client = boto3.client('bedrock-runtime')
            self._call_fn = self._call_bedrock
        except ImportError:
            raise ImportError("boto3 package not installed. Run: pip install boto3")

    def _init_azure(self):
        """Initialize Azure OpenAI client."""
        try:
            from openai import AzureOpenAI
            api_key = self.api_key or os.environ.get("AZURE_OPENAI_API_KEY")
            if not api_key:
                raise ValueError("Azure OpenAI API key not found.")
            if not self.endpoint:
                raise ValueError("Azure OpenAI endpoint required.")
            self._client = AzureOpenAI(
                api_key=api_key,
                api_version="2024-02-15-preview",
                azure_endpoint=self.endpoint
            )
            self._call_fn = self._call_azure
        except ImportError:
            raise ImportError("openai package not installed. Run: pip install openai")

    def _call_openai(self, prompt: str) -> str:
        """Call OpenAI API."""
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You are an expert at extracting entities and relationships from text. Always respond with valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        return response.choices[0].message.content

    def _call_ollama(self, prompt: str) -> str:
        """Call Ollama API."""
        response = self._client.generate(
            model=self.model,
            prompt=prompt,
            options={"temperature": self.temperature}
        )
        return response['response']

    def _call_ollama_requests(self, prompt: str) -> str:
        """Call Ollama API using requests."""
        response = self._client.post(
            f"{self._endpoint}/api/generate",
            json={
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": self.temperature}
            }
        )
        response.raise_for_status()
        return response.json()['response']

    def _call_bedrock(self, prompt: str) -> str:
        """Call AWS Bedrock API."""
        import json
        body = json.dumps({
            "prompt": prompt,
            "max_tokens_to_sample": self.max_tokens,
            "temperature": self.temperature,
        })
        response = self._client.invoke_model(
            modelId=self.model,
            body=body,
            contentType="application/json",
            accept="application/json"
        )
        response_body = json.loads(response['body'].read())
        return response_body.get('completion', '')

    def _call_azure(self, prompt: str) -> str:
        """Call Azure OpenAI API."""
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You are an expert at extracting entities and relationships from text. Always respond with valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        return response.choices[0].message.content

    def _call_llm(self, prompt: str) -> str:
        """Call the LLM with retries."""
        last_error = None
        for attempt in range(self.max_retries):
            try:
                return self._call_fn(prompt)
            except Exception as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (attempt + 1))
        raise last_error

    def _parse_json_response(self, response: str) -> Dict[str, Any]:
        """Parse JSON from LLM response."""
        # Try to find JSON in the response
        response = response.strip()

        # Try direct parse first
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            pass

        # Try to extract JSON from markdown code blocks
        json_patterns = [
            r'```json\s*(.*?)\s*```',
            r'```\s*(.*?)\s*```',
            r'\{[\s\S]*\}'
        ]

        for pattern in json_patterns:
            matches = re.findall(pattern, response, re.DOTALL)
            for match in matches:
                try:
                    return json.loads(match)
                except json.JSONDecodeError:
                    continue

        # Return empty result if parsing fails
        return {"entities": [], "relationships": []}

    def _convert_to_entities(
        self,
        raw_entities: List[Dict],
        text_unit_id: str
    ) -> List[Entity]:
        """Convert raw entity dicts to Entity objects."""
        entities = []
        for raw in raw_entities:
            name = raw.get("name", "").strip()
            if not name:
                continue

            entity_type = raw.get("type", "OTHER").upper()
            if entity_type not in [e.value for e in EntityType]:
                entity_type = EntityType.OTHER.value

            entity = Entity.create(
                name=name,
                entity_type=entity_type,
                description=raw.get("description", ""),
                source_unit_id=text_unit_id
            )
            entities.append(entity)

        return entities

    def _convert_to_relationships(
        self,
        raw_relationships: List[Dict],
        entities: List[Entity],
        text_unit_id: str
    ) -> List[Relationship]:
        """Convert raw relationship dicts to Relationship objects."""
        # Build entity name lookup
        name_to_entity = {e.name.lower(): e for e in entities}

        relationships = []
        for raw in raw_relationships:
            source_name = raw.get("source", "").strip().lower()
            target_name = raw.get("target", "").strip().lower()

            # Find matching entities
            source_entity = name_to_entity.get(source_name)
            target_entity = name_to_entity.get(target_name)

            if not source_entity or not target_entity:
                continue

            rel_type = raw.get("type", "RELATED_TO").upper()
            if rel_type not in [r.value for r in RelationshipType]:
                rel_type = RelationshipType.RELATED_TO.value

            strength = raw.get("strength", 0.5)
            if not isinstance(strength, (int, float)):
                strength = 0.5
            strength = max(0.0, min(1.0, float(strength)))

            relationship = Relationship.create(
                source_id=source_entity.id,
                target_id=target_entity.id,
                rel_type=rel_type,
                description=raw.get("description", ""),
                strength=strength,
                source_unit_id=text_unit_id
            )
            relationships.append(relationship)

        return relationships

    def extract_single(self, text: str, doc_id: str = "default") -> ExtractionResult:
        """Extract entities and relationships from a single text."""
        text_unit_id = f"{doc_id}_0"

        # Build prompt
        prompt = self.EXTRACTION_PROMPT.format(text=text)

        # Call LLM
        try:
            response = self._call_llm(prompt)
            parsed = self._parse_json_response(response)
        except Exception as e:
            # On error, return empty result
            return ExtractionResult(
                entities=[],
                relationships=[],
                source_units=[text_unit_id],
                metadata={"extractor": "llm", "error": str(e)}
            )

        # Convert to Entity and Relationship objects
        entities = self._convert_to_entities(
            parsed.get("entities", []),
            text_unit_id
        )
        relationships = self._convert_to_relationships(
            parsed.get("relationships", []),
            entities,
            text_unit_id
        )

        return ExtractionResult(
            entities=entities,
            relationships=relationships,
            source_units=[text_unit_id],
            metadata={"extractor": "llm", "provider": self.provider, "model": self.model}
        )

    def extract(self, text_units: List[TextUnit]) -> ExtractionResult:
        """Extract entities and relationships from multiple text units."""
        if not text_units:
            return ExtractionResult()

        combined_result = ExtractionResult()

        for unit in text_units:
            # Build prompt for this unit
            prompt = self.EXTRACTION_PROMPT.format(text=unit.text)

            try:
                response = self._call_llm(prompt)
                parsed = self._parse_json_response(response)

                entities = self._convert_to_entities(
                    parsed.get("entities", []),
                    unit.id
                )
                relationships = self._convert_to_relationships(
                    parsed.get("relationships", []),
                    entities,
                    unit.id
                )

                unit_result = ExtractionResult(
                    entities=entities,
                    relationships=relationships,
                    source_units=[unit.id]
                )

                combined_result = combined_result.merge_with(unit_result)

            except Exception as e:
                # Continue on error, just skip this unit
                combined_result.source_units.append(unit.id)
                continue

        combined_result.metadata = {
            "extractor": "llm",
            "provider": self.provider,
            "model": self.model
        }

        return combined_result

    def extract_batch(
        self,
        text_units: List[TextUnit],
        batch_size: int = 10,
        max_workers: int = 4
    ) -> ExtractionResult:
        """Extract from text units in parallel batches."""
        if not text_units:
            return ExtractionResult()

        combined_result = ExtractionResult()

        # Process in parallel with ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self._extract_single_unit, unit): unit
                for unit in text_units
            }

            for future in as_completed(futures):
                try:
                    unit_result = future.result()
                    combined_result = combined_result.merge_with(unit_result)
                except Exception as e:
                    unit = futures[future]
                    combined_result.source_units.append(unit.id)

        combined_result.metadata = {
            "extractor": "llm",
            "provider": self.provider,
            "model": self.model
        }

        return combined_result

    def _extract_single_unit(self, unit: TextUnit) -> ExtractionResult:
        """Extract from a single text unit (for parallel processing)."""
        prompt = self.EXTRACTION_PROMPT.format(text=unit.text)

        response = self._call_llm(prompt)
        parsed = self._parse_json_response(response)

        entities = self._convert_to_entities(
            parsed.get("entities", []),
            unit.id
        )
        relationships = self._convert_to_relationships(
            parsed.get("relationships", []),
            entities,
            unit.id
        )

        return ExtractionResult(
            entities=entities,
            relationships=relationships,
            source_units=[unit.id]
        )


def create_llm_extractor(config: Optional[GraphRAGConfig] = None) -> LLMExtractor:
    """Factory function to create an LLM extractor."""
    if config:
        return LLMExtractor(config=config)
    return LLMExtractor()
