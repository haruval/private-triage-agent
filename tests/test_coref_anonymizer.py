"""Tests for CorefAnonymizer's base-layer fidelity and pronoun resolution.

The coref model itself is never loaded: a stub stands in for fastcoref (the
constructor takes an injected ``coref``), so these tests run offline. The
deterministic tests also inject a fake NER layer; the corpus equivalence
test uses the real spaCy model (session-scoped, skipped if not installed).

Regression background: the base replacements used to be rebuilt from
``CombinedAnonymizer.detect()``'s value re-search, which (a) anchored a
repeated value to the wrong occurrence — garbling text and leaking the real
entity — and (b) silently dropped NER spans that contained a regex
placeholder, leaking their un-replaced remainder. Both leaked raw PII on
the default ``combined`` path.
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.anonymize.coref_anonymizer import (
    CorefAnonymizer,
    CorefModelLock,
    _build_coref,
    _materialize_coref_model,
    _resolve_coref_model,
    _resolve_installed_coref_model,
    _verify_coref_model_age,
    _verify_coref_model_files,
    load_coref_model_lock,
)
from src.anonymize.ner_anonymizer import CombinedAnonymizer, NERAnonymizer
from src.anonymize.regex_anonymizer import Detection, RegexAnonymizer
from src.anonymize.rehydrate import rehydrate
from src.eval.corpus import load_corpus


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeNER:
    """Deterministic stand-in for NERAnonymizer: tags fixed patterns."""

    def __init__(self, patterns: list[tuple[str, str]]) -> None:
        self._patterns = [(re.compile(p), t) for p, t in patterns]

    def detect(self, text: str) -> list[Detection]:
        hits = [
            Detection(m.start(), m.end(), type_, m.group(0))
            for pattern, type_ in self._patterns
            for m in pattern.finditer(text)
        ]
        return sorted(hits, key=lambda d: d.start)


class _StubCoref:
    """Stands in for fastcoref's FCoref: returns fixed mention clusters."""

    def __init__(self, clusters: list[list[tuple[int, int]]] | None = None) -> None:
        self._clusters = clusters or []

    def predict(self, texts: list[str]) -> list["_StubResult"]:
        return [_StubResult(self._clusters) for _ in texts]


class _StubResult:
    def __init__(self, clusters: list[list[tuple[int, int]]]) -> None:
        self._clusters = clusters

    def get_clusters(self, as_strings: bool = False) -> list[list[tuple[int, int]]]:
        assert as_strings is False
        return self._clusters


def _coref_over(
    patterns: list[tuple[str, str]],
    clusters: list[list[tuple[int, int]]] | None = None,
) -> CorefAnonymizer:
    base = CombinedAnonymizer(regex=RegexAnonymizer(), ner=_FakeNER(patterns))
    return CorefAnonymizer(base=base, coref=_StubCoref(clusters))


# ---------------------------------------------------------------------------
# Deterministic regressions (fake NER, no models)
# ---------------------------------------------------------------------------


def test_base_span_anchors_to_true_occurrence_not_first_substring() -> None:
    """Regression: 'Ann' must be replaced where NER found it, not inside the
    earlier 'Announcement' — the old value re-search garbled the text and
    left the real name for Claude to see."""
    text = "Announcement: contact Ann today."
    anon = _coref_over([(r"\bAnn\b", "person")])

    out, mapping = anon.anonymize(text)

    assert out == "Announcement: contact Alex_P1 today."
    assert mapping == {"Alex_P1": "Ann"}
    assert rehydrate(out, mapping) == text


def test_ner_span_containing_regex_placeholder_stays_covered() -> None:
    """Regression: an NER span that swallows a regex placeholder used to be
    dropped on the coref path, leaking 'million USD' and keeping a phantom
    mapping entry."""
    text = "We agreed on $2.5 million USD payable to John Smith."
    anon = _coref_over(
        [(r"Amount_M\d+ million USD", "money"), (r"John Smith", "person")]
    )

    out, mapping = anon.anonymize(text)

    assert out == "We agreed on Amount_M1 payable to Alex_P1."
    assert mapping == {"Amount_M1": "$2.5 million USD", "Alex_P1": "John Smith"}
    assert "million" not in out
    # No phantom entries: every placeholder in the mapping was applied.
    assert all(placeholder in out for placeholder in mapping)
    assert rehydrate(out, mapping) == text


