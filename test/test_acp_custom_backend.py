"""The operator-named ACP harness: ``ACP_BACKEND_CUSTOM``.

One id whose launch facts arrive from ``config.json`` rather than from the leaf.
What these pin is the property the whole design rests on -- there is NO state in
which ``custom`` is selectable and its routing row is missing -- and the seams that
property depends on:

* the leaf's one writer, ``register_custom_backend``, writes launch, permission
  pair, routing and selectability together and its inverse removes all four;
* an incomplete spec is REFUSED (and unregisters), never filled in;
* the config load registers BEFORE it normalizes ``agent.acp_backend``, so a
  persisted ``"custom"`` survives a load that carries a complete spec and degrades
  on one that does not;
* the spawn arm launches exactly the configured command and args, and a CHANGED
  command takes effect on the next spawn rather than the previous path;
* the session-config gate refuses a harness that does not advertise the configured
  option, with the option named;
* the dashboard's write path is owner-only and shape-checked;
* the install probe answers "not configured" before a spec exists and probes the
  configured command after.

Every test that registers a spec restores the registry, because the registry is
module state every other test reads.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.acp import client as client_mod
from kiro_crew.acp.client import AcpClient, AcpToolGateUnroutable
from kiro_crew.agent_sdk import backend_install, backends
from kiro_crew.agent_sdk import tool_gate as gate
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CODEX,
    ACP_BACKEND_CUSTOM,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_LAUNCH,
    ACP_BACKEND_PERMISSION_CONFIG,
    ACP_BACKEND_ROUTING,
    BASELINE_SELECTABLE_BACKENDS,
    CUSTOM_ACP_LABEL,
    CustomAcpSpec,
    Routing,
    coerce_custom_acp_spec,
    custom_backend_spec,
    register_custom_backend,
    routing_for,
    selectable_backends,
    unregister_custom_backend,
)
from kiro_crew.config.loader import KiroCrewConfig, _build_agent_config

COMPLETE = CustomAcpSpec(
    command="my-acp", args=("serve", "--stdio"), gate_option="mode", gate_value="read-only"
)
COMPLETE_RAW = {
    "command": "my-acp",
    "args": ["serve", "--stdio"],
    "gate_option": "mode",
    "gate_value": "read-only",
}


@pytest.fixture
def clean_registry():
    """Snapshot and restore every table the custom registration writes.

    Restores the selectable pair through ``agent_sdk.backends`` -- the module that
    DEFINES them -- and ends with the custom rows exactly as they were found, so a
    test that registered a spec leaves nothing behind for the next one to trip on.
    """
    baseline_before = set(backends._baseline)
    selectable_before = set(backends._selectable)
    denied_before = set(backends._denied)
    spec_before = custom_backend_spec()
    saved_caches = dict(client_mod._self_served_bin_caches)
    saved_records = dict(client_mod._self_served_bin_cache_records)
    yield
    backends._denied.clear()
    backends._denied.update(denied_before)
    register_custom_backend(spec_before)
    backends._baseline.clear()
    backends._baseline.update(baseline_before)
    backends._selectable.clear()
    backends._selectable.update(selectable_before)
    client_mod._self_served_bin_caches.clear()
    client_mod._self_served_bin_caches.update(saved_caches)
    client_mod._self_served_bin_cache_records.clear()
    client_mod._self_served_bin_cache_records.update(saved_records)


# ── The leaf: one writer, one inverse ──


class TestRegistration:
    def test_unconfigured_custom_is_known_but_not_selectable(self, clean_registry):
        """The import-time state: the id can be spelled and denied, and offers nothing."""
        unregister_custom_backend()
        assert ACP_BACKEND_CUSTOM in backends.ACP_BACKENDS_KNOWN
        assert ACP_BACKEND_CUSTOM not in BASELINE_SELECTABLE_BACKENDS
        assert ACP_BACKEND_CUSTOM not in selectable_backends()
        assert routing_for(ACP_BACKEND_CUSTOM) is Routing.UNVERIFIED
        assert ACP_BACKEND_CUSTOM not in ACP_BACKEND_LAUNCH
        assert gate.is_enforced(ACP_BACKEND_CUSTOM) is False

    def test_a_complete_spec_writes_launch_routing_and_selectability_together(self, clean_registry):
        register_custom_backend(COMPLETE)
        assert custom_backend_spec() == COMPLETE
        launch = ACP_BACKEND_LAUNCH[ACP_BACKEND_CUSTOM]
        assert launch.binary == "my-acp"
        assert launch.acp_args == ("serve", "--stdio")
        assert launch.label == CUSTOM_ACP_LABEL
        # The label a spawn is LOGGED under repeats the binary and the arg count, not
        # the args: they are the operator's and may carry a token.
        assert launch.spawn_label == "my-acp [2 operator-supplied args]"
        assert launch.args_are_operator_supplied is True
        assert ACP_BACKEND_ROUTING[ACP_BACKEND_CUSTOM] is Routing.SESSION_CONFIG
        assert ACP_BACKEND_PERMISSION_CONFIG[ACP_BACKEND_CUSTOM] == ("mode", "read-only")
        assert ACP_BACKEND_CUSTOM in selectable_backends()
        assert ACP_BACKEND_CUSTOM in backends.registered_backends()
        # The routing is the ENFORCED kind, which is what buys the credential mask
        # and the refusal on a harness that cannot be gated.
        assert gate.is_enforced(ACP_BACKEND_CUSTOM) is True
        verdict, _reason = gate.routing_verdict(ACP_BACKEND_CUSTOM)
        assert verdict is gate.Verdict.ROUTED

    def test_unregister_removes_every_row(self, clean_registry):
        register_custom_backend(COMPLETE)
        unregister_custom_backend()
        assert custom_backend_spec() is None
        assert ACP_BACKEND_CUSTOM not in ACP_BACKEND_LAUNCH
        # The routing row STAYS, reset to UNVERIFIED: the census over the table
        # covers every known id, and UNVERIFIED is what keeps this one unselectable.
        assert ACP_BACKEND_ROUTING[ACP_BACKEND_CUSTOM] is Routing.UNVERIFIED
        assert ACP_BACKEND_CUSTOM not in ACP_BACKEND_PERMISSION_CONFIG
        assert ACP_BACKEND_CUSTOM not in selectable_backends()
        # OUT of the baseline as well: a later policy recompute rebuilds the
        # effective set from the baseline, and an id left there would come back
        # with nothing runnable behind it.
        assert ACP_BACKEND_CUSTOM not in backends.registered_backends()
        # Idempotent.
        unregister_custom_backend()

    def test_none_unregisters(self, clean_registry):
        register_custom_backend(COMPLETE)
        register_custom_backend(None)
        assert ACP_BACKEND_CUSTOM not in selectable_backends()

    @pytest.mark.parametrize(
        "missing",
        ["command", "gate_option", "gate_value"],
    )
    def test_an_incomplete_spec_is_refused_and_unregisters(self, clean_registry, missing):
        """A spec missing any of its three required strings describes nothing Crew can
        run safely, so it is refused naming the gap -- and a spec registered before it
        does not survive it, because the refusal is what the operator's edit meant."""
        register_custom_backend(COMPLETE)
        raw = dict(COMPLETE_RAW)
        raw[missing] = ""
        with pytest.raises(ValueError, match=f"agent.custom_acp is incomplete.*{missing}"):
            register_custom_backend(coerce_custom_acp_spec(raw))
        assert ACP_BACKEND_CUSTOM not in selectable_backends()
        assert ACP_BACKEND_ROUTING[ACP_BACKEND_CUSTOM] is Routing.UNVERIFIED
        assert ACP_BACKEND_CUSTOM not in ACP_BACKEND_LAUNCH

    def test_a_changed_spec_replaces_rather_than_merges(self, clean_registry):
        register_custom_backend(COMPLETE)
        changed = CustomAcpSpec(
            command="other-acp", args=(), gate_option="approval", gate_value="ask"
        )
        register_custom_backend(changed)
        assert ACP_BACKEND_LAUNCH[ACP_BACKEND_CUSTOM].binary == "other-acp"
        assert ACP_BACKEND_LAUNCH[ACP_BACKEND_CUSTOM].acp_args == ()
        assert ACP_BACKEND_PERMISSION_CONFIG[ACP_BACKEND_CUSTOM] == ("approval", "ask")
        assert ACP_BACKEND_CUSTOM in selectable_backends()

    def test_the_policy_recompute_keeps_a_registered_custom(self, clean_registry):
        """Registration writes the BASELINE too, so a governance pass that runs after it
        sees the id rather than silently dropping it."""
        register_custom_backend(COMPLETE)
        backends.apply_selectable_denials(set())
        assert ACP_BACKEND_CUSTOM in selectable_backends()
        backends.apply_selectable_denials({ACP_BACKEND_CUSTOM})
        assert ACP_BACKEND_CUSTOM not in selectable_backends()
        assert ACP_BACKEND_CUSTOM in backends.registered_backends()

    def test_a_config_reload_cannot_undo_a_policy_denial(self, clean_registry):
        """The registrar runs on EVERY config load, and a load that follows a denial
        must leave the denied id off the switch -- for the unchanged spec (the
        idempotent return) and for a changed one (the full rewrite) alike. A
        loosened policy is what releases it, not a reload."""
        register_custom_backend(COMPLETE)
        backends.apply_selectable_denials({ACP_BACKEND_CUSTOM})
        register_custom_backend(COMPLETE)
        assert ACP_BACKEND_CUSTOM not in selectable_backends()
        changed = CustomAcpSpec(
            command="other-acp", args=(), gate_option="approval", gate_value="ask"
        )
        register_custom_backend(changed)
        assert ACP_BACKEND_CUSTOM not in selectable_backends()
        # Still REGISTERED: the rows are written and the baseline holds the id, so the
        # next recompute sees it and a loosened policy restores it.
        assert ACP_BACKEND_CUSTOM in backends.registered_backends()
        assert ACP_BACKEND_LAUNCH[ACP_BACKEND_CUSTOM].binary == "other-acp"
        backends.apply_selectable_denials(set())
        assert ACP_BACKEND_CUSTOM in selectable_backends()

    def test_registration_logs_no_args_and_no_gate_value(self, clean_registry, caplog):
        """The args and the gate pair are the operator's; a token on that command
        line must not land in a log that outlives the session."""
        secret = "sk-live-0123456789abcdef"
        spec = CustomAcpSpec(
            command="/opt/acp/my-acp",
            args=("--api-key", secret),
            gate_option="mode",
            gate_value="read-only",
        )
        with caplog.at_level(logging.INFO, logger="kiro_crew.agent_sdk.backends"):
            register_custom_backend(spec)
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "Custom ACP harness registered" in text
        assert secret not in text
        assert "--api-key" not in text
        assert "read-only" not in text
        assert "my-acp" in text
        assert "/opt/acp" not in text

    def test_there_is_no_path_to_selectable_without_routing(self, clean_registry):
        """The property everything above serves: the plain registrar still refuses the id
        while no spec has written its routing row."""
        unregister_custom_backend()
        with pytest.raises(ValueError, match="unverified"):
            backends.register_selectable_backend(ACP_BACKEND_CUSTOM)


