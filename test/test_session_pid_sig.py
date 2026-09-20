"""Tests for :mod:`kiro_crew.session_pid_sig` — signed session_pid publication.

The ``session_pid_<pid>.txt`` file is same-uid agent-writable, so the strict
identity path must not trust it bare. These tests lock in the sidecar
contract: publish writes ``.txt`` + HMAC ``.sig`` (keyed by the SEL trust
root); verify accepts only a matching pair and fails closed on every
tamper/degradation path.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import platform_compat, session_pid_sig

SESSION_KEY = "dashboard:chat-7-123456"
LOGGER_NAME = "kiro_crew.session_pid_sig"


def records_from_this_module(caplog, level="ERROR"):
    """Captured records this module emitted — scoped by LOGGER as well as level.

    ``caplog.at_level(..., logger=LOGGER_NAME)`` scopes the *level* it captures
    at; it does not scope *which* loggers land in ``caplog.records``, which
    still collects everything that propagates to the root handler. So counting
    by level alone makes every assertion below depend on whether an unrelated
    test happened to emit an ERROR inside the same window.

    That is not hypothetical: CI saw nine ``asyncio`` "Task was destroyed but it
    is pending!" records — leaked ``SessionManager._cleanup_loop()`` tasks from
    other tests sharing the xdist worker, reported whenever those task objects
    were collected — turn ``assert len(errors) == 1`` into ``assert 10 == 1``.
    Nothing about this module had changed.

    Scoping by logger name is strictly narrower than scoping by level: these
    assertions still require an exact count, they just do not count other
    people's records as ours.
    """
    return [
        r for r in caplog.records if r.levelname == level and r.name == LOGGER_NAME
    ]


def release_fifo_reader(path, thread):
    """Hand a parked FIFO reader the writer it is waiting for, then join it.

    Only ever reached when the regression the caller pins is PRESENT: a daemon
    thread blocked in ``open(O_RDONLY)`` on a writer-less FIFO outlives the test
    that already failed, and a thread that can never finish is a lost RUN rather
    than a failed test. ``O_WRONLY | O_NONBLOCK`` raises ``ENXIO`` when no reader
    is waiting, which is the case where there is nothing to release.
    """
    if not thread.is_alive():
        return
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
    except OSError:
        pass
    thread.join(10)


@pytest.fixture
def cfg(tmp_path):
    """Isolated config dir with a valid SEL trust-root key. Patches both the
    mapping-file dir (config_dir) and the canonical trust-root path accessor
    (sel_hmac_key_path — single source of truth owned by sel.py).

    ``_sel_hmac_key_bytes`` is stubbed to ``None`` so these tests exercise the
    FILE path in isolation: the in-memory recovery fallback depends on a live
    ``SecurityEventLog`` singleton, which other tests in the same process may
    or may not have initialized. Its own behavior is covered by
    ``TestTrustRootRecovery``.

    ``platform_compat.get_process_start_id`` is pinned to ``None`` (no start
    token) so the fixture is deterministic: the fake pids used here (4242,
    1000, ...) can be LIVE processes on the test host, and a live pid would
    otherwise make ``publish_session_pid`` capture a real start token and
    change the exact ``.txt`` bytes these tests assert on. Recycle-guard
    tests (``TestPidRecycleGuard``) re-patch it per test with controlled
    values.
    """
    (tmp_path / "sel_hmac.key").write_bytes(b"\x01" * 32)
    with patch.object(session_pid_sig, "config_dir", return_value=tmp_path), \
         patch.object(session_pid_sig, "_sel_hmac_key_bytes", return_value=None), \
         patch.object(platform_compat, "get_process_start_id", return_value=None), \
         patch.object(
             session_pid_sig,
             "sel_hmac_key_path",
             return_value=tmp_path / "sel_hmac.key",
         ):
        session_pid_sig._reported.clear()
        yield tmp_path
        session_pid_sig._reported.clear()


class TestPublish:
    def test_writes_txt_and_sig(self, cfg):
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        assert (cfg / "session_pid_4242.txt").read_text(encoding="utf-8") == SESSION_KEY
        sig = (cfg / "session_pid_4242.sig").read_text(encoding="utf-8")
        assert len(sig) == 64 and all(c in "0123456789abcdef" for c in sig)

    def test_publish_without_key_writes_unsigned_and_drops_stale_sig(self, cfg):
        """SEL key missing: txt still published (lenient readers keep
        working) but any stale sidecar is removed so a rekeyed mapping can
        never verify against an old signature."""
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)  # signed
        (cfg / "sel_hmac.key").unlink()
        session_pid_sig.publish_session_pid(4242, "dashboard:rekeyed")
        assert (
            cfg / "session_pid_4242.txt"
        ).read_text(encoding="utf-8") == "dashboard:rekeyed"
        assert not (cfg / "session_pid_4242.sig").exists()

    def test_rekey_overwrites_both_files(self, cfg):
        session_pid_sig.publish_session_pid(4242, "dashboard:old")
        old_sig = (cfg / "session_pid_4242.sig").read_text(encoding="utf-8")
        session_pid_sig.publish_session_pid(4242, "dashboard:new")
        assert session_pid_sig.verify_session_pid(4242) == "dashboard:new"
        assert (cfg / "session_pid_4242.sig").read_text(encoding="utf-8") != old_sig

    def test_preplanted_symlink_not_followed(self, cfg):
        """SYMLINK ATTACK: an agent plants symlinks at the predictable
        mapping paths pointing at another writable file. Publication must
        replace the symlink (os.replace semantics), never follow it and
        truncate the target."""
        victim = cfg / "victim.dat"
        victim.write_text("precious", encoding="utf-8")
        for name in ("session_pid_4242.txt", "session_pid_4242.sig"):
            (cfg / name).symlink_to(victim)
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        # Victim untouched; both paths are now regular files, not symlinks.
        assert victim.read_text(encoding="utf-8") == "precious"
        assert not (cfg / "session_pid_4242.txt").is_symlink()
        assert not (cfg / "session_pid_4242.sig").is_symlink()
        assert session_pid_sig.verify_session_pid(4242) == SESSION_KEY


class TestVerify:
    def test_round_trip(self, cfg):
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        assert session_pid_sig.verify_session_pid(4242) == SESSION_KEY
        # str pid (as read from KIROCREW_HOST_PID) verifies identically.
        assert session_pid_sig.verify_session_pid("4242") == SESSION_KEY

    def test_missing_files_refused(self, cfg):
        assert session_pid_sig.verify_session_pid(9999) == ""

    def test_unsigned_txt_refused(self, cfg):
        """FORGERY: bare .txt written without the SEL key."""
        (cfg / "session_pid_4242.txt").write_text(
            "dashboard:victim", encoding="utf-8"
        )
        assert session_pid_sig.verify_session_pid(4242) == ""

    def test_tampered_txt_refused(self, cfg):
        """FORGERY: legitimate pair, then the .txt is redirected at another
        slot — the old signature does not match."""
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        (cfg / "session_pid_4242.txt").write_text(
            "dashboard:victim", encoding="utf-8"
        )
        assert session_pid_sig.verify_session_pid(4242) == ""

    def test_replayed_pair_under_other_pid_refused(self, cfg):
        """REPLAY: parent's .txt/.sig copied under a different pid — the pid
        is bound into the MAC."""
        session_pid_sig.publish_session_pid(1000, "dashboard:parent")
        for ext in ("txt", "sig"):
            (cfg / f"session_pid_2000.{ext}").write_text(
                (cfg / f"session_pid_1000.{ext}").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        assert session_pid_sig.verify_session_pid(2000) == ""

    def test_short_key_refused(self, cfg):
        """A truncated/corrupted trust-root key must not verify anything."""
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        (cfg / "sel_hmac.key").write_bytes(b"\x01" * 8)
        assert session_pid_sig.verify_session_pid(4242) == ""

    def test_missing_key_refused(self, cfg, caplog):
        """Missing trust root refuses AND emits the trust-root diagnostic —
        distinguishable from the forgery (MAC-mismatch) warning so a
        publisher/verifier trust-root split doesn't silently reproduce the
        original sandboxed-session bug while looking like forgery refusal."""
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        (cfg / "sel_hmac.key").unlink()
        with caplog.at_level("WARNING", logger=session_pid_sig.logger.name):
            assert session_pid_sig.verify_session_pid(4242) == ""
        assert any(
            "trust-root key absent/short" in r.getMessage() for r in caplog.records
        )

    def test_symlinked_mapping_files_refused_on_read(self, cfg):
        """READ-SIDE SYMLINK ATTACK: after a legitimate publish, an agent
        swaps a mapping file for a symlink to a sensitive target. The
        trusted verifier must refuse (O_NOFOLLOW) — never follow the link
        and read the target."""
        secret = cfg / "secret.dat"
        secret.write_text("sensitive-content", encoding="utf-8")
        # Symlinked .txt refused.
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        (cfg / "session_pid_4242.txt").unlink()
        (cfg / "session_pid_4242.txt").symlink_to(secret)
        assert session_pid_sig.verify_session_pid(4242) == ""
        # Symlinked .sig refused (fresh legitimate pair first).
        session_pid_sig.publish_session_pid(5555, SESSION_KEY)
        (cfg / "session_pid_5555.sig").unlink()
        (cfg / "session_pid_5555.sig").symlink_to(secret)
        assert session_pid_sig.verify_session_pid(5555) == ""

    def test_oversized_mapping_file_refused(self, cfg):
        """RESOURCE ATTACK: an agent swaps a mapping file for a huge one.
        Verification must reject it from fstat size, never buffer it."""
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        (cfg / "session_pid_4242.txt").write_text(
            "x" * (session_pid_sig._MAX_MAPPING_FILE_BYTES + 1), encoding="utf-8"
        )
        assert session_pid_sig.verify_session_pid(4242) == ""


class TestLenientReader:
    """``read_session_pid_txt`` is the lenient (unsigned) read for callers
    that tolerate misattribution — but it MUST share the strict verifier's
    hardened read discipline: a planted symlink or non-regular file at the
    predictable agent-writable path is refused, never followed."""

    def test_reads_plain_txt_without_sig(self, cfg):
        (cfg / "session_pid_4242.txt").write_text(SESSION_KEY, encoding="utf-8")
        assert session_pid_sig.read_session_pid_txt(4242) == SESSION_KEY
        # Explicit cfg passthrough (lenient resolver passes its own dir).
        assert session_pid_sig.read_session_pid_txt("4242", cfg) == SESSION_KEY

    def test_missing_file_returns_empty(self, cfg):
        assert session_pid_sig.read_session_pid_txt(9999) == ""

    def test_symlinked_txt_refused(self, cfg, tmp_path):
        """SYMLINK ATTACK on the lenient path: without the hardened reader a
        plain read_text() in the trusted MCP process would follow this link
        (the read-side twin of the strict-path defense)."""
        secret = tmp_path / "victim-secret"
        secret.write_text("hunter2", encoding="utf-8")
        (cfg / "session_pid_4242.txt").symlink_to(secret)
        assert session_pid_sig.read_session_pid_txt(4242) == ""

    def test_oversized_txt_refused(self, cfg):
        (cfg / "session_pid_4242.txt").write_text(
            "x" * (session_pid_sig._MAX_MAPPING_FILE_BYTES + 1), encoding="utf-8"
        )
        assert session_pid_sig.read_session_pid_txt(4242) == ""

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="os.mkfifo is POSIX-only")
    def test_a_planted_fifo_is_refused_instead_of_waited_on(self, cfg):
        """FIFO ATTACK on the shared hardened reader: refused, never waited on.

        ``O_NOFOLLOW`` refuses a symlink but says nothing about a FIFO, and an
        ``O_RDONLY`` open of a FIFO with no writer BLOCKS INDEFINITELY -- so the
        ``S_ISREG`` rejection cannot run, because it only judges a descriptor the
        open already returned. ``O_NONBLOCK`` is what lets the open return so the
        check can refuse it. Both this lenient reader (on the MCP caller-identity
        path) and the retraction below reach that one helper.

        Bounded with a real timeout rather than a plain assertion because the
        failure mode is "never returns": no writer is ever opened on this FIFO,
        so without the flag the probe thread parks forever.
        """
        fifo = cfg / "session_pid_4242.txt"
        os.mkfifo(fifo)
        done = threading.Event()
        seen: list[str] = []

        def probe() -> None:
            seen.append(session_pid_sig.read_session_pid_txt(4242))
            done.set()

        reader = threading.Thread(target=probe, daemon=True)
        reader.start()
        try:
            assert done.wait(10), "the open waited for a FIFO writer instead of refusing"
        finally:
            release_fifo_reader(fifo, reader)
        assert seen == [""], "a non-regular file must read as absent"


class TestPidRecycleGuard:
    """The mapping and its MAC binding only the pid NUMBER let a recycled pid
    keep verifying and answer with the previous owner's session key until the
    next restart's orphan sweep. Publication also records the process START
    TOKEN (``platform_compat.get_process_start_id``
    — the same incarnation identity ``session_pid.py``'s
    ``<gw>:<pid>:<start_token>`` sweep records use), the signature covers it,
    and BOTH readers refuse on a proven mismatch.

    The asymmetry under test: a MISMATCH is positive evidence of a recycled
    pid → refuse; an ABSENT recorded token (legacy file) or an UNREADABLE
    live token (Windows, exited process) is merely unknown → resolve as
    before the guard existed.
    """

    @staticmethod
    def _live_token(value):
        return patch.object(
            platform_compat, "get_process_start_id", return_value=value
        )

    def test_recycled_pid_refused_by_strict_resolver(self, cfg):
        """HEADLINE (red against pre-fix main): publish under one process
        incarnation, present another incarnation of the same pid number —
        the strict resolver must refuse, not answer with the old key."""
        with self._live_token("111"):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        with self._live_token("222"):
            assert session_pid_sig.verify_session_pid(4242) == ""

    def test_recycled_pid_refused_by_lenient_reader(self, cfg):
        """A proven mismatch refuses on the LENIENT path too: callers like
        peer_resolve fall back from the strict resolver to this reader, so a
        refusal surfaced only from the strict path would be silently
        recovered by the fallback and the stale attribution kept."""
        with self._live_token("111"):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        with self._live_token("222"):
            assert session_pid_sig.read_session_pid_txt(4242) == ""

    def test_same_incarnation_still_resolves(self, cfg):
        with self._live_token("111"):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
            assert session_pid_sig.verify_session_pid(4242) == SESSION_KEY
            assert session_pid_sig.read_session_pid_txt(4242) == SESSION_KEY

    def test_legacy_tokenless_file_still_resolves(self, cfg):
        """BACKWARD COMPATIBILITY: a signed mapping written before the
        format change (no token line, MAC over ``"<pid>:<session_key>"``)
        must not read as tampered or as a mismatch, even when the live
        token IS readable (absent recorded token = unknown, not mismatch)."""
        key = b"\x01" * 32
        (cfg / "session_pid_4242.txt").write_text(SESSION_KEY, encoding="utf-8")
        (cfg / "session_pid_4242.sig").write_text(
            session_pid_sig._compute_sig(key, 4242, SESSION_KEY), encoding="utf-8"
        )
        with self._live_token("222"):
            assert session_pid_sig.verify_session_pid(4242) == SESSION_KEY
            assert session_pid_sig.read_session_pid_txt(4242) == SESSION_KEY

    def test_unreadable_live_token_resolves_as_today(self, cfg):
        """UNKNOWN ≠ MISMATCH: a recorded token whose live counterpart
        cannot be read (Windows, process exited, permission) keeps today's
        behaviour on both paths."""
        with self._live_token("111"):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        with self._live_token(None):
            assert session_pid_sig.verify_session_pid(4242) == SESSION_KEY
            assert session_pid_sig.read_session_pid_txt(4242) == SESSION_KEY

    def test_publish_records_the_token_as_a_second_line(self, cfg):
        """The on-disk form: ``<session_key>\\n<start_token>``. A second
        LINE, not a colon field like session_pid.py's integer records,
        because the session key itself contains colons."""
        with self._live_token("111"):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        assert (
            cfg / "session_pid_4242.txt"
        ).read_text(encoding="utf-8") == f"{SESSION_KEY}\n111"

    def test_signature_covers_the_token(self, cfg):
        """Flipping ONLY the token line invalidates the MAC — even when the
        rewritten token matches the live process, so the refusal proven
        here is the signature's, not the recycle check's."""
        with self._live_token("111"):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        (cfg / "session_pid_4242.txt").write_text(
            f"{SESSION_KEY}\n222", encoding="utf-8"
        )
        with self._live_token("222"):
            assert session_pid_sig.verify_session_pid(4242) == ""

    def test_unsigned_publish_with_token_still_degrades(self, cfg):
        """The documented unsigned-publish degrade path survives the token:
        SEL key unavailable → token-bearing ``.txt`` still published (the
        lenient reader keeps working, recycle guard included), stale sidecar
        removed, strict resolvers fail closed."""
        with self._live_token("111"):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)  # signed
        (cfg / "sel_hmac.key").unlink()
        with self._live_token("111"):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
            assert not (cfg / "session_pid_4242.sig").exists()
            assert session_pid_sig.verify_session_pid(4242) == ""
            assert session_pid_sig.read_session_pid_txt(4242) == SESSION_KEY
        with self._live_token("222"):
            assert session_pid_sig.read_session_pid_txt(4242) == ""

    def test_malformed_multiline_body_refused(self, cfg):
        """Three-plus lines were never written by publish_session_pid —
        refuse on both paths rather than guess a parse, even under a valid
        MAC over the raw body."""
        key = b"\x01" * 32
        body = f"{SESSION_KEY}\n111\nextra"
        (cfg / "session_pid_4242.txt").write_text(body, encoding="utf-8")
        (cfg / "session_pid_4242.sig").write_text(
            session_pid_sig._compute_sig(key, 4242, body), encoding="utf-8"
        )
        with self._live_token("111"):
            assert session_pid_sig.verify_session_pid(4242) == ""
            assert session_pid_sig.read_session_pid_txt(4242) == ""


class TestNoNofollowPlatform:
    """Platforms without ``O_NOFOLLOW`` (Windows) use an ``lstat`` pre-check
    plus a post-open ``(st_dev, st_ino)`` identity check. The identity check
    closes the lstat->open TOCTOU window: a path swapped to a symlink in
    that window opens the symlink's TARGET, whose identity can never match
    the vetted regular file. Simulated on POSIX by removing ``O_NOFOLLOW``."""

    def test_regular_file_still_reads(self, cfg, monkeypatch):
        monkeypatch.delattr("os.O_NOFOLLOW")
        (cfg / "session_pid_4242.txt").write_text(SESSION_KEY, encoding="utf-8")
        assert session_pid_sig.read_session_pid_txt(4242) == SESSION_KEY

    def test_lstat_open_swap_refused(self, cfg, monkeypatch, tmp_path):
        """TOCTOU RACE: the file vetted by lstat is not the file the open
        lands on (as when an agent swaps in a symlink between the two
        calls). Simulated by pointing lstat at a decoy file so the opened
        handle's identity mismatches the vetted one."""
        import os as _os

        monkeypatch.delattr("os.O_NOFOLLOW")
        target = cfg / "session_pid_4242.txt"
        target.write_text(SESSION_KEY, encoding="utf-8")
        decoy = tmp_path / "vetted-then-swapped"
        decoy.write_text("x", encoding="utf-8")
        real_lstat = _os.lstat
        monkeypatch.setattr(
            "os.lstat", lambda p, *a, **k: real_lstat(decoy)
        )
        assert session_pid_sig.read_session_pid_txt(4242) == ""

    def test_symlink_present_at_lstat_refused(self, cfg, monkeypatch, tmp_path):
        """The pre-check itself still refuses a symlink already in place."""
        monkeypatch.delattr("os.O_NOFOLLOW")
        secret = tmp_path / "victim-secret"
        secret.write_text("hunter2", encoding="utf-8")
        (cfg / "session_pid_4242.txt").symlink_to(secret)
        assert session_pid_sig.read_session_pid_txt(4242) == ""


