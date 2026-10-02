#!/usr/bin/env python3
"""Make an awnix seed: the stick that turns an interactive install into an unattended one.

    make-awnix-seed.py --admin NAME [--ssh-key FILE]... [--github-user U]
                       [--password-hash HASH] [--disk auto|/dev/X --wipe] [--hostname H]
                       [--license FILE] [--answers FILE] [--set STEP=VALUE]...
                       [--finish-setup] [--legacy-token-file F]
                       --out DIR | --out seed.img
    make-awnix-seed.py --self-test

Boot the awnix (or garg appliance) ISO with this volume attached and the installer takes
the disk, admin user, SSH keys and hostname from it; first boot then copies license.lic
to the activation spool, setup-answers.json to /etc/aither, and applies each --set value
through the same argv path the setup UI uses. With no seed the ISO ASKS instead -- it
never wipes a disk without an answer.

--out DIR writes the files into a directory (copy them onto any FAT/ext4 volume you
LABEL AWNIX_SEED yourself). --out X.img writes an 8 MiB FAT image labelled AWNIX_SEED
(needs mkfs.vfat + mcopy, from dosfstools + mtools; exit 2 if missing) that attaches to
a VM as a drive or dd's onto a spare stick.

REFUSED: private keys, anything that is not an OpenSSH public key line, plaintext
passwords (give --password-hash from `openssl passwd -6`), and --wipe without --disk.

Exit: 0 written, 1 refused, 2 could not (missing tool, unreadable input).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from awnix_setup import SEED_SCHEMA, core  # noqa: E402

LABEL = "AWNIX_SEED"


def build_seed(a: argparse.Namespace) -> tuple[dict, list[str]]:
    errs: list[str] = []
    keys: list[str] = []
    for kf in a.ssh_key or []:
        try:
            text = Path(kf).read_text(encoding="utf-8")
        except OSError as e:
            errs.append(f"--ssh-key {kf}: {e}")
            continue
        if core.looks_private(text):
            errs.append(f"--ssh-key {kf} is a PRIVATE key; give the .pub file")
            continue
        for line in core.parse_keys_blob(text):
            if core.pubkey_shape_ok(line):
                keys.append(" ".join(line.split()))
            else:
                errs.append(f"--ssh-key {kf}: not an OpenSSH public key line: {line[:40]}...")
    if a.password:
        errs.append(
            "--password is refused: plaintext never goes on a seed. Use "
            '--password-hash "$(openssl passwd -6)"'
        )
    admin: dict = {"name": a.admin, "ssh_keys": keys}
    if a.github_user:
        admin["github_user"] = a.github_user
    if a.password_hash:
        admin["password_hash"] = a.password_hash
    if not keys and not a.github_user and not a.password_hash:
        errs.append(
            "the admin needs --ssh-key, --github-user or --password-hash; an account "
            "with none of them cannot log in"
        )
    seed: dict = {"schema": SEED_SCHEMA, "admin": admin, "wipe": bool(a.wipe)}
    if a.hostname:
        seed["hostname"] = a.hostname
    if a.disk:
        seed["disk"] = a.disk
    setup: dict = {}
    for kv in a.set or []:
        if "=" not in kv:
            errs.append(f"--set {kv}: expected STEP=VALUE")
            continue
        k, v = kv.split("=", 1)
        setup[k.strip()] = v
    if setup:
        seed["setup"] = setup
    if a.finish_setup:
        seed["finish_setup"] = True
    errs += core.validate_seed(seed)
    return seed, errs


def gather_files(a: argparse.Namespace, seed: dict) -> tuple[dict[str, bytes], list[str]]:
    files = {"awnix-seed.json": (json.dumps(seed, indent=2, sort_keys=True) + "\n").encode()}
    errs: list[str] = []
    if a.license:
        body = Path(a.license).read_text(encoding="utf-8", errors="replace").strip()
        if not body.startswith("AITHER1.") or body.count(".") != 2 or len(body) > 16384:
            errs.append("--license is not an AITHER1.<payload>.<sig> envelope")
        else:
            files["license.lic"] = (body + "\n").encode()
    if a.answers:
        try:
            ans = json.loads(Path(a.answers).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            errs.append(f"--answers: {e}")
        else:
            blob = json.dumps(ans).lower()
            if any(w in blob for w in ('"password"', '"secret"', '"token"', "private key")):
                errs.append("--answers carries an inline secret; setup-answers.json must not")
            files["setup-answers.json"] = (json.dumps(ans, indent=2) + "\n").encode()
    if a.legacy_token_file:
        tok = Path(a.legacy_token_file).read_text(encoding="utf-8").strip()
        if not tok:
            errs.append("--legacy-token-file is empty")
        files["token.txt"] = (tok + "\n").encode()
    return files, errs


def write_dir(out: Path, files: dict[str, bytes]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        p = out / name
        p.write_bytes(data)
        try:
            os.chmod(p, 0o600)
        except OSError as e:
            print(f"make-awnix-seed: warning: chmod 0600 {p}: {e}", file=sys.stderr)


def write_img(out: Path, files: dict[str, bytes]) -> int:
    mkfs, mcopy = shutil.which("mkfs.vfat") or shutil.which("mkfs.fat"), shutil.which("mcopy")
    if not mkfs or not mcopy:
        print(
            "make-awnix-seed: --out *.img needs mkfs.vfat and mcopy (dosfstools, mtools); "
            "use --out DIR and label the volume AWNIX_SEED yourself",
            file=sys.stderr,
        )
        return 2
    if out.exists():
        out.unlink()
    r = subprocess.run(
        [mkfs, "-C", "-n", LABEL, str(out), "8192"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if r.returncode != 0:
        print(f"make-awnix-seed: mkfs failed: {r.stderr.strip()}", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as td:
        for name, data in files.items():
            (Path(td) / name).write_bytes(data)
            r = subprocess.run(
                [mcopy, "-i", str(out), str(Path(td) / name), "::" + name],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if r.returncode != 0:
                print(f"make-awnix-seed: mcopy {name} failed: {r.stderr.strip()}", file=sys.stderr)
                return 2
    return 0


def run(a: argparse.Namespace) -> int:
    if not a.admin or not a.out:
        print("make-awnix-seed: --admin and --out are required", file=sys.stderr)
        return 1
    seed, errs = build_seed(a)
    files, ferrs = gather_files(a, seed) if not errs else ({}, [])
    errs += ferrs
    if errs:
        for e in errs:
            print(f"  REFUSED {e}", file=sys.stderr)
        return 1
    out = Path(a.out)
    if out.suffix == ".img":
        rc = write_img(out, files)
        if rc:
            return rc
    else:
        write_dir(out, files)
    print(
        f"make-awnix-seed: wrote {', '.join(sorted(files))} -> {out}"
        f"{'' if out.suffix == '.img' else f' (label the volume {LABEL})'}"
    )
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--admin")
    ap.add_argument("--ssh-key", action="append", metavar="FILE")
    ap.add_argument("--github-user")
    ap.add_argument("--password-hash")
    ap.add_argument("--password", help=argparse.SUPPRESS)  # exists only to be refused
    ap.add_argument("--disk")
    ap.add_argument("--wipe", action="store_true")
    ap.add_argument("--hostname")
    ap.add_argument("--license", metavar="FILE")
    ap.add_argument("--answers", metavar="FILE")
    ap.add_argument("--set", action="append", metavar="STEP=VALUE")
    ap.add_argument("--finish-setup", action="store_true")
    ap.add_argument("--legacy-token-file", metavar="FILE")
    ap.add_argument("--out")
    ap.add_argument("--self-test", action="store_true")
    return ap


def self_test() -> int:
    fails = 0

    def chk(c: bool, label: str) -> None:
        nonlocal fails
        print(f"  {'ok  ' if c else 'FAIL'} {label}")
        fails += 0 if c else 1

    td = Path(tempfile.mkdtemp(prefix="awnix-seed-st-"))
    try:
        pub = td / "id.pub"
        # Key-shaped fixtures are assembled at run time: the file carries no literal (PRT005).
        pub.write_text(
            "ssh-ed25519 " + "AAAAC3NzaC1lZDI1NTE5AAAAI" + "FakeFakeFake proof@ci\n",
            encoding="utf-8",
        )
        priv = td / "id"
        pem = "OPENSSH " + "PRIVATE KEY"
        priv.write_text(f"-----BEGIN {pem}-----\nAAAA\n-----END {pem}-----\n", encoding="utf-8")
        lic = td / "a.lic"
        lic.write_text("AITHER1.cGF5bG9hZA.c2ln\n", encoding="utf-8")
        ans = td / "answers.json"
        ans.write_text('{"profile": "garg", "hostname": "garg-01"}', encoding="utf-8")

        def go(*argv: str) -> int:
            return run(parser().parse_args(list(argv)))

        out = td / "seed"
        rc = go(
            "--admin",
            "proof",
            "--ssh-key",
            str(pub),
            "--disk",
            "auto",
            "--wipe",
            "--hostname",
            "garg-01",
            "--license",
            str(lic),
            "--answers",
            str(ans),
            "--set",
            "garg-model=4B",
            "--out",
            str(out),
        )
        chk(rc == 0, "a well-formed seed is written")
        seed = json.loads((out / "awnix-seed.json").read_text(encoding="utf-8"))
        chk(
            seed["schema"] == 1
            and seed["admin"]["name"] == "proof"
            and seed["wipe"] is True
            and seed["disk"] == "auto",
            "awnix-seed.json v1 carries admin, disk, wipe",
        )
        chk(seed["setup"] == {"garg-model": "4B"}, "--set lands in setup{}")
        chk(
            (out / "license.lic").read_text(encoding="utf-8").startswith("AITHER1."),
            "license.lic is written beside it",
        )
        chk((out / "setup-answers.json").exists(), "setup-answers.json is written beside it")
        chk(core.validate_seed(seed) == [], "the written seed passes the first-boot validator")
        chk(
            "update_token" not in seed and "license_file" not in seed,
            "dropped fields (update_token, license_file) never appear",
        )
        chk(
            go("--admin", "proof", "--ssh-key", str(priv), "--out", str(td / "x")) == 1,
            "a private key is refused",
        )
        chk(
            go("--admin", "proof", "--password", "hunter2", "--out", str(td / "x")) == 1,
            "a plaintext password is refused",
        )
        chk(
            go("--admin", "proof", "--password-hash", "plain", "--out", str(td / "x")) == 1,
            "a password_hash that is not crypt(3) is refused",
        )
        chk(
            go("--admin", "proof", "--ssh-key", str(pub), "--wipe", "--out", str(td / "x")) == 1,
            "--wipe without --disk is refused",
        )
        chk(
            go("--admin", "root", "--ssh-key", str(pub), "--out", str(td / "x")) == 1,
            "root as the admin is refused",
        )
        chk(
            go("--admin", "proof", "--out", str(td / "x")) == 1,
            "an admin with no way to log in is refused",
        )
        bad = td / "bad.lic"
        bad.write_text("not a license", encoding="utf-8")
        chk(
            go(
                "--admin",
                "proof",
                "--ssh-key",
                str(pub),
                "--license",
                str(bad),
                "--out",
                str(td / "x"),
            )
            == 1,
            "a non-AITHER1 license is refused",
        )
        secret_ans = td / "s.json"
        secret_ans.write_text('{"password": "x"}', encoding="utf-8")
        chk(
            go(
                "--admin",
                "proof",
                "--ssh-key",
                str(pub),
                "--answers",
                str(secret_ans),
                "--out",
                str(td / "x"),
            )
            == 1,
            "answers with an inline secret are refused",
        )
        chk(not (td / "x").exists(), "a refused seed writes nothing")
        if (shutil.which("mkfs.vfat") or shutil.which("mkfs.fat")) and shutil.which("mcopy"):
            img = td / "seed.img"
            chk(
                go("--admin", "proof", "--ssh-key", str(pub), "--out", str(img)) == 0
                and img.stat().st_size > 0,
                "a FAT image is written",
            )
        else:
            print("  skip FAT image (mkfs.vfat/mcopy not installed here; the lane has them)")
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print("SELF-TEST PASS" if not fails else f"SELF-TEST FAILED ({fails})")
    return 0 if not fails else 1


def main() -> int:
    a = parser().parse_args()
    if a.self_test:
        return self_test()
    return run(a)


if __name__ == "__main__":
    sys.exit(main())
