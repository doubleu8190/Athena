"""Async Neo4j connection, graph projection, and retrieval adapter."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence
import hashlib
import json
from typing import Any

from athena.config.settings import Settings
from athena.core.graph.contracts import (
    GraphChunk,
    GraphEntity,
    GraphEntityMention,
    GraphPathCandidate,
    GraphRelation,
)
from athena.utils.logging import get_logger

from .schema import SCHEMA_STATEMENTS

logger = get_logger(__name__)

_PATH_QUERY_1_HOP = """
UNWIND $terms AS term
CALL db.index.fulltext.queryNodes('athena_entity_search', term)
YIELD node, score
WHERE ($knowledge_base_ids IS NULL
       OR node.knowledge_base_id IN $knowledge_base_ids)
  AND node.active = true
WITH node, max(score) AS seed_score
ORDER BY seed_score DESC, node.entity_key ASC
LIMIT $entity_limit
MATCH p=(node)-[:RELATES_TO*1..1]-(neighbor:Entity)
WHERE ALL(item IN nodes(p)
          WHERE ($knowledge_base_ids IS NULL
                 OR item.knowledge_base_id IN $knowledge_base_ids)
            AND item.active = true)
  AND ALL(rel IN relationships(p)
          WHERE rel.confidence >= $min_confidence)
WITH p, max(seed_score) AS seed_score,
     [item IN nodes(p) | item.entity_key] AS path_keys
ORDER BY seed_score DESC, path_keys ASC
LIMIT $path_limit
RETURN
    path_keys AS entity_keys,
    [item IN nodes(p) | item.canonical_name] AS entity_names,
    [rel IN relationships(p) | rel.relation_type] AS relation_types,
    [rel IN relationships(p) | rel.evidence_chunk_id] AS evidence_chunk_ids,
    [item IN nodes(p) | item.knowledge_base_id] AS knowledge_base_ids,
    seed_score AS score
"""

_PATH_QUERY_2_HOP = """
UNWIND $terms AS term
CALL db.index.fulltext.queryNodes('athena_entity_search', term)
YIELD node, score
WHERE ($knowledge_base_ids IS NULL
       OR node.knowledge_base_id IN $knowledge_base_ids)
  AND node.active = true
WITH node, max(score) AS seed_score
ORDER BY seed_score DESC, node.entity_key ASC
LIMIT $entity_limit
MATCH p=(node)-[:RELATES_TO*1..2]-(neighbor:Entity)
WHERE ALL(item IN nodes(p)
          WHERE ($knowledge_base_ids IS NULL
                 OR item.knowledge_base_id IN $knowledge_base_ids)
            AND item.active = true)
  AND ALL(rel IN relationships(p)
          WHERE rel.confidence >= $min_confidence)
WITH p, max(seed_score) AS seed_score,
     [item IN nodes(p) | item.entity_key] AS path_keys
ORDER BY seed_score DESC, path_keys ASC
LIMIT $path_limit
RETURN
    path_keys AS entity_keys,
    [item IN nodes(p) | item.canonical_name] AS entity_names,
    [rel IN relationships(p) | rel.relation_type] AS relation_types,
    [rel IN relationships(p) | rel.evidence_chunk_id] AS evidence_chunk_ids,
    [item IN nodes(p) | item.knowledge_base_id] AS knowledge_base_ids,
    seed_score AS score