class TestCoercion:
    def test_absent_and_empty_collapse_to_none(self):
        assert coerce_custom_acp_spec(None) is None
        assert coerce_custom_acp_spec({}) is None
        assert coerce_custom_acp_spec("my-acp") is None

    def test_hand_edited_shapes_are_tolerated(self):
        spec = coerce_custom_acp_spec(
            {"command": "my-acp", "args": "serve", "gate_option": "mode", "gate_value": 3}
        )
        assert spec == CustomAcpSpec("my-acp", ("serve",), "mode", "")
        assert spec.problems() == [
            "gate_value is empty: name the value of that option which makes it ask"
        ]

    def test_non_string_args_are_kept_and_refused_by_name(self, clean_registry):
        """``["--port", 8080]`` in a hand-edited file is neither trimmed to
        ``("--port",)`` nor coined into ``"8080"``: both would spawn an argv the
        operator did not write. The entry rides through as written so the
        registration refuses it and quotes the offender."""
        spec = coerce_custom_acp_spec({**COMPLETE_RAW, "args": ["--port", 8080, None]})
        assert spec.args == ("--port", 8080, None)
        with pytest.raises(ValueError, match=r"args must all be strings.*8080.*None"):
            register_custom_backend(spec)
        assert ACP_BACKEND_CUSTOM not in selectable_backends()


# ── The config load path ──