def test_pronoun_uses_linked_placeholder_and_round_trips() -> None:
    text = "Ann wrote the draft. She filed it."
    anon = _coref_over([(r"\bAnn\b", "person")], clusters=[[(0, 3), (21, 24)]])

    out, mapping = anon.anonymize(text)

    assert out == "Alex_P1 wrote the draft. They_P1 filed it."
    assert mapping == {"Alex_P1": "Ann", "They_P1": "She"}
    assert rehydrate(out, mapping) == text

    detections = {(d.type, d.value) for d in anon.detect(text)}
    assert ("pronoun", "She") in detections


def test_possessive_and_reflexive_pronouns_preserve_grammar() -> None:
    text = "Ann filed her report herself."
    anon = _coref_over(
        [(r"\bAnn\b", "person")],
        clusters=[[(0, 3), (10, 13), (21, 28)]],
    )

    out, mapping = anon.anonymize(text)

    assert out == "Alex_P1 filed Their_P1 report Themself_P1."
    assert mapping == {
        "Alex_P1": "Ann",
        "Their_P1": "her",
        "Themself_P1": "herself",
    }
    assert rehydrate(out, mapping) == text


def test_same_entity_pronoun_variants_get_distinct_linked_placeholders() -> None:
    text = "Ann said she agreed. She signed."
    anon = _coref_over(
        [(r"\bAnn\b", "person")],
        clusters=[[(0, 3), (9, 12), (21, 24)]],
    )

    out, mapping = anon.anonymize(text)

    assert out == "Alex_P1 said They_P1 agreed. Theya_P1 signed."
    assert mapping == {
        "Alex_P1": "Ann",
        "They_P1": "she",
        "Theya_P1": "She",
    }
    assert rehydrate(out, mapping) == text


def _lock_for_file(path: Path, *, committed_at: datetime | None = None) -> CorefModelLock:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return CorefModelLock(
        repo_id="biu-nlp/f-coref",
        revision="a" * 40,
        committed_at=committed_at or datetime(2022, 1, 1, tzinfo=timezone.utc),
        files=((path.name, digest),),
    )


