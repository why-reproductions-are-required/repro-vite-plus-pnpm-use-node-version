#!/usr/bin/env python3
"""Reproduce pnpm's extra Node fetch through the published Vite+ installer."""

import argparse
import base64
import hashlib
import http.server
import io
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


# Latest stable Vite+ on 2026-11-06; these are its shipped installation tools.
VITE_VERSION = "1.5.0"
PNPM_VERSION = "10.34.7"
NODE_VERSION = "24.22.2"
USER_NODE_VERSION = "22.18.0"
ROOT = Path(__file__).resolve().parent
CACHE = ROOT / ".artifacts"


def fetch(url, name, expected=None, algorithm="sha512"):
    """Cache public release artifacts; verify their published digest before use."""
    destination = CACHE / name
    CACHE.mkdir(exist_ok=True)
    data = destination.read_bytes() if destination.exists() else None
    if data is not None and expected is not None:
        if hashlib.new(algorithm, data).digest() != expected:
            data = None
    if data is None:
        print(f"Downloading {url}", flush=True)
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read()
        if expected is not None and hashlib.new(algorithm, data).digest() != expected:
            raise RuntimeError(f"Digest mismatch: {url}")
        destination.write_bytes(data)
    return data


def npm_package(name, version):
    cache_name = name.replace("/", "-").replace("@", "") + "-" + version
    metadata = json.loads(fetch(
        f"https://registry.npmjs.org/{name}/{version}", cache_name + ".json"
    ))
    algorithm, digest = metadata["dist"]["integrity"].split("-", 1)
    data = fetch(metadata["dist"]["tarball"], cache_name + ".tgz",
                 base64.b64decode(digest), algorithm)
    return metadata, data


def extract_files(data, destination, prefix, selected=None):
    """Extract regular files only, with a fixed archive prefix and no traversal."""
    found = set()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            if not member.isfile() or not member.name.startswith(prefix):
                continue
            relative = PurePosixPath(member.name[len(prefix):])
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError(f"Unexpected archive path: {member.name}")
            name = str(relative)
            if selected is not None and name not in selected:
                continue
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(member).read())
            target.chmod(member.mode & 0o777)
            found.add(name)
    if selected is not None and found != set(selected):
        raise RuntimeError(f"Missing archive files: {set(selected) - found}")


def host_platform():
    system = {"Darwin": "darwin", "Linux": "linux"}.get(platform.system())
    machine = {"arm64": "arm64", "aarch64": "arm64",
               "x86_64": "x64", "AMD64": "x64"}.get(platform.machine())
    if system is None or machine is None:
        raise RuntimeError("This reproduction supports macOS/Linux on arm64 or x64.")
    if system == "linux" and any(Path("/lib").glob("ld-musl-*.so.1")):
        raise RuntimeError("Use a glibc Linux distribution; the official Node archive requires it.")
    return system, machine


