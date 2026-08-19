"""Read-only Persona loading and replayable Markdown projection lifecycle."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Tuple,
)

from core.wiki_derived_projection import (
    DerivedProjectionLifecycle,
    ProjectionPageSpec,
    ProjectionGenerationReceipt,
    canonical_projection_revision,
)

from .hamartia import BlindSpotProfile
from .pythia import (
    CognitiveProfile,
    EnergyProfile,
    PreferenceProfile,
    ValueProfile,
)

if TYPE_CHECKING:
    from core.cognitive.decision_trace import MaterialActionAuthorization


PERSONA_PAGE_RELATIVE = Path("L5-Feedback", "user-persona.md")
PERSONA_HISTORY_RELATIVE = Path("L5-Feedback", "user-persona-history")


class PersonaProjectionSource:
    """Path-only canonical source reference used by projection replay."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path).expanduser().resolve(strict=False)


class PersonaProjectionMixin:
    """Projection-only Persona behavior shared by the canonical store facade."""

    wiki_dir: Path
    persona_page: Path
    history_dir: Path
    signal_store: Any
    _material_action_resolver: (
        Callable[[Mapping[str, str]], "MaterialActionAuthorization"] | None
    )
    projection_lifecycle: DerivedProjectionLifecycle
    last_trusted_push: Dict[str, Any] | None

    if TYPE_CHECKING:

        def _blindspot_to_dict(
            self, profile: BlindSpotProfile
        ) -> Dict[str, Any]: ...

        def _generate_persona_page(
            self,
            profile: PreferenceProfile,
            blindspot: BlindSpotProfile | None = None,
        ) -> str: ...

    @staticmethod
    def _profile_from_db_row(
        row: Dict[str, Any],
    ) -> Tuple[PreferenceProfile, Optional[BlindSpotProfile]]:
        """Rebuild typed Persona objects from one canonical database row."""

        energy_data = row.get("energy_profile", {})
        cognitive_data = row.get("cognitive_profile", {})
        value_data = row.get("value_profile", {})
        profile = PreferenceProfile(
            version=int(row.get("version") or 0),
            generated_at=str(row.get("generated_at") or ""),
            period_start=str(row.get("period_start") or ""),
            period_end=str(row.get("period_end") or ""),
            energy=EnergyProfile(
                **{
                    key: value
                    for key, value in energy_data.items()
                    if key in EnergyProfile.__dataclass_fields__
                }
            ),
            cognitive=CognitiveProfile(
                **{
                    key: value
                    for key, value in cognitive_data.items()
                    if key in CognitiveProfile.__dataclass_fields__
                }
            ),
            value=ValueProfile(
                **{
                    key: value
                    for key, value in value_data.items()
                    if key in ValueProfile.__dataclass_fields__
                }
            ),
            signal_count=int(row.get("signal_count_used") or 0),
            user_confirmed=bool(row.get("user_confirmed", False)),
            confirmed_at=str(row.get("confirmed_at") or ""),
            calibration_score=(
                float(row["calibration_score"])
                if row.get("calibration_score") is not None
                else None
            ),
        )
        blindspot_data = row.get("blindspot_profile", {})
        blindspot = None
        if blindspot_data:
            blindspot = BlindSpotProfile(
                # Legacy embedded JSON lacks canonical source-authority and
                # revision proof.  Keep its challenge telemetry, but never
                # revive those objects as active cognitive assets.
                confirmed=[],
                suspected=[],
                dismissed=[],
                total_challenges=blindspot_data.get("total_challenges", 0),
                accepted_count=blindspot_data.get("accepted_count", 0),
                ignored_count=blindspot_data.get("ignored_count", 0),
                rejected_count=blindspot_data.get("rejected_count", 0),
                acceptance_rate=blindspot_data.get("acceptance_rate", 0.0),
                challenge_credit=blindspot_data.get("challenge_credit", 10.0),
            )
        return profile, blindspot

    @classmethod
    def load_canonical_persona_read_only(
        cls,
        db_path: Path | str,
    ) -> Tuple[Optional[PreferenceProfile], Optional[BlindSpotProfile]]:
        """Read the latest committed Persona without initializing its database."""

        versions = cls.load_canonical_persona_versions_read_only(db_path)
        return versions[0] if versions else (None, None)

    @classmethod
    def load_canonical_persona_versions_read_only(
        cls,
        db_path: Path | str,
    ) -> List[Tuple[PreferenceProfile, Optional[BlindSpotProfile]]]:
        """Read every committed Persona version through one read-only snapshot."""

        path = Path(db_path).expanduser().resolve(strict=False)
        if not path.is_file():
            raise FileNotFoundError(path)
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            rows = connection.execute(
                """
                SELECT revision.*
                FROM persona_revisions AS revision
                LEFT JOIN persona_revision_heads AS head
                  ON head.scope_key='global' AND head.revision_id=revision.revision_id
                ORDER BY CASE WHEN head.revision_id IS NULL THEN 1 ELSE 0 END,
                         revision.version DESC
                """
            ).fetchall()
        finally:
            connection.close()
        versions: List[Tuple[PreferenceProfile, Optional[BlindSpotProfile]]] = []
        for row in rows:
            decoded = dict(row)
            for field in (
                "energy_profile",
                "cognitive_profile",
                "value_profile",
                "blindspot_profile",
            ):
                decoded[field] = json.loads(decoded.get(field) or "{}")
            versions.append(cls._profile_from_db_row(decoded))
        return versions

    @classmethod
    def for_projection_replay(
        cls,
        *,
        wiki_dir: Path | str,
        canonical_db_path: Path | str,
        projection_lifecycle: DerivedProjectionLifecycle | None = None,
    ) -> Any:
        """Build a projection-only facade without opening the canonical store."""

        instance = cls.__new__(cls)
        instance.wiki_dir = Path(wiki_dir).expanduser().resolve(strict=False)
        instance.persona_page = instance.wiki_dir / PERSONA_PAGE_RELATIVE
        instance.history_dir = instance.wiki_dir / PERSONA_HISTORY_RELATIVE
        instance.history_dir.mkdir(parents=True, exist_ok=True)
        instance.signal_store = PersonaProjectionSource(canonical_db_path)
        instance._material_action_resolver = None
        instance.projection_lifecycle = (
            projection_lifecycle or DerivedProjectionLifecycle(instance.wiki_dir)
        )
        instance.last_trusted_push = None
        return instance

    def project_persona(
        self,
        profile: PreferenceProfile,
        blindspot: BlindSpotProfile | None = None,
        *,
        material_action_commands: Mapping[str, str] | None = None,
    ) -> bool:
        """Render one already-committed Persona without changing canonical storage."""

        generation = self._project_persona_markdown(
            path=self.persona_page,
            profile=profile,
            blindspot=blindspot,
            page_role="formal_derived:persona",
        )
        self.last_trusted_push = dict(generation.__dict__)
        return generation.status == "committed"

    def project_all_personas(
        self,
        versions: Iterable[
            Tuple[PreferenceProfile, Optional[BlindSpotProfile]]
        ],
    ) -> Dict[str, int]:
        """Replay current plus complete history and remove stale Persona pages."""

        ordered = sorted(
            list(versions),
            key=lambda item: (item[0].version, str(item[0].generated_at)),
            reverse=True,
        )
        current_pages = []
        if ordered:
            profile, blindspot = ordered[0]
            current_pages.append(
                self._persona_page_spec(
                    path=self.persona_page,
                    profile=profile,
                    blindspot=blindspot,
                    page_role="formal_derived:persona",
                )
            )
        current_generation = self.projection_lifecycle.publish_generation(
            projection_kind="persona",
            scope_root=self.wiki_dir / "L5-Feedback",
            pages=current_pages,
            full=False,
            stale_paths=() if current_pages else (self.persona_page,),
        )
        if current_generation.status != "committed":
            raise RuntimeError("Persona current-page generation did not commit")

        history_pages = [
            self._persona_page_spec(
                path=self.history_dir / f"user-persona-v{profile.version}.md",
                profile=profile,
                blindspot=blindspot,
                page_role="derived_report:persona_history",
            )
            for profile, blindspot in ordered[1:]
        ]
        owned_history_paths = {
            *(
                path.resolve(strict=False)
                for path in self.history_dir.glob("user-persona-v*.md")
            ),
            *(page.path.resolve(strict=False) for page in history_pages),
        }
        history_generation = self.projection_lifecycle.publish_generation(
            projection_kind="persona",
            scope_root=self.history_dir,
            pages=history_pages,
            full=True,
            owned_paths=owned_history_paths,
        )
        if history_generation.status != "committed":
            raise RuntimeError("Persona history generation did not commit")
        self.last_trusted_push = {
            "status": "committed",
            "current_generation_id": current_generation.generation_id,
            "history_generation_id": history_generation.generation_id,
        }
        return {"current": len(current_pages), "history": len(history_pages)}

    def _project_persona_history(
        self,
        profile: PreferenceProfile,
        blindspot: BlindSpotProfile | None,
        *,
        material_action_commands: Mapping[str, str] | None,
    ) -> bool:
        path = self.history_dir / f"user-persona-v{profile.version}.md"
        generation = self._project_persona_markdown(
            path=path,
            profile=profile,
            blindspot=blindspot,
            page_role="derived_report:persona_history",
        )
        return generation.status == "committed"

    def _persona_page_spec(
        self,
        *,
        path: Path,
        profile: PreferenceProfile,
        blindspot: BlindSpotProfile | None,
        page_role: str,
    ) -> ProjectionPageSpec:
        canonical_source = {
            "profile": profile,
            "blindspot": self._blindspot_to_dict(blindspot) if blindspot else {},
        }
        return ProjectionPageSpec(
            path=path,
            content=self._generate_persona_page(profile, blindspot),
            page_role=page_role,
            canonical_revision=canonical_projection_revision(canonical_source),
            source_refs=(
                f"persona-store:{self.signal_store.db_path}#version/{profile.version}",
            ),
        )

    def _project_persona_markdown(
        self,
        *,
        path: Path,
        profile: PreferenceProfile,
        blindspot: BlindSpotProfile | None,
        page_role: str,
    ) -> ProjectionGenerationReceipt:
        page = self._persona_page_spec(
            path=path,
            profile=profile,
            blindspot=blindspot,
            page_role=page_role,
        )
        generation = self.projection_lifecycle.publish_generation(
            projection_kind="persona",
            scope_root=self.wiki_dir / "L5-Feedback",
            pages=[page],
            full=False,
        )
        item = next(item for item in generation.items if item.action == "upsert")
        if item.status != "published" or not item.event_trace_id:
            raise RuntimeError(f"Persona projection lifecycle did not publish: {path}")
        return generation