def test_resolve_coref_model_uses_pinned_local_snapshot(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    config = snapshot / "config.json"
    config.write_text("{}")
    model_lock = _lock_for_file(config)
    calls: list[dict[str, object]] = []

    def _download(**kwargs: object) -> str:
        calls.append(kwargs)
        return str(snapshot)

    resolved = _resolve_coref_model(
        model_lock, local_files_only=True, downloader=_download
    )

    assert resolved == snapshot
    assert calls == [{
        "repo_id": "biu-nlp/f-coref",
        "revision": "a" * 40,
        "allow_patterns": ["config.json"],
        "local_files_only": True,
    }]


def test_resolve_coref_model_rejects_modified_file(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    config = snapshot / "config.json"
    config.write_text("expected")
    model_lock = _lock_for_file(config)
    config.write_text("modified")

    with pytest.raises(RuntimeError, match="integrity check failed"):
        _resolve_coref_model(
            model_lock,
            local_files_only=True,
            downloader=lambda **kwargs: str(snapshot),
        )


def test_materialized_runtime_copy_contains_only_locked_regular_files(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    config = snapshot / "config.json"
    config.write_text("expected")
    (snapshot / "model.safetensors").write_text("unlisted alternate weights")
    model_lock = _lock_for_file(config)

    installed = _materialize_coref_model(snapshot, model_lock, tmp_path / "runtime")

    assert [path.name for path in installed.iterdir()] == ["config.json"]
    assert not (installed / "config.json").is_symlink()
    assert (installed / "config.json").read_text() == "expected"
    assert _resolve_installed_coref_model(
        model_lock, runtime_root=tmp_path / "runtime"
    ) == installed


def test_runtime_copy_rejects_unlisted_alternate_weights(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    installed = runtime_root / ("a" * 40)
    installed.mkdir(parents=True)
    config = installed / "config.json"
    config.write_text("expected")
    model_lock = _lock_for_file(config)
    (installed / "model.safetensors").write_text("unverified")

    with pytest.raises(RuntimeError, match="missing or invalid"):
        _resolve_installed_coref_model(model_lock, runtime_root=runtime_root)


def test_runtime_copy_rejects_unlisted_directory(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    installed = runtime_root / ("a" * 40)
    installed.mkdir(parents=True)
    config = installed / "config.json"
    config.write_text("expected")
    model_lock = _lock_for_file(config)
    (installed / "unlisted").mkdir()

    with pytest.raises(RuntimeError, match="unexpected_dirs"):
        _verify_coref_model_files(installed, model_lock, exact=True)


def test_runtime_copy_rejects_symlinked_file(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    installed = runtime_root / ("a" * 40)
    installed.mkdir(parents=True)
    source = tmp_path / "source.json"
    source.write_text("expected")
    linked = installed / "config.json"
    linked.symlink_to(source)
    model_lock = _lock_for_file(source)

    with pytest.raises(RuntimeError, match="symlink"):
        _verify_coref_model_files(installed, model_lock, exact=True)


def test_interrupted_materialization_leaves_no_runtime_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    config = snapshot / "config.json"
    config.write_text("expected")
    model_lock = _lock_for_file(config)
    runtime_root = tmp_path / "runtime"

    def _interrupt(*args: object, **kwargs: object) -> None:
        raise OSError("copy interrupted")

    monkeypatch.setattr(
        "src.anonymize.coref_anonymizer.shutil.copyfileobj", _interrupt
    )

    with pytest.raises(OSError, match="copy interrupted"):
        _materialize_coref_model(snapshot, model_lock, runtime_root)

    assert not (runtime_root / model_lock.revision).exists()
    assert list(runtime_root.iterdir()) == []


def test_missing_local_coref_model_has_actionable_error() -> None:
    def _missing(**kwargs: object) -> str:
        raise OSError("not cached")

    model_lock = CorefModelLock(
        repo_id="biu-nlp/f-coref",
        revision="a" * 40,
        committed_at=datetime(2022, 1, 1, tzinfo=timezone.utc),
        files=(("config.json", "0" * 64),),
    )
    with pytest.raises(RuntimeError, match=r"scripts/cache_coref_model\.py"):
        _resolve_coref_model(
            model_lock, local_files_only=True, downloader=_missing
        )


def test_build_coref_uses_blank_spacy_language(tmp_path: Path) -> None:
    language = object()
    calls: list[dict[str, object]] = []

    def _blank(name: str) -> object:
        assert name == "en"
        return language

    def _factory(**kwargs: object) -> object:
        calls.append(kwargs)
        return "coref"

    built = _build_coref(
        tmp_path,
        coref_factory=_factory,
        blank_factory=_blank,
    )

    assert built == "coref"
    assert calls == [{
        "model_name_or_path": str(tmp_path),
        "nlp": language,
        "enable_progress_bar": False,
    }]


def test_coref_model_loading_is_deferred_and_cached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []
    model_lock = object()
    model_path = tmp_path / "model"
    coref = _StubCoref()

    def _load(path: Path) -> object:
        calls.append(("lock", path))
        return model_lock

    def _resolve(lock: object, *, runtime_root: Path) -> Path:
        assert lock is model_lock
        calls.append(("resolve", runtime_root))
        return model_path

    def _build(path: Path) -> _StubCoref:
        calls.append(("build", path))
        return coref

    monkeypatch.setattr(
        "src.anonymize.coref_anonymizer.load_coref_model_lock", _load
    )
    monkeypatch.setattr(
        "src.anonymize.coref_anonymizer._resolve_installed_coref_model", _resolve
    )
    monkeypatch.setattr("src.anonymize.coref_anonymizer._build_coref", _build)

    base = CombinedAnonymizer(regex=RegexAnonymizer(), ner=_FakeNER([]))
    lock_path = tmp_path / "model.lock.json"
    runtime_root = tmp_path / "runtime"
    anon = CorefAnonymizer(
        base=base,
        coref_lock_path=lock_path,
        coref_runtime_root=runtime_root,
    )

    assert calls == []

    anon.anonymize("No sensitive entity here.")
    anon.detect("Still no sensitive entity here.")

    assert calls == [
        ("lock", lock_path),
        ("resolve", runtime_root),
        ("build", model_path),
    ]


def test_build_coref_disables_library_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import datasets

    progress_calls: list[bool] = []
    logger = logging.getLogger("fastcoref")
    previous_level = logger.level
    monkeypatch.setattr(
        datasets, "disable_progress_bars", lambda: progress_calls.append(True)
    )

    def _noisy_factory(**kwargs: object) -> object:
        logging.getLogger("fastcoref.modeling").info("fastcoref info")
        if kwargs["enable_progress_bar"]:
            print("fastcoref progress", file=sys.stderr)
        return object()

    try:
        _build_coref(
            tmp_path,
            coref_factory=_noisy_factory,
            blank_factory=lambda name: object(),
        )
        assert progress_calls == [True]
        assert logger.level == logging.WARNING
        assert capsys.readouterr().err == ""
    finally:
        logger.setLevel(previous_level)


def test_fastcoref_import_preserves_root_logging_in_clean_process() -> None:
    """Regression: fastcoref calls basicConfig(INFO) on its first import."""
    script = """
import logging

from src.anonymize.coref_anonymizer import _import_fcoref_factory

root = logging.getLogger()
level_before = root.level
handlers_before = tuple(root.handlers)
_import_fcoref_factory()
assert root.level == level_before
assert tuple(root.handlers) == handlers_before
logging.getLogger("httpx").info("must stay silent")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""


def test_coref_model_age_uses_official_commit_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALLOW_RECENT_MODELS", raising=False)
    model_lock = CorefModelLock(
        repo_id="biu-nlp/f-coref",
        revision="a" * 40,
        committed_at=datetime(2022, 1, 1, tzinfo=timezone.utc),
        files=(("config.json", "0" * 64),),
    )

    _verify_coref_model_age(
        model_lock,
        commit_time_lookup=lambda repo_id, revision: datetime(
            2022, 1, 1, tzinfo=timezone.utc
        ),
        now=datetime(2022, 2, 1, tzinfo=timezone.utc),
    )


def test_coref_model_age_rejects_recent_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALLOW_RECENT_MODELS", raising=False)
    committed_at = datetime(2026, 7, 10, tzinfo=timezone.utc)
    model_lock = CorefModelLock(
        repo_id="biu-nlp/f-coref",
        revision="a" * 40,
        committed_at=committed_at,
        files=(("config.json", "0" * 64),),
    )

    with pytest.raises(RuntimeError, match="ALLOW_RECENT_MODELS=1"):
        _verify_coref_model_age(
            model_lock,
            commit_time_lookup=lambda repo_id, revision: committed_at,
            now=datetime(2026, 7, 16, tzinfo=timezone.utc),
        )


def test_coref_model_age_override_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_RECENT_MODELS", "1")
    model_lock = CorefModelLock(
        repo_id="biu-nlp/f-coref",
        revision="a" * 40,
        committed_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
        files=(("config.json", "0" * 64),),
    )

    def _unexpected_lookup(repo_id: str, revision: str) -> datetime:
        raise AssertionError("override should skip the remote age lookup")

    _verify_coref_model_age(
        model_lock,
        commit_time_lookup=_unexpected_lookup,
        now=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )


def test_coref_model_lock_rejects_mutable_revision() -> None:
    with pytest.raises(ValueError, match="full commit SHA"):
        CorefModelLock.from_json_dict(
            {
                "repo_id": "biu-nlp/f-coref",
                "revision": "main",
                "committed_at": "2022-11-28T11:35:52+00:00",
                "reviewed_at": "2026-07-16",
                "files": {"config.json": "0" * 64},
            }
        )


def test_checked_in_coref_model_lock_is_valid() -> None:
    model_lock = load_coref_model_lock()
    assert model_lock.repo_id == "biu-nlp/f-coref"
    assert len(model_lock.revision) == 40
    assert "pytorch_model.bin" in dict(model_lock.files)


# ---------------------------------------------------------------------------
# Corpus equivalence (real spaCy model)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def real_combined() -> CombinedAnonymizer:
    try:
        return CombinedAnonymizer(ner=NERAnonymizer())
    except RuntimeError as e:
        pytest.skip(str(e))


def test_no_cluster_coref_matches_base_on_corpus(
    real_combined: CombinedAnonymizer,
) -> None:
    """With zero coref clusters, the coref path must reproduce the base
    anonymization exactly — same text, same mapping, no phantom entries —
    on every example of the hand-labeled corpus."""
    anon = CorefAnonymizer(base=real_combined, coref=_StubCoref())
    for ex in load_corpus():
        base_out, base_map = real_combined.anonymize(ex.text)
        out, mapping = anon.anonymize(ex.text)
        assert (out, mapping) == (base_out, base_map), f"diverged on {ex.text!r}"
        assert all(placeholder in out for placeholder in mapping)