class TestDomainSeparation:
    """The sidecar and the SEL audit chain share one on-disk trust-root key
    (``sel_hmac.key``) but MUST NOT share a signing key: the sidecar signs
    with a subkey *derived* from the root via a domain-separation label, so a
    MAC from one protocol can never be presented as a valid MAC for the
    other."""

    def test_sig_is_not_signed_with_raw_root_key(self, cfg):
        import hashlib
        import hmac

        root = b"\x01" * 32
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        stored = (cfg / "session_pid_4242.sig").read_text(encoding="utf-8")

        # A MAC computed with the RAW root key (the SEL scheme) must differ
        # from the stored sidecar MAC — proving the root key is not used
        # directly to sign the sidecar.
        raw_mac = hmac.new(
            root, f"4242:{SESSION_KEY}".encode("utf-8"), hashlib.sha256
        ).hexdigest()
        assert stored != raw_mac

        # The stored MAC matches the DERIVED-subkey scheme.
        subkey = hmac.new(
            root, session_pid_sig._SUBKEY_DOMAIN, hashlib.sha256
        ).digest()
        derived_mac = hmac.new(
            subkey, f"4242:{SESSION_KEY}".encode("utf-8"), hashlib.sha256
        ).hexdigest()
        assert stored == derived_mac


class TestTrustRootRecovery:
    """SEL signs from key bytes it cached at init, while this protocol re-reads
    the file on every call. The shared accessor re-resolves a key that MOVED
    (a concurrent legacy -> ``trust/`` migration), so what reaches
    recovery is the residue no path can resolve: a key deleted, unreadable,
    truncated, or replaced by bytes that are not the anchor. Those would
    otherwise take this protocol down for the life of the process — with a
    healthy audit chain giving no hint. Recovery reads the same bytes SEL
    validated at init.
    """

    def test_missing_file_recovers_from_live_sel_key(self, cfg):
        (cfg / "sel_hmac.key").unlink()
        with patch.object(
            session_pid_sig, "_sel_hmac_key_bytes", return_value=b"\x01" * 32
        ):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
            assert session_pid_sig.verify_session_pid(4242) == SESSION_KEY

    def test_recovery_still_announces_the_broken_file(self, cfg, caplog):
        """Recovering from memory must NOT go quiet: signing works HERE, but the
        file is what every other process resolves, so a verifier that never held
        these bytes still fails closed. Silence would move the original silent
        failure one layer over instead of removing it."""
        (cfg / "sel_hmac.key").unlink()
        with patch.object(
            session_pid_sig, "_sel_hmac_key_bytes", return_value=b"\x01" * 32
        ), caplog.at_level("ERROR", logger=LOGGER_NAME):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        errors = records_from_this_module(caplog)
        assert len(errors) == 1
        message = errors[0].getMessage()
        assert str(cfg / "sel_hmac.key") in message
        assert "every other process" in message
        assert "sub-agent dispatch" in message and "memory writes" in message

    def test_broken_file_report_is_throttled_per_path(self, cfg, caplog):
        (cfg / "sel_hmac.key").unlink()
        with patch.object(
            session_pid_sig, "_sel_hmac_key_bytes", return_value=b"\x01" * 32
        ), caplog.at_level("DEBUG", logger=LOGGER_NAME):
            session_pid_sig.publish_session_pid(1, SESSION_KEY)
            session_pid_sig.publish_session_pid(2, SESSION_KEY)
            session_pid_sig.publish_session_pid(3, SESSION_KEY)
        assert len(records_from_this_module(caplog)) == 1
        assert (
            len(
                [
                    r
                    for r in records_from_this_module(caplog, "DEBUG")
                    if "signing from memory" in r.getMessage()
                ]
            )
            == 2
        )

    def test_truncated_file_recovers_from_live_sel_key(self, cfg):
        """SEL validates the length only at init, this protocol on every call —
        so a post-init truncation is exactly the asymmetry to recover from."""
        (cfg / "sel_hmac.key").write_bytes(b"\x01" * 8)
        with patch.object(
            session_pid_sig, "_sel_hmac_key_bytes", return_value=b"\x01" * 32
        ):
            assert session_pid_sig._load_hmac_key() == b"\x01" * 32

    def test_readable_file_wins_over_live_sel_key(self, cfg):
        """The file is the anchor every OTHER process resolves independently, so
        a readable file must never be overridden by this process's memory —
        otherwise a publisher signs with bytes its verifier does not have."""
        with patch.object(
            session_pid_sig, "_sel_hmac_key_bytes", return_value=b"\x02" * 32
        ):
            assert session_pid_sig._load_hmac_key() == b"\x01" * 32

    def test_no_file_and_no_live_key_still_fails_closed(self, cfg):
        (cfg / "sel_hmac.key").unlink()
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        assert not (cfg / "session_pid_4242.sig").exists()
        assert session_pid_sig.verify_session_pid(4242) == ""