"""


@dataclass(frozen=True)
class Neo4jGraphConfig:
    """Connection settings needed by the infrastructure adapter."""

    uri: str
    username: str
    password: str
    database: str = "neo4j"
    max_connection_pool_size: int = 20
    connection_timeout_seconds: float = 5.0
    query_timeout_seconds: float = 5.0

    @classmethod
    def from_settings(cls, settings: Settings) -> "Neo4jGraphConfig":
        return cls(
            uri=settings.neo4j_uri,
            username=settings.neo4j_username,
            password=settings.neo4j_password,
            database=settings.neo4j_database,
            max_connection_pool_size=settings.neo4j_max_connection_pool_size,
            connection_timeout_seconds=settings.neo4j_connection_timeout_seconds,
            query_timeout_seconds=settings.neo4j_query_timeout_seconds,
        )


class Neo4jGraphStore:
    """Manage an async Neo4j driver and Athena's graph schema.

    The Neo4j package is imported only when ``connect`` is called. The
    application lifecycle treats a failed connection as a startup error.
    """

    def __init__(self, config: Neo4jGraphConfig) -> None:
        self.config = config
        self._driver: Any | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> Neo4jGraphStore:
        return cls(Neo4jGraphConfig.from_settings(settings))

    @property
    def is_connected(self) -> bool:
        """Whether a driver has completed initialization."""

        return self._driver is not None

    async def connect(self) -> None:
        """Create the driver and verify the configured database connection."""

        if self._driver is not None:
            return
        try:
            from neo4j import AsyncGraphDatabase
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise RuntimeError(
                "Neo4j is enabled but the 'neo4j' package is not installed"
            ) from exc

        driver = AsyncGraphDatabase.driver(
            self.config.uri,
            auth=(self.config.username, self.config.password),
            max_connection_pool_size=self.config.max_connection_pool_size,
            connection_timeout=self.config.connection_timeout_seconds,
        )
        try:
            await driver.verify_connectivity()
        except Exception:
            await driver.close()
            raise
        self._driver = driver
        logger.info(
            "neo4j_connected",
            uri=self.config.uri,
            database=self.config.database,
        )

    async def initialize(self) -> None:
        """Connect and create all idempotent constraints and indexes."""

        await self.connect()
        driver = self._require_driver()
        async with driver.session(database=self.config.database) as session:
            for statement in SCHEMA_STATEMENTS:
                result = await session.run(
                    statement,
                    timeout=self.config.query_timeout_seconds,
                )
                await result.consume()
        logger.info("neo4j_schema_initialized", statements=len(SCHEMA_STATEMENTS))

    async def health_check(self) -> bool:
        """Run a cheap round-trip check against the configured database."""

        if self._driver is None:
            return False
        try:
            async with self._driver.session(database=self.config.database) as session:
                result = await session.run(
                    "RETURN 1 AS health",
                    timeout=self.config.query_timeout_seconds,
                )
                record = await result.single()
                return record is not None and record["health"] == 1
        except Exception as exc:
            logger.warning("neo4j_health_check_failed", error=str(exc))
            return False

    async def close(self) -> None:
        """Close the driver and release its connection pool."""

        driver, self._driver = self._driver, None
        if driver is not None:
            await driver.close()
            logger.info("neo4j_disconnected")

    async def upsert_memory_node(self, record: dict[str, Any]) -> None:
        """Create or update the graph projection for one memory revision."""
        driver = self._require_driver()
        missing = {"id", "logical_memory_id"}.difference(record)
        if missing:
            raise ValueError(f"memory graph record missing fields: {sorted(missing)}")
        async with driver.session(database=self.config.database) as session:
            result = await session.run(
                """
                MERGE (m:Memory {id: $id})
                SET m.logical_memory_id = $logical_memory_id,
                    m.session_id = $session_id,
                    m.revision = $revision,
                    m.status = $status,
                    m.created_at = $created_at,
                    m.source_turn_id = $source_turn_id,
                    m.validity_status = $validity_status,
                    m.valid_until = $valid_until
                """,
                id=str(record["id"]),
                logical_memory_id=str(record["logical_memory_id"]),
                session_id=record.get("session_id", ""),
                revision=int(record.get("revision", 1)),
                status=record.get("status", "active"),
                created_at=record.get("created_at", ""),
                source_turn_id=record.get("source_turn_id"),
                validity_status=record.get("validity_status", "valid"),
                valid_until=record.get("valid_until"),
                timeout=self.config.query_timeout_seconds,
            )
            await result.consume()

    async def delete_memory_node(self, memory_id: str) -> None:
        """Remove one memory projection and its incident relationships."""
        driver = self._require_driver()
        async with driver.session(database=self.config.database) as session:
            result = await session.run(
                "MATCH (m:Memory {id: $id}) DETACH DELETE m",
                id=memory_id,
                timeout=self.config.query_timeout_seconds,
            )
            await result.consume()

    @staticmethod
    def _memory_relation_label(relation_type: str) -> str:
        labels = {
            "supports": "SUPPORTS",
            "contradicts": "CONTRADICTS",
            "supersedes": "SUPERSEDES",
        }
        try:
            return labels[relation_type.strip().lower()]
        except KeyError as exc:
            raise ValueError(
                f"unsupported memory relation type: {relation_type}"
            ) from exc

    async def add_relation(
        self,
        source_id: str,
        target_id: str,
        relation_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Create an idempotent semantic relationship between memory nodes."""
        label = self._memory_relation_label(relation_type)
        driver = self._require_driver()
        async with driver.session(database=self.config.database) as session:
            result = await session.run(
                f"""
                MATCH (source:Memory {{id: $source_id}})
                MATCH (target:Memory {{id: $target_id}})
                MERGE (source)-[r:{label}]->(target)
                SET r.metadata_json = $metadata_json,
                    r.updated_at = $updated_at
                """,
                source_id=source_id,
                target_id=target_id,
                metadata_json=json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                updated_at=(metadata or {}).get("updated_at"),
                timeout=self.config.query_timeout_seconds,
            )
            await result.consume()

    async def remove_relation(
        self, source_id: str, target_id: str, relation_type: str
    ) -> None:
        label = self._memory_relation_label(relation_type)
        driver = self._require_driver()
        async with driver.session(database=self.config.database) as session:
            result = await session.run(
                f"""
                MATCH (:Memory {{id: $source_id}})-[r:{label}]->(:Memory {{id: $target_id}})
                DELETE r
                """,
                source_id=source_id,
                target_id=target_id,
                timeout=self.config.query_timeout_seconds,
            )
            await result.consume()

    async def list_revisions(self, memory_id: str) -> list[dict[str, Any]]:
        """List all revisions in a logical memory chain."""
        driver = self._require_driver()
        async with driver.session(database=self.config.database) as session:
            result = await session.run(
                """
                MATCH (root:Memory {id: $memory_id})
                MATCH (memory:Memory {logical_memory_id: root.logical_memory_id})
                RETURN memory.id AS id,
                       memory.logical_memory_id AS logical_memory_id,
                       memory.revision AS revision,
                       memory.status AS status,
                       memory.created_at AS created_at,
                       memory.source_turn_id AS source_turn_id,
                       memory.validity_status AS validity_status,
                       memory.valid_until AS valid_until
                ORDER BY memory.revision ASC, memory.created_at ASC
                """,
                memory_id=memory_id,
                timeout=self.config.query_timeout_seconds,
            )
            return [dict(row) for row in await result.data()]

    async def replace_document_graph(
        self,
        *,
        knowledge_base_id: str,
        attachment_id: str,
        logical_document_id: str,
        document_version: int,
        filename: str,
        chunks: Sequence[GraphChunk],
        entities: Sequence[GraphEntity],
        mentions: Sequence[GraphEntityMention],
        relations: Sequence[GraphRelation],
    ) -> None:
        """Atomically replace all graph data projected from one document."""

        driver = self._require_driver()

        async def write_transaction(tx: Any) -> None:
            result = await tx.run(
                """
                MATCH (d:Document {attachment_id: $attachment_id})
                DETACH DELETE d
                """,
                attachment_id=attachment_id,
            )
            await result.consume()

            result = await tx.run(
                """
                MATCH (c:Chunk {attachment_id: $attachment_id})
                DETACH DELETE c
                """,
                attachment_id=attachment_id,
            )
            await result.consume()

            result = await tx.run(
                """
                MATCH ()-[r:RELATES_TO]->()
                WHERE r.attachment_id = $attachment_id
                DELETE r
                """,
                attachment_id=attachment_id,
            )
            await result.consume()

            result = await tx.run("""
                MATCH (e:Entity)
                WHERE NOT (e)<-[:MENTIONS]-()
                DETACH DELETE e
                """)
            await result.consume()

            result = await tx.run(
                """
                MERGE (kb:KnowledgeBase {id: $knowledge_base_id})
                SET kb.updated_at = datetime()
                MERGE (d:Document {attachment_id: $attachment_id})
                SET d.logical_document_id = $logical_document_id,
                    d.knowledge_base_id = $knowledge_base_id,
                    d.document_version = $document_version,
                    d.filename = $filename,
                    d.status = 'READY'
                MERGE (kb)-[:OWNS]->(d)
                """,
                knowledge_base_id=knowledge_base_id,
                attachment_id=attachment_id,
                logical_document_id=logical_document_id,
                document_version=document_version,
                filename=filename,
            )
            await result.consume()

            result = await tx.run(
                """
                MATCH (d:Document {attachment_id: $attachment_id})
                UNWIND $chunks AS item
                MERGE (c:Chunk {id: item.id})
                SET c.attachment_id = $attachment_id,
                    c.ordinal = item.ordinal,
                    c.locator = item.locator,
                    c.document_version = item.document_version
                MERGE (d)-[:HAS_CHUNK]->(c)
                """,
                attachment_id=attachment_id,
                chunks=[chunk.model_dump(mode="json") for chunk in chunks],
            )
            await result.consume()

            result = await tx.run(
                """
                UNWIND $entities AS item
                MERGE (e:Entity {entity_key: item.entity_key})
                SET e.knowledge_base_id = $knowledge_base_id,
                    e.canonical_name = item.canonical_name,
                    e.entity_type = item.entity_type,
                    e.aliases = item.aliases,
                    e.description = item.description,
                    e.active = true
                """,
                knowledge_base_id=knowledge_base_id,
                entities=[entity.model_dump(mode="json") for entity in entities],
            )
            await result.consume()

            result = await tx.run(
                """
                UNWIND $mentions AS item
                MATCH (c:Chunk {id: item.chunk_id})
                MATCH (e:Entity {entity_key: item.entity_key})
                MERGE (c)-[m:MENTIONS {entity_key: item.entity_key}]->(e)
                SET m.surface = item.surface, m.confidence = item.confidence
                """,
                mentions=[mention.model_dump(mode="json") for mention in mentions],
            )
            await result.consume()

            result = await tx.run(
                """
                UNWIND $relations AS item
                MATCH (source:Entity {entity_key: item.source_entity_key})
                MATCH (target:Entity {entity_key: item.target_entity_key})
                MERGE (source)-[r:RELATES_TO {
                    attachment_id: $attachment_id,
                    evidence_chunk_id: item.evidence_chunk_id,
                    relation_type: item.relation_type
                }]->(target)
                SET r.confidence = item.confidence,
                    r.evidence = item.evidence,
                    r.document_version = $document_version,
                    r.knowledge_base_id = $knowledge_base_id
                """,
                attachment_id=attachment_id,
                document_version=document_version,
                knowledge_base_id=knowledge_base_id,
                relations=[relation.model_dump(mode="json") for relation in relations],
            )
            await result.consume()

        async with driver.session(database=self.config.database) as session:
            await session.execute_write(write_transaction)

    async def delete_document_graph(
        self, *, attachment_id: str, document_version: int | None = None
    ) -> None:
        """Delete one document projection and entities no longer mentioned."""

        driver = self._require_driver()

        async def delete_transaction(tx: Any) -> None:
            result = await tx.run(
                """
                MATCH (d:Document {attachment_id: $attachment_id})
                DETACH DELETE d
                """,
                attachment_id=attachment_id,
            )
            await result.consume()

            result = await tx.run(
                """
                MATCH (c:Chunk {attachment_id: $attachment_id})
                DETACH DELETE c
                """,
                attachment_id=attachment_id,
            )
            await result.consume()
            result = await tx.run(
                """
                MATCH ()-[r:RELATES_TO {attachment_id: $attachment_id}]->()
                WHERE $document_version IS NULL
                   OR r.document_version = $document_version
                DELETE r
                """,
                attachment_id=attachment_id,
                document_version=document_version,
            )
            await result.consume()
            result = await tx.run("""
                MATCH (e:Entity)
                WHERE NOT (e)<-[:MENTIONS]-()
                DETACH DELETE e
                """)
            await result.consume()

        async with driver.session(database=self.config.database) as session:
            await session.execute_write(delete_transaction)

    async def search_paths(
        self,
        *,
        query: str,
        knowledge_base_ids: Sequence[str] | None,
        max_hops: int,
        entity_limit: int,
        path_limit: int,
        min_confidence: float,
    ) -> list[GraphPathCandidate]:
        """Resolve query terms and traverse bounded relation paths."""

        if max_hops not in (1, 2):
            raise ValueError("graph max_hops must be 1 or 2")
        terms = list(dict.fromkeys([query.strip(), *query.split()]))
        terms = [term for term in terms if term][:20]
        if not terms:
            return []
        driver = self._require_driver()
        statement = (
            _PATH_QUERY_1_HOP if max_hops == 1 else _PATH_QUERY_2_HOP
        )
        async with driver.session(database=self.config.database) as session:
            result = await session.run(
                statement,
                terms=terms,
                knowledge_base_ids=(
                    list(knowledge_base_ids) if knowledge_base_ids else None
                ),
                entity_limit=entity_limit,
                path_limit=path_limit,
                min_confidence=min_confidence,
                timeout=self.config.query_timeout_seconds,
            )
            rows = await result.data()
        candidates: list[GraphPathCandidate] = []
        for row in rows:
            entity_keys = [str(value) for value in row.get("entity_keys", [])]
            evidence_ids = list(
                dict.fromkeys(
                    str(value) for value in row.get("evidence_chunk_ids", []) if value
                )
            )
            knowledge_ids = list(
                dict.fromkeys(
                    str(value) for value in row.get("knowledge_base_ids", []) if value
                )
            )
            if len(knowledge_ids) != 1 or not entity_keys:
                continue
            path_id = hashlib.sha256(
                ("|".join(entity_keys) + "|" + "|".join(evidence_ids)).encode()
            ).hexdigest()[:24]
            candidates.append(
                GraphPathCandidate(
                    path_id=f"graph:path:{path_id}",
                    knowledge_base_id=knowledge_ids[0],
                    entity_keys=entity_keys,
                    entity_names=[str(value) for value in row.get("entity_names", [])],
                    relation_types=[
                        str(value) for value in row.get("relation_types", [])
                    ],
                    evidence_chunk_ids=evidence_ids,
                    hop_count=len(row.get("relation_types", [])),
                    score=float(row.get("score") or 0.0),
                )
            )
        return candidates

    def _require_driver(self) -> Any:
        if self._driver is None:
            raise RuntimeError("Neo4jGraphStore is not connected")
        return self._driver


__all__ = ["Neo4jGraphConfig", "Neo4jGraphStore"]