class TestConfigLoad:
    def test_a_complete_block_makes_a_persisted_custom_survive_the_load(self, clean_registry):
        cfg = _build_agent_config({"acp_backend": "custom", "custom_acp": COMPLETE_RAW})
        assert cfg.acp_backend == ACP_BACKEND_CUSTOM
        assert cfg.custom_acp == COMPLETE_RAW
        assert routing_for(ACP_BACKEND_CUSTOM) is Routing.SESSION_CONFIG

    def test_a_blanked_block_takes_the_id_off_the_switch(self, clean_registry, caplog):
        """The persisted ``"custom"`` then degrades through the one gate every unusable
        value takes, with its reason logged, rather than crashing the load."""
        _build_agent_config({"acp_backend": "custom", "custom_acp": COMPLETE_RAW})
        cfg = _build_agent_config({"acp_backend": "custom", "custom_acp": {}})
        assert cfg.acp_backend == ACP_BACKEND_KIRO
        assert cfg.custom_acp == {}
        assert ACP_BACKEND_CUSTOM not in selectable_backends()

    def test_an_incomplete_block_is_logged_and_the_load_proceeds(self, clean_registry, caplog):
        with caplog.at_level("WARNING"):
            cfg = _build_agent_config(
                {"acp_backend": "custom", "custom_acp": {"command": "my-acp"}}
            )
        assert cfg.acp_backend == ACP_BACKEND_KIRO
        assert cfg.custom_acp == {
            "command": "my-acp",
            "args": [],
            "gate_option": "",
            "gate_value": "",
        }
        assert any("gate_option is empty" in r.getMessage() for r in caplog.records)

    def test_the_whole_load_registers_from_the_file(self, clean_registry, tmp_path, monkeypatch):
        """Through ``KiroCrewConfig.load`` itself, so the schema pass, the section coercion
        and the registration are all exercised on the real path -- and ``args`` as an
        array does not trip the schema."""
        path = tmp_path / "config.json"
        path.write_text(
            json.dumps({"agent": {"acp_backend": "custom", "custom_acp": COMPLETE_RAW}}),
            encoding="utf-8",
        )
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: path)
        cfg = KiroCrewConfig.load()
        assert cfg.agent.acp_backend == ACP_BACKEND_CUSTOM
        assert cfg.agent.custom_acp == COMPLETE_RAW
        assert ACP_BACKEND_LAUNCH[ACP_BACKEND_CUSTOM].binary == "my-acp"


# ── The spawn arm ──


def _client(tmp_path) -> AcpClient:
    return AcpClient(
        work_dir=tmp_path / "workspace",
        session_key="custom-session",
        acp_backend=ACP_BACKEND_CUSTOM,
        model="auto",
    )


class TestResolveLaunch:
    def test_the_configured_command_and_args_are_the_argv(self, clean_registry, tmp_path):
        register_custom_backend(COMPLETE)
        client_mod._self_served_bin_caches.pop(ACP_BACKEND_CUSTOM, None)
        client_mod._self_served_bin_cache_records.pop(ACP_BACKEND_CUSTOM, None)
        with patch.object(
            client_mod, "_resolve_self_served_bin", return_value=("/opt/bin/my-acp", "/opt/bin")
        ):
            binary, argv, spawn_label, stderr_label = asyncio.run(
                _client(tmp_path)._resolve_self_served_launch()
            )
        assert binary == "/opt/bin/my-acp"
        assert argv == ["/opt/bin/my-acp", "serve", "--stdio"]
        # The argv carries the args; the label the spawn is LOGGED under does not.
        assert spawn_label == "my-acp [2 operator-supplied args]"
        assert stderr_label == "my-acp"

    def test_a_changed_command_is_resolved_afresh(self, clean_registry, tmp_path):
        """The per-process cache is keyed to the launch record that produced it, so an
        operator who changes the command gets the new one on the next spawn -- not the
        old path until a restart, which is what the shared cache would otherwise do."""
        register_custom_backend(COMPLETE)
        client_mod._self_served_bin_caches.pop(ACP_BACKEND_CUSTOM, None)
        client_mod._self_served_bin_cache_records.pop(ACP_BACKEND_CUSTOM, None)
        answers = {"my-acp": "/opt/bin/my-acp", "other-acp": "/usr/local/bin/other-acp"}

        def _resolve(backend, launch=None):
            return answers[ACP_BACKEND_LAUNCH[backend].binary], "/opt/bin"

        with patch.object(client_mod, "_resolve_self_served_bin", side_effect=_resolve) as resolver:
            first = asyncio.run(_client(tmp_path)._resolve_self_served_launch())
            # Same record: the cache answers and the resolver is not consulted again.
            again = asyncio.run(_client(tmp_path)._resolve_self_served_launch())
            assert again == first
            assert resolver.call_count == 1
            register_custom_backend(CustomAcpSpec("other-acp", ("--acp",), "mode", "read-only"))
            changed = asyncio.run(_client(tmp_path)._resolve_self_served_launch())
        assert first[1] == ["/opt/bin/my-acp", "serve", "--stdio"]
        assert changed[1] == ["/usr/local/bin/other-acp", "--acp"]
        assert resolver.call_count == 2

    def test_a_reload_during_resolution_cannot_mix_two_records(self, clean_registry, tmp_path):
        """The spawn captures ONE launch record and resolves THAT record. A config
        reload that rewrites the custom row while the resolver is off-loop must not
        hand the spawn the new command's binary paired with the old command's args --
        so the resolver is given the captured record and does not re-read the table."""
        register_custom_backend(COMPLETE)
        client_mod._self_served_bin_caches.pop(ACP_BACKEND_CUSTOM, None)
        client_mod._self_served_bin_cache_records.pop(ACP_BACKEND_CUSTOM, None)
        seen: list = []

        def _resolve(backend, launch=None):
            # Simulates the reload landing mid-resolve: by the time the worker runs,
            # the table already holds the OTHER record.
            register_custom_backend(CustomAcpSpec("other-acp", ("--acp",), "mode", "read-only"))
            seen.append((launch, ACP_BACKEND_LAUNCH[backend]))
            return f"/opt/bin/{launch.binary}", "/opt/bin"

        with patch.object(client_mod, "_resolve_self_served_bin", side_effect=_resolve):
            binary, argv, _spawn_label, _stderr_label = asyncio.run(
                _client(tmp_path)._resolve_self_served_launch()
            )
        captured, table_now = seen[0]
        assert captured.binary == "my-acp" and table_now.binary == "other-acp"
        # The spawn is the OLD record whole: its binary AND its args.
        assert binary == "/opt/bin/my-acp"
        assert argv == ["/opt/bin/my-acp", "serve", "--stdio"]

    def test_the_resolver_reads_the_record_it_is_handed(self, clean_registry, monkeypatch):
        """And the unpatched resolver honours that argument: handed a record, it
        resolves that record's binary and override variable, not the table's."""
        register_custom_backend(COMPLETE)
        captured = ACP_BACKEND_LAUNCH[ACP_BACKEND_CUSTOM]
        register_custom_backend(CustomAcpSpec("other-acp", ("--acp",), "mode", "read-only"))
        monkeypatch.setattr(client_mod.shutil, "which", lambda name, path=None: f"/found/{name}")
        monkeypatch.setattr(client_mod, "_mise_which", lambda name: None)
        monkeypatch.delenv(captured.bin_env_var, raising=False)
        # The resolver hands back an absolute, OS-normalized path (``C:\found\...``
        # on Windows), so compare the POSIX rendering's tail, not the literal.
        resolved, _searched = client_mod._resolve_self_served_bin(ACP_BACKEND_CUSTOM, captured)
        assert resolved is not None and Path(resolved).as_posix().endswith("/found/my-acp")
        resolved_now, _searched = client_mod._resolve_self_served_bin(ACP_BACKEND_CUSTOM)
        assert resolved_now is not None and Path(resolved_now).as_posix().endswith(
            "/found/other-acp"
        )

    def test_an_absent_command_names_the_configured_one(self, clean_registry, tmp_path):
        register_custom_backend(COMPLETE)
        client_mod._self_served_bin_caches.pop(ACP_BACKEND_CUSTOM, None)
        client_mod._self_served_bin_cache_records.pop(ACP_BACKEND_CUSTOM, None)
        with patch.object(client_mod, "_resolve_self_served_bin", return_value=(None, "/opt/bin")):
            with pytest.raises(client_mod.AcpError) as excinfo:
                asyncio.run(_client(tmp_path)._resolve_self_served_launch())
        message = str(excinfo.value)
        assert message.startswith("my-acp not found")
        assert "agent.custom_acp.command" in message
        # No override variable exists for this harness: ``command`` is the operator's
        # own path, so the remedy must not send them to a second place to name it.
        assert "_BIN" not in message
        assert "or set" not in message

    def test_a_record_withdrawn_mid_start_is_a_named_refusal(self, clean_registry, tmp_path):
        """Found live: a config reload between two spawn attempts unregistered the
        harness, and the retry surfaced a bare ``KeyError('custom')``."""
        register_custom_backend(COMPLETE)
        client = _client(tmp_path)
        unregister_custom_backend()
        with pytest.raises(client_mod.AcpError) as excinfo:
            asyncio.run(client._resolve_self_served_launch())
        message = str(excinfo.value)
        assert "is not configured" in message
        assert "agent.custom_acp" in message