class TestTrustRootRelocationIsFollowed:
    """Trust-root relocation is followed, from the dependent protocol's side.

    Deliberately does NOT use the ``cfg`` fixture: that fixture patches
    ``sel_hmac_key_path`` to a fixed path, which is exactly the seam under test.
    A real singleton is required because re-resolution is verified against the
    key bytes it validated at init.
    """

    def test_a_relocated_key_is_read_from_the_file_not_memory(self, tmp_path):
        """The class is closed rather than worked around: the accessor follows
        the moved file, so a verifier in ANOTHER process resolves the same bytes.
        The memory fallback is stubbed to different bytes, so a result equal to
        the real key can only have come from the file."""
        from kiro_crew.sel import SecurityEventLog

        SecurityEventLog._instance = None
        SecurityEventLog._initialized = False
        try:
            log = SecurityEventLog(base_dir=tmp_path, sync=True)
            key = log._hmac_key
            # A failed migration left this process naming the legacy location;
            # the key actually lives in trust/ because a sibling completed it.
            log._hmac_key_file = tmp_path / "sel_hmac.key"
            assert not (tmp_path / "sel_hmac.key").exists()

            with patch.object(
                session_pid_sig, "config_dir", return_value=tmp_path
            ), patch.object(
                session_pid_sig, "_sel_hmac_key_bytes", return_value=b"\xfe" * 32
            ):
                session_pid_sig._reported.clear()
                loaded = session_pid_sig._load_hmac_key()
                session_pid_sig._reported.clear()

            assert loaded == key
            assert loaded != b"\xfe" * 32
        finally:
            SecurityEventLog._instance = None
            SecurityEventLog._initialized = False


