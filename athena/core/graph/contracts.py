"""Typed values exchanged between graph extraction and storage adapters."""

from __future__ import annotations

from pydantic import BaseModel, Field


class GraphChunk(BaseModel):
    id: str
    ordinal: int
    locator: dict[str, object] = Field(default_factory=dict)
    document_version: int


class GraphEntity(BaseModel):
    entity_key: str
    canonical_name: str
    entity_type: str
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    confidence: float = Field(default=1.0, ge=0, le=1)


class GraphEntityMention(BaseModel):
    entity_key: str
    chunk_id: str
    surface: str
    confidence: float = Field(default=1.0, ge=0, le=1)


class GraphRelation(BaseModel):
    source_entity_key: str
    target_entity_key: str
    relation_type: str
    evidence: str = ""
    evidence_chunk_id: str
    confidence: float = Field(default=1.0, ge=0, le=1)


class ExtractedEntity(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    entity_type: str = Field(default="Concept", min_length=1, max_length=80)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    description: str = Field(default="", max_length=1000)
    confidence: float = Field(default=1.0, ge=0, le=1)


class ExtractedRelation(BaseModel):
    source: str = Field(min_length=1, max_length=300)
    target: str = Field(min_length=1, max_length=300)
    relation_type: str = Field(default="related_to", min_length=1, max_length=100)
    evidence: str = Field(default="", max_length=2000)
    confidence: float = Field(default=1.0, ge=0, le=1)


class GraphExtractionResult(BaseModel):
    entities: list[ExtractedEntity] = Field(default_factory=list, max_length=100)
    relations: list[ExtractedRelation] = Field(default_factory=list, max_length=200)


class GraphPathCandidate(BaseModel):
    path_id: str
    knowledge_base_id: str
    entity_keys: list[str] = Field(default_factory=list)
    entity_names: list[str] = Field(default_factory=list)
    relation_types: list[str] = Field(default_factory=list)
    evidence_chunk_ids: list[str] = Field(default_factory=list)
    hop_count: int = Field(ge=1, le=2)
    score: float = 0.0


__all__ = [
    "ExtractedEntity",
    "ExtractedRelation",
    "GraphChunk",
    "GraphEntity",
    "GraphEntityMention",
    "GraphExtractionResult",
    "GraphRelation",
    "GraphPathCandidate",
]
