# ADR-0001: Use pgvector instead of Qdrant for face embeddings

**Date:** 2026-03-01
**Status:** Accepted
**Discussion:** https://github.com/lucas42/lucos_photos/issues/23

## Context

The lucos_photos architecture includes a face detection and recognition pipeline (see #8) that generates vector embeddings for detected faces and uses similarity search to match them against known people. The original design included Qdrant as a dedicated vector database for storing these embeddings.

Qdrant is a purpose-built vector search engine with strong performance characteristics at scale: distributed sharding, payload filtering, and high throughput on approximate nearest-neighbour (ANN) queries. However, lucos_photos is a personal photo library managing approximately 100,000 photos, with an estimated 1-3 faces per photo. The face embedding dataset is therefore on the order of 100,000-300,000 vectors -- small by vector search standards.

At the time of this decision, the face detection pipeline has not yet been implemented, meaning there is no embedding data in Qdrant and no migration cost.

The following concerns were raised about the Qdrant-based design:

1. **Operational overhead.** Qdrant adds a fifth container to the Docker Compose stack. It requires its own monitoring, backup consideration, and version management -- all for a dataset small enough to fit comfortably in PostgreSQL.

2. **Version instability.** Qdrant was pinned to `latest` (#19), and Qdrant has a documented history of breaking API changes between minor versions. A routine `docker compose pull` during deployment could silently break the face recognition feature.

3. **Transactional inconsistency.** Face metadata lives in PostgreSQL while embeddings live in Qdrant. There is no transactional guarantee between the two stores. If a Postgres write succeeds but a Qdrant upsert fails (or vice versa), the system is left in an inconsistent state that must be detected and repaired. Recovery from such inconsistency would require either re-running the embedding pipeline for affected photos or manual reconciliation between the two stores.

4. **Observability gap.** Qdrant runs as a separate process with its own health API. At the time of this decision, Qdrant had no entry in the service's `/_info` checks -- meaning Qdrant could be down without the monitoring system detecting it.

5. **The data is not precious.** The `lucos_photos_qdrant_data` volume was already classified as `automatic` recreate effort in `lucos_configy/config/volumes.yaml`, acknowledging that embeddings are fully regenerable from source photos. Running a dedicated specialist container for regenerable data at this scale is a cost with no commensurate benefit.

6. **Incident-response asymmetry.** The two-store design creates meaningfully different failure investigation costs. A Postgres outage affects the entire service and has a clear recovery path: restart the container, confirm it comes back, done. A Qdrant outage creates a partial degradation where face recognition is unavailable but the rest of the service appears healthy. The correct recovery action depends on whether any in-flight writes left the two stores in an inconsistent state -- a question that is straightforward to answer at a desk and considerably harder to investigate under pressure during an incident.

## Decision

Use **pgvector** (a PostgreSQL extension) to store face embeddings and perform similarity search, instead of running a separate Qdrant instance.

Face embeddings will be stored in a column on the `face` table (or a closely related table) in the existing PostgreSQL database. Similarity search will use pgvector's indexed vector operations.

The Qdrant container, its volume, and the `QDRANT_URL` environment variable will be removed from `docker-compose.yml`.

## Alternatives considered

### Keep Qdrant (status quo)

Qdrant is technically capable and would provide superior performance at very large scale (millions of vectors). However, at the current and projected scale of lucos_photos, this performance advantage is irrelevant. The operational, consistency, and observability costs outlined above are real and ongoing, while the benefits are theoretical and contingent on growth that may never happen.

### Other dedicated vector databases (Milvus, Weaviate, Pinecone, etc.)

These share the same fundamental problem as Qdrant at this scale: they add infrastructure complexity for a dataset that PostgreSQL can handle natively. They were not seriously considered.

## Consequences

### Positive

- **One fewer container** to run, monitor, version-manage, and reason about at 2am.
- **Transactional consistency** between face metadata and embeddings. A single Postgres transaction can atomically write the face record and its embedding, eliminating an entire class of inconsistency bugs.
- **Simpler backup story.** Embeddings are included in the existing Postgres backup. No separate Qdrant volume to track.
- **Version pinning resolved.** Issue #19 (Qdrant pinned to `latest`) becomes moot. pgvector upgrades with PostgreSQL, which is already explicitly version-pinned.
- **No observability gap.** pgvector operations go through the same Postgres connection the API already monitors, so `/_info` health checks cover embeddings automatically.
- **Simpler development setup.** One fewer service to start locally.

### Negative

- **Performance ceiling.** pgvector's exact and approximate nearest-neighbour search is well-suited to 100k-300k vectors, but would become a bottleneck at millions of vectors. If lucos_photos grows far beyond its current scope, a migration to a dedicated vector database would be needed.
- **Migration cost if we outgrow pgvector.** The path would be: spin up Qdrant alongside pgvector, re-run the embedding pipeline to populate it, verify parity, cut over, remove pgvector columns. This is tractable (the data is regenerable) but not free.
- **PostgreSQL becomes a larger single point of failure.** It was already critical, but now it also handles embedding search. In practice this changes very little -- if Postgres is down, the service is already broken.
- **The `face.id` as Qdrant point ID convention** documented in the data model is no longer needed. The embedding becomes a column rather than an external reference.

### Follow-up actions

- Remove the Qdrant container, volume, and environment variable from `docker-compose.yml`
- Remove `lucos_photos_qdrant_data` from `lucos_configy/config/volumes.yaml`
- Change the Postgres image to `pgvector/pgvector:pg16` (the officially maintained pgvector distribution), avoiding the need for a custom Postgres Dockerfile and an additional image in the CI build pipeline
- Add an embedding column (using pgvector's `vector` type) to the face-related schema
- Update the CLAUDE.md to reflect the new architecture (4 containers, no Qdrant)
- Close #19 as moot
- Update #8 implementation plans to use pgvector instead of Qdrant
