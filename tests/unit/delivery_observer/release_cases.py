"""HTTP fixtures for GitHub release metadata."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx

from brain_v42.delivery_config import DeliverySettings
from brain_v42.delivery_observer.transport import GitHubTransport

from .github_cases import NOW, RID

ROOT = "/repos/hawkixs/brain-v42"


def tag(name: str, sha: str) -> dict:
    return {"name": name, "commit": {"sha": sha}}


class ReleaseCase:
    def __init__(self, *, tags=None, dates=None, compare=None, tag_next_paths=None):
        self.tags = tags or []
        self.dates = dates or {}
        self.compare = compare or {}
        self.tag_next_paths = tag_next_paths or []
        self.requests = []
        self.elapsed = 0.0

    def handle(self, request):
        self.requests.append((request.method, request.url.path, dict(request.url.params)))
        path = request.url.path
        if path in {f"{ROOT}/tags", f"/repositories/{RID}/tags"}:
            page = int(request.url.params.get("page", "1"))
            records = self.tags[page - 1] if page <= len(self.tags) else []
            headers = {}
            if page < len(self.tags):
                next_path = (
                    self.tag_next_paths[page - 1]
                    if page - 1 < len(self.tag_next_paths)
                    else f"{ROOT}/tags"
                )
                headers["Link"] = (
                    f'<https://api.github.com{next_path}?per_page=100&page={page + 1}>; rel="next"'
                )
            return httpx.Response(200, json=records, headers=headers)
        if path.startswith(f"{ROOT}/git/commits/"):
            sha = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"sha": sha, "committer": {"date": self.dates[sha]}})
        if path.startswith(f"{ROOT}/compare/"):
            pair = path.rsplit("/", 1)[-1].split("...")
            return httpx.Response(200, json=self.compare[tuple(pair)])
        raise AssertionError(f"unexpected fixture endpoint: {path}")

    async def call(self, method, *args):
        from brain_v42.delivery_observer.github import GitHubClient

        async def sleep(delay):
            self.elapsed += delay
            await asyncio.sleep(0)

        settings = DeliverySettings(repository_registry={"brain-v42": {RID: "hawkixs/brain-v42"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.handle)) as http:
            transport = GitHubTransport(http, settings, monotonic=lambda: self.elapsed, sleep=sleep)

            async def headers():
                return {"Authorization": "Bearer fixture"}

            async def invalidate(_headers):
                return None

            auth = SimpleNamespace(
                transport=transport, authorization_headers=headers, invalidate=invalidate
            )
            client = GitHubClient(http, settings, auth, now=lambda: NOW)
            return await getattr(client, method)(*args)
