============================================================
  Enterprise RAG Architecture — Dutch Tax Authority
  Technical Assessment Submission by Emre Ram
============================================================

START HERE
----------
Open the presentation first:

  assessment_presentation_final.pptx

It walks you through how I approached this assessment using
a multi-agent AI workflow (ChatGPT 5.4, Claude Code with
sub-agents, Hermes Agent), and gives a global overview of
the architecture I designed.


MAIN WRITTEN SUBMISSION
-----------------------
After the presentation, the full technical write-up is in:

  drafts/final_submission_v2.md

This covers all four modules in detail:
  Module 1 — Ingestion & Knowledge Structuring
  Module 2 — Retrieval Strategy
  Module 3 — Agentic RAG (CRAG State Machine)
  Module 4 — Production Ops, Security & Evaluation


SUPPORTING ARTIFACTS
--------------------
Everything the submission references lives in this repo:

  pseudocode/       5 Python files — ingestion, retrieval,
                    CRAG state machine, grader, cache

  schemas/          OpenSearch index mapping, chunk metadata
                    schema (22 fields), RBAC role definitions

  diagrams/         Architecture overview, retrieval flow,
                    CRAG state machine, security model

  prompts/          LLM prompts — grader, generator, HyDE,
                    query decomposition

  eval/             Evaluation metrics matrix + golden test
                    set specification (5 sample entries)

  performance/      Voluntary supplementary deep-dive on
                    resource allocation and cost per query

  reference/        Assumptions list (A1-A18) and full
                    tools & technologies inventory

  .github/          CI/CD evaluation gate workflow stub

  requirements.txt  Python dependencies


LIVE DEMO (Docker)
------------------
A working CRAG pipeline you can run locally:

  cd demo
  cp .env.example .env          # add your Gemini API key
  docker-compose up --build

Opens at: http://localhost:8000/docs

Runs OpenSearch + Redis + FastAPI with the full CRAG state
machine. Try the demo queries in demo/README.md to see:
  - Citation-grounded answers with pipeline trace
  - RBAC tiers (PUBLIC vs CLASSIFIED_FIOD access)
  - Redis semantic cache (second call ~10ms)
  - IRRELEVANT query refusal

Requires: Docker Desktop + a Google Gemini API key.


ORIGINAL ASSIGNMENT
-------------------
  assesment.txt     The assessment brief as received
