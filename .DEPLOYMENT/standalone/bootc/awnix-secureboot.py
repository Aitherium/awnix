#!/usr/bin/python3.11
"""awnix secureboot -- Secure Boot + NVIDIA kernel-module state, MOK enrollment, doctor.

Installed at /usr/libexec/awnix/awnix-secureboot, so it runs as `awnix secureboot ...`
through the dispatcher.

Verbs
  status [--json] [--brief] [--write]   detect and report; --write persists
                                        /var/lib/awnix/secureboot/status.json and prints
                                        the serial marker
  enroll [--reveal | --hash-file F] [--cert DER]
                                        queue the image's MOK certificate for enrollment
                                        at the next boot (MokManager)
  doctor [--json]                       per-check verdicts plus one action
  cert [--json]                         the shipped public certificate and its fingerprints
  --self-test                           hermetic fixture sysroots, no host access
  --list-verbs                          one verb per line

Exit: 0 ok (or not applicable), 1 action needed / refused, 2 could not judge.

Security posture
  * The module-signing PRIVATE key never exists on a box. The image ships only the
    public DER at /usr/share/awnix/secureboot/awnix-mok.der.
  * The one-time MOK password is never on argv, never written to disk in clear and never
    stored: it is piped to `openssl passwd -6 -stdin`, the crypt hash goes to a 0600 temp
    file, `mokutil --import DER --hash-file TMP` reads it, and the file is removed.

Test seams
  AWNIX_SB_SYSROOT   path prefix for every /sys, /usr and /var path read or written
  AWNIX_SB_FAKE      JSON file {"<argv prefix>": {"rc":0,"stdout":"","stderr":""}} that
                     replaces every external command (absent key = command not found)
  AWNIX_SB_FAKE_LOG  JSONL file each faked call is appended to ({argv, stdin})
Python 3.10 compatible, stdlib only.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA = 1
VERBS = ("status", "enroll", "doctor", "cert")
EFI_GLOBAL_GUID = "8be4df61-93ca-11d2-aa0d-00e098032b8c"
NVIDIA_VENDOR = "0x10de"
CERT_REL = "usr/share/awnix/secureboot/awnix-mok.der"
MANIFEST_REL = "usr/share/awnix/secureboot/kmod-manifest.json"
STATUS_REL = "var/lib/awnix/secureboot/status.json"
VULKAN_ICD_DIRS = ("usr/share/vulkan/icd.d", "etc/vulkan/icd.d")
# MokManager reads keys with a US layout. Letters that move on QWERTZ/AZERTY (y, z, a, q,
# w, m) and look-alikes (l, 1, o, 0, i) are left out so the password types the same
# everywhere.
PASSWORD_ALPHABET = "bcdefghjknprstuvx" + "23456789"
PASSWORD_LEN = 8

STATES = (
    "not-applicable", "sb-off", "ok", "needs-enroll", "enroll-pending",
    "unsigned-kmod", "kmod-rejected", "driver-not-loaded", "unknown",
)
ACTIONS: Dict[str, str] = {
    "sb-off": "none required: Secure Boot is off, so the NVIDIA module loads unsigned. "
              "To turn it on, run `awnix secureboot enroll --reveal` first, then enable "
              "Secure Boot in firmware.",
    "needs-enroll": "run `sudo awnix secureboot enroll --reveal`, reboot, and in MokManager "
                    "choose Enroll MOK -> Continue -> Yes, then type the one-time password.",
    "enroll-pending": "reboot now; MokManager will ask for the one-time password shown at "
                      "enrollment. Lost it? run `sudo mokutil --revoke-import` and enroll again.",
    "unsigned-kmod": "this image's NVIDIA module is unsigned and Secure Boot is on: update to "
                     "a signed image (`awnix update check`) or turn Secure Boot off in firmware.",
    "kmod-rejected": "the kernel refused the NVIDIA module's signature: confirm the key with "
                     "`awnix secureboot cert` and `mokutil --test-key`, then re-enroll "
                     "(`sudo awnix secureboot enroll --reveal`) and reboot.",
    "driver-not-loaded": "the NVIDIA module is present but not loaded: run "
                         "`sudo modprobe nvidia` and read `journalctl -k -b | grep -i nvidia`.",
    "unknown": "Secure Boot or key state could not be read (mokutil or efivars missing): "
               "run `sudo awnix secureboot doctor` as root and check firmware settings.",
}
OK_STATES = ("ok", "not-applicable", "sb-off")
REJECT_PATTERNS = (
    re.compile(r"Key was rejected by service", re.I),
    re.compile(r"Loading of (unsigned module|module with unavailable key) is rejected", re.I),
    # "module verification failed ... tainting kernel" is a LOAD with SB off, not a refusal.
    re.compile(r"Lockdown: .*unsigned module loading is restricted", re.I),
)

# In-process fake command table (self-test); env AWNIX_SB_FAKE is the out-of-process one.
_FAKE: Optional[Dict[str, Dict[str, Any]]] = None
_FAKE_LOG: Optional[List[Dict[str, Any]]] = None


# --------------------------------------------------------------------------- plumbing
def sysroot() -> Path:
    return Path(os.environ.get("AWNIX_SB_SYSROOT") or "/")


def _p(root: Path, rel: str) -> Path:
    return root / rel.lstrip("/")


def _fake_table() -> Optional[Dict[str, Dict[str, Any]]]:
    if _FAKE is not None:
        return _FAKE
    path = os.environ.get("AWNIX_SB_FAKE")
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _log_fake(argv: List[str], stdin: Optional[str]) -> None:
    rec = {"argv": list(argv), "stdin": stdin}
    if _FAKE_LOG is not None:
        _FAKE_LOG.append(rec)
    path = os.environ.get("AWNIX_SB_FAKE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")


def run(argv: List[str], stdin: Optional[str] = None, timeout: int = 30) -> Tuple[int, str, str]:
    """Run a command; (rc, stdout, stderr). rc 127 = command not found."""
    fake = _fake_table()
    if fake is not None:
        _log_fake(argv, stdin)
        joined = " ".join(argv)
        best = ""
        for key in fake:
            if joined.startswith(key) and len(key) > len(best):
                best = key
        if not best:
            return 127, "", f"{argv[0]}: not found (fake)"
        spec = fake[best] or {}
        return int(spec.get("rc", 0)), str(spec.get("stdout", "")), str(spec.get("stderr", ""))
    exe = shutil.which(argv[0])
    if not exe:
        return 127, "", f"{argv[0]}: not found"
    try:
        proc = subprocess.run([exe] + argv[1:], input=stdin, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 126, "", str(exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _read_bytes(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _read_text(path: Path) -> Optional[str]:
    raw = _read_bytes(path)
    return None if raw is None else raw.decode("utf-8", "replace").strip()


def _now() -> str:
    now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
    return now.isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- detection
def detect_firmware(root: Path) -> str:
    return "uefi" if _p(root, "sys/firmware/efi").is_dir() else "bios"


def _efivar_flag(root: Path, name: str) -> Optional[bool]:
    raw = _read_bytes(_p(root, f"sys/firmware/efi/efivars/{name}-{EFI_GLOBAL_GUID}"))
    if raw is None or len(raw) < 5:
        return None
    return raw[4] == 1


def detect_secure_boot(root: Path, firmware: str) -> str:
    if firmware != "uefi":
        return "off"
    sb = _efivar_flag(root, "SecureBoot")
    setup = _efivar_flag(root, "SetupMode")
    if sb is True:
        return "on"
    if setup is True:
        return "setup-mode"
    if sb is False:
        return "off"
    return "unknown"


def detect_lockdown(root: Path) -> Optional[str]:
    text = _read_text(_p(root, "sys/kernel/security/lockdown"))
    if not text:
        return None
    m = re.search(r"\[(\w+)\]", text)
    return m.group(1) if m else text.split()[0]


def detect_gpu(root: Path) -> str:
    base = _p(root, "sys/bus/pci/devices")
    try:
        devices = sorted(base.iterdir())
    except OSError:
        return "none"
    for dev in devices:
        vendor = (_read_text(dev / "vendor") or "").lower()
        klass = (_read_text(dev / "class") or "").lower()
        if vendor == NVIDIA_VENDOR and klass.startswith("0x03"):
            return "nvidia"
    return "none"


def _read_manifest(root: Path) -> Dict[str, Any]:
    raw = _read_text(_p(root, MANIFEST_REL))
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _modinfo(root: Path, field: str) -> Tuple[int, str]:
    argv = ["modinfo", "-F", field]
    if str(root) not in ("/", ""):
        argv += ["-b", str(root)]
    rc, out, _err = run(argv + ["nvidia"])
    return rc, out.strip()


def detect_kmod(root: Path) -> Dict[str, Any]:
    kmod: Dict[str, Any] = {"present": False, "signed": None, "signer": None,
                            "loaded": _p(root, "sys/module/nvidia").is_dir(),
                            "rejected": False, "source": None}
    rc, filename = _modinfo(root, "filename")
    if rc == 0 and filename:
        kmod["present"] = True
        kmod["source"] = "modinfo"
        _rc, signer = _modinfo(root, "signer")
        kmod["signer"] = signer or None
        kmod["signed"] = bool(signer)
    elif rc == 127:
        man = _read_manifest(root)
        if man.get("modules"):
            kmod["present"] = True
            kmod["source"] = "manifest"
            kmod["signed"] = bool(man.get("signed"))
            kmod["signer"] = man.get("signer_cn") or None
    if kmod["loaded"]:
        kmod["present"] = True
    rc, out, _err = run(["journalctl", "-k", "-b", "-o", "cat", "--no-pager"])
    if rc == 0:
        kmod["rejected"] = any(p.search(line) for line in out.splitlines() for p in REJECT_PATTERNS)
    else:
        kmod["rejected"] = None if rc != 127 else False
    return kmod


def detect_driver(root: Path, kmod: Dict[str, Any]) -> str:
    if kmod.get("present"):
        return "kmod"
    for rel in VULKAN_ICD_DIRS:
        try:
            if any(_p(root, rel).iterdir()):
                return "vulkan-userspace"
        except OSError:
            continue
    return "none"


def cert_info(root: Path, cert: Optional[Path] = None) -> Optional[Dict[str, str]]:
    path = cert or _p(root, CERT_REL)
    der = _read_bytes(path)
    if not der:
        return None
    sha1 = hashlib.sha1(der).hexdigest()  # noqa: S324 - fingerprint display, as mokutil prints it
    return {"path": str(path), "sha256": hashlib.sha256(der).hexdigest(), "sha1": sha1,
            "sha1_colon": ":".join(sha1[i:i + 2] for i in range(0, 40, 2))}


def detect_mok(root: Path, firmware: str) -> Dict[str, Any]:
    info = cert_info(root)
    mok: Dict[str, Any] = {"cert_sha256": info["sha256"] if info else None,
                           "enrolled": None, "pending": None}
    if info is None or firmware != "uefi":
        return mok
    rc, out, err = run(["mokutil", "--test-key", info["path"]])
    text = (out + "\n" + err).lower()
    if rc != 127:
        if "already enrolled" in text:
            mok["enrolled"], mok["pending"] = True, False
        elif "enrollment request" in text:
            mok["enrolled"], mok["pending"] = False, True
        elif "not enrolled" in text:
            mok["enrolled"] = False
    rc, out, _err = run(["mokutil", "--list-new"])
    if rc == 0 and mok["pending"] is not True:
        norm = out.lower().replace(" ", "")
        mok["pending"] = info["sha1_colon"] in norm or info["sha1"] in norm
    elif rc not in (0, 127) and mok["pending"] is None and mok["enrolled"] is not None:
        mok["pending"] = False  # mokutil answers "MokNew is empty" with rc 1
    return mok


def classify(st: Dict[str, Any]) -> str:
    """The single state for a detection record. Order matters; see the guide chapter."""
    kmod = st["kmod"]
    mok = st["mok"]
    sb = st["secure_boot"]
    if st["gpu"] != "nvidia" or not kmod.get("present"):
        return "not-applicable"
    if sb == "unknown":
        return "unknown"
    if sb != "on":
        return "sb-off" if kmod.get("loaded") else "driver-not-loaded"
    if kmod.get("signed") is False:
        return "unsigned-kmod"
    if kmod.get("loaded"):
        return "ok"
    if mok.get("pending"):
        return "enroll-pending"
    if mok.get("enrolled") is False:
        return "needs-enroll"
    if kmod.get("rejected"):
        return "kmod-rejected"
    if mok.get("enrolled") is True:
        return "driver-not-loaded"
    return "unknown"


def detect(root: Optional[Path] = None) -> Dict[str, Any]:
    root = root or sysroot()
    firmware = detect_firmware(root)
    kmod = detect_kmod(root)
    st: Dict[str, Any] = {
        "schema": SCHEMA,
        "firmware": firmware,
        "secure_boot": detect_secure_boot(root, firmware),
        "lockdown": detect_lockdown(root),
        "gpu": detect_gpu(root),
        "driver": detect_driver(root, kmod),
        "kmod": {k: kmod[k] for k in ("present", "signed", "signer", "loaded", "rejected")},
        "mok": detect_mok(root, firmware),
    }
    st["state"] = classify(st)
    st["action"] = ACTIONS.get(st["state"])
    st["checked_at"] = _now()
    return st


def exit_for(state: str) -> int:
    if state in OK_STATES:
        return 0
    if state == "unknown":
        return 2
    return 1


def marker(st: Dict[str, Any]) -> str:
    sb = {"on": "on", "off": "off", "setup-mode": "off"}.get(st["secure_boot"], "n/a")
    if st["firmware"] != "uefi":
        sb = "n/a"
    k = st["kmod"]
    if not k.get("present"):
        km = "absent"
    elif k.get("loaded"):
        km = "loaded"
    elif k.get("signed") is False:
        km = "unsigned"
    elif k.get("rejected"):
        km = "rejected"
    else:
        km = "absent"
    m = st["mok"]
    if m.get("cert_sha256") is None or st["firmware"] != "uefi":
        key = "n/a"
    elif m.get("enrolled"):
        key = "enrolled"
    elif m.get("pending"):
        key = "pending"
    elif m.get("enrolled") is False:
        key = "missing"
    else:
        key = "n/a"
    return (f"awnix-secureboot: sb={sb} gpu={st['gpu']} kmod={km} key={key} "
            f"state={st['state']}")


def brief(st: Dict[str, Any]) -> str:
    k = st["kmod"]
    kmod_txt = ("absent" if not k.get("present") else
                "loaded" if k.get("loaded") else
                "unsigned" if k.get("signed") is False else "not loaded")
    line = (f"Secure Boot: {st['secure_boot']} ({st['firmware']}) | GPU: {st['gpu']} "
            f"| driver: {st['driver']} ({kmod_txt}) | state: {st['state']}")
    if st.get("action"):
        line += "\nNext: " + st["action"]
    return line


def write_status(st: Dict[str, Any], root: Optional[Path] = None) -> Path:
    root = root or sysroot()
    path = _p(root, STATUS_REL)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".status.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(st, fh, indent=2, sort_keys=True)
            fh.write("\n")
        try:
            os.chmod(tmp, 0o644)
        except OSError as exc:  # e.g. a filesystem without POSIX modes
            print(f"awnix-secureboot: chmod {tmp}: {exc}", file=sys.stderr)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError as exc:
            print(f"awnix-secureboot: could not remove {tmp}: {exc}", file=sys.stderr)
        raise
    return path


# --------------------------------------------------------------------------- doctor
def doctor(st: Dict[str, Any]) -> Dict[str, Any]:
    k, m = st["kmod"], st["mok"]
    kmod_relevant = st["gpu"] == "nvidia" and bool(k.get("present"))
    sb_on = st["secure_boot"] == "on"
    checks: List[Dict[str, Any]] = []

    def add(cid: str, ok: Optional[bool], detail: str) -> None:
        checks.append({"id": cid, "ok": ok, "detail": detail})

    add("SBD001", True, f"firmware={st['firmware']}")
    add("SBD002", st["secure_boot"] != "unknown" or st["firmware"] != "uefi",
        f"secure_boot={st['secure_boot']} lockdown={st.get('lockdown')}")
    add("SBD003", True, f"gpu={st['gpu']} driver={st['driver']}")
    if kmod_relevant:
        add("SBD004", bool(k.get("signed")) or not sb_on,
            f"kmod signed={k.get('signed')} signer={k.get('signer')}")
        add("SBD005", m.get("cert_sha256") is not None or not sb_on,
            "MOK certificate shipped" if m.get("cert_sha256") else "no MOK certificate in image")
        if sb_on:
            add("SBD006", bool(m.get("enrolled")) if m.get("enrolled") is not None else None,
                f"enrolled={m.get('enrolled')} pending={m.get('pending')}")
        add("SBD007", bool(k.get("loaded")), f"nvidia module loaded={k.get('loaded')}")
        add("SBD008", (not k.get("rejected")) if k.get("rejected") is not None else None,
            "no signature rejection in kernel log" if not k.get("rejected")
            else "kernel log shows a rejected module key")
    code = exit_for(st["state"])
    verdict = {0: "ok", 1: "action-needed", 2: "unknown"}[code]
    return {"schema": SCHEMA, "verdict": verdict, "state": st["state"],
            "action": st.get("action"), "checks": checks, "checked_at": st["checked_at"]}


# --------------------------------------------------------------------------- enroll
def make_password(n: int = PASSWORD_LEN) -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(n))


def _crypt_hash(password: str) -> Optional[str]:
    rc, out, _err = run(["openssl", "passwd", "-6", "-stdin"], stdin=password + "\n")
    h = out.strip()
    if rc != 0 or not h.startswith("$6$"):
        return None
    return h


def _import(cert: Path, hash_file: Path) -> Tuple[int, str]:
    rc, out, err = run(["mokutil", "--import", str(cert), "--hash-file", str(hash_file)])
    return rc, (out + err).strip()


def enroll(reveal: bool, hash_file: Optional[str], cert: Optional[str],
           out=sys.stdout) -> int:
    root = sysroot()
    if not reveal and not hash_file:
        print("enroll: pass --reveal (generate a one-time password) or --hash-file F",
              file=sys.stderr)
        return 2
    if str(root) == "/" and hasattr(os, "geteuid") and os.geteuid() != 0:
        print("enroll: must run as root (sudo awnix secureboot enroll ...)", file=sys.stderr)
        return 2
    info = cert_info(root, Path(cert) if cert else None)
    if info is None:
        print(f"enroll: no MOK certificate at {cert or _p(root, CERT_REL)}; this image "
              "ships no signed NVIDIA module, nothing to enroll", file=out)
        return 0 if not cert else 1
    firmware = detect_firmware(root)
    if firmware != "uefi":
        print("enroll: legacy BIOS boot, Secure Boot does not apply; nothing to enroll", file=out)
        return 0
    mok = detect_mok(root, firmware) if not cert else _mok_for(info)
    if mok.get("enrolled"):
        print(f"enroll: key {info['sha256'][:12]} is already enrolled", file=out)
        return 0
    if mok.get("pending"):
        print(f"enroll: key {info['sha256'][:12]} is already queued; reboot and finish in "
              "MokManager (lost the password? `sudo mokutil --revoke-import`, then enroll "
              "again)", file=out)
        return 0
    if hash_file:
        hf = Path(hash_file)
        if not hf.is_file():
            print(f"enroll: hash file {hf} not found", file=sys.stderr)
            return 1
        rc, msg = _import(Path(info["path"]), hf)
        if rc == 127:
            print("enroll: mokutil is not installed", file=sys.stderr)
            return 2
        if rc != 0:
            print(f"enroll: mokutil --import failed (rc={rc}): {msg}", file=sys.stderr)
            return 1
        print(f"enroll: key {info['sha256'][:12]} queued; reboot and finish in MokManager",
              file=out)
        _refresh_status()
        return 0
    password = make_password()
    hashed = _crypt_hash(password)
    if hashed is None:
        print("enroll: could not hash the password (openssl passwd -6 unavailable)",
              file=sys.stderr)
        return 2
    tmpdir = tempfile.mkdtemp(prefix="awnix-mok.")
    tmp = Path(tmpdir) / "hash"
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as fh:
            fh.write(hashed + "\n")
        rc, msg = _import(Path(info["path"]), tmp)
    finally:
        # The hash file is removed whatever happened; a failure to remove it is loud.
        for victim, rm in ((str(tmp), os.unlink), (tmpdir, os.rmdir)):
            try:
                rm(victim)
            except FileNotFoundError:
                continue
            except OSError as exc:
                print(f"awnix-secureboot: WARNING could not remove {victim}: {exc}",
                      file=sys.stderr)
    if rc == 127:
        print("enroll: mokutil is not installed", file=sys.stderr)
        return 2
    if rc != 0:
        print(f"enroll: mokutil --import failed (rc={rc}): {msg}", file=sys.stderr)
        return 1
    print(f"MOK enrollment queued for key {info['sha256'][:12]} (SHA1 {info['sha1_colon']}).",
          file=out)
    print("", file=out)
    print(f"    One-time password:  {password}", file=out)
    print("", file=out)
    print("Write it down now. It is shown once and stored nowhere.", file=out)
    print("Reboot. In the blue MokManager screen choose: Enroll MOK -> Continue -> Yes,", file=out)
    print("type the password, then Reboot. You have about 10 seconds to press a key.", file=out)
    _refresh_status()
    return 0


def _mok_for(info: Dict[str, str]) -> Dict[str, Any]:
    rc, out, err = run(["mokutil", "--test-key", info["path"]])
    text = (out + err).lower()
    return {"enrolled": "already enrolled" in text, "pending": "enrollment request" in text}


def _refresh_status() -> None:
    try:
        write_status(detect())
    except OSError as exc:
        # enrollment already succeeded; the next boot's status --write records it
        print(f"awnix-secureboot: status not refreshed: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- self-test
def build_fixture(root: Path, spec: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Materialise a fixture sysroot; return the fake command table for it.

    spec keys: uefi, sb (True/False/None), setup_mode, gpu ('nvidia'|'none'|'amd'),
    kmod ('signed'|'unsigned'|None), loaded, cert, enrolled (True/False/None),
    pending, rejected, vulkan, lockdown, mokutil (bool, default True).
    """
    root.mkdir(parents=True, exist_ok=True)
    if spec.get("uefi", True):
        ev = _p(root, "sys/firmware/efi/efivars")
        ev.mkdir(parents=True, exist_ok=True)
        if spec.get("sb") is not None:
            (ev / f"SecureBoot-{EFI_GLOBAL_GUID}").write_bytes(
                b"\x06\x00\x00\x00" + (b"\x01" if spec["sb"] else b"\x00"))
        (ev / f"SetupMode-{EFI_GLOBAL_GUID}").write_bytes(
            b"\x06\x00\x00\x00" + (b"\x01" if spec.get("setup_mode") else b"\x00"))
    if spec.get("lockdown"):
        lp = _p(root, "sys/kernel/security")
        lp.mkdir(parents=True, exist_ok=True)
        (lp / "lockdown").write_text("none [integrity] confidentiality\n", encoding="utf-8")
    gpu = spec.get("gpu", "nvidia")
    if gpu != "none":
        # Real entries are named 0000:01:00.0; a colon is not a legal Windows path
        # character, and the detector only globs devices/*, so the fixture uses '_'.
        dev = _p(root, "sys/bus/pci/devices/pci-0000_01_00.0")
        dev.mkdir(parents=True, exist_ok=True)
        (dev / "vendor").write_text({"nvidia": NVIDIA_VENDOR}.get(gpu, "0x1002") + "\n",
                                    encoding="utf-8")
        (dev / "class").write_text("0x030000\n", encoding="utf-8")
    if spec.get("loaded"):
        _p(root, "sys/module/nvidia").mkdir(parents=True, exist_ok=True)
    if spec.get("vulkan"):
        icd = _p(root, "usr/share/vulkan/icd.d")
        icd.mkdir(parents=True, exist_ok=True)
        (icd / "radeon_icd.x86_64.json").write_text("{}\n", encoding="utf-8")
    der = b"0\x82\x01\x00awnix-fixture-der-" + json.dumps(spec, sort_keys=True).encode()
    if spec.get("cert", True):
        cp = _p(root, CERT_REL)
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_bytes(der)
    sha1 = hashlib.sha1(der).hexdigest()  # noqa: S324
    fake: Dict[str, Dict[str, Any]] = {}
    kmod = spec.get("kmod")
    if kmod:
        fake["modinfo -F filename"] = {"rc": 0,
                                       "stdout": "/usr/lib/modules/6.12/extra/nvidia.ko\n"}
        fake["modinfo -F signer"] = {"rc": 0, "stdout": "awnix MOK kmod signing\n"
                                     if kmod == "signed" else "\n"}
    else:
        fake["modinfo"] = {"rc": 1, "stderr": "modinfo: ERROR: Module nvidia not found.\n"}
    log = "nvidia: loading out-of-tree module taints kernel.\n"
    if spec.get("rejected"):
        log += "Loading of module with unavailable key is rejected\n"
    fake["journalctl"] = {"rc": 0, "stdout": log}
    if spec.get("mokutil", True):
        enrolled = spec.get("enrolled")
        if spec.get("pending"):
            tk = "is already in the enrollment request\n"
        elif enrolled:
            tk = "is already enrolled\n"
        else:
            tk = "is not enrolled\n"
        fake["mokutil --test-key"] = {"rc": 0 if not enrolled else 1, "stdout": "awnix-mok.der " + tk}
        if spec.get("pending"):
            fp = ":".join(sha1[i:i + 2] for i in range(0, 40, 2))
            fake["mokutil --list-new"] = {"rc": 0, "stdout": f"[key 1]\nSHA1 Fingerprint: {fp}\n"}
        else:
            fake["mokutil --list-new"] = {"rc": 1, "stdout": "MokNew is empty\n"}
        fake["mokutil --import"] = {"rc": 0, "stdout": ""}
    fake["openssl passwd"] = {"rc": 0, "stdout": "$6$fixturesalt$fixturehash\n"}
    return fake


