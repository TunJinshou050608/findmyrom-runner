"""Private-only GitHub Release assets; token never travels to redirect hosts."""

import hashlib
import os
from pathlib import Path
import re
import time
from urllib.parse import quote
import httpx


class VaultAuthError(RuntimeError):
    pass


def credentials():
    token = os.environ.get("GH_VAULT_TOKEN", "")
    repo = os.environ.get("GH_VAULT_REPO", "")
    if not token or any(c.isspace() or ord(c) < 32 for c in token):
        raise ValueError("Invalid GH_VAULT_TOKEN")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or any(
        x in {".", ".."} for x in repo.split("/")
    ):
        raise ValueError("Invalid GH_VAULT_REPO")
    return token, repo


class VaultClient:
    def __init__(self, token=None, repo=None, http_client=None):
        if token is None or repo is None:
            env_token, env_repo = credentials()
            token = token or env_token
            repo = repo or env_repo
        if not token or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("Invalid Vault configuration")
        self.token, self.repo = token, repo
        self.client = http_client or httpx.Client(timeout=60)
        self.owned = http_client is None
        self.deadline = None

    def close(self):
        if self.owned:
            self.client.close()

    def _request(self, method, path, **kwargs):
        upload = kwargs.pop("upload", False)
        host = "https://uploads.github.com" if upload else "https://api.github.com"
        request_headers = kwargs.pop("headers", {})
        for attempt in range(3):
            if self.deadline is not None and time.monotonic() >= self.deadline:
                raise TimeoutError("Vault deadline exceeded")
            response = self.client.request(
                method,
                host + path,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    **request_headers,
                },
                follow_redirects=False,
                timeout=min(60, max(0.1, self.deadline - time.monotonic()))
                if self.deadline
                else 60,
                **kwargs,
            )
            if response.status_code in {401, 403}:
                raise VaultAuthError("Vault authorization failed")
            if response.status_code < 500 or attempt == 2:
                return response
            if os.environ.get("FMR_TEST_NO_SLEEP") != "1":
                time.sleep(attempt + 1)
        raise RuntimeError("Unreachable")

    def upload_partition_asset(
        self, release_tag: str, gz_path: Path, sha256_hash: str
    ) -> int:
        if not re.fullmatch("[0-9a-f]{64}", sha256_hash):
            raise ValueError("Invalid asset digest")
        blob = Path(gz_path).read_bytes()
        if hashlib.sha256(blob).hexdigest() != sha256_hash:
            raise ValueError("Asset digest mismatch")
        # Check visibility every time, before any release or asset mutation.
        repo = self._request("GET", f"/repos/{self.repo}")
        repo.raise_for_status()
        if repo.json().get("private") is not True:
            raise VaultAuthError("Vault must be private")
        root = f"/repos/{self.repo}/releases"
        release = self._request("GET", root + "/tags/" + quote(release_tag, safe=""))
        if release.status_code == 404:
            release = self._request(
                "POST",
                root,
                json={
                    "tag_name": release_tag,
                    "name": release_tag,
                    "draft": False,
                    "prerelease": False,
                },
            )
            if release.status_code == 422:  # Another worker created the release.
                release = self._request(
                    "GET", root + "/tags/" + quote(release_tag, safe="")
                )
        release.raise_for_status()
        release_id = release.json()["id"]
        if type(release_id) is not int or release_id <= 0:
            raise ValueError("Invalid Release id")
        name = "blob_" + sha256_hash[:20] + ".bin"
        for attempt in range(2):
            uploaded = self._request(
                "POST",
                root + f"/{release_id}/assets",
                upload=True,
                params={"name": name},
                content=blob,
                headers={"Content-Type": "application/octet-stream"},
            )
            if uploaded.status_code != 422 or attempt:
                uploaded.raise_for_status()
                asset_id = uploaded.json().get("id")
                if type(asset_id) is not int or asset_id <= 0:
                    raise ValueError("Invalid asset id")
                return asset_id
            # Exact-name conflict only; do not remove unrelated assets.
            for page in range(1, 101):
                response = self._request(
                    "GET",
                    root + f"/{release_id}/assets",
                    params={"per_page": 100, "page": page},
                )
                response.raise_for_status()
                assets = response.json()
                old = next((a for a in assets if a["name"] == name), None)
                if old:
                    # Content-addressed name plus exact size is the contract's
                    # safe reuse path, avoiding deletion of live referenced assets.
                    if (
                        old.get("size") == len(blob)
                        and type(old.get("id")) is int
                        and old["id"] > 0
                    ):
                        return old["id"]
                    raise ValueError("Conflicting content-addressed asset size")
                if len(assets) < 100:
                    break
        raise RuntimeError("Unresolved asset conflict")
