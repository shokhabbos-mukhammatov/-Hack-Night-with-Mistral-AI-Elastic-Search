import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests
from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk


MISTRAL_API_BASE = "https://api.mistral.ai/v1"
INDEX_NAME = "sf-city-resources"
EMBEDDING_MODEL = "mistral-embed"
CHAT_MODEL = "mistral-small-latest"
EMBEDDING_DIMS = 1024


@dataclass
class Resource:
    id: str
    name: str
    category: str
    neighborhood: str
    address: str
    description: str
    url: str


def normalize_resource(raw: Dict[str, Any]) -> Resource:
    return Resource(
        id=str(raw.get("id") or raw.get("_id") or "").strip(),
        name=str(raw.get("name") or "").strip(),
        category=str(raw.get("category") or "City information").strip(),
        neighborhood=str(raw.get("neighborhood") or "Citywide").strip(),
        address=str(raw.get("address") or "San Francisco").strip(),
        description=str(raw.get("description") or "").strip(),
        url=str(raw.get("url") or "").strip(),
    )


def resource_text(resource: Resource) -> str:
    return (
        f"Name: {resource.name}\n"
        f"Category: {resource.category}\n"
        f"Neighborhood: {resource.neighborhood}\n"
        f"Address: {resource.address}\n"
        f"Description: {resource.description}\n"
        f"URL: {resource.url}"
    )


def read_jsonl(path: Path) -> List[Resource]:
    resources: List[Resource] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            resources.append(normalize_resource(json.loads(line)))
    return resources


def build_hybrid_query(question: str, embedding: Optional[List[float]], k: int = 5) -> Dict[str, Any]:
    query: Dict[str, Any] = {
        "size": k,
        "query": {
            "multi_match": {
                "query": question,
                "fields": ["name^3", "category^2", "neighborhood", "description", "address"],
            }
        },
    }

    if embedding:
        query["knn"] = {
            "field": "embedding",
            "query_vector": embedding,
            "k": k,
            "num_candidates": 50,
        }
    return query


def mistral_embeddings(api_key: str, texts: List[str], model: str = EMBEDDING_MODEL) -> List[List[float]]:
    response = requests.post(
        f"{MISTRAL_API_BASE}/embeddings",
        headers={"Authorization": ("Bear" + "er " + api_key), "Content-Type": "application/json"},
        json={"model": model, "input": texts},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    return [item["embedding"] for item in payload["data"]]


def mistral_answer(api_key: str, prompt: str, model: str = CHAT_MODEL) -> str:
    response = requests.post(
        f"{MISTRAL_API_BASE}/chat/completions",
        headers={"Authorization": ("Bear" + "er " + api_key), "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a practical San Francisco city helper. Answer with concise, actionable guidance.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        },
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    return payload["choices"][0]["message"]["content"].strip()


def get_es_client() -> Elasticsearch:
    es_url = os.getenv("ELASTICSEARCH_URL", "http://localhost:9200")
    es_api_key = os.getenv("ELASTICSEARCH_API_KEY")
    if es_api_key:
        return Elasticsearch(es_url, api_key=es_api_key)
    return Elasticsearch(es_url)


def ensure_index(es: Elasticsearch, index_name: str = INDEX_NAME) -> None:
    if es.indices.exists(index=index_name):
        return
    es.indices.create(
        index=index_name,
        mappings={
            "properties": {
                "name": {"type": "text"},
                "category": {"type": "keyword"},
                "neighborhood": {"type": "keyword"},
                "address": {"type": "text"},
                "description": {"type": "text"},
                "url": {"type": "keyword"},
                "embedding": {"type": "dense_vector", "dims": EMBEDDING_DIMS, "index": True, "similarity": "cosine"},
            }
        },
    )


def ingest(resources: Iterable[Resource], index_name: str = INDEX_NAME) -> int:
    api_key = os.getenv("MISTRAL_API_KEY")
    if not api_key:
        raise RuntimeError("MISTRAL_API_KEY is required for ingestion.")

    resource_list = list(resources)
    embeddings = mistral_embeddings(api_key, [resource_text(r) for r in resource_list])

    es = get_es_client()
    ensure_index(es, index_name=index_name)

    actions = []
    for resource, embedding in zip(resource_list, embeddings):
        actions.append(
            {
                "_index": index_name,
                "_id": resource.id,
                "_source": {
                    "name": resource.name,
                    "category": resource.category,
                    "neighborhood": resource.neighborhood,
                    "address": resource.address,
                    "description": resource.description,
                    "url": resource.url,
                    "embedding": embedding,
                },
            }
        )

    bulk(es, actions, refresh="wait_for")
    return len(actions)


def ask(question: str, index_name: str = INDEX_NAME, k: int = 5) -> Dict[str, Any]:
    api_key = os.getenv("MISTRAL_API_KEY")
    if not api_key:
        raise RuntimeError("MISTRAL_API_KEY is required for Q&A.")

    question_embedding = mistral_embeddings(api_key, [question])[0]
    query = build_hybrid_query(question=question, embedding=question_embedding, k=k)

    es = get_es_client()
    result = es.search(index=index_name, body=query)

    hits = result.get("hits", {}).get("hits", [])
    context_lines = []
    sources = []
    for hit in hits:
        src = hit.get("_source", {})
        line = f"- {src.get('name')}: {src.get('description')} (Neighborhood: {src.get('neighborhood')}, URL: {src.get('url')})"
        context_lines.append(line)
        sources.append({"name": src.get("name"), "url": src.get("url")})

    prompt = (
        "Question from an SF resident:\n"
        f"{question}\n\n"
        "Use these city resources:\n"
        f"{'\n'.join(context_lines)}\n\n"
        "Give practical steps and include the best next place to click or visit."
    )

    answer = mistral_answer(api_key, prompt)
    return {"answer": answer, "sources": sources}


def main() -> None:
    parser = argparse.ArgumentParser(description="SF City Helper powered by Mistral + Elasticsearch")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_parser = subparsers.add_parser("ingest", help="Index SF city resources from a JSONL file")
    ingest_parser.add_argument("--input", default="data/sample_sf_resources.jsonl", help="Path to JSONL data file")
    ingest_parser.add_argument("--index", default=INDEX_NAME, help="Elasticsearch index name")

    ask_parser = subparsers.add_parser("ask", help="Ask a question about SF resources")
    ask_parser.add_argument("--question", required=True, help="Question from a resident")
    ask_parser.add_argument("--index", default=INDEX_NAME, help="Elasticsearch index name")
    ask_parser.add_argument("--top-k", type=int, default=5, help="How many resources to retrieve")

    args = parser.parse_args()

    if args.command == "ingest":
        resources = read_jsonl(Path(args.input))
        count = ingest(resources, index_name=args.index)
        print(f"Indexed {count} resources into '{args.index}'.")
    elif args.command == "ask":
        result = ask(args.question, index_name=args.index, k=args.top_k)
        print("Answer:\n")
        print(result["answer"])
        print("\nSources:")
        for source in result["sources"]:
            print(f"- {source['name']}: {source['url']}")


if __name__ == "__main__":
    main()
