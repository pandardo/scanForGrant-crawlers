"""Open a PR carrying a drafted extraction rule (§6.4 Track 2).

The rule is written to a small JSON file on a new branch, and a PR is opened. The
PR is NEVER auto-merged: a human reads the rule and merges it, and only then does
a follow-up (applying it to sources.extraction_rules) take effect.

Why a file in the repo rather than writing straight to the database: the review
has to happen *before* the rule is ever used, and a PR against the public repo is
the reviewable, revertable unit §6.4 asks for. The rule file is applied to the DB
by a human step after merge — the crawler never self-applies a drafted rule.

Uses the GitHub REST API with a token scoped to Contents + Pull requests on this
one repo. No git binary required.
"""

from __future__ import annotations

import base64
import json
import logging

import httpx

log = logging.getLogger(__name__)

API = "https://api.github.com"


class PRError(RuntimeError):
    pass


class GitHubPR:
    def __init__(self, *, token: str, repo: str, client: httpx.Client | None = None):
        self._repo = repo
        self._client = client or httpx.Client(timeout=30)
        self._headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _get(self, path: str):
        r = self._client.get(f"{API}/repos/{self._repo}{path}", headers=self._headers)
        if r.status_code >= 300:
            raise PRError(f"GET {path} -> {r.status_code}")
        return r.json()

    def _post(self, path: str, body: dict):
        r = self._client.post(f"{API}/repos/{self._repo}{path}", headers=self._headers, json=body)
        if r.status_code >= 300:
            # Never echo the body — it can quote the request; the token is in headers.
            raise PRError(f"POST {path} -> {r.status_code}")
        return r.json()

    def _put(self, path: str, body: dict):
        r = self._client.put(f"{API}/repos/{self._repo}{path}", headers=self._headers, json=body)
        if r.status_code >= 300:
            raise PRError(f"PUT {path} -> {r.status_code}")
        return r.json()

    def open_rule_pr(self, *, source_id: str, source_label: str, rule: dict, note: str) -> str:
        """Create a branch, commit the rule file, open a PR. Returns the PR URL."""
        default_branch = self._get("")["default_branch"]
        base_sha = self._get(f"/git/ref/heads/{default_branch}")["object"]["sha"]

        # A stable branch name per source, so re-drafting updates the same PR path
        # rather than spawning many. source_id is a uuid, safe in a ref name.
        branch = f"track2/rule-{source_id}"
        try:
            self._post("/git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha})
        except PRError:
            # Branch already exists from a previous draft — reuse it.
            log.info("branch %s already exists, updating", branch)

        path = f"drafted_rules/{source_id}.json"
        content = json.dumps(
            {"source_id": source_id, "label": source_label, "note": note, "rule": rule},
            indent=2,
        )
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")

        # Look up the file's sha on the branch if it already exists (update vs create).
        existing_sha = None
        try:
            existing_sha = self._get(f"/contents/{path}?ref={branch}").get("sha")
        except PRError:
            pass

        commit_body = {
            "message": f"Track 2: drafted extraction rule for {source_label}",
            "content": encoded,
            "branch": branch,
        }
        if existing_sha:
            commit_body["sha"] = existing_sha
        self._put(f"/contents/{path}", commit_body)

        # Open the PR (or reuse an open one for this branch).
        existing = self._get(f"/pulls?head={self._repo.split('/')[0]}:{branch}&state=open")
        if existing:
            return existing[0]["html_url"]

        pr = self._post(
            "/pulls",
            {
                "title": f"Track 2: extraction rule for {source_label}",
                "head": branch,
                "base": default_branch,
                "body": (
                    f"Auto-drafted extraction rule for **{source_label}** "
                    f"(`{source_id}`).\n\n"
                    f"> {note}\n\n"
                    "This rule is **inert data** — the crawler interprets it, it is "
                    "not executed. Review the selectors/paths below, then merge. "
                    "After merge, apply it to `sources.extraction_rules` and set the "
                    "source `type` to `rule`.\n\n"
                    "```json\n" + json.dumps(rule, indent=2) + "\n```"
                ),
            },
        )
        return pr["html_url"]
