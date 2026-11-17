from __future__ import annotations

import io
import json
from unittest.mock import Mock

def _crossref(monkeypatch, payload):
    response = io.BytesIO(json.dumps(payload).encode())
    calls = Mock(return_value=response)
    monkeypatch.setattr("urllib.request.urlopen", calls)
    return calls


def _work(doi="10.48550/arXiv.1706.03762", title="Attention Is All You Need", family="Vaswani", year=2017):
    return {
        "message": {
            "DOI": doi, "title": [title], "URL": f"https://doi.org/{doi}",
            "author": [{"family": family}], "issued": {"date-parts": [[year]]},
        }
    }


def _project(client, headers):
    return client.post("/api/research/projects", json={"name": "引用核验"}, headers=headers).json()["data"]


def test_l1_citation_metadata_is_verified_without_real_network(client, auth, monkeypatch):
    headers = auth("citation-verified-user")
    project = _project(client, headers)
    calls = _crossref(monkeypatch, _work())
    response = client.post(
        f"/api/research/projects/{project['id']}/citations/verify",
        json={"doi": "10.48550/arXiv.1706.03762", "title": "Attention Is All You Need", "author": "Vaswani", "year": 2017},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    result = response.json()["data"]
    assert result["status"] == "verified"
    assert result["record"]["first_author"]["family"] == "Vaswani"
    assert result["record"]["year"] == 2017
    assert result["claim_verification"] == "manual_required"
    assert result["source"] == "crossref"
    assert calls.call_count == 1


def test_mismatched_metadata_is_not_marked_verified(client, auth, monkeypatch):
    headers = auth("citation-mismatch-user")
    project = _project(client, headers)
    _crossref(monkeypatch, _work(title="A Different Paper"))
    response = client.post(
        f"/api/research/projects/{project['id']}/citations/verify",
        json={"doi": "10.48550/arXiv.1706.03762", "title": "Attention Is All You Need", "author": "Vaswani", "year": 2017},
        headers=headers,
    )
    assert response.status_code == 201
    result = response.json()["data"]
    assert result["status"] == "mismatch"
    assert result["checks"][0]["match"] is False


def test_l2_citation_verification_requires_approval_then_sends_once(client, auth, monkeypatch):
    headers = auth("citation-l2-user")
    project = _project(client, headers)
    calls = _crossref(monkeypatch, _work())
    body = {
        "doi": "10.48550/arXiv.1706.03762", "title": "Attention Is All You Need",
        "author": "Vaswani", "year": 2017, "confidentiality": "L2",
    }
    pending = client.post(f"/api/research/projects/{project['id']}/citations/verify", json=body, headers=headers)
    assert pending.status_code == 200, pending.text
    result = pending.json()["data"]
    assert result["status"] == "awaiting_outbound_approval"
    assert result["approval"]["purpose"] == "学术引用元数据核验"
    assert result["approval"]["scope"]["fragments"][0]["classification"] == "L2"
    calls.assert_not_called()

    approval_id = result["approval"]["id"]
    client.post(f"/api/research/outbound/{approval_id}/decision", json={"approved": True}, headers=headers)
    completed = client.post(
        f"/api/research/projects/{project['id']}/citations/verify",
        json={**body, "task_id": result["task_id"], "approval_id": approval_id}, headers=headers,
    )
    assert completed.status_code == 201, completed.text
    assert completed.json()["data"]["status"] == "verified"
    assert calls.call_count == 1


def test_not_found_and_empty_lookup_are_explicit(client, auth, monkeypatch):
    headers = auth("citation-not-found-user")
    project = _project(client, headers)
    _crossref(monkeypatch, {"message": {"items": []}})
    response = client.post(
        f"/api/research/projects/{project['id']}/citations/verify",
        json={"title": "Definitely nonexistent synthetic title"}, headers=headers,
    )
    assert response.status_code == 201
    assert response.json()["data"]["status"] == "not-found"
    empty = client.post(f"/api/research/projects/{project['id']}/citations/verify", json={}, headers=headers)
    assert empty.status_code == 400
    assert empty.json()["detail"]["code"] == "citation_lookup_required"


def test_l3_citation_verification_never_uses_network(client, auth, monkeypatch):
    headers = auth("citation-l3-user")
    project = _project(client, headers)
    calls = _crossref(monkeypatch, _work())
    response = client.post(
        f"/api/research/projects/{project['id']}/citations/verify",
        json={"doi": "10.48550/arXiv.1706.03762", "confidentiality": "L3"}, headers=headers,
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "l3_external_forbidden"
    calls.assert_not_called()
