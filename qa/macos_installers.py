"""Run exact-byte installation and validation only on a disposable GitHub Mac."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time

SPEC = json.loads((Path(__file__).parent / "expected.json").read_text())
REPO = "mikersn/dubspot-plugin-downloads"
PRODUCTS = SPEC["products"]
PACKAGES = [*PRODUCTS, SPEC["bundle"]]


def require(ok, why):
    if not ok:
        raise RuntimeError(why)


def run(*cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as src:
        for block in iter(lambda: src.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def checked_paths(product):
    allowed = ("/Library/Audio/Plug-Ins/Components/Dubspot ", "/Library/Audio/Plug-Ins/VST3/Dubspot ",
               "/Library/Application Support/Avid/Audio/Plug-Ins/Dubspot ", "/Library/Application Support/Dubspot/")
    for path, expected in product["trees"].items():
        target = Path("/") / path
        require(str(target).startswith(allowed) and target.resolve() == target, "Unrecognized install path")
        require(target.name == "Documentation" or target.suffix in (".component", ".vst3", ".aaxplugin"), "Invalid install root")
        yield target, expected


def contents(root):
    files = []
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), f"Unexpected link: {path}")
        if path.is_file():
            files.append(dict(path=path.relative_to(root).as_posix(), size=path.stat().st_size,
                              mode=path.stat().st_mode & 0o777, sha256=digest(path)))
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def pristine():
    for item in PRODUCTS:
        for p, _ in checked_paths(item):
            require(not p.exists(), f"Pre-existing plug-in/docs: {p}")
        for rid in item["receipts"]:
            require(subprocess.run(["pkgutil", "--pkg-info", rid], capture_output=True).returncode != 0, "Receipt already present: " + rid)


def remove_ours():
    # No globbing, no shared parent directory removal. Preserve any extra user files.
    for item in PRODUCTS:
        for p, expected in checked_paths(item):
            require(p.is_dir() and contents(p) == expected, f"Cannot safely remove modified payload: {p}")
            run("sudo", "-n", "/bin/rm", "-r", str(p))
        for rid in item["receipts"]:
            require(re.fullmatch(r"com\.dubspot\.[a-z0-9]+\.pkg\.(au|vst3|aax|docs)", rid), "Invalid receipt ID")
            run("sudo", "-n", "pkgutil", "--forget", rid)
    pristine()


def install(item, tmp):
    package = tmp / item["package"]
    print("Installing:", package.name, flush=True)
    require(digest(package) == item["sha256"], "Installer differs from the recorded candidate")
    run("sudo", "-n", "/usr/sbin/installer", "-pkg", str(package), "-target", "/", timeout=240)


def verify_installed():
    for item in PRODUCTS:
        for p, expected in checked_paths(item):
            require(p.is_dir() and contents(p) == expected, f"Installed bytes or modes differ: {p}")
            if p.name != "Documentation":
                run("codesign", "--verify", "--deep", "--strict", str(p))
        for rid in item["receipts"]:
            detail = run("pkgutil", "--pkg-info", rid, capture_output=True, text=True).stdout
            require("version: " + item["version"] in detail.splitlines(), f"Wrong receipt: {rid}")
        print("Installed trees, signatures and receipts match:", item["name"], flush=True)


def validate_aus():
    failed = []
    for item in PRODUCTS:
        good = False
        for attempt in range(3):
            got = subprocess.run(["/usr/bin/auval", "-v", *item["au"]], text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=100)
            print(got.stdout, flush=True)
            good = got.returncode == 0 and "AU VALIDATION SUCCEEDED" in got.stdout
            if good or "couldn't be opened" not in got.stdout.lower() and "not found" not in got.stdout.lower():
                break
            time.sleep(3)
        print("AU validation:", item["name"], good, flush=True)
        if not good:
            failed.append(item["name"])
    return failed


def main(mode):
    require(os.getenv("GITHUB_ACTIONS") == "true" and os.getenv("RUNNER_ENVIRONMENT") == "github-hosted"
            and os.getenv("GITHUB_REPOSITORY") == REPO and os.getenv("GITHUB_REF") == "refs/heads/qualification/analog-macos"
            and platform.system() == "Darwin", "Only a fresh GitHub-hosted Mac on the qualification branch is permitted")
    tmp = Path(os.environ["RUNNER_TEMP"]) / "dubspot-candidates"
    if mode == "download":
        tmp.mkdir()
        run("gh", "release", "download", SPEC["tag"], "--repo", REPO, "--dir", str(tmp), "--pattern", "*.pkg", timeout=300)
        require({p.name for p in tmp.iterdir()} == {p["package"] for p in PACKAGES}, "Unexpected package list")
        for item in PACKAGES:
            require(digest(tmp / item["package"]) == item["sha256"], "Download checksum mismatch")
        return
    require(mode == "qualify" and platform.machine() == os.environ["EXPECTED_ARCH"], "Wrong test environment")
    print("macOS", platform.mac_ver()[0], platform.machine(), flush=True)
    pristine()
    for item in PACKAGES:
        p = tmp / item["package"]
        require(digest(p) == item["sha256"], "Package changed")
        sig = run("pkgutil", "--check-signature", str(p), text=True, stdout=subprocess.PIPE).stdout
        require("Developer ID Installer:" in sig and "(ZD8632A7UJ)" in sig, "Unexpected installer signer")
        for n in range(3):
            ticket = subprocess.run(["xcrun", "stapler", "validate", str(p)], text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, timeout=60)
            if ticket.returncode == 0:
                print("Apple ticket validated:", p.name, flush=True)
                break
            if "NSURLErrorDomain" not in ticket.stdout or "cloudkit.com" not in ticket.stdout:
                raise RuntimeError(f"Invalid installer ticket: {p.name} {ticket.stdout}")
            if n == 2:
                print("Online Apple ticket lookup inconclusive (CloudKit transport error):", p.name, flush=True)
            else:
                time.sleep(2 + 3*n)
        # The public bytes are pinned and local Gatekeeper must still accept every installer.
        run("spctl", "--assess", "--type", "install", "--verbose=2", str(p), timeout=60)
    for item in PRODUCTS:
        install(item, tmp)
    verify_installed()
    issues = validate_aus()
    print("Checking removal before a clean bundle install", flush=True)
    remove_ours()
    install(SPEC["bundle"], tmp)
    verify_installed()
    for item in PRODUCTS:  # Same-version overlay, not a claim about upgrading from older versions.
        install(item, tmp)
    verify_installed()
    remove_ours()
    require(not issues, "Failed Audio Units: " + ", ".join(issues))
    print("PASS: clean individual and bundle installs, packaged bytes, receipts, AU validation, same-version reinstall and scoped removal.", flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
