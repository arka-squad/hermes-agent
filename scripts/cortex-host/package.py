#!/usr/bin/env python3
"""Construit le paquet de capacités hôte Cortex pour cette branche.

Usage : package.py <sha-de-base-amont> <dossier-de-sortie>

Le paquet est une archive tar.gz : `manifest.json` puis, pour chaque fichier que
la branche change par rapport à la base amont, son contenu patché à son chemin.
Le manifeste porte la base, la version Hermes, le commit de cette branche, et
pour chaque fichier l'empreinte SHA-256 de l'original amont (vérifiée par
Cortex avant toute écriture) et celle du contenu patché. Les empreintes sont
calculées sur le contenu aux fins de ligne normalisées (\\r\\n → \\n) : un
clone Windows en CRLF reste reconnu.

Cortex embarque ce paquet à son build (`Hermers-plugin/host/generate.py`) et
le télécharge à l'exécution depuis l'index publié (`scripts/publish-hermes-host.sh`
côté Cortex signe et publie). Les deux voies posent exactement ces octets.
"""
import gzip
import hashlib
import io
import json
import pathlib
import re
import subprocess
import sys
import tarfile
import time

root = pathlib.Path(__file__).resolve().parents[2]
base_ref = sys.argv[1]
out_dir = pathlib.Path(sys.argv[2]).resolve()


def git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


def sha_normalized(data: bytes) -> str:
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


base_sha = git("rev-parse", f"{base_ref}^{{commit}}").decode().strip()  # un tag annoté se résout en son commit
host_commit = git("rev-parse", "HEAD").decode().strip()
# Le paquet est reproductible : mêmes commits, mêmes octets. L'horodatage est
# celui du commit hôte, jamais l'heure de construction.
built_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(git("show", "-s", "--format=%ct", "HEAD"))))
if git("status", "--porcelain", "--untracked-files=no").strip():
    raise SystemExit("arbre modifié : commettez avant de construire un paquet")
version = re.search(r'^version\s*=\s*"([^"]+)"', git("show", f"{base_sha}:pyproject.toml").decode(), re.M).group(1)
changes = [line.split("\t") for line in git("diff", "--name-status", base_sha, "HEAD").decode().splitlines() if line]
entries, blobs = [], {}
for status, path in changes:
    if path.startswith("scripts/cortex-host/"):
        continue  # l'outillage de construction n'est pas une capacité hôte
    if status not in {"A", "M"}:
        raise SystemExit(f"statut {status} non pris en charge pour {path}")
    patched = (root / path).read_bytes()
    entry = {"path": path, "patchedSha256": sha_normalized(patched), "created": status == "A"}
    if status == "M":
        entry["baseSha256"] = sha_normalized(git("show", f"{base_sha}:{path}"))
    entries.append(entry)
    blobs[path] = patched
entries.sort(key=lambda e: e["path"])
manifest = {
    "schemaVersion": 1,
    "baseSha": base_sha,
    "hermesVersion": version,
    "hostRepo": "https://github.com/arka-squad/hermes-agent",
    "hostCommit": host_commit,
    "builtAt": built_at,
    "files": entries,
}
out_dir.mkdir(parents=True, exist_ok=True)
name = f"cortex-host-{version}-{base_sha[:8]}.tar.gz"
target = out_dir / name


def add(tar: tarfile.TarFile, path: str, data: bytes) -> None:
    info = tarfile.TarInfo(path)
    info.size = len(data)
    info.mtime = 0
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


buffer = io.BytesIO()
with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as tar:
    add(tar, "manifest.json", (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode())
    for entry in entries:
        add(tar, entry["path"], blobs[entry["path"]])
# gzip sans nom ni date dans son en-tête : rien ne dépend du moment ni du dossier.
with open(target, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
    gz.write(buffer.getvalue())
digest = hashlib.sha256(target.read_bytes()).hexdigest()
(out_dir / (name + ".sha256")).write_text(f"{digest}  {name}\n")
print(json.dumps({"package": str(target), "sha256": digest, "size": target.stat().st_size,
                  "baseSha": base_sha, "hermesVersion": version, "hostCommit": host_commit, "files": len(entries)}))
