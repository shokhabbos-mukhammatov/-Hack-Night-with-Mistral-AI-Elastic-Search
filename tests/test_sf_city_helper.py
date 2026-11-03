from sf_city_helper import Resource, build_hybrid_query, normalize_resource, resource_text


def test_normalize_resource_defaults():
    resource = normalize_resource({"id": 42, "name": "Library"})
    assert resource.id == "42"
    assert resource.category == "City information"
    assert resource.neighborhood == "Citywide"
    assert resource.address == "San Francisco"


def test_resource_text_contains_core_fields():
    resource = Resource(
        id="1",
        name="Mission Playground",
        category="Public spaces",
        neighborhood="Mission",
        address="2450 Harrison St",
        description="Family programs and recreation center",
        url="https://example.org",
    )
    text = resource_text(resource)
    assert "Mission Playground" in text
    assert "Public spaces" in text
    assert "https://example.org" in text


def test_build_hybrid_query_without_embedding():
    query = build_hybrid_query("find a nearby park", embedding=None, k=3)
    assert query["size"] == 3
    assert "knn" not in query


def test_build_hybrid_query_with_embedding():
    query = build_hybrid_query("find transit alerts", embedding=[0.1, 0.2], k=4)
    assert query["size"] == 4
    assert query["knn"]["field"] == "embedding"
    assert query["knn"]["query_vector"] == [0.1, 0.2]
