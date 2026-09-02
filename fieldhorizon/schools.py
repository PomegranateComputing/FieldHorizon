from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from .config import AppConfig
from .db import connect
from .embeddings import load_cycle_embedding
from .events import (
    ACTOR_CLI,
    AGGREGATE_SCHOOL,
    EVT_SCHOOL_NAMED,
    EVT_SCHOOLS_COMPLETED,
    EVT_SCHOOLS_FAILED,
    EVT_SCHOOLS_STARTED,
    OperationEmitter,
)
from .interpreter import extract_json_object
from .llm import call_ollama
from .manifests import record_operation_manifest
from .provenance import record_member_of_edges, record_supersedes_edge

logger = logging.getLogger(__name__)

MIN_CYCLES_TO_CLUSTER = 4
DEFAULT_K_MIN = 2
DEFAULT_K_MAX = 8
NAMING_SAMPLE_SIZE = 5

_SCHOOL_NAMING_PROMPT = """The following are fragments from the same doctrinal cluster -- statements
that landed closest to each other in meaning among everything canonized so
far. Name this school of thought and summarize its shared doctrine in
exactly two sentences.

Fragments:
{fragments}

Return JSON ONLY, exactly this shape, no markdown, no commentary:
{{"name": "...", "summary": "..."}}
"""


