# SF City Helper (Mistral + Elasticsearch)

This project is a lightweight end-to-end demo for improving daily life in San Francisco.

It helps residents quickly find city resources for:
- Getting around (transit/bike updates)
- Neighborhood businesses
- Public spaces and art
- Community volunteering
- City services and public data

## Why this helps

Residents often need answers that are spread across many city pages and datasets. This app combines:
- **Elasticsearch** for indexing and hybrid retrieval (keyword + vector)
- **Mistral** for embedding and answer generation

## Project flow

1. Ingest SF resource records (sample JSONL included)
2. Create vector embeddings with Mistral
3. Index content in Elasticsearch
4. Retrieve relevant resources with hybrid search
5. Generate a concise, actionable answer with Mistral

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Set environment variables:

```bash
export MISTRAL_API_KEY="<your-mistral-key>"
export ELASTICSEARCH_URL="http://localhost:9200"
# optional if your cluster requires auth
export ELASTICSEARCH_API_KEY="<your-elasticsearch-api-key>"
```

## Run

Ingest sample resources:

```bash
python sf_city_helper.py ingest --input data/sample_sf_resources.jsonl
```

Ask a resident-focused question:

```bash
python sf_city_helper.py ask --question "I don't have a car. Where can I check transit disruptions and bike options today?"
```

## Testing

```bash
pytest -q
```

## Notes

- Replace `data/sample_sf_resources.jsonl` with larger SF open datasets (311, parks, business locations, public art, trees) to scale this for real usage.
- The current sample is intentionally small for a fast hack-night demo.