class TestSpawnArm:
    def test_the_arm_runs_the_preflight_and_spawns_the_configured_command(
        self, clean_registry, tmp_path
    ):
        """The arm is the opencode arm minus its seed and read-back: resolve, preflight,
        spawn. Pinned through the same recorder the launch golden uses, so the argv is
        the one handed to the process factory rather than an intermediate."""
        import acp_launch_capture as capture_mod

        unregister_custom_backend()
        launched = capture_mod.capture(ACP_BACKEND_CUSTOM, tmp_path)
        assert launched["argv"] == ["/opt/bin/custom-acp", "--acp"]
        assert launched["spawn_label"] == "custom-acp [1 operator-supplied arg]"
        assert launched["stderr_label"] == "custom-acp"
        # The capture restores the registry, so the arm left nothing registered.
        assert custom_backend_spec() is None

    # ── One registration, both halves ──
    #
    # The arm resolves the launch record AND arms the gate pair. Read separately
    # from the tables, with an await between them, a reload landing mid-resolve
    # pairs one registration's command with another's option. Both therefore come
    # from ONE read of the frozen spec object, before any await.

    def _arm_recording_the_resolver(self, tmp_path, monkeypatch, during_resolve=None):
        client = _client(tmp_path)
        handed: list = []

        async def _resolve(launch=None):
            handed.append(launch)
            if during_resolve is not None:
                during_resolve()
            raise client_mod.AcpError("stop here: the arm has done its reads")

        monkeypatch.setattr(client, "_resolve_self_served_launch", _resolve)
        with pytest.raises(client_mod.AcpError, match="stop here"):
            asyncio.run(client._spawn())
        return client, handed

    def test_the_arm_reads_launch_and_gate_from_one_spec_object(
        self, clean_registry, tmp_path, monkeypatch
    ):
        """The tables hold one registration and the spec reader answers another: a
        half read from the tables would show the table's values. Both halves show
        the spec's, so neither is read from a table."""
        other = CustomAcpSpec(
            command="other-acp", args=("--acp",), gate_option="approval", gate_value="ask"
        )
        register_custom_backend(other)
        monkeypatch.setattr(client_mod, "custom_backend_spec", lambda: COMPLETE)
        client, handed = self._arm_recording_the_resolver(tmp_path, monkeypatch)
        assert handed == [COMPLETE.launch()]
        assert client._custom_gate_snapshot == ("mode", "read-only")

    def test_a_reload_during_resolution_leaves_the_snapshot_as_captured(
        self, clean_registry, tmp_path, monkeypatch
    ):
        """The gate pair is copied BEFORE the resolver's await, so a registration that
        changes while the resolver runs cannot reach it."""
        register_custom_backend(COMPLETE)
        swapped = CustomAcpSpec(
            command="my-acp", args=("serve", "--stdio"), gate_option="approval", gate_value="ask"
        )
        client, handed = self._arm_recording_the_resolver(
            tmp_path, monkeypatch, during_resolve=lambda: register_custom_backend(swapped)
        )
        assert handed == [COMPLETE.launch()]
        assert client._custom_gate_snapshot == ("mode", "read-only")
        assert gate.permission_config_for(ACP_BACKEND_CUSTOM) == ("approval", "ask")

    def test_a_registration_withdrawn_before_the_arm_is_a_named_refusal(
        self, clean_registry, tmp_path, monkeypatch
    ):
        unregister_custom_backend()
        client = _client(tmp_path)
        with pytest.raises(client_mod.AcpError) as excinfo:
            asyncio.run(client._spawn())
        message = str(excinfo.value)
        assert "withdrawn while starting" in message
        assert "agent.custom_acp" in message
        assert client._custom_gate_snapshot is None


# ── The gate ──


