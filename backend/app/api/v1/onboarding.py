"""Onboarding library analysis and new-playlist selection (U9, F5, R7).

The user sees AI-proposed new-playlist candidates (name/theme/estimated
count, no songs attached) alongside their existing playlists, and can also
add a custom playlist by natural-language description on the same screen.
Only the selection step creates real (empty) playlist rows; nothing here
ever creates a review_queue item or touches YouTube.
"""

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import (
    ReorganizeMatchingDependencies,
    get_default_user,
    get_library_analysis_service,
    get_music_client,
    get_reorganize_matching_dependencies,
)
from app.integrations.base import MusicServiceClient
from app.jobs.ingestion import (
    IngestionAlreadyInProgressError,
    run_ingestion_check_to_completion,
    trigger_ingestion_check,
)
from app.jobs.reorganize_matching import (
    MatchingAlreadyInProgressError,
    run_reorganize_matching,
    trigger_matching,
)
from app.models.user import User
from app.services.library_analysis import (
    LibraryAnalysisService,
    OnboardingProposalsAlreadyInProgressError,
    PlaylistRemovalRequiresConfirmationError,
    run_propose_new_playlists,
    run_reorganize_clustering,
    trigger_propose_new_playlists,
)
from app.services.reorganize_apply import (
    ApplyAlreadyInProgressError,
    ReorganizeSessionNotFoundError,
    run_finish_and_apply,
    trigger_apply,
)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


class PlaylistProposalResponse(BaseModel):
    name: str
    theme: str
    song_count: int
    confidence: float


class AddedPlaylistResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]
    rule: Optional[dict]
    # "proposal" | "custom" | "adopted" | None (rows created before this
    # field existed) -- lets the frontend keep a playlist in the UI section
    # it originated from, checked, across a remount.
    source: Optional[str]


class YouTubePlaylistResponse(BaseModel):
    playlist_id: str
    title: str


class PlaylistsResponse(BaseModel):
    existing_playlists: list[YouTubePlaylistResponse]
    added_playlists: list[AddedPlaylistResponse]


class ProposalsTriggerResponse(BaseModel):
    proposals_status: str


class ProposalsStatusResponse(BaseModel):
    proposals_status: str
    proposals_processed_count: int
    proposals_total_count: int
    proposals: list[PlaylistProposalResponse]


class AcceptedProposal(BaseModel):
    name: str
    theme: str


class CustomPlaylist(BaseModel):
    name: str
    description: str


class AdoptedPlaylist(BaseModel):
    playlist_id: str
    name: str


class SelectionRequest(BaseModel):
    accepted_proposals: list[AcceptedProposal] = []
    custom_playlists: list[CustomPlaylist] = []
    adopted_playlists: list[AdoptedPlaylist] = []
    removed_playlist_ids: list[int] = []
    # KTD7: playlist ids the user has explicitly confirmed removing despite
    # having non-terminal (pending/approved_pending_apply) review work still
    # referencing them -- omitted ids that turn out to need confirmation
    # cause a 409 (see select()) instead of silently proceeding.
    confirmed_removed_playlist_ids: list[int] = []


class CreatedPlaylistResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]


class SelectionResponse(BaseModel):
    created_playlists: list[CreatedPlaylistResponse]


@router.get("/playlists", response_model=PlaylistsResponse)
def get_playlists(
    user: User = Depends(get_default_user),
    service: LibraryAnalysisService = Depends(get_library_analysis_service),
) -> PlaylistsResponse:
    """Fast onboarding data (a DB query and a plain YouTube playlists list) --
    split out from `/proposals` (AI clustering, can take a long time for a
    big library) so the frontend can render this immediately instead of
    both being stuck behind the slow one on a single combined endpoint.
    """
    added = service.list_added_playlists(user.id)
    existing_on_youtube = service.list_existing_youtube_playlists(user.id)
    return PlaylistsResponse(
        existing_playlists=[
            YouTubePlaylistResponse(playlist_id=pl["playlistId"], title=pl["title"])
            for pl in existing_on_youtube
        ],
        added_playlists=[
            AddedPlaylistResponse(
                id=pl.id, name=pl.name, description=pl.description, rule=pl.rule, source=pl.source
            )
            for pl in added
        ],
    )