class TestSigningUnavailableReport:
    """Publication happens on every session claim, so the operator-facing
    message must not be emitted per publish, and must name what stops working
    rather than only the mechanism."""

    def test_the_two_reports_do_not_suppress_each_other(self, cfg, caplog):
        """The broken-file notice and the cannot-sign notice tell an operator
        different things (signing survives here vs signing is gone), so they are
        throttled independently. Sharing one key would let whichever fired first
        silence the other for the rest of the process."""
        (cfg / "sel_hmac.key").unlink()
        with caplog.at_level("ERROR", logger=LOGGER_NAME):
            with patch.object(
                session_pid_sig, "_sel_hmac_key_bytes", return_value=b"\x01" * 32
            ):
                session_pid_sig.publish_session_pid(4242, SESSION_KEY)
            # Same path, but the in-memory fallback is gone now.
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        messages = [r.getMessage() for r in records_from_this_module(caplog)]
        assert len(messages) == 2, messages
        assert "every other process" in messages[0]
        assert "cannot sign session identities" in messages[1]

    def test_reported_once_per_process_then_debug(self, cfg, caplog):
        (cfg / "sel_hmac.key").unlink()
        with caplog.at_level("DEBUG", logger=LOGGER_NAME):
            session_pid_sig.publish_session_pid(1, SESSION_KEY)
            session_pid_sig.publish_session_pid(2, SESSION_KEY)
            session_pid_sig.publish_session_pid(3, SESSION_KEY)
        errors = records_from_this_module(caplog)
        assert len(errors) == 1
        debugs = [
            r
            for r in records_from_this_module(caplog, "DEBUG")
            if "still unavailable" in r.getMessage()
        ]
        assert len(debugs) == 2

    def test_message_names_the_consequence_and_the_path(self, cfg, caplog):
        (cfg / "sel_hmac.key").unlink()
        with caplog.at_level("ERROR", logger=LOGGER_NAME):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        message = records_from_this_module(caplog)[0].getMessage()
        assert str(cfg / "sel_hmac.key") in message
        assert "sub-agent dispatch" in message
        assert "memory writes" in message

    def test_relocated_path_is_reported_again(self, cfg, caplog):
        """Suppression is keyed on the resolved path, so a genuine relocation
        is not swallowed by the first failure's entry."""
        (cfg / "sel_hmac.key").unlink()
        with caplog.at_level("ERROR", logger=LOGGER_NAME):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
            with patch.object(
                session_pid_sig,
                "sel_hmac_key_path",
                return_value=cfg / "trust" / "sel_hmac.key",
            ):
                session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        assert len(records_from_this_module(caplog)) == 2

    def test_recovery_rearms_the_report_for_the_same_path(self, cfg, caplog):
        """Break -> restore -> break again on ONE path must produce a second
        ERROR: on a long-lived gateway that is never restarted, the log is the
        only signal the operator gets."""
        with caplog.at_level("ERROR", logger=LOGGER_NAME):
            (cfg / "sel_hmac.key").unlink()
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
            (cfg / "sel_hmac.key").write_bytes(b"\x01" * 32)
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
            (cfg / "sel_hmac.key").unlink()
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        assert len(records_from_this_module(caplog)) == 2

    def test_a_stray_error_from_another_logger_is_not_counted_as_ours(
        self, cfg, caplog
    ):
        """Guards `records_from_this_module` against being narrowed back to a
        level-only filter.

        Every count in this class is exact, and `caplog.records` collects every
        record that propagates — not only this module's. A leaked asyncio task
        being destroyed inside the window (observed in CI) must therefore not be
        counted as one of our reports, or these assertions fail for a reason
        that has nothing to do with the code under test.
        """
        (cfg / "sel_hmac.key").unlink()
        with caplog.at_level("ERROR", logger=LOGGER_NAME):
            logging.getLogger("asyncio").error(
                "Task was destroyed but it is pending!\n"
                "task: <Task pending coro=<SessionManager._cleanup_loop()>>"
            )
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)

        ours = records_from_this_module(caplog)
        assert len(ours) == 1
        assert str(cfg / "sel_hmac.key") in ours[0].getMessage()
        # The stray record really was captured — this test would be vacuous if
        # caplog had filtered it out for us.
        assert any(
            r.name == "asyncio" and r.levelname == "ERROR" for r in caplog.records
        )