class TestSessionConfigGate:
    def _client_with_options(self, tmp_path, options):
        client = _client(tmp_path)
        client._acp_config_options = options
        return client

    def test_a_harness_that_advertises_the_option_is_armed(self, clean_registry, tmp_path):
        register_custom_backend(COMPLETE)
        client = self._client_with_options(
            tmp_path,
            [{"id": "mode", "options": [{"value": "agent"}, {"value": "read-only"}]}],
        )
        with patch.object(AcpClient, "set_config_option", new=AsyncMock()) as setter:
            asyncio.run(client._apply_session_permission_routing())
        setter.assert_awaited_once_with("mode", "read-only")

    def test_a_harness_that_does_not_advertise_the_option_is_refused(
        self, clean_registry, tmp_path
    ):
        """The product framing in one assertion: Custom means any harness that can be
        gated through a config option. One that cannot fails to start, with the option
        named, instead of running ungated."""
        register_custom_backend(COMPLETE)
        client = self._client_with_options(tmp_path, [{"id": "model", "options": [{"value": "x"}]}])
        with patch.object(AcpClient, "set_config_option", new=AsyncMock()) as setter:
            with pytest.raises(AcpToolGateUnroutable) as excinfo:
                asyncio.run(client._apply_session_permission_routing())
        setter.assert_not_awaited()
        message = str(excinfo.value)
        assert "'mode'" in message
        assert CUSTOM_ACP_LABEL in message

    def test_a_rejected_write_is_a_bypass_not_an_unknown(self, clean_registry, tmp_path):
        register_custom_backend(COMPLETE)
        client = self._client_with_options(
            tmp_path, [{"id": "mode", "options": [{"value": "read-only"}]}]
        )
        with patch.object(
            AcpClient,
            "set_config_option",
            new=AsyncMock(side_effect=client_mod.AcpError("rejected")),
        ):
            with pytest.raises(AcpToolGateUnroutable, match="rejected its required"):
                asyncio.run(client._apply_session_permission_routing())

    def test_the_remediation_names_the_configured_option(self, clean_registry):
        register_custom_backend(COMPLETE)
        remedy = gate.remediation_for(ACP_BACKEND_CUSTOM)
        assert "'mode'" in remedy and "'read-only'" in remedy
        assert CUSTOM_ACP_LABEL in remedy
        # There is no adapter to install for a harness the operator named; the
        # remedy is to the spec.
        assert "gate_option" in remedy
        assert "Install" not in remedy

    def test_the_verdict_names_the_attestation(self, clean_registry):
        """Same mechanism as codex, different provenance -- and the verdict says which
        half Kiro Crew verified and which half is the operator's word."""
        register_custom_backend(COMPLETE)
        verdict, reason = gate.routing_verdict(ACP_BACKEND_CUSTOM)
        assert verdict is gate.Verdict.ROUTED
        assert "mode=read-only" in reason
        assert "attestation" in reason
        assert "not something Kiro Crew verified" in reason

    # ── The spawn-time snapshot ──
    #
    # A config reload can unregister the harness between spawn and ``session/new``.
    # The tables then say "no routing declared", and every table-keyed check --
    # ``session_config_issue`` returns "", ``is_enforced`` returns False -- would let
    # ``_initialize_session`` skip the arm for a process that is already running. The
    # arm therefore runs from the pair the spawn arm copied, and these pin that the
    # copy answers every question without the tables.

    def test_the_snapshot_arms_after_the_registration_is_withdrawn(self, clean_registry, tmp_path):
        register_custom_backend(COMPLETE)
        client = self._client_with_options(
            tmp_path, [{"id": "mode", "options": [{"value": "read-only"}]}]
        )
        snapshot = gate.permission_config_for(ACP_BACKEND_CUSTOM)
        unregister_custom_backend()
        # The hazard, stated: with the row gone the table path sees nothing to arm.
        assert gate.session_config_issue(ACP_BACKEND_CUSTOM, client._acp_config_options) == ""
        assert gate.is_enforced(ACP_BACKEND_CUSTOM) is False
        with patch.object(AcpClient, "set_config_option", new=AsyncMock()) as setter:
            asyncio.run(client._apply_session_permission_routing(gate=snapshot))
        setter.assert_awaited_once_with("mode", "read-only")

    def test_the_snapshot_refuses_an_unadvertised_option_after_withdrawal(
        self, clean_registry, tmp_path
    ):
        """Table-keyed enforcement would answer "not enforced" here and return; the
        snapshot path refuses, because this harness was registered as enforced and
        nothing else."""
        register_custom_backend(COMPLETE)
        client = self._client_with_options(tmp_path, [{"id": "model", "options": [{"value": "x"}]}])
        snapshot = gate.permission_config_for(ACP_BACKEND_CUSTOM)
        unregister_custom_backend()
        with patch.object(AcpClient, "set_config_option", new=AsyncMock()) as setter:
            with pytest.raises(AcpToolGateUnroutable) as excinfo:
                asyncio.run(client._apply_session_permission_routing(gate=snapshot))
        setter.assert_not_awaited()
        message = str(excinfo.value)
        assert "'mode'" in message and "gate_option" in message

    def test_the_snapshot_treats_a_rejected_write_as_a_refusal(self, clean_registry, tmp_path):
        register_custom_backend(COMPLETE)
        client = self._client_with_options(
            tmp_path, [{"id": "mode", "options": [{"value": "read-only"}]}]
        )
        snapshot = gate.permission_config_for(ACP_BACKEND_CUSTOM)
        unregister_custom_backend()
        with patch.object(
            AcpClient,
            "set_config_option",
            new=AsyncMock(side_effect=client_mod.AcpError("rejected")),
        ):
            with pytest.raises(AcpToolGateUnroutable, match="rejected its required"):
                asyncio.run(client._apply_session_permission_routing(gate=snapshot))

    def test_a_custom_client_starts_with_no_snapshot(self, clean_registry, tmp_path):
        """The copy is the spawn arm's to take; a client that never spawned holds none,
        and ``_initialize_session`` refuses on that rather than reading the table."""
        register_custom_backend(COMPLETE)
        assert _client(tmp_path)._custom_gate_snapshot is None

    def test_config_option_issue_reads_no_table(self, clean_registry):
        unregister_custom_backend()
        advertised = [{"id": "mode", "options": [{"value": "read-only"}]}]
        assert gate.config_option_issue("mode", "read-only", advertised) == ""
        assert "'agent'" in gate.config_option_issue("mode", "agent", advertised)
        assert "did not advertise config option" in gate.config_option_issue(
            "other", "x", advertised
        )
        assert "names no config option" in gate.config_option_issue("", "", advertised)


