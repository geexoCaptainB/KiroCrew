"""Contract tests for ``messaging/auto_title.py``.

Auto-titling was Slack-only, and dead on Slack's own default path:
``_maybe_auto_title_slack`` was called from the native loop and nowhere else,
while ``messaging.use_transport`` defaults True — so a default install titled
nothing and every surface fell back to a deterministic truncation.

These tests pin the hoisted core: the claim that makes a session titled exactly
once even with two channels racing, the two guards that stop a generated name
from replacing a name a person chose, and the tool-free turn.

Every test is written so that reverting the guard it names turns it red — see the
per-test notes on what to break.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, EVENT_TEXT_CHUNK
from kiro_crew.messaging import auto_title

_KEY = "telegram:kirocrew:direct:4242"


def _ev(kind: str, **kw):
    return SimpleNamespace(kind=kind, text=kw.get("text", ""), request_id=kw.get("request_id"))


class _Provider:
    """Yields a scripted event list, recording every tool it was refused."""

    def __init__(self, events=None, raises: BaseException | None = None, delay: float = 0.0):
        self._events = events or []
        self._raises = raises
        self._delay = delay
        self.rejected: list = []
        self.prompts: list[str] = []

    async def stream(self, message, timeout=120.0):
        self.prompts.append(message)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        for event in self._events:
            yield event

    @staticmethod
    def slow(title: str, delay: float) -> "_Provider":
        """A provider that WOULD produce a usable title, but only eventually.

        The delay has to sit ahead of a real title: a slow provider that ends up
        yielding nothing is indistinguishable from a SKIP, so a test built on one
        passes with the timeout deleted.
        """
        return _Provider([_ev(EVENT_TEXT_CHUNK, text=title), _ev(EVENT_COMPLETE)], delay=delay)

    async def reject_tool(self, request_id):
        self.rejected.append(request_id)


class _Sessions:
    """Minimal ``SessionManager`` surface ``background_turn`` needs."""

    def __init__(self, provider: _Provider | None = None):
        self._provider = provider or _Provider()
        self.acquired: list[str] = []
        self.released: list[str] = []
        self.recycled = 0

    async def get_or_create(self, key, agent=None, channel_id=None):
        self.acquired.append(key)
        return self._provider, True, False

    def release(self, key):
        self.released.append(key)

    async def recycle_background(self):
        self.recycled += 1


class _Log:
    """``ConversationLog`` stand-in over one in-memory metadata dict.

    Implements the real ``update_metadata_if`` contract: the guard is evaluated
    against the record as it stands at write time, the return value says whether
    the merge was applied, and ``require_existing`` refuses a session with no
    file at all. *exists* stands for that file, which is a separate question from
    what the metadata dict holds -- in the real store an absent session and an
    untitled one both reach the guard as ``{}``.

    *becomes* models the record being REPLACED during the naming turn: the first
    status read (the caller's own, before the turn) sees the original, and
    everything after it sees the replacement. That is what a deletion plus a new
    message on the same thread does, because the session key is derived from the
    thread rather than from the record. An empty *becomes* means the record is
    GONE, which is a different state from a record that is merely stamp-less.

    *becomes_unreadable* models the first line being damaged during the turn: the
    real store answers ``({}, False)`` for that, without raising, and refuses the
    write. An unreadable record is evidence of neither presence nor absence.
    """

    def __init__(
        self,
        meta: dict | None = None,
        raises: BaseException | None = None,
        *,
        exists: bool = True,
        becomes: dict | None = None,
        becomes_unreadable: bool = False,
    ):
        self.meta = dict(meta or {})
        self.raises = raises
        self.exists = exists
        self.becomes = becomes
        self.becomes_unreadable = becomes_unreadable
        self.readable = True
        self.guarded_calls: list[tuple[str, dict]] = []
        self.required_existing: list[bool] = []
        self.metadata_reads: list[str] = []

    def get_metadata_status(self, key: str) -> tuple[dict, bool]:
        self.metadata_reads.append(key)
        current = (dict(self.meta) if self.exists else {}, self.readable)
        if len(self.metadata_reads) == 1:
            if self.becomes is not None:
                self.meta = dict(self.becomes)
                self.exists = bool(self.becomes)
            if self.becomes_unreadable:
                self.readable = False
        return current

    def get_metadata(self, key: str) -> dict:
        return self.get_metadata_status(key)[0]

    def update_metadata_if(self, key, fields, guard, *, require_existing: bool = False):
        if self.raises is not None:
            raise self.raises
        self.guarded_calls.append((key, dict(fields)))
        self.required_existing.append(require_existing)
        if require_existing and not self.exists:
            return False
        # The real store refuses an unreadable record before it consults the
        # guard, so a damaged first line is a refusal rather than an exception.
        if not self.readable:
            return False
        if not guard(self.meta):
            return False
        self.meta.update(fields)
        return True


def _title_provider(title: str = "Deploy the gateway") -> _Provider:
    return _Provider([_ev(EVENT_TEXT_CHUNK, text=title), _ev(EVENT_COMPLETE)])


@pytest.fixture(autouse=True)
def _isolate_claims():
    """The claim tracker is a process global; make every test hermetic."""
    auto_title.reset()
    yield
    auto_title.reset()


@pytest.fixture()
def audits(monkeypatch):
    events: list[dict] = []
    fake = MagicMock()
    fake.log_api_access = lambda **kw: events.append(kw)
    monkeypatch.setattr(auto_title, "sel", lambda: fake)
    return events


# ──────────────────────────────────────────────────────────────────────
# The claim
# ──────────────────────────────────────────────────────────────────────
class TestClaim:
    def test_only_the_first_caller_gets_the_claim(self):
        """Mutation: make ``try_claim`` always return True — red.

        Without check-and-mark in ONE synchronous step, two turns that resolved
        to the same session each fire a naming task and the conversation is
        titled twice (and billed twice).
        """
        assert auto_title.try_claim(_KEY) is True
        assert auto_title.try_claim(_KEY) is False
        assert auto_title.is_titled(_KEY) is True

    def test_releasing_the_claim_allows_a_retry(self):
        auto_title.try_claim(_KEY)
        auto_title.release_claim(_KEY)
        assert auto_title.try_claim(_KEY) is True

    def test_the_lru_evicts_the_least_recently_marked(self, monkeypatch):
        """Mutation: drop the ``popitem`` in ``mark_titled`` — red."""
        monkeypatch.setattr(auto_title, "TITLE_LRU_MAX", 1)
        auto_title.mark_titled("a", auto_title.TITLE_KIND_AUTO)
        auto_title.mark_titled("b", auto_title.TITLE_KIND_MANUAL)
        assert auto_title.is_titled("a") is False
        assert auto_title.titled_kind("b") == auto_title.TITLE_KIND_MANUAL

    @pytest.mark.asyncio
    async def test_two_concurrent_turns_title_the_session_once(self, audits):
        """The claim-early race, driven through the real entry point.

        Both turns arrive together; whoever loses ``try_claim`` must not run a
        naming turn at all. Mutation: replace the ``try_claim`` calls below with
        an unguarded ``mark_titled`` — red, because both would title.
        """
        provider = _title_provider()
        sessions = _Sessions(provider)
        log = _Log()
        applied: list[str] = []

        async def _one_turn() -> None:
            if not auto_title.try_claim(_KEY):
                return
            title = await auto_title.maybe_auto_title(
                sessions, log, _KEY, "user", "assistant", source="telegram"
            )
            if title:
                applied.append(title)

        await asyncio.gather(_one_turn(), _one_turn())
        assert applied == ["Deploy the gateway"]
        assert len(provider.prompts) == 1  # one naming turn, so one bill
        assert log.meta["title"] == "Deploy the gateway"


# ──────────────────────────────────────────────────────────────────────
# A person's name always wins
# ──────────────────────────────────────────────────────────────────────
class TestManualTitleWins:
    @pytest.mark.asyncio
    async def test_a_manual_rename_landing_mid_stream_is_not_overwritten(self, audits):
        """The in-process guard.

        Mutation: delete the ``titled_kind(...) == TITLE_KIND_MANUAL`` check —
        red, because the generated name replaces the one the user just typed.
        """
        auto_title.mark_titled(_KEY, auto_title.TITLE_KIND_MANUAL)
        renamed: list[str] = []
        log = _Log()
        title = await auto_title.maybe_auto_title(
            _Sessions(_title_provider()),
            log,
            _KEY,
            "user",
            "assistant",
            source="telegram",
            set_channel_title=lambda t: _append(renamed, t),
        )
        assert title == ""
        assert renamed == []
        assert log.guarded_calls == []

    @pytest.mark.asyncio
    async def test_a_title_from_before_a_restart_is_not_overwritten(self, audits):
        """The PERSISTED guard, which is the one that survives a restart.

        After a restart the claim tracker is empty, so the in-process guard above
        is blind and the claim is taken again. The record itself still carries the
        name, and ``update_metadata_if``'s guard refuses under the lock.

        Mutation: write with an unguarded ``set_title``/``update_metadata`` (or
        ignore the returned ``applied``) — red, because a manual title made in an
        earlier process is silently replaced, on the transcript AND on the
        channel.
        """
        log = _Log({"title": "Quarterly review"})
        renamed: list[str] = []
        assert auto_title.try_claim(_KEY) is True  # ← the restart: no memory of it
        title = await auto_title.maybe_auto_title(
            _Sessions(_title_provider()),
            log,
            _KEY,
            "user",
            "assistant",
            source="telegram",
            set_channel_title=lambda t: _append(renamed, t),
        )
        assert title == ""
        assert log.meta["title"] == "Quarterly review"
        assert renamed == []  # the channel keeps the user's name too

    @pytest.mark.asyncio
    async def test_a_deterministic_fallback_record_is_titled(self, audits):
        """The other side of the same guard: no title on the record means the
        surface is still showing its deterministic fallback, so name it."""
        log = _Log({"agent": "kirocrew"})
        renamed: list[str] = []
        title = await auto_title.maybe_auto_title(
            _Sessions(_title_provider()),
            log,
            _KEY,
            "user",
            "assistant",
            source="telegram",
            set_channel_title=lambda t: _append(renamed, t),
        )
        assert title == "Deploy the gateway"
        assert log.meta["title"] == "Deploy the gateway"
        assert renamed == ["Deploy the gateway"]

    @pytest.mark.asyncio
    async def test_a_blank_title_on_the_record_does_not_block_naming(self, audits):
        log = _Log({"title": "   "})
        title = await auto_title.maybe_auto_title(
            _Sessions(_title_provider()), log, _KEY, "u", "a", source="telegram"
        )
        assert title == "Deploy the gateway"


# ──────────────────────────────────────────────────────────────────────
# A conversation deleted during the naming turn stays deleted
# ──────────────────────────────────────────────────────────────────────
class TestDeletedDuringTheTurn:
    """The naming turn is a whole LLM round trip, so a deletion can land inside
    it. The guard alone cannot refuse that: an absent record and an untitled one
    both reach it as an empty dict, and the merge upserts. Nor can existence
    alone, because the session key is derived from the thread and outlives the
    record it named, so the deleted conversation can be replaced under it. And
    the claim, which lives in a process-wide LRU, must not outlive either."""

    @pytest.mark.asyncio
    async def test_a_session_deleted_mid_turn_is_not_recreated_as_a_title(self, audits):
        """Mutation: drop ``require_existing=True`` at the write -- red.

        Without it the empty record passes ``_record_is_untitled``, the merge
        upserts, and the deleted conversation comes back as a sidebar row whose
        only content is a generated name.
        """
        log = _Log(exists=False)  # deleted while the title was being generated
        renamed: list[str] = []
        title = await auto_title.maybe_auto_title(
            _Sessions(_title_provider()),
            log,
            _KEY,
            "user",
            "assistant",
            source="telegram",
            set_channel_title=lambda t: _append(renamed, t),
        )
        assert title == ""
        assert log.meta == {}  # nothing was written back
        assert renamed == []  # and the channel is not named either

    @pytest.mark.asyncio
    async def test_the_write_asks_the_store_to_refuse_absence(self, audits):
        """The opt-in reaches the store on the ordinary path too.

        Mutation: drop the keyword, or pass ``require_existing=False`` -- red.
        Asserted separately from the behaviour above because the fake could
        refuse for its own reasons and leave that test green with the real
        request never made.
        """
        log = _Log({"agent": "kirocrew"})
        title = await auto_title.maybe_auto_title(
            _Sessions(_title_provider()), log, _KEY, "u", "a", source="telegram"
        )
        assert title == "Deploy the gateway"
        assert log.required_existing == [True]

    @pytest.mark.asyncio
    async def test_a_replacement_under_the_same_key_is_not_given_the_old_title(self, audits):
        """Existence alone is not enough, because the key outlives the record.

        A channel session key is derived from the thread, so deleting the
        conversation and messaging that thread again mints a NEW record under the
        SAME key. The file then exists and carries no title, so a check that asks
        only whether the session is there lets the turn write a name derived from
        the conversation that was deleted.

        Mutation: drop the identity term from the guard (pass
        ``_record_is_untitled``) -- red, because the replacement is untitled.
        """
        log = _Log(
            {"created_at": "2026-09-20T05:00:00.100000+00:00"},
            becomes={"created_at": "2026-09-20T05:00:31.900000+00:00"},
        )
        renamed: list[str] = []
        title = await auto_title.maybe_auto_title(
            _Sessions(_title_provider()),
            log,
            _KEY,
            "user",
            "assistant",
            source="telegram",
            set_channel_title=lambda t: _append(renamed, t),
        )
        assert title == ""
        assert "title" not in log.meta  # the replacement keeps its own identity
        assert renamed == []

    @pytest.mark.asyncio
    async def test_a_vanished_record_releases_the_claim(self, audits):
        """The claim must not outlive the conversation it was taken for.

        It lives in a process-wide LRU, so a claim held through a deletion
        silences auto-titling for whatever takes the key next until the gateway
        restarts.

        Mutation: remove the ``release_claim`` call in the refusal branch -- red.
        """
        assert auto_title.try_claim(_KEY) is True
        log = _Log(exists=False)
        title = await auto_title.maybe_auto_title(
            _Sessions(_title_provider()), log, _KEY, "u", "a", source="telegram"
        )
        assert title == ""
        assert auto_title.is_titled(_KEY) is False  # a later conversation may be named

    @pytest.mark.asyncio
    async def test_a_record_without_a_creation_stamp_is_still_pinned(self, audits):
        """A stamp-less record is not the same state as no record.

        Every path that mints a metadata line stamps it from a clock, so a
        replacement always acquires one. Requiring a stamp-less record to still
        have none therefore pins it as firmly as a stamp pins the ordinary case.

        Mutation: fold the two states together (accept anything when the stamp is
        empty) -- red, because the replacement is untitled and the original had
        no stamp to compare.
        """
        log = _Log(
            {"agent": "kirocrew"},  # written in an older shape: no created_at
            becomes={"agent": "kirocrew", "created_at": "2026-09-20T06:00:12.500000+00:00"},
        )
        renamed: list[str] = []
        title = await auto_title.maybe_auto_title(
            _Sessions(_title_provider()),
            log,
            _KEY,
            "user",
            "assistant",
            source="telegram",
            set_channel_title=lambda t: _append(renamed, t),
        )
        assert title == ""
        assert "title" not in log.meta
        assert renamed == []

    @pytest.mark.asyncio
    async def test_a_deleted_record_without_a_stamp_still_releases_the_claim(self, audits):
        """The gone case has to be asked separately from the replaced one.

        An absent record carries no stamp either, so a stamp-less record reads as
        an identity MATCH once it is deleted, and a check that only compares
        stamps would keep the claim on a conversation that is gone.

        Mutation: drop the ``not meta`` term from the re-read -- red.
        """
        assert auto_title.try_claim(_KEY) is True
        log = _Log({"agent": "kirocrew"}, becomes={})  # no stamp, then deleted
        title = await auto_title.maybe_auto_title(
            _Sessions(_title_provider()), log, _KEY, "u", "a", source="telegram"
        )
        assert title == ""
        assert auto_title.is_titled(_KEY) is False

    @pytest.mark.asyncio
    async def test_an_unreadable_record_keeps_the_claim(self, audits):
        """A read that did not answer is not evidence the record is gone.

        The store answers a damaged first line with an empty dict and a false
        readable flag, without raising, so a check that reads only the dict sees
        the same value a deletion produces. Releasing the claim there bills a
        fresh naming turn on every following exchange for as long as the record
        stays damaged.

        Mutation: read the dict alone (``get_metadata``) instead of the status --
        red, because the empty dict reads as a deletion.
        """
        assert auto_title.try_claim(_KEY) is True
        log = _Log({"created_at": "2026-09-20T06:30:00.250000+00:00"}, becomes_unreadable=True)
        title = await auto_title.maybe_auto_title(
            _Sessions(_title_provider()), log, _KEY, "u", "a", source="telegram"
        )
        assert title == ""
        assert auto_title.is_titled(_KEY) is True  # unreadable is not a verdict

    @pytest.mark.asyncio
    async def test_a_record_that_is_already_named_keeps_the_claim(self, audits):
        """The complement, so the release above is conditional and not blanket.

        A refusal because somebody already named the conversation must KEEP the
        claim: releasing it spends another naming turn on a conversation that
        does not need one.

        Mutation: release the claim unconditionally on refusal -- red.
        """
        assert auto_title.try_claim(_KEY) is True
        log = _Log({"title": "Chosen by hand", "created_at": "2026-09-20T05:00:00.100000+00:00"})
        title = await auto_title.maybe_auto_title(
            _Sessions(_title_provider()), log, _KEY, "u", "a", source="telegram"
        )
        assert title == ""
        assert log.meta["title"] == "Chosen by hand"  # untouched
        assert auto_title.is_titled(_KEY) is True


async def _append(sink: list[str], title: str) -> None:
    sink.append(title)


# ──────────────────────────────────────────────────────────────────────
# The turn itself
# ──────────────────────────────────────────────────────────────────────
class TestTurn:
    @pytest.mark.asyncio
    async def test_every_tool_request_is_rejected_and_audited(self, audits):
        """A naming turn must never run a tool.

        The prompt is built from text the model itself produced, so a tool call
        here is prompt-injection reach. Mutation: drop the
        ``EVENT_PERMISSION_REQUEST`` branch — red on both assertions (nothing
        rejected, nothing audited), and the request is left unanswered so the
        agent process wedges.
        """
        provider = _Provider(
            [
                _ev(EVENT_PERMISSION_REQUEST, request_id="rq1"),
                _ev(EVENT_TEXT_CHUNK, text="Deploy the gateway"),
                _ev(EVENT_COMPLETE),
            ]
        )
        title = await auto_title.maybe_auto_title(
            _Sessions(provider), None, _KEY, "u", "a", source="telegram"
        )
        assert provider.rejected == ["rq1"]
        assert title == "Deploy the gateway"
        rejections = [e for e in audits if e["operation"] == "auto_title.tool_rejected"]
        assert rejections and rejections[0]["outcome"] == "denied"
        assert rejections[0]["source"] == "telegram"
        assert rejections[0]["resources"] == "rq1"

    @pytest.mark.asyncio
    async def test_the_background_session_is_released(self, audits):
        sessions = _Sessions(_title_provider())
        await auto_title.maybe_auto_title(sessions, None, _KEY, "u", "a", source="telegram")
        assert sessions.released  # BACKGROUND_KEY released in background_turn's finally

    @pytest.mark.asyncio
    async def test_the_turn_label_names_the_channel(self, audits, monkeypatch):
        """Background spend is attributed per channel, not pooled.

        Mutation: hardcode ``task="slack_auto_title"`` — red.
        """
        seen: dict = {}
        real = auto_title.background_turn

        def _spy(sessions, *, task, agent=None):
            seen["task"] = task
            return real(sessions, task=task, agent=agent)

        monkeypatch.setattr(auto_title, "background_turn", _spy)
        await auto_title.maybe_auto_title(
            _Sessions(_title_provider()), None, _KEY, "u", "a", source="telegram"
        )
        assert seen["task"] == "telegram_auto_title"

    @pytest.mark.asyncio
    async def test_the_prompt_is_bounded_on_both_sides(self, audits):
        """Mutation: drop the ``[:TITLE_INPUT_CHARS]`` slices — red.

        An unbounded prompt is an unbounded bill on a turn whose whole output is
        six words.
        """
        provider = _title_provider()
        await auto_title.maybe_auto_title(
            _Sessions(provider), None, _KEY, "u" * 5000, "a" * 5000, source="telegram"
        )
        prompt = provider.prompts[0]
        assert "u" * auto_title.TITLE_INPUT_CHARS in prompt
        assert "u" * (auto_title.TITLE_INPUT_CHARS + 1) not in prompt
        assert "a" * (auto_title.TITLE_INPUT_CHARS + 1) not in prompt

    @pytest.mark.asyncio
    async def test_a_skip_verdict_releases_the_claim(self, audits):
        """Mutation: drop the ``release_claim`` on the SKIP branch — red.

        A conversation that was not nameable YET must be nameable at its next
        exchange; keeping the claim leaves it on the fallback name forever.
        """
        auto_title.try_claim(_KEY)
        renamed: list[str] = []
        title = await auto_title.maybe_auto_title(
            _Sessions(_Provider([_ev(EVENT_TEXT_CHUNK, text="SKIP"), _ev(EVENT_COMPLETE)])),
            _Log(),
            _KEY,
            "hi",
            "hello",
            source="telegram",
            set_channel_title=lambda t: _append(renamed, t),
        )
        assert title == ""
        assert renamed == []
        assert auto_title.is_titled(_KEY) is False

    @pytest.mark.asyncio
    async def test_a_stream_failure_releases_the_claim(self, audits):
        """Mutation: drop the ``release_claim`` in the outer ``except`` — red."""
        auto_title.try_claim(_KEY)
        title = await auto_title.maybe_auto_title(
            _Sessions(_Provider(raises=RuntimeError("provider died"))),
            _Log(),
            _KEY,
            "u",
            "a",
            source="telegram",
        )
        assert title == ""
        assert auto_title.is_titled(_KEY) is False

    @pytest.mark.asyncio
    async def test_a_slow_turn_is_abandoned_and_the_claim_released(self, audits, monkeypatch):
        """Mutation: replace the ``wait_for`` timeout with ``None`` — red.

        The provider WOULD produce a usable title, just far too late, so without
        the budget the title lands and the log is written. The budget is lowered
        rather than waited out, so the passing case stays fast.
        """
        monkeypatch.setattr(auto_title, "TITLE_TURN_TIMEOUT_SECS", 0.01)
        auto_title.try_claim(_KEY)
        log = _Log()
        title = await auto_title.maybe_auto_title(
            _Sessions(_Provider.slow("Deploy the gateway", 0.5)),
            log,
            _KEY,
            "u",
            "a",
            source="telegram",
        )
        assert title == ""
        assert log.meta == {}
        assert auto_title.is_titled(_KEY) is False

    @pytest.mark.asyncio
    async def test_a_transcript_write_failure_still_renames_the_channel(self, audits):
        """A name was generated and the turn was spent; losing the transcript
        write must not also lose the visible rename, and must not look like a
        retryable failure."""
        auto_title.try_claim(_KEY)
        renamed: list[str] = []
        title = await auto_title.maybe_auto_title(
            _Sessions(_title_provider()),
            _Log(raises=OSError("log locked")),
            _KEY,
            "u",
            "a",
            source="telegram",
            set_channel_title=lambda t: _append(renamed, t),
        )
        assert title == "Deploy the gateway"
        assert renamed == ["Deploy the gateway"]
        assert auto_title.is_titled(_KEY) is True

    @pytest.mark.asyncio
    async def test_no_channel_setter_still_titles_the_transcript(self, audits):
        """A channel with no renameable conversation omits the callback."""
        log = _Log()
        title = await auto_title.maybe_auto_title(
            _Sessions(_title_provider()), log, _KEY, "u", "a", source="telegram"
        )
        assert title == "Deploy the gateway"
        assert log.meta["title"] == "Deploy the gateway"

    @pytest.mark.asyncio
    async def test_the_success_audit_names_the_channel(self, audits):
        await auto_title.maybe_auto_title(
            _Sessions(_title_provider()),
            None,
            _KEY,
            "u",
            "a",
            source="telegram",
            resources="chat42:" + _KEY,
        )
        applied = [e for e in audits if e["operation"] == "telegram.thread_auto_title"]
        assert applied and applied[0]["source"] == "telegram"
        assert applied[0]["resources"] == "chat42:" + _KEY


# ──────────────────────────────────────────────────────────────────────
# Prompt and title cleaning
# ──────────────────────────────────────────────────────────────────────
class TestCleaning:
    def test_a_curly_brace_in_the_conversation_does_not_raise(self):
        """The conversation text reaches the prompt verbatim, braces included.

        Mutation: apply ``.format(...)`` to the assembled prompt (the shape that
        made this an f-string) — red with ``KeyError: '"key"'``, swallowed by the
        outer ``except`` as a silently missing title for every JSON exchange.
        """
        prompt = auto_title.build_title_prompt('parse this: {"key": "value"}', "sure {}")
        assert '{"key": "value"}' in prompt
        assert "sure {}" in prompt

    def test_only_the_first_line_is_kept_and_quoting_is_trimmed(self):
        assert auto_title.clean_title('"Deploy the gateway".\nand more') == "Deploy the gateway"

    def test_angle_brackets_are_dropped(self):
        """They open a link in Slack mrkdwn and a tag in Telegram HTML, and a
        title is rendered as-is on both. Mutation: drop the ``replace`` calls —
        red."""
        cleaned = auto_title.clean_title("<https://evil.test|click me>")
        assert "<" not in cleaned and ">" not in cleaned

    def test_the_skip_verdict_and_an_empty_reply_mean_no_title(self):
        assert auto_title.clean_title("SKIP") == ""
        assert auto_title.clean_title("skip") == ""
        assert auto_title.clean_title("") == ""
        assert auto_title.clean_title("   \n  ") == ""

    def test_a_credential_in_the_title_is_redacted(self):
        """The model can echo a secret back in the name it proposes, and a title
        is displayed everywhere the conversation is listed. Mutation: drop the two
        redactor calls — red."""
        cleaned = auto_title.clean_title("AKIAIOSFODNN7EXAMPLE key rotation")
        assert "AKIAIOSFODNN7EXAMPLE" not in cleaned

    def test_the_title_is_capped(self):
        """Mutation: drop the ``[:TITLE_MAX_CHARS]`` slice — red."""
        assert len(auto_title.clean_title("z" * 500)) == auto_title.TITLE_MAX_CHARS


# ──────────────────────────────────────────────────────────────────────
# The per-loop lock
# ──────────────────────────────────────────────────────────────────────
class TestLock:
    @pytest.mark.asyncio
    async def test_reset_releases_a_held_permit(self):
        """``reset()`` does both halves, and this is the half easy to leave out.

        A caller resetting this state is recovering from something that did not
        finish: a test crashing mid-title leaves the claim marked AND the lock held.
        Clearing only the claim leaves the next caller blocking on a permit nobody
        will release, and `LoopBoundLock` rebinding per loop covers a NEW loop but
        not a leaked permit on the same one.
        """
        held = auto_title.get_lock()
        await held.acquire()  # deliberately never released, as a crash would leave it
        assert held.locked()

        auto_title.reset()

        fresh = auto_title.get_lock()
        assert fresh is not held, "reset must install a lock, not reuse the held one"
        assert not fresh.locked()
        # And it is actually usable, not merely reporting itself free.
        await asyncio.wait_for(fresh.acquire(), timeout=1)
        fresh.release()

    def test_the_lock_still_works_when_the_event_loop_changes(self):
        """A bare module-global ``asyncio.Lock`` acquired from a second loop raises
        ``RuntimeError``, which the outer ``except Exception`` then swallows as a
        silently skipped title. The shared ``LoopBoundLock`` keeps one inner lock
        per loop, so the guarantee is asserted through what a caller can observe:
        the second loop acquires it and its title still lands.

        The lock OBJECT is deliberately stable across loops -- it is the module
        global callers hold -- so identity is not the thing to assert here; a
        rebound pointer is the design ``LoopBoundLock`` exists to replace, because
        a release on one loop would unlock another's critical section.
        """

        def _run_once(key: str):
            provider = _title_provider()
            log = _Log()

            async def _hold(lock, seen: list[int]):
                async with lock:
                    seen.append(1)
                    await asyncio.sleep(0)

            async def _go():
                lock = auto_title.get_lock()
                # CONTEND it. An uncontended `asyncio.Lock.acquire()` fast-paths
                # without ever calling `_get_loop()`, so it never binds a loop and
                # a bare lock would pass this test with the defect intact. Two
                # concurrent holders make the second one wait, which is the call
                # that binds -- and, for a bare lock on the second loop, raises.
                seen: list[int] = []
                await asyncio.gather(*(_hold(lock, seen) for _ in range(3)))
                assert len(seen) == 3
                await auto_title.maybe_auto_title(
                    _Sessions(provider), log, key, "u", "a", source="telegram"
                )
                return lock, lock._bound()  # this loop's underlying asyncio.Lock

            lock, inner = asyncio.run(_go())
            return lock, inner, log

        lock1, inner1, log1 = _run_once("k1")
        lock2, inner2, log2 = _run_once("k2")
        # One shared chokepoint object, one inner lock per loop.
        assert lock2 is lock1, "the module global is the object callers hold"
        assert inner2 is not inner1
        assert log1.meta["title"] == "Deploy the gateway"
        assert log2.meta["title"] == "Deploy the gateway"
