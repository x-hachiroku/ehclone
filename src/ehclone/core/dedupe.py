"""
Dedupe galleries by vector embedding and metadata

Only include galleries that:
    - Having a vector embedding
    - Rating at least `filter.min_rating`
    - `updated_at` set after metadata was fetched at the filtering age
    - `dupe_with = NULL`. Checking the winner from the previous groups is enough

Two galleries are considered dupe only if:
    - The vector embeddings has a cosine distance below `filter.dedupe.distance_threshold`
    - Same language. Both having no language tag are considered same language
    - Page difference less than max(page_tolerance, ceil(base filecount * page_tolerance_rate)),
      plus page_tolerance_ad when either gallery has the other:extraneous ads tag
    - At least one of these metadata conditions holds:
        - Both have no artist and no group
        - Both have exactly one group, and it is the same group
        - Both have the same unordered set of 1-3 artists
        - Both have more than 3 artists

Local score is computed by:
    `gallery.rating` * `rating_factor`
    + `gallery.filecount` * `page_count_factor` (each extra page earns an extra `page_count_factor`)
    + sum over tag biases
    + expunged_bias, if expunged

For each gallery in gallery ID ascending order, search up to
`filter.dedupe.candidate_limit` nearest eligible galleries using approximate
cosine HNSW search, then group it with candidates passing the distance and metadata
checks. Approximate search and the candidate cap can miss duplicates.
Only unchecked bases or bases with refreshed metadata are processed; previously
checked winners remain in the comparison pool. A successful comparison records
deduped_at on the base, including when no duplicates are found.
The highest score in the group keeps `dupe_with = NULL`, and set `dupe_with` to it for the rest,
and update recursively for all galleries having `dupe_with` point to rest.
"""
from __future__ import annotations

import numpy as np

from sqlalchemy.orm import joinedload

from ehclone.config import config
from ehclone.logger import logger
from ehclone.db.entities import Gallery
from ehclone.db.session import session_generator
from ehclone.db.crud.dedupe import (
    build_pending_query,
    expand_dupe_links,
    iter_base_candidates,
    fetch_dupe_pool,
    persist_dedupe_resolution,
)


def _normalize_vector(vector):
    if vector is None:
        return None
    try:
        arr = np.asarray(vector, dtype=float)
    except Exception as exc:
        logger.error(f"Invalid vector values: {exc}")
        return None

    if arr.ndim != 1 or arr.size == 0 or not np.all(np.isfinite(arr)):
        return None

    norm = np.linalg.norm(arr)
    if not np.isfinite(norm) or norm <= 0:
        return None

    normalized = (arr / norm).tolist()
    return normalized


def _tag_names(gallery: Gallery, namespace: str) -> set[str]:
    return {t.name for t in getattr(gallery, 'tags', []) if t.namespace == namespace}


def _ensure_vector(gallery: Gallery) -> list[float] | None:
    if gallery.thumb is not None and gallery.thumb.mobile_net_v3 is not None:
        return _normalize_vector(gallery.thumb.mobile_net_v3)
    return None


def rate_gallery(gallery: Gallery) -> float:
    base = (gallery.rating or 0) * 0.01

    dedupe_cfg = config.filter.dedupe
    score = base * dedupe_cfg.rating_factor

    if getattr(gallery, 'filecount', None):
        score += gallery.filecount * dedupe_cfg.page_count_factor

    if gallery.expunged or any(
        f'{tag.namespace}:{tag.name}' in dedupe_cfg.expunged_tags for tag in gallery.tags
    ):
        score += dedupe_cfg.expunged_bias

    for tag in getattr(gallery, 'tags', []) or []:
        key = f"{getattr(tag, 'namespace', '')}:{getattr(tag, 'name', '')}"
        score += dedupe_cfg.tag_biases.get(key, 0)

    return score


def _matches_dupe_metadata(base: Gallery, candidate: Gallery) -> bool:
    """Match language and any documented artist/group condition pairwise."""
    if _tag_names(base, 'language') != _tag_names(candidate, 'language'):
        return False

    base_groups = _tag_names(base, 'group')
    candidate_groups = _tag_names(candidate, 'group')
    if len(base_groups) == 1 and base_groups == candidate_groups:
        return True

    base_artists = _tag_names(base, 'artist')
    candidate_artists = _tag_names(candidate, 'artist')
    if not base_artists and not candidate_artists:
        return not base_groups and not candidate_groups
    if len(base_artists) > 3 and len(candidate_artists) > 3:
        return True
    return base_artists == candidate_artists


def _select_winner(galleries: list[Gallery]) -> Gallery:
    return min(galleries, key=lambda g: (-rate_gallery(g), g.gid))


def _collect_dupe_group(session, gallery: Gallery) -> list[Gallery]:
    """Compare current winners against this gallery's vector and metadata."""
    vector = _ensure_vector(gallery)
    if vector is None or gallery.filecount is None:
        return []
    return [gallery, *(
        candidate for candidate in fetch_dupe_pool(session, gallery, vector)
        if _matches_dupe_metadata(gallery, candidate)
    )]


def _process_gallery(gid: int) -> set[int]:
    with session_generator() as session:
        gallery = (
            build_pending_query(session)
            .options(joinedload(Gallery.tags), joinedload(Gallery.thumb))
            .filter(Gallery.gid == gid)
            .one_or_none()
        )
        if gallery is None:
            return set()

        group_list = _collect_dupe_group(session, gallery)
        if not group_list:
            return set()
        winner = _select_winner(group_list)
        group_list.extend(expand_dupe_links(session, {g.gid for g in group_list}))
        persist_dedupe_resolution(session, winner.gid, group_list, gallery.gid)
        return {g.gid for g in group_list}


def run() -> None:
    if not config.filter.dedupe.enabled:
        logger.warning('Filter dedupe is disabled.')
        return

    for gid, token in iter_base_candidates():
        with logger.contextualize(gid=gid, token=token):
            try:
                _process_gallery(gid)
            except Exception:
                logger.exception('Failed to deduplicate gallery')


if __name__ == '__main__':
    run()