def isolated_env(case, npmrc, registry):
    # Do not inherit the user's package-manager configuration, tool overrides,
    # credentials, proxies, or shell setup. Keep OS basics such as locale/TMPDIR.
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("VP_", "VITE_PLUS_", "XDG_", "NPM_", "PNPM_",
                                           "NODE_", "COREPACK_", "BUN_", "YARN_"))
           and not key.upper().endswith("_PROXY")}
    home = case / "user"
    home.mkdir(parents=True)
    env.update({
        "HOME": str(home), "PATH": "/usr/bin:/bin", "CI": "true",
        "VP_HOME": str(case / "vp-home"), "PNPM_HOME": str(case / "pnpm-home"),
        "XDG_CONFIG_HOME": str(home / "config"), "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_DATA_HOME": str(home / "data"), "XDG_STATE_HOME": str(home / "state"),
        "NPM_CONFIG_USERCONFIG": str(npmrc), "NPM_CONFIG_REGISTRY": registry,
        "VP_NODE_MANAGER": "yes", "VP_SELF_SETUP_NO_MODIFY_PATH": "1",
        "VP_SELF_SETUP_REPLACE_EXISTING": "1", "NO_PROXY": "127.0.0.1,localhost",
    })
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="keep the isolated installation directories")
    args = parser.parse_args()
    system, machine = host_platform()
    suffix = f"{system}-{machine}" + ("-gnu" if system == "linux" else "")
    cli_metadata, cli_archive = npm_package(f"@voidzero-dev/vite-plus-cli-{suffix}", VITE_VERSION)
    pnpm_metadata, pnpm_archive = npm_package("pnpm", PNPM_VERSION)
    if cli_metadata.get("vpNodeVersion") != NODE_VERSION:
        raise RuntimeError("The release's pinned Node version does not match this fixture.")

    node_name = f"node-v{NODE_VERSION}-{system}-{machine}.tar.gz"
    node_base = f"https://nodejs.org/dist/v{NODE_VERSION}/"
    checksums = fetch(node_base + "SHASUMS256.txt", f"node-{NODE_VERSION}-SHASUMS256.txt").decode()
    node_checksum = next(line.split()[0] for line in checksums.splitlines()
                         if line.split()[-1] == node_name)
    node_archive = fetch(node_base + node_name, node_name, bytes.fromhex(node_checksum), "sha256")

    temporary = Path(tempfile.mkdtemp(prefix="vite-plus-pnpm-node-repro-"))
    server = None
    try:
        payload = temporary / "download"
        extract_files(cli_archive, payload, "package/", {"vp", "vp-install-lock.yaml"})
        tools = temporary / "tools"
        extract_files(node_archive, tools / "node", f"node-v{NODE_VERSION}-{system}-{machine}/", {"bin/node"})
        extract_files(pnpm_archive, tools / "pnpm", "package/")
        (tools / "pnpm/bin/pnpm").symlink_to("pnpm.cjs")
        node = tools / "node/bin/node"
        assert subprocess.check_output([str(node), "--version"], text=True).strip() == "v" + NODE_VERSION

        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Reproduction fixture: stop before downloading an archive.\n")

            def log_message(self, *unused):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        results = []
        print(f"Vite+ {VITE_VERSION}; installation Node {NODE_VERSION}; pnpm {PNPM_VERSION}")
        for name, pinned in [("control", False), ("user-node-version", True)]:
            case = temporary / name
            npmrc = case / "user/.npmrc"
            env = isolated_env(case, npmrc, base + "/npm/")
            npmrc.write_text((f"use-node-version={USER_NODE_VERSION}\n" if pinned else "")
                             + f"node-mirror:release={base}/node/\nfetch-retries=0\n")
            vp_home = case / "vp-home"
            shutil.copytree(tools / "node", vp_home / f"js_runtime/node/{NODE_VERSION}")
            shutil.copytree(tools / "pnpm", vp_home / f"package_manager/pnpm/{PNPM_VERSION}/pnpm", symlinks=True)
            assert "VP_ENV_FORBIDDEN" not in env
            assert "VP_SETUP_SKIP_INSTALL_LOCKFILE" not in env
            requests.clear()
            result = subprocess.run([str(payload / "vp"), "--help"], cwd=case, env=env,
                                    text=True, capture_output=True, timeout=60)
            paths = sorted(set(requests))
            node_requests = [path for path in paths if path.startswith("/node/")]
            package_requests = [path for path in paths if path.startswith("/npm/")]
            results.append({
                "case": name, "user_node_version": USER_NODE_VERSION if pinned else None,
                "vp_exit_code": result.returncode, "requests": paths,
                "stdout": result.stdout.replace(str(temporary), "<temporary>"),
                "stderr": result.stderr.replace(str(temporary), "<temporary>"),
            })
            print(f"{name}: vp exit {result.returncode}; {len(node_requests)} Node request(s); "
                  f"{len(package_requests)} dependency request(s)")
            for path in node_requests:
                print(f"  GET {path}")

        control, configured = results
        reproduced = (
            control["vp_exit_code"] != 0 and configured["vp_exit_code"] != 0
            and any(path.startswith("/npm/") for path in control["requests"])
            and not any(path.startswith("/node/") for path in control["requests"])
            and f"/node/v{USER_NODE_VERSION}/SHASUMS256.txt" in configured["requests"]
            and not any(path.startswith("/npm/") for path in configured["requests"])
        )
        report = {
            "recorded_at": datetime.now(timezone.utc).isoformat(), "platform": suffix,
            "vite_plus": VITE_VERSION, "pnpm": PNPM_VERSION, "installation_node": NODE_VERSION,
            "release_commit": cli_metadata.get("gitHead"),
            "vp_env_forbidden": "unset", "installation_lockfile": "published, unchanged",
            "artifacts": {"vite_plus_cli": cli_metadata["dist"], "pnpm": pnpm_metadata["dist"],
                          "node": {"url": node_base + node_name, "sha256": node_checksum}},
            "reproduced": reproduced, "cases": results,
        }
        (ROOT / "results").mkdir(exist_ok=True)
        (ROOT / "results/latest.json").write_text(json.dumps(report, indent=2) + "\n")
        if not reproduced:
            raise RuntimeError("Expected request difference was not observed; inspect results/latest.json.")
        print("REPRODUCED: user npm configuration triggers an extra Node fetch on the released installer.")
        print("Both vp failures are intentional: the local server returns 404. See results/latest.json.")
        return 0
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if args.keep:
            print(f"Kept isolated installation: {temporary}")
        else:
            shutil.rmtree(temporary)


if __name__ == "__main__":
    raise SystemExit(main())