SELF_TEST_CASES: List[Tuple[str, Dict[str, Any], str, str]] = [
    ("bios", {"uefi": False, "kmod": "signed", "loaded": True}, "sb-off",
     "awnix-secureboot: sb=n/a gpu=nvidia kmod=loaded key=n/a state=sb-off"),
    ("sb-off", {"sb": False, "kmod": "signed", "loaded": True, "enrolled": False}, "sb-off",
     "awnix-secureboot: sb=off gpu=nvidia kmod=loaded key=missing state=sb-off"),
    ("ok", {"sb": True, "kmod": "signed", "loaded": True, "enrolled": True, "lockdown": True},
     "ok", "awnix-secureboot: sb=on gpu=nvidia kmod=loaded key=enrolled state=ok"),
    ("needs-enroll", {"sb": True, "kmod": "signed", "enrolled": False, "rejected": True},
     "needs-enroll",
     "awnix-secureboot: sb=on gpu=nvidia kmod=rejected key=missing state=needs-enroll"),
    ("pending", {"sb": True, "kmod": "signed", "enrolled": False, "pending": True},
     "enroll-pending",
     "awnix-secureboot: sb=on gpu=nvidia kmod=absent key=pending state=enroll-pending"),
    ("rejected", {"sb": True, "kmod": "signed", "enrolled": True, "rejected": True},
     "kmod-rejected",
     "awnix-secureboot: sb=on gpu=nvidia kmod=rejected key=enrolled state=kmod-rejected"),
    ("no-gpu", {"sb": True, "gpu": "none", "kmod": "signed"}, "not-applicable",
     "awnix-secureboot: sb=on gpu=none kmod=absent key=missing state=not-applicable"),
    ("unsigned", {"sb": True, "kmod": "unsigned", "enrolled": False}, "unsigned-kmod",
     "awnix-secureboot: sb=on gpu=nvidia kmod=unsigned key=missing state=unsigned-kmod"),
    ("garg-vulkan", {"sb": True, "kmod": None, "vulkan": True, "cert": False},
     "not-applicable",
     "awnix-secureboot: sb=on gpu=nvidia kmod=absent key=n/a state=not-applicable"),
    ("sb-unknown", {"sb": None, "kmod": "signed", "enrolled": True}, "unknown",
     "awnix-secureboot: sb=n/a gpu=nvidia kmod=absent key=enrolled state=unknown"),
    ("setup-mode", {"sb": False, "setup_mode": True, "kmod": "signed", "loaded": True},
     "sb-off", "awnix-secureboot: sb=off gpu=nvidia kmod=loaded key=missing state=sb-off"),
]


