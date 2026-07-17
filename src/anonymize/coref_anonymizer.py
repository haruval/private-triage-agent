"""Coreference-aware anonymizer.

Wraps :class:`CombinedAnonymizer` (regex + NER) with a third pass that uses
fastcoref to find pronoun chains pointing to already-tagged entities, then
replaces those pronouns with grammatical placeholders linked to the entity.

# Library choice: fastcoref over spaCy's experimental coref.
#
# spaCy's experimental coref (``spacy-experimental==0.6.4``, model
# ``en_coreference_web_trf``) ships only as a source distribution and fails
# to build on Python 3.12 / Apple Silicon — its Cython output references
# ``_PyCFrame->use_tracing``, which was removed from CPython 3.12.
# ``fastcoref`` installs cleanly on Apple Silicon as a pure-Python wheel
# (after pinning ``transformers<5`` for the ``FCorefModel.all_tied_weights_keys``
# attribute introduced in transformers 5.x).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from src.anonymize.ner_anonymizer import CombinedAnonymizer
from src.anonymize.regex_anonymizer import Detection

PRONOUNS: frozenset[str] = frozenset({
    "he", "him", "his", "himself",
    "she", "her", "hers", "herself",
    "they", "them", "their", "theirs", "themselves",
    "it", "its", "itself",
})

_PRONOUN_PREFIX: dict[str, str] = {
    "he": "They",
    "she": "They",
    "they": "They",
    "him": "Them",
    "them": "Them",
    "his": "Their",
    "her": "Their",
    "their": "Their",
    "hers": "Theirs",
    "theirs": "Theirs",
    "himself": "Themself",
    "herself": "Themself",
    "themselves": "Themself",
    "it": "It",
    "its": "Its",
    "itself": "Itself",
}

DEFAULT_COREF_LOCK_PATH = (
    Path(__file__).resolve().parents[2] / "configs" / "coref_model.lock.json"
)
COREF_CACHE_COMMAND = "venv/bin/python scripts/cache_coref_model.py"
DEFAULT_MIN_AGE_DAYS = 14


@dataclass(frozen=True)
class CorefModelLock:
    """Validated immutable identity and file hashes for the coref model."""

    repo_id: str
    revision: str
    committed_at: datetime
    files: tuple[tuple[str, str], ...]
    reviewed_at: date | None = None

    @classmethod
    def from_json_dict(cls, data: Any) -> "CorefModelLock":
        if not isinstance(data, dict):
            raise ValueError("coref model lock must be a JSON object")

        repo_id = data.get("repo_id")
        if not isinstance(repo_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_id
        ):
            raise ValueError("coref model lock 'repo_id' must be owner/name")

        revision = data.get("revision")
        if not isinstance(revision, str) or not re.fullmatch(
            r"[0-9a-f]{40}", revision
        ):
            raise ValueError("coref model lock 'revision' must be a full commit SHA")

        committed_raw = data.get("committed_at")
        if not isinstance(committed_raw, str):
            raise ValueError("coref model lock 'committed_at' must be an ISO timestamp")
        try:
            committed_at = datetime.fromisoformat(committed_raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                "coref model lock 'committed_at' must be an ISO timestamp"
            ) from exc
        if committed_at.tzinfo is None:
            raise ValueError("coref model lock 'committed_at' must include a timezone")
        committed_at = committed_at.astimezone(timezone.utc)

        reviewed_raw = data.get("reviewed_at")
        if not isinstance(reviewed_raw, str):
            raise ValueError("coref model lock 'reviewed_at' must be an ISO date")
        try:
            reviewed_at = date.fromisoformat(reviewed_raw)
        except ValueError as exc:
            raise ValueError("coref model lock 'reviewed_at' must be an ISO date") from exc

        files_raw = data.get("files")
        if not isinstance(files_raw, dict) or not files_raw:
            raise ValueError("coref model lock 'files' must be a non-empty object")
        files: list[tuple[str, str]] = []
        for filename, digest in files_raw.items():
            if not isinstance(filename, str):
                raise ValueError("coref model lock filenames must be strings")
            rel = PurePosixPath(filename)
            if rel.is_absolute() or ".." in rel.parts or str(rel) != filename:
                raise ValueError(f"unsafe coref model filename: {filename!r}")
            if not isinstance(digest, str) or not re.fullmatch(
                r"[0-9a-f]{64}", digest
            ):
                raise ValueError(
                    f"coref model hash for {filename!r} must be lowercase SHA-256"
                )
            files.append((filename, digest))

        return cls(
            repo_id=repo_id,
            revision=revision,
            committed_at=committed_at,
            files=tuple(sorted(files)),
            reviewed_at=reviewed_at,
        )


def load_coref_model_lock(
    path: Path | str = DEFAULT_COREF_LOCK_PATH,
) -> CorefModelLock:
    """Load and validate the checked-in coref model lock manifest."""
    lock_path = Path(path)
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read coref model lock {lock_path}: {exc}") from exc
    return CorefModelLock.from_json_dict(data)


@dataclass(frozen=True)
class _Replacement:
    start: int
    end: int
    placeholder: str
    value: str
    source: str  # "base" or "coref"


class CorefAnonymizer:
    """Regex + NER + coref pronoun resolution.

    Public surface mirrors :class:`CombinedAnonymizer`. ``detect()`` returns
    every span we replace — base detections plus pronoun mentions resolved
    via coref — all anchored to offsets in the *original* text.
    """

    def __init__(
        self,
        *,
        base: CombinedAnonymizer | None = None,
        coref: Any = None,
        coref_lock_path: Path | str = DEFAULT_COREF_LOCK_PATH,
    ) -> None:
        self._base = base if base is not None else CombinedAnonymizer()
        if coref is not None:
            self._coref = coref
        else:
            model_lock = load_coref_model_lock(coref_lock_path)
            model_path = _resolve_coref_model(model_lock, local_files_only=True)
            self._coref = _build_coref(model_path)

    # --- public API -------------------------------------------------------

    def anonymize(self, text: str) -> tuple[str, dict[str, str]]:
        reps, base_map = self._build_replacements(text)
        out = text
        for r in sorted(reps, key=lambda r: r.start, reverse=True):
            out = out[: r.start] + r.placeholder + out[r.end :]
        return out, base_map

    def detect(self, text: str) -> list[Detection]:
        reps, _ = self._build_replacements(text)
        type_for = {"base": None, "coref": "pronoun"}
        return [
            Detection(r.start, r.end, type_for[r.source] or "base", r.value)
            for r in sorted(reps, key=lambda r: r.start)
        ]

    # --- internals --------------------------------------------------------

    def _build_replacements(
        self, text: str
    ) -> tuple[list[_Replacement], dict[str, str]]:
        # The base plan already carries original-text spans and the exact
        # placeholder per span, so this stays offset-faithful to what the
        # base anonymizer would substitute.
        plan, base_map = self._base.replacements(text)
        mapping = dict(base_map)
        base_reps = [
            _Replacement(r.start, r.end, r.placeholder, r.value, "base") for r in plan
        ]

        # Coref runs on the ORIGINAL text — pronouns and their antecedents
        # must both be visible.
        clusters = self._coref.predict(texts=[text])[0].get_clusters(as_strings=False)

        base_spans = [(r.start, r.end) for r in base_reps]
        coref_reps: list[_Replacement] = []
        for cluster in clusters:
            placeholder = _placeholder_for_cluster(cluster, base_reps)
            if placeholder is None:
                continue
            for (cs, ce) in cluster:
                mention = text[cs:ce]
                normalized = mention.lower().strip()
                if normalized not in PRONOUNS:
                    continue
                if _overlaps_any(cs, ce, base_spans):
                    continue
                if _overlaps_any(cs, ce, [(r.start, r.end) for r in coref_reps]):
                    continue
                pronoun_placeholder = _linked_pronoun_placeholder(
                    mention, placeholder, mapping
                )
                coref_reps.append(
                    _Replacement(cs, ce, pronoun_placeholder, mention, "coref")
                )

        return base_reps + coref_reps, mapping


def _resolve_coref_model(
    model_lock: CorefModelLock,
    *,
    local_files_only: bool,
    downloader: Any = None,
) -> Path:
    """Resolve only the locked revision, then verify every required file."""
    if downloader is None:
        from huggingface_hub import snapshot_download

        downloader = snapshot_download

    try:
        resolved = downloader(
            repo_id=model_lock.repo_id,
            revision=model_lock.revision,
            allow_patterns=[filename for filename, _ in model_lock.files],
            local_files_only=local_files_only,
        )
    except Exception as exc:
        if local_files_only:
            raise RuntimeError(
                f"Coreference model {model_lock.repo_id!r} at revision "
                f"{model_lock.revision} is not cached locally. "
                f"Run:\n  {COREF_CACHE_COMMAND}"
            ) from exc
        raise RuntimeError(
            f"Could not download coreference model {model_lock.repo_id!r} "
            f"at revision {model_lock.revision}: {exc}"
        ) from exc
    snapshot = Path(resolved)
    _verify_coref_model_files(snapshot, model_lock)
    return snapshot


def cache_coref_model(
    lock_path: Path | str = DEFAULT_COREF_LOCK_PATH,
) -> Path:
    """Age-check, download, and hash-check the locked model during setup."""
    model_lock = load_coref_model_lock(lock_path)
    _verify_coref_model_age(model_lock)
    return _resolve_coref_model(model_lock, local_files_only=False)


def _build_coref(
    model_path: Path,
    *,
    coref_factory: Callable[..., Any] | None = None,
    blank_factory: Callable[[str], Any] | None = None,
) -> Any:
    """Build fastcoref with an in-memory tokenizer and no model downloads."""
    if coref_factory is None:
        from fastcoref import FCoref

        coref_factory = FCoref
    if blank_factory is None:
        import spacy

        blank_factory = spacy.blank
    return coref_factory(
        model_name_or_path=str(model_path),
        nlp=blank_factory("en"),
    )


def _verify_coref_model_files(
    snapshot: Path,
    model_lock: CorefModelLock,
) -> None:
    """Reject incomplete or modified model snapshots before loading them."""
    for filename, expected in model_lock.files:
        path = snapshot / filename
        if not path.is_file():
            raise RuntimeError(
                f"coref model integrity check failed: missing {filename!r}; "
                f"run {COREF_CACHE_COMMAND}"
            )
        digest = hashlib.sha256()
        with path.open("rb") as model_file:
            for chunk in iter(lambda: model_file.read(1024 * 1024), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
        if actual != expected:
            raise RuntimeError(
                f"coref model integrity check failed for {filename!r}: "
                f"expected {expected}, got {actual}; run {COREF_CACHE_COMMAND}"
            )


def _fetch_coref_commit_time(repo_id: str, revision: str) -> datetime:
    """Read the locked revision's timestamp from the Hugging Face API."""
    from huggingface_hub import HfApi

    commits = HfApi().list_repo_commits(repo_id, revision=revision)
    for commit in commits:
        if commit.commit_id == revision:
            return commit.created_at.astimezone(timezone.utc)
    raise RuntimeError(f"Hugging Face did not return locked revision {revision}")


