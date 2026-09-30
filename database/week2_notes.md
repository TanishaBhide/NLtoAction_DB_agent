# Week 2 Schema Retrieval Layer

## Goal
Week 2 adds a searchable schema metadata layer so the agent can map a natural-language request to the most relevant tables and columns before generating SQL or action plans.

## What was added

- `schema_metadata.json` with structured descriptions for the operational tables, their columns, keys, foreign keys, and retrieval keywords.
- `schema_retrieval.py`, a dependency-free schema lookup engine that converts schema metadata into sparse term vectors and returns top-k matches by cosine similarity.
- This note as the Week 2 implementation record.

## Design choice
The project timeline mentions Chroma or FAISS, but the repository currently has no vector-store dependency installed. To keep Week 2 runnable in the existing environment, the schema lookup layer uses sparse, deterministic embeddings built from the schema metadata itself. The architecture is still compatible with a later swap to a true vector database because the input contract is already explicit metadata JSON.

## How retrieval works

1. Load the schema metadata.
2. Build a text representation for each table from the table name, description, keys, foreign keys, and column semantics.
3. Tokenize the text into lowercase word tokens.
4. Create normalized sparse vectors.
5. Score a user query against each table with cosine similarity.
6. Return the top-k tables and their metadata.

## Week 2 deliverable status
Done:

- schema metadata structure is in place
- schema lookup layer is implemented
- top-k schema lookup can run from the command line
- the design is documented for future replacement with Chroma/FAISS
- retrieval evaluation on 17 labeled requests achieved top-1 hit rate 1.0000 and top-3 hit rate 1.0000

Still to do later:

- add a real vector store if the project wants one
- connect retrieval output to the intent parser and SQL generation steps
- add evaluation examples for schema lookup quality