@router.post("/proposals", response_model=ProposalsTriggerResponse)
def trigger_proposals(
    background_tasks: BackgroundTasks,
    user: User = Depends(get_default_user),
    service: LibraryAnalysisService = Depends(get_library_analysis_service),
) -> ProposalsTriggerResponse:
    """AI-suggested new playlists -- embeddings + clustering + LLM naming,
    slow for a large library (previously up to ~2 minutes of invisible work
    behind a single "Loading…" spinner). Fetches the complete current
    liked-songs library fresh (same live call as Reorganize's own trigger)
    rather than depending on whatever's already been ingested locally --
    ingestion itself doesn't start until onboarding's selection completes
    (F5 step 4), so a DB-scoped read here would see nothing for a brand-new
    user. Runs as a background task (mirrors Reorganize's clustering
    trigger) so the response returns immediately; poll `GET /proposals` for
    live progress and results. Split from `/playlists` (see there) so the
    frontend isn't blocked on this before showing anything at all.
    """
    try:
        updated_user = trigger_propose_new_playlists(service.user_repository, user.id)
    except OnboardingProposalsAlreadyInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    liked_songs = service.music_client.get_liked_songs()
    background_tasks.add_task(run_propose_new_playlists, user.id, liked_songs)
    return ProposalsTriggerResponse(proposals_status=updated_user.proposals_status)


@router.get("/proposals", response_model=ProposalsStatusResponse)
def get_proposals_status(
    user: User = Depends(get_default_user),
    service: LibraryAnalysisService = Depends(get_library_analysis_service),
) -> ProposalsStatusResponse:
    status = service.get_proposals_status(user.id)
    return ProposalsStatusResponse(
        proposals_status=status.proposals_status,
        proposals_processed_count=status.proposals_processed_count,
        proposals_total_count=status.proposals_total_count,
        proposals=[
            PlaylistProposalResponse(
                name=p.name, theme=p.theme, song_count=p.song_count, confidence=1.0
            )
            for p in status.proposals
        ],
    )


@router.post("/select", response_model=SelectionResponse)
def select(
    body: SelectionRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_default_user),
    service: LibraryAnalysisService = Depends(get_library_analysis_service),
) -> SelectionResponse:
    try:
        created = service.complete_onboarding(
            user_id=user.id,
            accepted_proposals=[p.model_dump() for p in body.accepted_proposals],
            custom_playlists=[c.model_dump() for c in body.custom_playlists],
            adopted_playlists=[a.model_dump() for a in body.adopted_playlists],
            removed_playlist_ids=body.removed_playlist_ids,
            confirmed_removed_playlist_ids=body.confirmed_removed_playlist_ids,
        )
    except PlaylistRemovalRequiresConfirmationError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "removal_requires_confirmation",
                "playlist_id": exc.playlist_id,
                "playlist_name": exc.playlist_name,
                "pending_count": exc.pending_count,
            },
        )
    # R4: confirming playlist selection here must automatically start
    # library classification -- no separate manual "Load new songs" action
    # -- for both first-time setup and any later re-confirm (e.g.
    # adding/removing a playlist). KTD8: complete_onboarding above already
    # handles both cases through the same call, so this single unconditional
    # trigger covers both without branching on whether onboarding was
    # already completed. Claims the same synchronous pre-flight
    # `/ingestion/check` itself uses (trigger_ingestion_check) before
    # scheduling the background task -- without it, `ingestion_status` never
    # flips to "in_progress" for this trigger path, silently breaking both
    # R10's live indicator and the double-run guard a manual "Load new
    # songs" click relies on. A run already in progress (e.g. this is a
    # later re-confirm while an earlier one is still going) isn't fatal to
    # finishing setup -- swallow it the same way triggerReorganizeMatching's
    # own best-effort call is treated on the frontend.
    try:
        trigger_ingestion_check(service.user_repository, user.id)
    except IngestionAlreadyInProgressError:
        pass
    else:
        background_tasks.add_task(run_ingestion_check_to_completion, user.id)
    return SelectionResponse(
        created_playlists=[
            CreatedPlaylistResponse(id=pl.id, name=pl.name, description=pl.description)
            for pl in created
        ]
    )


class ReorganizeTriggerResponse(BaseModel):
    session_id: int
    clustering_status: str


class ReorganizeProposalResponse(BaseModel):
    name: str
    theme: str
    song_count: int


class ReorganizeStatusResponse(BaseModel):
    session_id: int
    clustering_status: str
    proposals: list[ReorganizeProposalResponse]
    enriched_count: int
    total_count: int
    matching_status: str
    matched_count: int


@router.post("/reorganize", response_model=ReorganizeTriggerResponse)
def trigger_reorganize(
    background_tasks: BackgroundTasks,
    user: User = Depends(get_default_user),
    service: LibraryAnalysisService = Depends(get_library_analysis_service),
) -> ReorganizeTriggerResponse:
    """R1/R2/R3: fetches the complete liked-songs library, reuses-or-creates
    the user's open reorganize session, and starts clustering as a
    background task (KTD9) -- the response returns immediately with the
    session id so the frontend can start polling `/reorganize/{id}`.
    """
    reorganize_session, liked_songs = service.trigger_reorganize(user.id)
    background_tasks.add_task(run_reorganize_clustering, reorganize_session.id, liked_songs)
    return ReorganizeTriggerResponse(
        session_id=reorganize_session.id, clustering_status=reorganize_session.clustering_status
    )