class TestSigningHealth:
    """The diagnostic surface (`kirocrew doctor`) asks proactively; publication
    only reports once a session is claimed."""

    def test_reports_healthy_with_the_resolved_path(self, cfg):
        ok, path = session_pid_sig.signing_health()
        assert ok is True
        assert path == cfg / "sel_hmac.key"

    def test_reports_unhealthy_when_the_trust_root_is_gone(self, cfg):
        (cfg / "sel_hmac.key").unlink()
        ok, path = session_pid_sig.signing_health()
        assert ok is False
        assert path == cfg / "sel_hmac.key"

    def test_never_constructs_the_sel_singleton(self, cfg):
        """Asking the question must not create the trust root it asks about,
        and must not put a mkdir + key write behind a read-only command."""
        with patch("kiro_crew.sel.SecurityEventLog") as sel_cls:
            session_pid_sig.signing_health()
        sel_cls.assert_not_called()

    def test_is_not_wired_into_the_gateway_boot_path(self):
        """`no-new-work-on-gateway-boot-path` forbids a new awaited step before
        the socket binds, and this check is a diagnostic, not a gate."""
        import inspect

        from kiro_crew.dashboard import token_auth

        assert "signing_health" not in inspect.getsource(
            token_auth.warm_auth_singletons
        )


