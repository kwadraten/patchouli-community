#!/usr/bin/env python3
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import tomllib
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.request import Request, urlopen

SCHEMA = "patchouli.community.index/1"
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
PLATFORMS = {"win-x64", "win-arm64", "osx-x64", "osx-arm64", "linux-x64", "linux-arm64"}
VERBS = {"find", "fetch", "send"}


def download(url, maximum=MAX_ARTIFACT_BYTES):
    request = Request(url, headers={"User-Agent": "patchouli-community-index/1"})
    digest = hashlib.sha256()
    size = 0
    chunks = []
    with urlopen(request, timeout=60) as response:
        if response.status != 200:
            raise ValueError(f"HTTP {response.status} from {url}")
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > maximum:
                raise ValueError(f"Download exceeds {maximum} bytes: {url}")
            digest.update(chunk)
            chunks.append(chunk)
    return b"".join(chunks), size, digest.hexdigest()


def source_manifest(repository, revision):
    parsed = urlparse(repository)
    parts = parsed.path.strip("/").removesuffix(".git").split("/")
    if parsed.scheme != "https" or parsed.hostname != "github.com" or len(parts) != 2:
        raise ValueError(f"Only GitHub HTTPS source repositories are supported: {repository}")
    url = f"https://raw.githubusercontent.com/{parts[0]}/{parts[1]}/{revision}/patchouli-plugin.toml"
    content, _, _ = download(url, 256 * 1024)
    return tomllib.loads(content.decode("utf-8"))


def plugin_entry(path):
    with path.open("rb") as handle:
        index = tomllib.load(handle)
    plugin_id = index["id"]
    if path.stem != plugin_id:
        raise ValueError(f"Index filename must equal plugin id: {path}")
    revision = index["source_revision"]
    if len(revision) not in (40, 64) or any(c not in "0123456789abcdefABCDEF" for c in revision):
        raise ValueError(f"Invalid pinned source revision for {plugin_id}")
    repository = index["repository"]
    manifest = source_manifest(repository, revision)
    if manifest["id"] != plugin_id:
        raise ValueError(f"Manifest id mismatch for {plugin_id}")
    if manifest.get("host-api") != "patchouli.plugins/1":
        raise ValueError(f"Unsupported host API for {plugin_id}")
    if manifest.get("library", {}).get("grants", []):
        raise ValueError(f"Library grants require explicit host support: {plugin_id}")
    if manifest.get("repository") != repository:
        raise ValueError(f"Index and manifest repositories differ for {plugin_id}")

    verbs = {verb for node in manifest.get("vfs", {}).get("nodes", []) for verb in node["verbs"]}
    if manifest.get("vfs", {}).get("endpoints"):
        verbs.add("send")
    if not verbs or not verbs <= VERBS:
        raise ValueError(f"Unsupported requested verbs for {plugin_id}: {verbs}")

    artifacts = []
    platforms = set()
    for artifact in index["artifacts"]:
        platform = artifact["platform"]
        if platform not in PLATFORMS or platform in platforms:
            raise ValueError(f"Invalid or duplicate platform {platform} for {plugin_id}")
        platforms.add(platform)
        _, size, sha256 = download(artifact["url"])
        if size != artifact["size"] or sha256 != artifact["sha256"].lower():
            raise ValueError(f"Artifact size/SHA-256 mismatch for {plugin_id} {platform}")
        artifacts.append({
            "platform": platform,
            "url": artifact["url"],
            "size": size,
            "sha256": sha256,
        })
    if not artifacts:
        raise ValueError(f"No release artifacts for {plugin_id}")

    return {
        "id": plugin_id,
        "name": manifest["name"],
        "description": manifest["description"],
        "version": manifest["version"],
        "license": manifest["license"],
        "repository": repository,
        "source_revision": revision.lower(),
        "requested_verbs": sorted(verbs),
        "library_grants": [],
        "artifacts": artifacts,
    }


def sign_minisign(content, seed, key_id, trusted_comment):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.from_private_bytes(seed)
    digest = hashlib.blake2b(content, digest_size=64).digest()
    signature = private.sign(digest)
    comment_signature = private.sign(signature + trusted_comment.encode("utf-8"))
    envelope = b"ED" + key_id + signature
    return (
        "untrusted comment: Patchouli community index\n"
        + base64.b64encode(envelope).decode("ascii")
        + "\ntrusted comment: " + trusted_comment + "\n"
        + base64.b64encode(comment_signature).decode("ascii") + "\n"
    ).encode("ascii")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sign", action="store_true")
    parser.add_argument("--output", default="_site")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    plugins = [plugin_entry(path) for path in sorted((root / "plugins").glob("*.toml"))]
    document = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "plugins": plugins,
    }
    content = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    output = root / args.output
    if output.exists():
        shutil.rmtree(output)
    target = output / "index" / "v1"
    target.mkdir(parents=True)
    (target / "index.json").write_bytes(content)
    if args.sign:
        seed = base64.b64decode(os.environ["PATCHOULI_INDEX_SIGNING_SEED"], validate=True)
        key_id = base64.b64decode(os.environ["PATCHOULI_INDEX_KEY_ID"], validate=True)
        if len(seed) != 32 or len(key_id) != 8:
            raise ValueError("Invalid signing seed or key id length")
        trusted = f"timestamp:{int(datetime.now(timezone.utc).timestamp())}\tfile:index.json\thashed"
        (target / "index.json.minisig").write_bytes(sign_minisign(content, seed, key_id, trusted))
    if (root / "keys").exists():
        shutil.copytree(root / "keys", output / "keys")
    print(f"Built signed={args.sign} plugin_count={len(plugins)} index={target / 'index.json'}")


if __name__ == "__main__":
    main()