@router.get("/reorganize/{session_id}", response_model=ReorganizeStatusResponse)
def get_reorganize_status(
    session_id: int,
    service: LibraryAnalysisService = Depends(get_library_analysis_service),
) -> ReorganizeStatusResponse:
    status = service.get_reorganize_status(session_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"reorganize session {session_id} not found")
    return ReorganizeStatusResponse(
        session_id=status.session_id,
        clustering_status=status.clustering_status,
        proposals=[
            ReorganizeProposalResponse(name=p.name, theme=p.theme, song_count=p.song_count)
            for p in status.proposals
        ],
        enriched_count=status.enriched_count,
        total_count=status.total_count,
        matching_status=status.matching_status,
        matched_count=status.matched_count,
    )


class MatchTriggerResponse(BaseModel):
    session_id: int
    matching_status: str


@router.post("/reorganize/{session_id}/match", response_model=MatchTriggerResponse)
def match_reorganize(
    session_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_default_user),
    deps: ReorganizeMatchingDependencies = Depends(get_reorganize_matching_dependencies),
) -> MatchTriggerResponse:
    """U4/R6/R7: matches the session's full snapshot against candidate
    playlists as a background task (mirrors clustering's KTD9 pattern) --
    the response returns immediately with matching_status="in_progress" so
    the frontend can switch to the Review Queue and poll `/reorganize/{id}`
    for live progress there instead of babysitting the whole run itself.
    Rejected, like the apply trigger, while this session's own matching is
    already running (a rapid double-click on "Finish setup" used to fire
    two overlapping runs that raced to create the same LibraryItem).
    """
    try:
        reorganize_session = trigger_matching(deps.reorganize_session_repository, session_id, user.id)
    except ReorganizeSessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except MatchingAlreadyInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    background_tasks.add_task(run_reorganize_matching, session_id, user.id)
    return MatchTriggerResponse(
        session_id=reorganize_session.id, matching_status=reorganize_session.matching_status
    )


class ApplyTriggerResponse(BaseModel):
    session_id: int
    apply_status: str


class ApplyStatusResponse(BaseModel):
    session_id: int
    apply_status: str
    apply_last_result: Optional[dict]


@router.post("/reorganize/{session_id}/apply", response_model=ApplyTriggerResponse)
def apply_reorganize(
    session_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_default_user),
    music_client: MusicServiceClient = Depends(get_music_client),
    deps: ReorganizeMatchingDependencies = Depends(get_reorganize_matching_dependencies),
) -> ApplyTriggerResponse:
    """R9/R10/R11: creates any missing playlists and writes every
    approved_pending_apply item, best-effort, as a background task (KTD9).
    Can be triggered at any point in a session and applies whatever has been
    decided so far (R10) -- rejected outright if this session's own apply is
    already running (KTD10).
    """
    try:
        reorganize_session = trigger_apply(deps.reorganize_session_repository, session_id, user.id)
    except ReorganizeSessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ApplyAlreadyInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    background_tasks.add_task(run_finish_and_apply, session_id, user.id, music_client)
    return ApplyTriggerResponse(session_id=reorganize_session.id, apply_status=reorganize_session.apply_status)


@router.get("/reorganize/{session_id}/apply-status", response_model=ApplyStatusResponse)
def get_apply_status(
    session_id: int,
    deps: ReorganizeMatchingDependencies = Depends(get_reorganize_matching_dependencies),
) -> ApplyStatusResponse:
    reorganize_session = deps.reorganize_session_repository.get(session_id)
    if reorganize_session is None:
        raise HTTPException(status_code=404, detail=f"reorganize session {session_id} not found")
    return ApplyStatusResponse(
        session_id=reorganize_session.id,
        apply_status=reorganize_session.apply_status,
        apply_last_result=reorganize_session.apply_last_result,
    )


class CancelReorganizeResponse(BaseModel):
    session_id: int
    clustering_status: str


@router.post("/reorganize/{session_id}/cancel", response_model=CancelReorganizeResponse)
def cancel_reorganize(
    session_id: int,
    user: User = Depends(get_default_user),
    service: LibraryAnalysisService = Depends(get_library_analysis_service),
) -> CancelReorganizeResponse:
    """Lets the user abandon an open session instead of finishing it --
    every non-terminal session-tagged item is discarded (never written to
    YouTube) and the session is marked cancelled. Rejected, like the apply
    trigger, while this session's own apply is already running."""
    try:
        reorganize_session = service.cancel_reorganize(session_id, user.id)
    except ReorganizeSessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ApplyAlreadyInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return CancelReorganizeResponse(
        session_id=reorganize_session.id, clustering_status=reorganize_session.clustering_status
    )