class TestUnpublish:
    """``unpublish_session_pid`` retracts ONLY a mapping that provably names no
    live session.

    The motivation is measured rather than hypothetical. On a Windows host six
    released sessions left twelve ``session_pid_*`` files behind inside an hour,
    and two of those pid numbers had already been recycled to unrelated live
    processes (a ``cmd.exe`` and an Office ``FileCoAuth.exe``) while still
    carrying a dashboard slot's key. ``_prune_stale_session_pid_files`` is the
    other retraction path and it is reached solely from
    ``cleanup_orphaned_sessions`` -- startup + shutdown only -- so a gateway that
    keeps running retracts nothing.

    The direction of every decision below is the same as that sweep's: proof of
    death removes, and "unknown" retains. The ``.txt`` is what ``mcp_caller``
    resolves a tool call's caller identity through, so removing a live one would
    cost that session its identity.
    """

    def test_removes_the_mapping_when_the_pid_is_gone(self, cfg):
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        assert (cfg / "session_pid_4242.txt").exists()
        assert (cfg / "session_pid_4242.sig").exists()

        with patch.object(platform_compat, "pid_exists", return_value=False):
            assert session_pid_sig.unpublish_session_pid(4242) is True

        assert not (cfg / "session_pid_4242.txt").exists()
        assert not (cfg / "session_pid_4242.sig").exists()

    def test_keeps_a_live_legacy_mapping(self, cfg):
        """An absent recorded token is identity UNKNOWN, never read as dead."""
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)  # fixture: token None
        with patch.object(platform_compat, "pid_exists", return_value=True):
            assert session_pid_sig.unpublish_session_pid(4242) is False
        assert (cfg / "session_pid_4242.txt").exists()

    def test_keeps_a_live_mapping_whose_token_still_matches(self, cfg):
        """Same pid, same incarnation: this is a session that is still serving."""
        with patch.object(platform_compat, "get_process_start_id", return_value="tok-1"):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
            with patch.object(platform_compat, "pid_exists", return_value=True):
                assert session_pid_sig.unpublish_session_pid(4242) is False
        assert (cfg / "session_pid_4242.txt").exists()

    def test_removes_a_mapping_whose_pid_was_recycled(self, cfg):
        """The measured case: the number is live, but it is a different process."""
        with patch.object(platform_compat, "get_process_start_id", return_value="tok-1"):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)

        with (
            patch.object(platform_compat, "pid_exists", return_value=True),
            patch.object(platform_compat, "get_process_start_id", return_value="tok-2"),
        ):
            assert session_pid_sig.unpublish_session_pid(4242) is True

        assert not (cfg / "session_pid_4242.txt").exists()
        assert not (cfg / "session_pid_4242.sig").exists()

    def test_an_absent_mapping_is_not_an_error(self, cfg):
        with patch.object(platform_compat, "pid_exists", return_value=False):
            assert session_pid_sig.unpublish_session_pid(4242) is False

    def test_a_dangling_sidecar_is_retracted_with_the_mapping(self, cfg):
        """A ``.sig`` whose ``.txt`` is already gone still accumulates."""
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        (cfg / "session_pid_4242.txt").unlink()
        with patch.object(platform_compat, "pid_exists", return_value=False):
            assert session_pid_sig.unpublish_session_pid(4242) is True
        assert not (cfg / "session_pid_4242.sig").exists()

    def test_a_republished_mapping_survives_the_retraction_it_raced(self, cfg):
        """Decide and unlink are two steps; a body that moved between them wins.

        The window is real rather than theoretical: this teardown proves pid P
        dead, the OS recycles P to a NEW runtime, and that runtime's
        ``publish_session_pid`` lands before the unlink. Deleting then would cost
        the new session the identity ``mcp_caller`` resolves its tool calls
        through -- the worst outcome this function can produce. The revalidation
        is the half that holds when the republisher is another process, where the
        module mutex does not reach, so it is pinned separately from the mutex.
        """
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        republished = "dashboard:chat-brand-new"
        moved = False

        def read(path):
            # The first read is the decision; the second is the revalidation, by
            # which point the recycled pid's new owner has republished.
            nonlocal moved
            already_read = moved
            moved = True
            return republished if already_read else SESSION_KEY

        with (
            patch.object(platform_compat, "pid_exists", return_value=False),
            patch.object(session_pid_sig, "_read_regular_nofollow", side_effect=read),
        ):
            assert session_pid_sig.unpublish_session_pid(4242) is False

        assert (cfg / "session_pid_4242.txt").exists(), "raced retraction deleted a live mapping"
        assert (cfg / "session_pid_4242.sig").exists()

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="os.mkfifo is POSIX-only")
    def test_a_planted_fifo_does_not_park_the_retraction(self, cfg):
        """The retraction runs on the event loop, so it must not be parkable.

        ``AcpClient._reset_state`` is synchronous and runs on the gateway's one
        event loop; it calls ``_untrack_session_pid``, which calls this. An agent
        that plants a FIFO at the predictable ``session_pid_<pid>.txt`` path would
        therefore freeze every task on that loop -- the chat turn AND the liveness
        heartbeat -- on an ``open`` waiting for a writer that never arrives.

        The FIFO is left in place: a read the helper refuses is UNKNOWN identity,
        and unknown retains (no writer is opened here either, so the timeout is
        the assertion).
        """
        fifo = cfg / "session_pid_4242.txt"
        os.mkfifo(fifo)
        done = threading.Event()
        verdict: list[bool] = []

        def probe() -> None:
            with patch.object(platform_compat, "pid_exists", return_value=True):
                verdict.append(session_pid_sig.unpublish_session_pid(4242))
            done.set()

        reader = threading.Thread(target=probe, daemon=True)
        reader.start()
        try:
            assert done.wait(10), "retraction parked the event loop on a planted FIFO"
        finally:
            release_fifo_reader(fifo, reader)
        assert verdict == [False]
        assert fifo.is_fifo(), "refusal is not deletion"

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="os.mkfifo is POSIX-only")
    def test_a_planted_symlink_does_not_park_the_retraction_on_a_stat(self, cfg):
        """A stat that FOLLOWS a planted symlink is the loop's other parking spot.

        ``Path.exists()`` resolves the final component, so a symlink planted at
        the predictable ``session_pid_<pid>.txt`` and aimed into a filesystem
        that does not answer -- a hung automount, a dead NFS server -- parks the
        gateway event loop inside the probe, before the hardened reader whose
        ``O_NOFOLLOW`` was supposed to make touching that path safe ever runs.

        A stat that never returns is what such a mount IS, so that is what is
        planted here: the link is real and its target is a writer-less FIFO, and
        ``Path.exists`` on the two mapping paths alone is held open for the
        length of the probe. Bounded with a timeout rather than a plain
        assertion because the failure mode is "never returns", and the hold is
        released in ``finally`` so a thread parked by a regression cannot outlive
        the test and turn a failure into a lost run.
        """
        fifo = cfg / "planted-fifo"
        os.mkfifo(fifo)
        link = cfg / "session_pid_4242.txt"
        link.symlink_to(fifo)
        watched = {link, cfg / "session_pid_4242.sig"}
        released = threading.Event()
        real_exists = Path.exists

        def unanswering_exists(self, *args, **kwargs):
            if self in watched:
                released.wait(30)
            return real_exists(self, *args, **kwargs)

        done = threading.Event()
        verdict: list[bool] = []

        def probe() -> None:
            with patch.object(platform_compat, "pid_exists", return_value=False):
                verdict.append(session_pid_sig.unpublish_session_pid(4242))
            done.set()

        prober = threading.Thread(target=probe, daemon=True)
        with patch.object(Path, "exists", unanswering_exists):
            prober.start()
            try:
                assert done.wait(10), "a symlink-following stat parked the retraction"
            finally:
                released.set()
                prober.join(10)
        assert verdict == [True]
        assert not link.is_symlink(), "the planted link outlived a proven-dead pid"
        assert fifo.is_fifo(), "the unlink dropped the link, not its target"

    def test_a_dangling_symlinked_mapping_is_retracted_like_the_sweep_does(self, cfg):
        """A link to nothing still occupies the path, so a dead pid retracts it.

        This is the one answer that a non-following retraction changes: a
        dangling symlink has no stat to succeed, so a following probe reads the
        path as empty and leaves the link sitting at it. The sweep does not --
        ``_prune_stale_session_pid_files`` globs the directory entry and unlinks
        it once the pid is proven dead -- and the two paths must not disagree
        about the same file, or the docstring's "can only remove what the sweep
        would also remove" stops being true in the direction that accumulates.
        """
        link = cfg / "session_pid_4242.txt"
        link.symlink_to(cfg / "target-that-was-never-created")
        with patch.object(platform_compat, "pid_exists", return_value=False):
            assert session_pid_sig.unpublish_session_pid(4242) is True
        assert not link.is_symlink()

    def test_a_live_pid_keeps_even_a_dangling_symlinked_mapping(self, cfg):
        """Retracting a link to nothing is still gated on proof of death.

        An unreadable mapping is identity UNKNOWN, and unknown retains -- the
        same direction as every other branch. Pinned separately because the
        unlink reached by dropping the stat is otherwise one proof away from
        deleting a path whose owner is still running.
        """
        link = cfg / "session_pid_4242.txt"
        link.symlink_to(cfg / "target-that-was-never-created")
        with patch.object(platform_compat, "pid_exists", return_value=True):
            assert session_pid_sig.unpublish_session_pid(4242) is False
        assert link.is_symlink()

    def test_retraction_never_waits_for_the_publication_mutex(self, cfg):
        """Contention defers the retraction; it never blocks the loop on a peer.

        The other side of this mutex is ``publish_session_pid``, which holds it
        across two ``atomic_write`` fsyncs on the maintenance executor. A blocking
        acquire here would hand the event loop the length of that fsync. Giving up
        costs nothing a control depends on -- the mapping is simply left for
        ``_prune_stale_session_pid_files``, the same outcome as an unproven death.
        """
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        done = threading.Event()
        verdict: list[bool] = []

        def probe() -> None:
            with patch.object(platform_compat, "pid_exists", return_value=False):
                verdict.append(session_pid_sig.unpublish_session_pid(4242))
            done.set()

        with session_pid_sig._mapping_mutation_lock:
            threading.Thread(target=probe, daemon=True).start()
            assert done.wait(10), "retraction waited for the publication mutex"
            assert verdict == [False]
            assert (cfg / "session_pid_4242.txt").exists(), "a deferred retraction deletes nothing"

    def test_publication_and_retraction_take_the_same_mutex(self):
        """Serialization is the in-process half of the same fix.

        Both directions mutate one predictable path, and both run in the gateway
        process -- publication on the maintenance executor, retraction on a
        provider teardown -- so a shared mutex is what stops them interleaving at
        all. Pinned structurally because a lock that one side quietly stops
        taking still passes every single-threaded behavioural test above.
        """
        import inspect

        for fn in (session_pid_sig.publish_session_pid, session_pid_sig.unpublish_session_pid):
            assert "_mapping_mutation_lock" in inspect.getsource(fn), fn.__name__