def _verify_coref_model_age(
    model_lock: CorefModelLock,
    *,
    commit_time_lookup: Callable[[str, str], datetime] = _fetch_coref_commit_time,
    now: datetime | None = None,
) -> None:
    """Apply the package-age policy to the immutable model revision."""
    if os.environ.get("ALLOW_RECENT_MODELS") == "1":
        return
    try:
        min_age_days = int(os.environ.get("MIN_AGE_DAYS", DEFAULT_MIN_AGE_DAYS))
    except ValueError as exc:
        raise RuntimeError(f"invalid MIN_AGE_DAYS={os.environ.get('MIN_AGE_DAYS')!r}") from exc
    if min_age_days < 0:
        raise RuntimeError("MIN_AGE_DAYS must not be negative")

    try:
        official_time = commit_time_lookup(model_lock.repo_id, model_lock.revision)
    except Exception as exc:
        raise RuntimeError(
            f"could not verify age of coref model revision {model_lock.revision}: {exc}"
        ) from exc
    if official_time.tzinfo is None:
        official_time = official_time.replace(tzinfo=timezone.utc)
    official_time = official_time.astimezone(timezone.utc)
    if official_time != model_lock.committed_at:
        raise RuntimeError(
            "coref model commit time does not match the reviewed lock: "
            f"expected {model_lock.committed_at.isoformat()}, "
            f"got {official_time.isoformat()}"
        )

    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = current_time - official_time
    if age < timedelta(days=min_age_days):
        raise RuntimeError(
            f"coref model revision {model_lock.revision} is only {age.days} day(s) "
            f"old; minimum is {min_age_days}. Wait or deliberately override with "
            "ALLOW_RECENT_MODELS=1"
        )