# ── The in-band tripwire ──


def _update(tool_call_id, status="completed"):
    return client_mod.JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "s",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_call_id,
                "status": status,
            },
        },
    )


def _tool_call(tool_call_id, kind=None):
    update = {
        "sessionUpdate": "tool_call",
        "toolCallId": tool_call_id,
        "title": "t",
        "status": "pending",
    }
    if kind is not None:
        update["kind"] = kind
    return client_mod.JsonRpcMessage(
        method="session/update", params={"sessionId": "s", "update": update}
    )


def _permission(tool_call_id, request_id=7):
    return client_mod.JsonRpcMessage(
        id=request_id,
        method="session/request_permission",
        params={
            "sessionId": "s",
            "toolCall": {
                "toolCallId": tool_call_id,
                "title": "bash",
                "kind": "execute",
                "status": "pending",
            },
            "options": [
                {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
            ],
        },
    )


class TestGateTripwire:
    """The operator ATTESTED that the option makes the harness ask; this is where
    the attestation is observed. A completed call no permission frame asked about
    -- other than one the harness itself declared passive -- ends the session with
    the option named, and so does a completed call the host denied."""

    def _armed(self, tmp_path, monkeypatch):
        register_custom_backend(COMPLETE)
        client = _client(tmp_path)
        killed: list = []

        async def _kill(*, force=False):
            killed.append(force)

        async def _send(request_id, payload):
            pass

        monkeypatch.setattr(client, "_kill_process", _kill)
        monkeypatch.setattr(client, "_send_response", _send)
        return client, killed

    def test_an_unasked_completed_call_kills_the_harness_and_names_the_option(
        self, clean_registry, tmp_path, monkeypatch
    ):
        client, killed = self._armed(tmp_path, monkeypatch)
        client._extract_tool_event(_tool_call("c1", kind="execute"))
        with pytest.raises(AcpToolGateUnroutable) as excinfo:
            asyncio.run(client._tripwire_custom_gate(_update("c1")))
        assert killed == [True]
        message = str(excinfo.value)
        assert "without asking" in message
        assert "mode=read-only" in message
        assert "gate_option" in message

    def test_an_undeclared_kind_is_held_to_asking(self, clean_registry, tmp_path, monkeypatch):
        """Unknown reads as fail-closed: a call that never said what it was must ask."""
        client, killed = self._armed(tmp_path, monkeypatch)
        client._extract_tool_event(_tool_call("c2"))
        with pytest.raises(AcpToolGateUnroutable):
            asyncio.run(client._tripwire_custom_gate(_update("c2")))
        assert killed == [True]

    def test_an_asked_call_passes(self, clean_registry, tmp_path, monkeypatch):
        client, killed = self._armed(tmp_path, monkeypatch)
        client._extract_tool_event(_tool_call("c3", kind="execute"))
        client._build_permission_event(_permission("c3"))
        asyncio.run(client.approve_tool(7))
        asyncio.run(client._tripwire_custom_gate(_update("c3")))
        assert killed == []

    def test_the_auto_approve_path_also_records_the_asked_call(
        self, clean_registry, tmp_path, monkeypatch
    ):
        client, killed = self._armed(tmp_path, monkeypatch)
        approved: list = []

        async def _approve(request_id):
            approved.append(request_id)

        monkeypatch.setattr(client, "approve_tool", _approve)
        assert not client._spec_denied_tools
        client._extract_tool_event(_tool_call("c4", kind="edit"))
        asyncio.run(client._handle_permission(_permission("c4")))
        assert approved
        asyncio.run(client._tripwire_custom_gate(_update("c4")))
        assert killed == []

    @pytest.mark.parametrize("kind", ["read", "search", "think"])
    def test_a_passive_call_is_tolerated_like_every_session_config_harness(
        self, clean_registry, tmp_path, monkeypatch, kind
    ):
        """The line codex is held to, not a stricter one: passive reads bypass the gate
        on every SESSION_CONFIG harness and the OS credential mask compensates."""
        client, killed = self._armed(tmp_path, monkeypatch)
        client._extract_tool_event(_tool_call("c5", kind=kind))
        asyncio.run(client._tripwire_custom_gate(_update("c5")))
        assert killed == []

    def test_fetch_is_not_passive(self, clean_registry, tmp_path, monkeypatch):
        client, killed = self._armed(tmp_path, monkeypatch)
        client._extract_tool_event(_tool_call("c6", kind="fetch"))
        with pytest.raises(AcpToolGateUnroutable):
            asyncio.run(client._tripwire_custom_gate(_update("c6")))

    def test_a_later_stricter_kind_wins(self, clean_registry, tmp_path, monkeypatch):
        """A harness cannot declare a call passive and then refine it into an action."""
        client, killed = self._armed(tmp_path, monkeypatch)
        client._extract_tool_event(_tool_call("c7", kind="read"))
        refinement = client_mod.JsonRpcMessage(
            method="session/update",
            params={
                "sessionId": "s",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "c7",
                    "kind": "execute",
                },
            },
        )
        client._extract_tool_call_refinement(refinement)
        with pytest.raises(AcpToolGateUnroutable):
            asyncio.run(client._tripwire_custom_gate(_update("c7")))

    def test_a_denied_call_that_completes_kills_the_harness(
        self, clean_registry, tmp_path, monkeypatch
    ):
        client, killed = self._armed(tmp_path, monkeypatch)
        client._extract_tool_event(_tool_call("c8", kind="execute"))
        client._build_permission_event(_permission("c8"))
        asyncio.run(client.reject_tool(7))
        assert "c8" in client._custom_gate_denied_ids
        with pytest.raises(AcpToolGateUnroutable) as excinfo:
            asyncio.run(client._tripwire_custom_gate(_update("c8")))
        assert killed == [True]
        assert "DENIED" in str(excinfo.value)

    def test_a_denied_call_that_fails_is_the_expected_outcome(
        self, clean_registry, tmp_path, monkeypatch
    ):
        client, killed = self._armed(tmp_path, monkeypatch)
        client._build_permission_event(_permission("c9"))
        asyncio.run(client.reject_tool(7))
        asyncio.run(client._tripwire_custom_gate(_update("c9", status="failed")))
        assert killed == []

    def test_it_is_a_no_op_on_every_other_backend(self, clean_registry, tmp_path, monkeypatch):
        client = AcpClient(work_dir=tmp_path / "w", session_key="k", acp_backend=ACP_BACKEND_KIRO)
        killed: list = []

        async def _kill(*, force=False):
            killed.append(force)

        monkeypatch.setattr(client, "_kill_process", _kill)
        client._extract_tool_event(_tool_call("k1", kind="execute"))
        asyncio.run(client._tripwire_custom_gate(_update("k1")))
        assert killed == []
        assert not client._custom_gate_passive_ids

    def test_it_is_wired_at_every_reader_the_pi_tripwire_is(self):
        """The three dispatch sites that answer permission frames each run it."""
        import inspect

        source = inspect.getsource(client_mod.AcpClient)
        assert source.count("await self._tripwire_pi_gate(msg)") == source.count(
            "await self._tripwire_custom_gate(msg)"
        )
        assert source.count("await self._tripwire_custom_gate(msg)") >= 3

    @pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
    def test_a_terminal_status_releases_the_id_from_every_tracking_set(
        self, clean_registry, tmp_path, monkeypatch, status
    ):
        """The sets hold only calls still in flight: a long session cannot grow them by
        one entry per call the harness ever made (the bound the review asked for)."""
        client, killed = self._armed(tmp_path, monkeypatch)
        client._extract_tool_event(_tool_call("t1", kind="execute"))
        client._build_permission_event(_permission("t1"))
        asyncio.run(client.approve_tool(7))
        client._extract_tool_event(_tool_call("t2", kind="read"))
        client._build_permission_event(_permission("t3", request_id=8))
        asyncio.run(client.reject_tool(8))
        assert "t1" in client._custom_gate_asked_ids
        assert "t2" in client._custom_gate_passive_ids
        assert "t3" in client._custom_gate_denied_ids
        for tool_call_id in ("t1", "t2"):
            asyncio.run(client._tripwire_custom_gate(_update(tool_call_id, status=status)))
        if status == "completed":
            with pytest.raises(AcpToolGateUnroutable):
                asyncio.run(client._tripwire_custom_gate(_update("t3", status=status)))
        else:
            asyncio.run(client._tripwire_custom_gate(_update("t3", status=status)))
            assert killed == []
        assert not client._custom_gate_asked_ids
        assert not client._custom_gate_passive_ids
        assert not client._custom_gate_denied_ids
        assert not client._custom_gate_request_tool

    def test_an_in_flight_status_keeps_the_id(self, clean_registry, tmp_path, monkeypatch):
        client, killed = self._armed(tmp_path, monkeypatch)
        client._extract_tool_event(_tool_call("p1", kind="execute"))
        client._build_permission_event(_permission("p1"))
        asyncio.run(client.approve_tool(7))
        asyncio.run(client._tripwire_custom_gate(_update("p1", status="in_progress")))
        assert "p1" in client._custom_gate_asked_ids
        assert killed == []

    # ── The in-flight bound ──
    #
    # A terminal status releases an id, so the collections hold the calls in flight.
    # A harness that opens calls it never finishes would grow them without limit;
    # ``_CUSTOM_GATE_MAX_INFLIGHT`` is the named bound and these pin that it is
    # enforced on both frames that add an id, and nowhere on another backend.

    def test_calls_open_up_to_the_bound_are_followed(self, clean_registry, tmp_path, monkeypatch):
        client, killed = self._armed(tmp_path, monkeypatch)
        bound = AcpClient._CUSTOM_GATE_MAX_INFLIGHT
        for i in range(bound):
            client._extract_tool_event(_tool_call(f"o{i}", kind="read"))
        asyncio.run(client._tripwire_custom_gate(_tool_call("x", kind="execute")))
        assert client._custom_gate_tracked() == bound
        assert killed == []

    def test_one_tool_call_past_the_bound_stops_the_harness(
        self, clean_registry, tmp_path, monkeypatch
    ):
        client, killed = self._armed(tmp_path, monkeypatch)
        bound = AcpClient._CUSTOM_GATE_MAX_INFLIGHT
        for i in range(bound + 1):
            client._extract_tool_event(_tool_call(f"o{i}", kind="read"))
        # The frame that carried the id over the bound is a ``tool_call``, not a
        # terminal update: the tripwire must check before its status filter.
        with pytest.raises(AcpToolGateUnroutable) as excinfo:
            asyncio.run(client._tripwire_custom_gate(_tool_call(f"o{bound}", kind="read")))
        assert killed == [True]
        message = str(excinfo.value)
        assert str(bound) in message and "open at once" in message

    def test_permission_frames_past_the_bound_stop_the_harness(
        self, clean_registry, tmp_path, monkeypatch
    ):
        """The other frame that adds an id. A harness asking about calls it never
        reports as ``tool_call`` frames grows ``asked`` and the request map alone,
        so the bound is checked on the permission path too, both readers."""
        client, killed = self._armed(tmp_path, monkeypatch)
        bound = AcpClient._CUSTOM_GATE_MAX_INFLIGHT
        for i in range(bound + 1):
            client._note_custom_gate_asked(_permission(f"q{i}", request_id=100 + i))
        assert client._custom_gate_tracked() > bound
        with pytest.raises(AcpToolGateUnroutable):
            asyncio.run(client._enforce_custom_gate_bound())
        assert killed == [True]
        import inspect

        source = inspect.getsource(client_mod.AcpClient)
        # Wired at both permission readers (the judging one and the auto-approve
        # one) and at the tripwire's entry, which the three ``session/update``
        # readers share.
        assert source.count("await self._enforce_custom_gate_bound()") == 3

    def test_the_bound_is_a_no_op_on_every_other_backend(self, clean_registry, tmp_path):
        client = AcpClient(
            work_dir=tmp_path / "workspace",
            session_key="codex-session",
            acp_backend=ACP_BACKEND_CODEX,
            model="auto",
        )
        client._custom_gate_asked_ids = {
            f"o{i}" for i in range(AcpClient._CUSTOM_GATE_MAX_INFLIGHT + 5)
        }
        asyncio.run(client._enforce_custom_gate_bound())