def _normalize_rows(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return vectors / norms


def _kmeans(vectors: np.ndarray, k: int, seed: int, max_iter: int = 100) -> tuple[np.ndarray, np.ndarray]:
    """
    Deterministic k-means++ given a fixed seed: same vectors + k + seed
    always produce the same labels. Runs on L2-normalized rows, so
    Euclidean distance here is monotonic with cosine distance
    (||a-b||^2 = 2 - 2*cos(a,b) for unit vectors) -- this is cosine
    clustering without needing a custom distance metric in the loop.
    Returns (labels, centroids).
    """
    normed = _normalize_rows(vectors)
    n = normed.shape[0]
    rng = np.random.RandomState(seed)

    centroids = np.empty((k, normed.shape[1]))
    centroids[0] = normed[rng.randint(n)]
    for i in range(1, k):
        dists = np.min(
            [np.sum((normed - centroids[j]) ** 2, axis=1) for j in range(i)], axis=0
        )
        total = dists.sum()
        probs = dists / total if total > 0 else np.full(n, 1.0 / n)
        centroids[i] = normed[rng.choice(n, p=probs)]

    labels = np.full(n, -1, dtype=int)
    for _iteration in range(max_iter):
        distances = np.array([np.sum((normed - centroids[j]) ** 2, axis=1) for j in range(k)])
        new_labels = np.argmin(distances, axis=0)
        if np.array_equal(new_labels, labels):
            break
        labels = new_labels
        for j in range(k):
            members = normed[labels == j]
            if len(members) > 0:
                centroids[j] = members.mean(axis=0)

    return labels, centroids


def _cosine_distance_matrix(vectors: np.ndarray) -> np.ndarray:
    normed = _normalize_rows(vectors)
    return 1.0 - (normed @ normed.T)


def silhouette_score(distance_matrix: np.ndarray, labels: np.ndarray) -> float:
    """Mean silhouette coefficient over cosine distances. -1.0 (worst) if fewer than 2 clusters are populated."""
    unique_labels = np.unique(labels)
    if len(unique_labels) < 2:
        return -1.0

    n = len(labels)
    scores = np.zeros(n)
    for i in range(n):
        own = labels[i]
        same_mask = labels == own
        same_mask[i] = False

        a = distance_matrix[i, same_mask].mean() if same_mask.any() else 0.0
        b = min(
            distance_matrix[i, labels == other].mean()
            for other in unique_labels
            if other != own and (labels == other).any()
        )
        scores[i] = (b - a) / max(a, b) if max(a, b) > 0 else 0.0

    return float(scores.mean())


def select_k_and_cluster(
    vectors: np.ndarray, seed: int, k_min: int = DEFAULT_K_MIN, k_max: int = DEFAULT_K_MAX
) -> tuple[int, np.ndarray, np.ndarray]:
    """Tries k in [k_min, min(k_max, n-1)], picks the k with the highest silhouette score. Returns (k, labels, centroids)."""
    n = vectors.shape[0]
    k_max = min(k_max, n - 1)

    if k_max < k_min:
        # Too few points to form more than one meaningful cluster.
        labels = np.zeros(n, dtype=int)
        return 1, labels, _normalize_rows(vectors).mean(axis=0, keepdims=True)

    dist_matrix = _cosine_distance_matrix(vectors)
    best: tuple[int, np.ndarray, np.ndarray, float] | None = None

    for k in range(k_min, k_max + 1):
        labels, centroids = _kmeans(vectors, k, seed)
        score = silhouette_score(dist_matrix, labels)
        if best is None or score > best[3]:
            best = (k, labels, centroids, score)

    assert best is not None
    return best[0], best[1], best[2]


def build_school_naming_prompt(member_fragments: list[str]) -> str:
    fragments_block = "\n\n---\n\n".join(fragment[:600] for fragment in member_fragments)
    return _SCHOOL_NAMING_PROMPT.format(fragments=fragments_block)


def name_school(cfg: AppConfig, member_fragments: list[str], model: str | None = None) -> dict:
    """One temperature-0 call naming a cluster and summarizing its shared doctrine in two sentences."""
    if not member_fragments:
        raise ValueError("cannot name a school with no member fragments")

    raw = call_ollama(cfg, build_school_naming_prompt(member_fragments), model=model, options={"temperature": 0})
    data = extract_json_object(raw)

    name = str(data.get("name", "")).strip() or "Unnamed School"
    summary = str(data.get("summary", "")).strip()
    return {"name": name, "summary": summary}


def _active_canon_cycle_ids_with_embeddings(cfg: AppConfig) -> list[int]:
    with connect(cfg.database) as conn:
        rows = conn.execute(
            "SELECT id FROM cycles WHERE verdict = 'CANON' AND dry_run = 0 AND retired_at IS NULL ORDER BY id ASC"
        ).fetchall()

    ids = [int(row["id"]) for row in rows]
    return [cid for cid in ids if load_cycle_embedding(cfg, cid) is not None]


def _fragments_for(cfg: AppConfig, cycle_ids: list[int]) -> list[str]:
    if not cycle_ids:
        return []
    with connect(cfg.database) as conn:
        placeholders = ",".join("?" for _ in cycle_ids)
        rows = conn.execute(f"SELECT fragment FROM cycles WHERE id IN ({placeholders})", cycle_ids).fetchall()
    return [row["fragment"] for row in rows if row["fragment"]]


def _best_overlapping_school(cfg: AppConfig, member_cycle_ids: set[int]) -> int | None:
    """
    Lineage between re-clustering runs: the existing school whose member
    set has the highest Jaccard overlap with this new cluster, if any
    overlap at all. Schools are purely descriptive labels -- this lineage
    link exists only so a re-run's dashboard can say "formerly X," not to
    feed anything back into retrieval or evaluation.
    """
    with connect(cfg.database) as conn:
        school_ids = [int(row["id"]) for row in conn.execute("SELECT id FROM schools").fetchall()]

    best_id: int | None = None
    best_overlap = 0.0
    for school_id in school_ids:
        with connect(cfg.database) as conn:
            members = {
                int(row["cycle_id"])
                for row in conn.execute(
                    "SELECT cycle_id FROM school_members WHERE school_id = ?", (school_id,)
                ).fetchall()
            }
        if not members:
            continue
        union = members | member_cycle_ids
        overlap = len(members & member_cycle_ids) / len(union) if union else 0.0
        if overlap > best_overlap:
            best_id, best_overlap = school_id, overlap

    return best_id


@dataclass(frozen=True)
class SchoolResult:
    school_id: int
    name: str
    summary: str
    member_cycle_ids: list[int]
    previous_school_id: int | None


@dataclass(frozen=True)
class ActiveSchool:
    id: int
    name: str
    summary: str
    member_count: int
    previous_school_id: int | None


def latest_schools(cfg: AppConfig) -> list[ActiveSchool]:
    """The most recent run_schools() run's schools -- "active schools" for the dashboard."""
    with connect(cfg.database) as conn:
        latest_run = conn.execute("SELECT MAX(run_at) AS run_at FROM schools").fetchone()["run_at"]
        if latest_run is None:
            return []

        rows = conn.execute(
            """
            SELECT s.id, s.name, s.summary, s.previous_school_id, COUNT(m.cycle_id) AS member_count
            FROM schools s
            LEFT JOIN school_members m ON m.school_id = s.id
            WHERE s.run_at = ?
            GROUP BY s.id
            ORDER BY member_count DESC
            """,
            (latest_run,),
        ).fetchall()

    return [
        ActiveSchool(
            id=int(row["id"]),
            name=row["name"],
            summary=row["summary"],
            member_count=int(row["member_count"]),
            previous_school_id=row["previous_school_id"],
        )
        for row in rows
    ]


@dataclass(frozen=True)
class SchoolRunEntry:
    """One school as it existed in one specific run -- unlike ActiveSchool (latest run only), this is every run ever computed."""

    id: int
    name: str
    summary: str
    member_count: int
    previous_school_id: int | None
    run_at: str


def all_schools(cfg: AppConfig) -> list[SchoolRunEntry]:
    """Every school from every run_schools() run ever, newest run first -- "lineage across clustering runs" for the explorer."""
    with connect(cfg.database) as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.name, s.summary, s.previous_school_id, s.run_at, COUNT(m.cycle_id) AS member_count
            FROM schools s
            LEFT JOIN school_members m ON m.school_id = s.id
            GROUP BY s.id
            ORDER BY s.run_at DESC, member_count DESC
            """
        ).fetchall()

    return [
        SchoolRunEntry(
            id=int(row["id"]),
            name=row["name"],
            summary=row["summary"],
            member_count=int(row["member_count"]),
            previous_school_id=row["previous_school_id"],
            run_at=row["run_at"],
        )
        for row in rows
    ]


def school_member_cycle_ids(cfg: AppConfig, school_id: int) -> list[int]:
    with connect(cfg.database) as conn:
        rows = conn.execute("SELECT cycle_id FROM school_members WHERE school_id = ?", (school_id,)).fetchall()
    return [int(row["cycle_id"]) for row in rows]


@dataclass(frozen=True)
class SchoolMember:
    cycle_id: int
    query: str
    fragment: str
    verdict: str
    created_at: str
    distance: float


def school_members_detail(cfg: AppConfig, school_id: int) -> list[SchoolMember]:
    """Membership with enough real detail to show, sorted closest-to-centroid first -- the school's own most representative members."""
    with connect(cfg.database) as conn:
        rows = conn.execute(
            """
            SELECT c.id AS cycle_id, c.query, c.fragment, c.verdict, c.created_at, m.distance
            FROM school_members m
            JOIN cycles c ON c.id = m.cycle_id
            WHERE m.school_id = ?
            ORDER BY m.distance ASC
            """,
            (school_id,),
        ).fetchall()
    return [
        SchoolMember(
            cycle_id=int(row["cycle_id"]),
            query=row["query"],
            fragment=row["fragment"] or "",
            verdict=row["verdict"] or "",
            created_at=row["created_at"],
            distance=float(row["distance"]),
        )
        for row in rows
    ]


@dataclass(frozen=True)
class SchoolDistance:
    school_a_id: int
    school_b_id: int
    distance: float


def school_distances_for_run(cfg: AppConfig, run_at: str) -> list[SchoolDistance]:
    """
    Inter-school distances (FABLE Sec.10.3's "inter-school distances"):
    cosine distance between each pair of schools' own centroids, recomputed
    from their real member embeddings -- centroids are never persisted
    (schools are descriptive labels only, see run_schools's own docstring),
    so this is the honest way to get them rather than storing a second,
    potentially-stale copy.
    """
    with connect(cfg.database) as conn:
        school_ids = [int(row["id"]) for row in conn.execute("SELECT id FROM schools WHERE run_at = ?", (run_at,)).fetchall()]

    centroids: dict[int, np.ndarray] = {}
    for school_id in school_ids:
        member_ids = school_member_cycle_ids(cfg, school_id)
        vectors = [v for v in (load_cycle_embedding(cfg, cid) for cid in member_ids) if v is not None]
        if vectors:
            centroids[school_id] = _normalize_rows(np.array(vectors)).mean(axis=0)

    distances: list[SchoolDistance] = []
    ids_with_centroids = sorted(centroids)
    for i, a in enumerate(ids_with_centroids):
        for b in ids_with_centroids[i + 1 :]:
            cosine_dist = float(1.0 - np.dot(centroids[a], centroids[b]) / (np.linalg.norm(centroids[a]) * np.linalg.norm(centroids[b])))
            distances.append(SchoolDistance(school_a_id=a, school_b_id=b, distance=cosine_dist))

    return distances


def run_schools(
    cfg: AppConfig,
    k: str | int = "auto",
    seed: int = 42,
    model: str | None = None,
    actor: str = ACTOR_CLI,
    correlation_id: str | None = None,
) -> list[SchoolResult]:
    """
    Clusters active canon embeddings into schools of thought (descriptive
    labels only -- never consulted by retrieval or evaluation). Returns []
    (and emits no events at all -- there is no run to watch) if there
    aren't enough embedded canon cycles to cluster meaningfully. Re-running
    re-clusters from scratch and records lineage to the most
    member-overlapping prior school for each new cluster.

    Emits SchoolsStarted/SchoolNamed (one per cluster -- each is its own
    LLM call, the slow step)/SchoolsCompleted (or SchoolsFailed) via the
    same OperationEmitter ingest.py and multicycle.py use, watchable on
    GET /events/stream?run_id=<correlation_id>.
    """
    cycle_ids = _active_canon_cycle_ids_with_embeddings(cfg)
    if len(cycle_ids) < MIN_CYCLES_TO_CLUSTER:
        return []

    emitter = OperationEmitter(cfg, actor=actor, aggregate_type=AGGREGATE_SCHOOL, correlation_id=correlation_id)
    run_id = emitter.correlation_id
    run_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")

    try:
        vectors = np.array([load_cycle_embedding(cfg, cid) for cid in cycle_ids])

        if k == "auto":
            chosen_k, labels, centroids = select_k_and_cluster(vectors, seed)
        else:
            chosen_k = int(k)
            labels, centroids = _kmeans(vectors, chosen_k, seed)

        emitter.emit(EVT_SCHOOLS_STARTED, payload={"cycle_count": len(cycle_ids), "k": chosen_k})

        normed = _normalize_rows(vectors)
        results: list[SchoolResult] = []

        for cluster_label in sorted(set(labels.tolist())):
            member_indices = [i for i, label in enumerate(labels) if label == cluster_label]
            if not member_indices:
                continue

            member_cycle_ids = [cycle_ids[i] for i in member_indices]
            member_fragments = _fragments_for(cfg, member_cycle_ids)
            if not member_fragments:
                continue

            naming = name_school(cfg, member_fragments[:NAMING_SAMPLE_SIZE], model=model)
            previous_school_id = _best_overlapping_school(cfg, set(member_cycle_ids))

            with connect(cfg.database) as conn:
                cur = conn.execute(
                    "INSERT INTO schools(name, summary, previous_school_id, run_at) VALUES (?, ?, ?, ?)",
                    (naming["name"], naming["summary"], previous_school_id, run_at),
                )
                assert cur.lastrowid is not None
                school_id = int(cur.lastrowid)

                for i in member_indices:
                    distance = float(np.linalg.norm(normed[i] - centroids[cluster_label]))
                    conn.execute(
                        "INSERT INTO school_members(school_id, cycle_id, distance) VALUES (?, ?, ?)",
                        (school_id, cycle_ids[i], distance),
                    )
                conn.commit()

            try:
                record_member_of_edges(cfg, member_cycle_ids, school_id, provenance_ref=run_id)
                record_supersedes_edge(cfg, school_id, previous_school_id, provenance_ref=run_id)
            except Exception as exc:
                # Best-effort, matching record_operation_manifest's own recording below.
                logger.warning("Provenance edge recording failed for school %d: %s", school_id, exc)

            emitter.emit(
                EVT_SCHOOL_NAMED,
                aggregate_id=school_id,
                payload={"name": naming["name"], "member_count": len(member_cycle_ids), "previous_school_id": previous_school_id},
            )

            results.append(
                SchoolResult(
                    school_id=school_id,
                    name=naming["name"],
                    summary=naming["summary"],
                    member_cycle_ids=member_cycle_ids,
                    previous_school_id=previous_school_id,
                )
            )

        try:
            record_operation_manifest(
                cfg,
                run_id=run_id,
                operation="schools",
                random_seeds={"seed": seed, "k": chosen_k},
                output_ids=[f"school:{r.school_id}" for r in results],
            )
        except Exception as exc:
            # Best-effort, matching cycle.py/council.py/dream.py's own manifest recording.
            logger.warning("Manifest recording failed for schools run: %s", exc)

        emitter.emit(EVT_SCHOOLS_COMPLETED, payload={"school_count": len(results)})
    except Exception as exc:
        emitter.emit(EVT_SCHOOLS_FAILED, payload={"error_type": type(exc).__name__, "error_message": str(exc)})
        raise

    return results