def self_test() -> int:
    global _FAKE, _FAKE_LOG
    failures: List[str] = []
    saved_env = os.environ.get("AWNIX_SB_SYSROOT")
    with tempfile.TemporaryDirectory(prefix="awnix-sb-selftest.") as td:
        try:
            for name, spec, want_state, want_marker in SELF_TEST_CASES:
                root = Path(td) / name
                _FAKE = build_fixture(root, spec)
                os.environ["AWNIX_SB_SYSROOT"] = str(root)
                st = detect(root)
                got_marker = marker(st)
                if st["state"] != want_state:
                    failures.append(f"{name}: state {st['state']} != {want_state}")
                if got_marker != want_marker:
                    failures.append(f"{name}: marker {got_marker!r} != {want_marker!r}")
                if st["state"] not in STATES:
                    failures.append(f"{name}: state {st['state']} not in enum")
                silent = st["state"] in ("ok", "not-applicable")
                if silent == bool(st["action"]):
                    failures.append(f"{name}: action/state mismatch")
                doc = doctor(st)
                want_verdict = {0: "ok", 1: "action-needed", 2: "unknown"}[exit_for(want_state)]
                if doc["verdict"] != want_verdict:
                    failures.append(f"{name}: doctor verdict {doc['verdict']}")
                path = write_status(st, root)
                back = json.loads(path.read_text(encoding="utf-8"))
                if back.get("schema") != 1 or back.get("state") != want_state:
                    failures.append(f"{name}: status.json round trip")
            # enroll --reveal: password only on stdin, never argv; hash file removed.
            root = Path(td) / "enroll"
            _FAKE = build_fixture(root, {"sb": True, "kmod": "signed", "enrolled": False})
            _FAKE_LOG = []
            os.environ["AWNIX_SB_SYSROOT"] = str(root)

            class _Buf:
                def __init__(self) -> None:
                    self.parts: List[str] = []

                def write(self, s: str) -> int:
                    self.parts.append(s)
                    return len(s)

                def flush(self) -> None:
                    pass

            buf = _Buf()
            rc = enroll(True, None, None, out=buf)
            text = "".join(buf.parts)
            m = re.search(r"One-time password:\s+(\S+)", text)
            if rc != 0 or not m:
                failures.append(f"enroll --reveal rc={rc} output={text!r}")
            else:
                pw = m.group(1)
                if len(pw) != PASSWORD_LEN or any(c not in PASSWORD_ALPHABET for c in pw):
                    failures.append("enroll: password shape")
                for call in _FAKE_LOG:
                    if any(pw in a for a in call["argv"]):
                        failures.append("enroll: password leaked onto argv")
                ossl = [c for c in _FAKE_LOG if c["argv"][:2] == ["openssl", "passwd"]]
                if not ossl or ossl[0]["stdin"] != pw + "\n":
                    failures.append("enroll: password not piped to openssl stdin")
                imp = [c for c in _FAKE_LOG if c["argv"][:2] == ["mokutil", "--import"]]
                if not imp or "--hash-file" not in imp[0]["argv"]:
                    failures.append("enroll: mokutil --import --hash-file not called")
                elif Path(imp[0]["argv"][-1]).exists():
                    failures.append("enroll: hash temp file left behind")
            # enroll with no mode is could-not-judge; mokutil missing is 2.
            if enroll(False, None, None, out=buf) != 2:
                failures.append("enroll without a mode should exit 2")
            _FAKE = {k: v for k, v in _FAKE.items() if not k.startswith("mokutil --import")}
            if enroll(True, None, None, out=buf) != 2:
                failures.append("enroll without mokutil --import should exit 2")
            # a failing detector must never be reported as ok
            if exit_for("unknown") != 2 or exit_for("needs-enroll") != 1:
                failures.append("exit mapping")
        finally:
            _FAKE = None
            _FAKE_LOG = None
            if saved_env is None:
                os.environ.pop("AWNIX_SB_SYSROOT", None)
            else:
                os.environ["AWNIX_SB_SYSROOT"] = saved_env
    for f in failures:
        print("SELF-TEST FAIL:", f)
    print(f"awnix-secureboot self-test: {len(SELF_TEST_CASES)} fixtures + enroll, "
          f"{len(failures)} failure(s)")
    return 0 if not failures else 1