# ── The install probe ──


class TestInstallProbe:
    def test_unconfigured_reports_the_config_block_as_missing(self, clean_registry):
        unregister_custom_backend()
        state = backend_install._probe_custom()
        assert state.installed == backend_install.MISSING
        assert state.missing_components == (backend_install.COMPONENT_CUSTOM_ACP_SPEC,)
        assert "agent.custom_acp" in state.install_command
        assert state.policy_id == "custom"

    def test_configured_probes_the_configured_command(self, clean_registry):
        register_custom_backend(COMPLETE)
        with patch.object(client_mod, "_resolve_self_served_bin", return_value=(None, "/opt/bin")):
            absent = backend_install._probe_custom()
        assert absent.installed == backend_install.MISSING
        assert absent.missing_components == ("my-acp",)
        assert "my-acp" in absent.install_command
        with patch.object(
            client_mod, "_resolve_self_served_bin", return_value=("/opt/bin/my-acp", "/opt/bin")
        ):
            present = backend_install._probe_custom()
        assert present.installed == backend_install.INSTALLED

    def test_the_probe_table_binds_it(self):
        assert backend_install._PROBES[ACP_BACKEND_CUSTOM] is backend_install._probe_custom


# ── The dashboard write path ──

_BASE_CONFIG = {
    "agents": {"kirocrew": {"kiro_agent": "kirocrew"}},
    "default_agent": "kirocrew",
}


