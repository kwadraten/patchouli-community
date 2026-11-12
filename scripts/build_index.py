#!/usr/bin/env python3
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tomllib
import zipfile
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

SCHEMA = "patchouli.community.index/1"
SITE_BASE = "https://kwadraten.github.io/patchouli-community"
REPOSITORY = "https://github.com/kwadraten/patchouli-community"
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_PACKAGE_BYTES = 256 * 1024 * 1024
PLATFORMS = {"win-x64", "win-arm64", "osx-x64", "osx-arm64", "linux-x64", "linux-arm64"}
VERBS = {"find", "fetch", "send", "view"}
EXTENSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RESERVED_EXTENSION_PREFIXES = ("metadata.", "translation.", "ocr.", "pdf-text.", "layout.")
EXTENSION_KINDS = {"translation", "metadata", "ocr", "retrieval", "pdf-text-layer", "layout-recognition"}
EXTENSION_HOST_APIS = {"patchouli.extensions/3", "patchouli.extensions/4"}
STRICT_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$")
LOAD_REFERENCE = re.compile(r'#load\s+@?\s*"([^"]+)"')
RUNTIME_REFERENCE = re.compile(r'#r\s+@?\s*"([^"]+)"')
FORBIDDEN_PACKAGE_FILES = {"extension.yml", "patchouli-plugin.toml", "plugin.yml",
                           "config.json", "extension-state.json"}
FORBIDDEN_PACKAGE_DIRS = {".generations", ".lkg", ".compiled"}


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


def verify_package(content, source, plugin_id):
    with zipfile.ZipFile(io.BytesIO(content)) as package:
        names = {name for name in package.namelist() if not name.endswith("/")}
        if "patchouli-plugin.toml" not in names:
            raise ValueError(f"Package manifest is missing for {plugin_id}")
        manifest = tomllib.loads(package.read("patchouli-plugin.toml").decode("utf-8"))
        if manifest != source:
            raise ValueError(f"Package and pinned-source manifests differ for {plugin_id}")
        assets = [manifest["readme"]]
        assets += [node["schema"] for node in manifest["vfs"]["nodes"] if "schema" in node]
        assets += [message[key] for endpoint in manifest["vfs"].get("endpoints", [])
                   for message in endpoint["messages"] for key in ("input_schema", "result_schema")]
        missing = [name for name in assets if name not in names]
        if missing:
            raise ValueError(f"Package declaration assets are missing for {plugin_id}: {missing}")


def normalize_package_path(relative):
    path = relative.replace("\\", "/")
    if path.startswith("/") or ":" in path or any(c in path for c in '<>"|?*\0'):
        raise ValueError(f"Extension source paths must be relative portable paths: {relative}")
    segments = path.split("/")
    if any(not segment or segment in (".", "..") or segment.endswith((" ", "."))
           for segment in segments):
        raise ValueError(f"Extension source paths cannot contain empty, dot, or parent segments: {relative}")
    return path


def package_files(directory):
    files = {}
    for path in sorted(directory.rglob("*")):
        relative = normalize_package_path(path.relative_to(directory).as_posix())
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Extension packages only contain regular files: {relative}")
        if relative in FORBIDDEN_PACKAGE_FILES or relative.split("/")[0] in FORBIDDEN_PACKAGE_DIRS:
            raise ValueError(f"Reserved package path is not allowed in extension sources: {relative}")
        files[relative] = path
    if not files:
        raise ValueError(f"Extension package is empty: {directory}")
    total = sum(path.stat().st_size for path in files.values())
    if total > MAX_PACKAGE_BYTES:
        raise ValueError(f"Extension package exceeds {MAX_PACKAGE_BYTES} bytes: {directory}")
    return files


def extension_doc_block(source, extension_id):
    lines = []
    exported = 0
    selected = None
    for line in source.split("\n"):
        line = line.rstrip("\r")
        if line.startswith("///"):
            lines.append(line[3:])
        elif line.startswith("let extension") and (
                len(line) == 13 or line[13].isspace() or line[13] in ":="):
            exported += 1
            if exported == 1 and lines:
                selected = "\n".join(lines)
            else:
                lines.clear()
        elif line.strip():
            lines.clear()
    if exported != 1:
        raise ValueError(f"Extension {extension_id} must export exactly one top-level 'let extension' binding")
    if selected is None:
        raise ValueError(f"Extension {extension_id} is missing the /// XML package declaration")
    try:
        root = ElementTree.fromstring("<documentation>\n" + selected + "\n</documentation>")
    except ElementTree.ParseError as error:
        raise ValueError(f"Extension {extension_id} has invalid declaration XML: {error}") from error
    declarations = root.findall("patchouli-extension")
    if len(declarations) != 1:
        raise ValueError(f"Extension {extension_id} must declare exactly one <patchouli-extension> element")
    declaration = declarations[0]
    attributes = set(declaration.attrib)
    if attributes != {"id", "kind", "version", "host-api"} or \
            any(not declaration.get(name) for name in attributes):
        raise ValueError(f"Extension {extension_id} requires id, kind, version, and host-api attributes")
    children = [child.tag for child in declaration]
    if any(tag not in ("name", "summary", "remarks") for tag in children) or children.count("name") != 1:
        raise ValueError(f"Extension {extension_id} allows only name/summary/remarks and one <name>")
    name = (declaration.find("name").text or "").strip()
    if not name:
        raise ValueError(f"Extension {extension_id} requires a non-empty <name> element")
    summaries = [element for element in root if element.tag == "summary"]
    if len(summaries) != 1 or not (summaries[0].text or "").strip():
        raise ValueError(f"Extension {extension_id} requires one non-empty <summary> element")
    return declaration, name, summaries[0].text.strip()