# --------------------------------------------------------------------------- CLI
def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--list-verbs" in argv:
        print("\n".join(VERBS))
        return 0
    if "--self-test" in argv:
        return self_test()
    ap = argparse.ArgumentParser(prog="awnix secureboot",
                                 description="Secure Boot + NVIDIA kmod state and MOK enrollment")
    sub = ap.add_subparsers(dest="verb")
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    s.add_argument("--brief", action="store_true")
    s.add_argument("--write", action="store_true")
    e = sub.add_parser("enroll")
    g = e.add_mutually_exclusive_group()
    g.add_argument("--reveal", action="store_true")
    g.add_argument("--hash-file")
    e.add_argument("--cert")
    d = sub.add_parser("doctor")
    d.add_argument("--json", action="store_true")
    c = sub.add_parser("cert")
    c.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    verb = args.verb or "status"
    if verb == "enroll":
        return enroll(args.reveal, args.hash_file, args.cert)
    if verb == "cert":
        info = cert_info(sysroot())
        if info is None:
            print("no MOK certificate shipped in this image", file=sys.stderr)
            return 2
        if getattr(args, "json", False):
            print(json.dumps(info, indent=2))
        else:
            print(f"{info['path']}\nSHA256 {info['sha256']}\nSHA1   {info['sha1_colon']}")
        return 0
    try:
        st = detect()
    except Exception as exc:  # noqa: BLE001 - a detector crash is could-not-judge, never ok
        print(f"awnix-secureboot: could not judge: {exc}", file=sys.stderr)
        return 2
    code = exit_for(st["state"])
    if verb == "doctor":
        doc = doctor(st)
        if args.json:
            print(json.dumps(doc, indent=2))
        else:
            for chk in doc["checks"]:
                mark = {True: "ok  ", False: "FAIL", None: "??  "}[chk["ok"]]
                print(f"{mark} {chk['id']} {chk['detail']}")
            print(f"verdict: {doc['verdict']} (state={doc['state']})")
            if doc.get("action"):
                print("next: " + doc["action"])
        return code
    if getattr(args, "write", False):
        try:
            write_status(st)
        except OSError as exc:
            print(f"awnix-secureboot: cannot write status: {exc}", file=sys.stderr)
        print(marker(st), flush=True)
    if getattr(args, "json", False):
        print(json.dumps(st, indent=2, sort_keys=True))
    elif getattr(args, "brief", False):
        print(brief(st))
    elif not getattr(args, "write", False):
        print(brief(st))
    return code


if __name__ == "__main__":
    sys.exit(main())