def _app(owner: bool) -> web.Application:
    from dashboard_owner_helpers import as_owner

    from kiro_crew.dashboard.handlers import api_kirocrew_config_patch

    app = web.Application()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    if owner:
        as_owner(app)
    else:
        from dashboard_owner_helpers import NoConfiguredOwner

        app["state"] = NoConfiguredOwner()
    return app


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_BASE_CONFIG), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: path)
    return path


async def _patch(client, value, headers=None):
    return await client.patch(
        "/api/config/kirocrew",
        json={"path": "agent.custom_acp", "value": value},
        headers=headers or {},
    )


class TestDashboardWrite:
    @pytest.mark.asyncio
    async def test_the_owner_writes_the_record_and_the_load_registers_it(
        self, clean_registry, config_file
    ):
        unregister_custom_backend()
        async with TestClient(TestServer(_app(owner=True))) as client:
            resp = await _patch(client, COMPLETE_RAW)
            assert resp.status == 200, await resp.text()
        stored = json.loads(config_file.read_text(encoding="utf-8"))["agent"]["custom_acp"]
        assert stored == COMPLETE_RAW
        # The handler reloads after the write, and the load is what registers.
        assert ACP_BACKEND_CUSTOM in selectable_backends()
        assert routing_for(ACP_BACKEND_CUSTOM) is Routing.SESSION_CONFIG

    @pytest.mark.asyncio
    async def test_the_audit_records_the_path_and_shape_never_the_values(
        self, clean_registry, config_file, monkeypatch
    ):
        """``args`` is whatever the operator's harness takes on its command line -- a
        token included -- and the SEL is readable back through the API. So the record
        is audited by path and shape only, on success and on refusal alike."""
        from unittest.mock import MagicMock

        import kiro_crew.dashboard.handlers as handlers_pkg

        sel = MagicMock()
        monkeypatch.setattr(handlers_pkg, "sel", lambda: sel)
        secret = "tok-SECRET-9f8e7d"
        record = dict(COMPLETE_RAW, args=["serve", f"--token={secret}"])
        unregister_custom_backend()
        async with TestClient(TestServer(_app(owner=True))) as client:
            assert (await _patch(client, record)).status == 200
            # A refused shape is still an attempted write of the same values.
            bad = dict(record, extra=1)
            assert (await _patch(client, bad)).status == 400
        calls = sel.log_api_access.call_args_list
        assert calls, "the PATCH audits every outcome"
        for call in calls:
            resources = call.kwargs["resources"]
            assert resources.startswith("agent.custom_acp=<record")
            assert secret not in resources and "my-acp" not in resources
        assert any(call.kwargs["outcome"] == "success" for call in calls)
        assert any(call.kwargs["outcome"] == "denied" for call in calls)
        success = [c for c in calls if c.kwargs["outcome"] == "success"][0]
        assert "2 arg(s)" in success.kwargs["resources"]

    @pytest.mark.asyncio
    async def test_a_non_owner_is_refused(self, clean_registry, config_file):
        """The record names a program the gateway will execute; a dashboard token alone
        does not imply ownership."""
        async with TestClient(TestServer(_app(owner=True))) as client:
            resp = await _patch(client, COMPLETE_RAW, headers={"X-Test-User": "someone-else"})
            assert resp.status == 403
        assert "custom_acp" not in json.loads(config_file.read_text(encoding="utf-8")).get(
            "agent", {}
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "value",
        [
            {"command": "x", "args": "serve", "gate_option": "m", "gate_value": "v"},
            {"command": "x", "args": [1], "gate_option": "m", "gate_value": "v"},
            {"command": "x", "args": [], "gate_option": "m"},
            {"command": "x", "args": [], "gate_option": "m", "gate_value": "v", "extra": 1},
            {"command": 5, "args": [], "gate_option": "m", "gate_value": "v"},
            "my-acp",
        ],
    )
    async def test_a_malformed_record_is_refused(self, clean_registry, config_file, value):
        async with TestClient(TestServer(_app(owner=True))) as client:
            resp = await _patch(client, value)
            assert resp.status == 400, await resp.text()

    @pytest.mark.asyncio
    async def test_clearing_the_record_unregisters(self, clean_registry, config_file):
        async with TestClient(TestServer(_app(owner=True))) as client:
            assert (await _patch(client, COMPLETE_RAW)).status == 200
            assert ACP_BACKEND_CUSTOM in selectable_backends()
            cleared = {"command": "", "args": [], "gate_option": "", "gate_value": ""}
            assert (await _patch(client, cleared)).status == 200
        assert ACP_BACKEND_CUSTOM not in selectable_backends()


# ── What the operator is shown ──


class TestCard:
    def test_the_card_reports_routing_and_no_tools(self, clean_registry):
        from kiro_crew.agent_sdk import backend_cards

        unregister_custom_backend()
        unconfigured = backend_cards.card_for(ACP_BACKEND_CUSTOM)
        assert unconfigured.tool_approval == Routing.UNVERIFIED.value
        # Offered by the build even while unregistered: the row is the operator's to
        # complete, and the panel would otherwise hide it as a policy denial.
        assert unconfigured.offered_by_build is True
        payload = backend_cards.card_payload(ACP_BACKEND_CUSTOM)
        assert payload["configurable"] is True
        register_custom_backend(COMPLETE)
        configured = backend_cards.card_for(ACP_BACKEND_CUSTOM)
        assert configured.tool_approval == Routing.SESSION_CONFIG.value
