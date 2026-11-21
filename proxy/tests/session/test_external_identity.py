"""External identity rules (core/session/external_identity.py)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

import config
from core.session import external_identity as ei

SID = "11111111-2222-4333-8444-555555555555"


class TestNormalize:
    @pytest.mark.parametrize("raw, expected", [
        ("+302101234567", "+302101234567"),
        ("+30 210 123-4567", "+302101234567"),
        ("(210) 123.4567", "2101234567"),
        ("2101234567", "2101234567"),
        ("1001", "1001"),                    # an extension
        ("", ""),
        ("anonymous", ""),
        ("Restricted", ""),
        ("unknown", ""),
        ("abc+302101234567", ""),            # letters → withheld, never stripped
        ("+30210123456789012345678", ""),    # too long
        ("1", ""),                           # too short
        ("+", ""),
    ])
    def test_phone(self, raw, expected):
        assert ei.normalize_id(ei.PHONE, raw) == expected

    def test_slug_drops_the_plus(self):
        assert ei.slug_for("+302101234567") == "302101234567"
        assert ei.slug_for("2101234567") == "2101234567"


class TestResolve:
    def test_durable_caller(self):
        ident = ei.resolve(ei.PHONE, "+30 210 1234567", session_id=SID)
        assert ident.id == "+302101234567"
        assert ident.slug == "302101234567"
        assert ident.ephemeral is False
        assert ident.has_tree
        assert ident.claim == "phone:+302101234567"
        assert ident.label == "caller:+302101234567"
        assert ei.resolve(ei.PHONE, "+302101234567", session_id=SID, verified=True).label == \
            "caller-pin:+302101234567"

    def test_withheld_is_ephemeral(self):
        ident = ei.resolve(ei.PHONE, "anonymous", session_id=SID)
        assert ident.id == ""
        assert ident.ephemeral is True
        assert ident.slug == f"{ei.EPHEMERAL_DIRNAME}/{SID}"
        assert ident.claim == f"phone:ephemeral:{SID}"
        assert ident.label == "ephemeral"

    def test_remember_off_keeps_the_id_but_ephemeral_tree(self):
        ident = ei.resolve(ei.PHONE, "+302101234567", session_id=SID, remember=False)
        assert ident.id == "+302101234567"      # the prompt may still say who called
        assert ident.ephemeral is True
        assert ident.claim == f"phone:ephemeral:{SID}"

    def test_shared_has_no_tree(self):
        ident = ei.resolve(ei.PHONE, "+302101234567", session_id=SID, shared=True)
        assert ident.id == "" and ident.slug == "" and not ident.has_tree
        assert ident.claim == "phone:"
        assert ident.label == "shared"

    def test_session_id_must_be_a_uuid_for_ephemeral_trees(self):
        with pytest.raises(ValueError):
            ei.resolve(ei.PHONE, "", session_id="../../etc")
        # A durable caller never uses the session id as a path component.
        ei.resolve(ei.PHONE, "+302101234567", session_id="not-a-uuid")

    def test_channel_is_validated(self):
        with pytest.raises(ValueError):
            ei.resolve("Phone/../x", "+302101234567", session_id=SID)


class TestHomes:
    def test_home_and_claim_are_inverse(self, tmp_path):
        agent_dir = tmp_path / "agent"
        for raw in ("+302101234567", "anonymous"):
            ident = ei.resolve(ei.PHONE, raw, session_id=SID)
            home = ei.external_home(agent_dir, ident)
            assert home == ei.home_from_claim(agent_dir, ident.claim)
            assert home.is_relative_to(agent_dir / ei.EXTERNALS_DIRNAME / ei.PHONE)
        assert ei.external_home(agent_dir, ei.resolve(ei.PHONE, "x", session_id=SID, shared=True)) is None
        assert ei.home_from_claim(agent_dir, "phone:") is None

    @pytest.mark.parametrize("claim", [
        "", "phone", "phone:abc", "phone:ephemeral:nope", "ph one:+30210", "phone:../x",
    ])
    def test_malformed_claims_are_refused(self, tmp_path, claim):
        with pytest.raises(ValueError):
            ei.home_from_claim(tmp_path, claim)

    def test_parse_claim(self):
        assert ei.parse_claim("phone:+302101234567") == ("phone", "+302101234567", "")
        assert ei.parse_claim("phone:") == ("phone", "", "")
        assert ei.parse_claim(f"phone:ephemeral:{SID}") == ("phone", "", SID)

    def test_prune_only_ephemeral_trees(self):
        agent_dir = config.get_agent_dir("prune-agent")
        durable = ei.external_home(agent_dir, ei.resolve(ei.PHONE, "+302101234567", session_id=SID))
        ephemeral = ei.external_home(agent_dir, ei.resolve(ei.PHONE, "", session_id=SID))
        for p in (durable, ephemeral):
            (p / "context").mkdir(parents=True)
        assert ei.prune_ephemeral(durable) is False and durable.is_dir()
        assert ei.prune_ephemeral(ephemeral) is True and not ephemeral.exists()
        assert ei.prune_ephemeral(None) is False
        assert ei.prune_ephemeral(Path(str(uuid.uuid4()))) is False


class TestTheScratchFolders:
    """The scratch homes and the ephemeral trees, made and removed beneath the
    agent's real folder with no link followed at any part. A stand-in folder
    beside the agents folder holds what a link would reach."""

    AGENT = "scratch-agent"

    @staticmethod
    def _outside(tmp_path: Path, name: str) -> Path:
        out = tmp_path / "outside"
        (out / name).mkdir(parents=True, exist_ok=True)
        (out / name / "keep.md").write_text("kept")
        return out

    def _ephemeral_as_link(self, tmp_path: Path) -> tuple[Path, Path]:
        """``externals/phone/_ephemeral`` a link to the stand-in folder."""
        out = self._outside(tmp_path, SID)
        base = config.get_agent_dir(self.AGENT) / ei.EXTERNALS_DIRNAME / ei.PHONE
        base.mkdir(parents=True)
        (base / ei.EPHEMERAL_DIRNAME).symlink_to(out, target_is_directory=True)
        return base / ei.EPHEMERAL_DIRNAME, out

    def test_a_prune_never_follows_a_link_above_the_tree(self, tmp_path):
        eph, out = self._ephemeral_as_link(tmp_path)
        assert ei.prune_ephemeral(eph / SID) is False
        assert (out / SID / "keep.md").read_text() == "kept"

    def test_a_sessions_scratch_is_never_listed_through_a_link(self, tmp_path):
        _eph, out = self._ephemeral_as_link(tmp_path)
        ei.prune_session_scratch(config.get_agent_dir(self.AGENT), SID)
        assert (out / SID / "keep.md").read_text() == "kept"

    def test_a_scratch_home_is_never_made_through_a_link(self, tmp_path):
        _eph, out = self._ephemeral_as_link(tmp_path)
        other = "22222222-3333-4444-8555-666666666666"
        with pytest.raises(OSError):
            ei.scratch_home(config.get_agent_dir(self.AGENT), ei.PHONE, other)
        assert sorted(p.name for p in out.iterdir()) == [SID]

    def test_a_prune_takes_only_the_ephemeral_shape(self):
        agent_dir = config.get_agent_dir(self.AGENT)
        odd = agent_dir / "workspace" / ei.EPHEMERAL_DIRNAME / SID
        (odd / "context").mkdir(parents=True)
        assert ei.prune_ephemeral(odd) is False and odd.is_dir()
        assert ei.is_ephemeral_home(odd) is False
        home = ei.scratch_home(agent_dir, ei.PHONE, SID)
        assert ei.is_ephemeral_home(home) is True
        assert ei.prune_ephemeral(home) is True and not home.exists()

    def test_the_scratch_homes_of_one_session_go_and_others_stay(self):
        agent_dir = config.get_agent_dir(self.AGENT)
        agent_dir.mkdir(parents=True)
        mine = ei.scratch_home(agent_dir, ei.PHONE, SID)
        other = ei.scratch_home(agent_dir, ei.PHONE, "22222222-3333-4444-8555-666666666666")
        (mine / ".claude").mkdir()
        ei.prune_session_scratch(agent_dir, SID)
        assert not mine.exists() and other.is_dir()

    def test_a_touch_reaches_only_the_home_itself(self, tmp_path):
        agent_dir = config.get_agent_dir(self.AGENT)
        missing = agent_dir / ei.EXTERNALS_DIRNAME / ei.PHONE / "302101234567"
        ei.touch_home(str(missing))
        assert not missing.exists()
        out = self._outside(tmp_path, "home")
        missing.parent.mkdir(parents=True)
        missing.symlink_to(out / "home", target_is_directory=True)
        import os
        os.utime(out / "home", (1_000_000_000, 1_000_000_000))
        ei.touch_home(str(missing))
        assert (out / "home").stat().st_mtime == 1_000_000_000
        missing.unlink()
        missing.mkdir()
        old = 1_000_000_000
        import os
        os.utime(missing, (old, old))
        ei.touch_home(str(missing))
        assert missing.stat().st_mtime > old