def _linked_pronoun_placeholder(
    mention: str,
    entity_placeholder: str,
    mapping: dict[str, str],
) -> str:
    """Build a grammatical pronoun token linked to its entity placeholder.

    ``Alex_P1`` and ``Their_P1`` share the suffix that tells Claude they refer
    to the same entity. The pronoun token maps to the original surface form,
    so local rehydration restores ``her`` rather than turning it into a name.
    """
    normalized = mention.lower().strip()
    prefix = _PRONOUN_PREFIX[normalized]
    _, separator, suffix = entity_placeholder.rpartition("_")
    if not separator:
        raise ValueError(f"invalid entity placeholder: {entity_placeholder!r}")

    candidate = f"{prefix}_{suffix}"
    if candidate not in mapping or mapping[candidate] == mention:
        mapping[candidate] = mention
        return candidate

    variant = 0
    while True:
        candidate = f"{prefix}{_alpha_tag(variant)}_{suffix}"
        if candidate not in mapping or mapping[candidate] == mention:
            mapping[candidate] = mention
            return candidate
        variant += 1


def _alpha_tag(index: int) -> str:
    """Return a lowercase alphabetic tag: a..z, aa..az, ba..."""
    chars: list[str] = []
    while True:
        index, remainder = divmod(index, 26)
        chars.append(chr(ord("a") + remainder))
        if index == 0:
            return "".join(reversed(chars))
        index -= 1


def _placeholder_for_cluster(
    cluster: list[tuple[int, int]],
    base_reps: list[_Replacement],
) -> str | None:
    """Return the placeholder for the first cluster mention that overlaps
    a base detection. ``None`` if no mention overlaps any base span."""
    for (cs, ce) in cluster:
        for r in base_reps:
            if cs < r.end and r.start < ce:
                return r.placeholder
    return None


def _overlaps_any(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    for cs, ce in spans:
        if start < ce and cs < end:
            return True
    return False