def resolve_package_reference(base, reference, extension_id):
    segments = []
    for segment in (base + reference.replace("\\", "/")).split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not segments:
                raise ValueError(
                    f"Extension {extension_id} reference escapes the package: {reference}")
            segments.pop()
        else:
            segments.append(segment)
    return "/".join(segments)


def check_fsx_references(files, extension_id):
    sources = {path: file.read_text(encoding="utf-8")
               for path, file in files.items() if path.endswith((".fsx", ".fs", ".fsi"))}
    for path, source in sources.items():
        base = path.rsplit("/", 1)[0] + "/" if "/" in path else ""
        for pattern in (LOAD_REFERENCE, RUNTIME_REFERENCE):
            for reference in pattern.findall(source):
                target = reference.split(":", 1)
                if len(target) == 2 and target[0].strip().lower() in ("nuget", "framework"):
                    continue
                resolved = resolve_package_reference(base, reference, extension_id)
                if resolved not in files:
                    raise ValueError(
                        f"Extension {extension_id} references missing package file {reference} from {path}")


def last_revision(root, directory):
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--",
             directory.relative_to(root).as_posix()],
            cwd=root, capture_output=True, text=True, check=True)
        revision = result.stdout.strip()
        if revision:
            return revision
    except (OSError, subprocess.CalledProcessError):
        pass
    result = subprocess.run(["git", "rev-parse", "HEAD"],
                            cwd=root, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def deterministic_zip(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative in sorted(files):
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, files[relative].read_bytes())
    return buffer.getvalue()


def extension_entry(root, directory, output):
    extension_id = directory.name
    if not EXTENSION_ID.match(extension_id):
        raise ValueError(f"Invalid extension id directory name: {extension_id}")
    if extension_id.startswith(RESERVED_EXTENSION_PREFIXES):
        raise ValueError(f"Extension id uses a reserved built-in prefix: {extension_id}")
    files = package_files(directory)
    if "main.fsx" not in files:
        raise ValueError(f"Extension {extension_id} is missing main.fsx")
    source = files["main.fsx"].read_text(encoding="utf-8")
    declaration, name, summary = extension_doc_block(source, extension_id)
    if declaration.get("id") != extension_id:
        raise ValueError(f"Extension {extension_id} declares a different package id")
    kind = declaration.get("kind")
    if kind not in EXTENSION_KINDS:
        raise ValueError(f"Extension {extension_id} has unsupported kind '{kind}'")
    version = declaration.get("version")
    host_api = declaration.get("host-api")
    if host_api not in EXTENSION_HOST_APIS:
        raise ValueError(f"Extension {extension_id} targets unsupported host API '{host_api}'")
    if not STRICT_SEMVER.match(version):
        raise ValueError(f"Extension {extension_id} version '{version}' is not a semantic version")
    check_fsx_references(files, extension_id)

    package = deterministic_zip(files)
    digest = hashlib.sha256(package).hexdigest()
    target = output / "extensions" / extension_id / version
    target.mkdir(parents=True)
    (target / "package.zip").write_bytes(package)
    return {
        "id": extension_id,
        "kind": kind,
        "name": name,
        "description": summary,
        "version": version,
        "host_api": host_api,
        "repository": REPOSITORY,
        "source_revision": last_revision(root, directory),
        "artifact": {
            "url": f"{SITE_BASE}/extensions/{extension_id}/{version}/package.zip",
            "size": len(package),
            "sha256": digest,
        },
    }


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
    if manifest.get("ui", {}).get("views"):
        verbs.add("view")
    if not verbs or not verbs <= VERBS:
        raise ValueError(f"Unsupported requested verbs for {plugin_id}: {verbs}")

    artifacts = []
    platforms = set()
    for artifact in index["artifacts"]:
        platform = artifact["platform"]
        if platform not in PLATFORMS or platform in platforms:
            raise ValueError(f"Invalid or duplicate platform {platform} for {plugin_id}")
        platforms.add(platform)
        content, size, sha256 = download(artifact["url"])
        verify_package(content, manifest, plugin_id)
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
    output = root / args.output
    if output.exists():
        shutil.rmtree(output)
    target = output / "index" / "v1"
    target.mkdir(parents=True)
    plugins = [plugin_entry(path) for path in sorted((root / "plugins").glob("*.toml"))]
    extensions_dir = root / "extensions"
    extensions = sorted(
        (extension_entry(root, path, output)
         for path in extensions_dir.iterdir() if path.is_dir()),
        key=lambda entry: entry["id"]) if extensions_dir.is_dir() else []
    document = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "extensions": extensions,
        "plugins": plugins,
    }
    content = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
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
    print(f"Built signed={args.sign} plugin_count={len(plugins)} "
          f"extension_count={len(extensions)} index={target / 'index.json'}")


if __name__ == "__main__":
    main()
