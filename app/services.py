"""Application services: candidate creation, publishing and read models."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import storage
from .db import transaction
from .validation import Observation

PER_PAGE_DEFAULT = 100


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Conflict(Exception):
    def __init__(self, newer: sqlite3.Row) -> None:
        self.newer = newer
        super().__init__("该日期已存在更新版本")


@dataclass
class PublishOutcome:
    status: str
    message: str = ""
    version: sqlite3.Row | None = None
    newer: sqlite3.Row | None = None
    error: Exception | None = None


@dataclass
class WithdrawOutcome:
    status: str
    message: str = ""
    version: sqlite3.Row | None = None
    published: sqlite3.Row | None = None
    error: Exception | None = None


def withdrawal_counter(conn: sqlite3.Connection, dataset_date: str) -> int:
    """Return a monotonic counter of withdrawals recorded for a date.

    The counter is the highest withdrawal audit id for the date.  Candidates
    remember the counter value at creation time; publishing is refused once
    the date's counter has moved on, which is exactly when a withdrawal was
    committed after the candidate was created.
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS seq FROM withdrawals WHERE dataset_date = ?",
        (dataset_date,),
    ).fetchone()
    return int(row["seq"])


def create_candidate(
    conn: sqlite3.Connection,
    dataset_date: date,
    original_filename: str,
    stored: storage.StoredFile,
    observations: Sequence[Observation],
) -> int:
    """Insert a validated candidate version and its observations atomically."""
    now = utc_now()
    with transaction(conn):
        row = conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 AS next_no "
            "FROM versions WHERE dataset_date = ?",
            (dataset_date.isoformat(),),
        ).fetchone()
        version_no = int(row["next_no"])
        withdrawal_seq = withdrawal_counter(conn, dataset_date.isoformat())
        cursor = conn.execute(
            """
            INSERT INTO versions (
                dataset_date, version_no, status, original_filename,
                stored_path, sha256, size_bytes, row_count, created_at,
                withdrawal_seq
            ) VALUES (?, ?, 'candidate', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dataset_date.isoformat(),
                version_no,
                original_filename,
                stored.relative_path,
                stored.sha256,
                stored.size_bytes,
                len(observations),
                now,
                withdrawal_seq,
            ),
        )
        version_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO observations (
                version_id, row_no, site, metric, observed_at,
                observed_date, value, value_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    version_id,
                    item.row_no,
                    item.site,
                    item.metric,
                    item.observed_at,
                    item.observed_date,
                    item.value,
                    item.value_text,
                )
                for item in observations
            ],
        )
    return version_id


def publish_candidate(
    conn: sqlite3.Connection,
    files_dir: str | Path,
    version_id: int,
    *,
    now: str | None = None,
) -> PublishOutcome:
    """Publish a candidate unless a newer version for its date exists.

    The original file is verified *before* the transaction.  Files are
    content-addressed and immutable, so a verified file cannot change while
    the transaction replaces the current published version.  The update is
    one transaction: either the new version becomes the single published
    version for the date, or nothing changes and the previous published
    version stays fully usable.
    """
    candidate = conn.execute(
        "SELECT * FROM versions WHERE id = ?", (version_id,)
    ).fetchone()
    if candidate is None:
        return PublishOutcome("not_found", "版本不存在")

    blocked = _publishability_problem(candidate)
    if blocked is not None:
        return blocked

    # A cheap pre-check gives the conflict message priority over file errors.
    newer = _latest_version_for_date(conn, candidate["dataset_date"])
    if newer is not None and newer["version_no"] > candidate["version_no"]:
        return PublishOutcome(
            "conflict",
            "该日期已存在更新版本，旧候选不能发布",
            candidate,
            newer,
        )

    # Candidates formed before a withdrawal must never bring the date back
    # online; only a fresh upload after the withdrawal can be published.
    if withdrawal_counter(conn, candidate["dataset_date"]) != candidate["withdrawal_seq"]:
        return PublishOutcome(
            "withdrawn",
            "该日期已撤回，撤回前形成的候选不能发布；请重新上传并发布。",
            candidate,
        )

    try:
        storage.verify_file(
            files_dir, candidate["stored_path"], candidate["sha256"]
        )
    except storage.StorageError as exc:
        return PublishOutcome("file_error", str(exc), candidate, error=exc)

    timestamp = now or utc_now()
    try:
        with transaction(conn):
            # Re-read under the write lock: another request may have created
            # a newer version, published this candidate, or withdrawn the
            # date meanwhile.  Whichever commits first decides the outcome.
            current = conn.execute(
                "SELECT * FROM versions WHERE id = ?", (version_id,)
            ).fetchone()
            if current is None:
                return PublishOutcome("not_found", "版本不存在")
            blocked = _publishability_problem(current)
            if blocked is not None:
                return blocked
            latest = _latest_version_for_date(conn, current["dataset_date"])
            if latest is not None and latest["version_no"] > current["version_no"]:
                raise Conflict(latest)
            if (
                withdrawal_counter(conn, current["dataset_date"])
                != current["withdrawal_seq"]
            ):
                return PublishOutcome(
                    "withdrawn",
                    "该日期已撤回，撤回前形成的候选不能发布；请重新上传并发布。",
                    current,
                )

            conn.execute(
                """
                UPDATE versions
                   SET status = 'superseded', superseded_at = ?
                 WHERE dataset_date = ? AND status = 'published'
                """,
                (timestamp, current["dataset_date"]),
            )
            conn.execute(
                """
                UPDATE versions
                   SET status = 'published', published_at = ?, superseded_at = NULL
                 WHERE id = ?
                """,
                (timestamp, version_id),
            )
    except Conflict as exc:
        return PublishOutcome(
            "conflict", "该日期已存在更新版本，旧候选不能发布", candidate, exc.newer
        )
    except sqlite3.Error as exc:
        return PublishOutcome(
            "db_error",
            "数据库提交失败，原有已发布版本保持不变",
            candidate,
            error=exc,
        )

    published = conn.execute(
        "SELECT * FROM versions WHERE id = ?", (version_id,)
    ).fetchone()
    return PublishOutcome(
        "published", "发布成功，版本已对外可见", published
    )


def _publishability_problem(row: sqlite3.Row) -> PublishOutcome | None:
    if row["status"] == "published":
        return PublishOutcome("already_published", "该版本已是当前发布版本", row)
    if row["status"] == "withdrawn":
        return PublishOutcome(
            "withdrawn", "该版本已撤回，不能重新发布；请重新上传并通过审核。", row
        )
    if row["status"] != "candidate":
        return PublishOutcome("not_candidate", "该版本不是待发布候选", row)
    return None


def _latest_version_for_date(
    conn: sqlite3.Connection, dataset_date: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM versions
         WHERE dataset_date = ?
         ORDER BY version_no DESC
         LIMIT 1
        """,
        (dataset_date,),
    ).fetchone()


def get_version(conn: sqlite3.Connection, version_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM versions WHERE id = ?", (version_id,)
    ).fetchone()


def withdraw_version(
    conn: sqlite3.Connection,
    version_id: int,
    reason: str,
    *,
    now: str | None = None,
) -> WithdrawOutcome:
    """Withdraw the published version for a date, taking it offline.

    Only the version the administrator is looking at can be withdrawn; if a
    newer version has been published by the time the transaction commits, the
    request is rejected as a stale-page conflict and the current published
    version is left untouched.  Withdrawal, upload and publishing are all
    serialised by ``BEGIN IMMEDIATE``, so the first commit wins.

    The audit row and the status change happen in one transaction: a failed
    withdrawal never changes which version is public, and a repeated
    withdrawal creates no second audit row.
    """
    reason = (reason or "").strip()
    if not reason:
        return WithdrawOutcome("reason_required", "请填写撤回原因。")
    if len(reason) > 500:
        return WithdrawOutcome("reason_too_long", "撤回原因不能超过 500 个字符。")

    timestamp = now or utc_now()
    try:
        with transaction(conn):
            current = conn.execute(
                "SELECT * FROM versions WHERE id = ?", (version_id,)
            ).fetchone()
            if current is None:
                return WithdrawOutcome("not_found", "版本不存在")
            if current["status"] == "withdrawn":
                published = get_current_published(conn, current["dataset_date"])
                if published is not None and published["id"] != current["id"]:
                    # The form came from a page that predates both the
                    # withdrawal and the later republication.
                    return WithdrawOutcome(
                        "conflict",
                        (
                            "页面已过期：该日期已重新发布版本 "
                            f"v{published['version_no']}，"
                            "当前公开版本保持不变，请刷新后重试。"
                        ),
                        current,
                        published,
                    )
                return WithdrawOutcome(
                    "already_withdrawn", "该版本已经撤回，无需重复操作。", current
                )
            if current["status"] == "candidate":
                return WithdrawOutcome(
                    "not_published",
                    "只有当前发布版本可以撤回。",
                    current,
                )
            published = get_current_published(conn, current["dataset_date"])
            if current["status"] != "published" or (
                published is not None and published["id"] != current["id"]
            ):
                # The form came from an out-of-date page: a newer version has
                # since been published (or the date was withdrawn).  Nothing
                # changes; the current public version stays exactly as it is.
                return WithdrawOutcome(
                    "conflict",
                    (
                        "页面已过期：该版本已不是当前发布版本，"
                        "当前公开版本保持不变，请刷新后重试。"
                    ),
                    current,
                    published,
                )
            if published is None:
                return WithdrawOutcome(
                    "not_published",
                    "该日期当前没有已发布版本。",
                    current,
                )

            cursor = conn.execute(
                """
                INSERT INTO withdrawals (version_id, dataset_date, reason, withdrawn_at)
                VALUES (?, ?, ?, ?)
                """,
                (current["id"], current["dataset_date"], reason, timestamp),
            )
            update = conn.execute(
                """
                UPDATE versions
                   SET status = 'withdrawn',
                       withdrawn_at = ?,
                       withdrawal_reason = ?,
                       withdrawal_seq = ?
                 WHERE id = ? AND status = 'published'
                """,
                (timestamp, reason, int(cursor.lastrowid), version_id),
            )
            if update.rowcount != 1:
                # Should be impossible under the write lock; abort the whole
                # transaction rather than record a withdrawal that did not
                # apply, so the public version is left untouched.
                raise sqlite3.IntegrityError("withdrawal target is no longer published")
    except sqlite3.Error as exc:
        return WithdrawOutcome(
            "db_error",
            "数据库提交失败，原已发布版本保持不变。",
            error=exc,
        )

    withdrawn = get_version(conn, version_id)
    return WithdrawOutcome(
        "withdrawn",
        "已撤回该日期的发布版本，公开入口不再提供该日数据。",
        withdrawn,
    )


def get_withdrawal(conn: sqlite3.Connection, version_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM withdrawals WHERE version_id = ?", (version_id,)
    ).fetchone()



def get_current_published(
    conn: sqlite3.Connection, dataset_date: str
) -> sqlite3.Row | None:
    """Return the single externally visible version for a date."""
    return conn.execute(
        """
        SELECT * FROM versions
         WHERE dataset_date = ? AND status = 'published'
         ORDER BY version_no DESC
         LIMIT 1
        """,
        (dataset_date,),
    ).fetchone()


def list_published(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM versions
         WHERE status = 'published'
         ORDER BY dataset_date DESC
        """
    ).fetchall()


def list_versions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM versions
         ORDER BY dataset_date DESC, version_no DESC
        """
    ).fetchall()


def build_admin_groups(versions: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for version in versions:
        day = version["dataset_date"]
        group = groups.setdefault(
            day,
            {
                "dataset_date": day,
                "versions": [],
                "latest_no": 0,
                "published": None,
                "withdrawn": None,
            },
        )
        group["versions"].append(version)
        group["latest_no"] = max(group["latest_no"], version["version_no"])
        if version["status"] == "published":
            group["published"] = version
        elif version["status"] == "withdrawn":
            # Versions are ordered newest first, so the first withdrawn row is
            # the most recent withdrawal for the date.
            group["withdrawn"] = group["withdrawn"] or version
    return list(groups.values())


def fetch_observations(
    conn: sqlite3.Connection,
    version_id: int,
    *,
    site: str = "",
    metric: str = "",
    page: int = 1,
    per_page: int = PER_PAGE_DEFAULT,
) -> dict[str, Any]:
    conditions = ["version_id = ?"]
    params: list[Any] = [version_id]
    if site:
        conditions.append("site LIKE ?")
        params.append(f"%{site}%")
    if metric:
        conditions.append("metric LIKE ?")
        params.append(f"%{metric}%")
    where = " AND ".join(conditions)

    total = int(
        conn.execute(
            f"SELECT COUNT(*) FROM observations WHERE {where}", params
        ).fetchone()[0]
    )
    per_page = max(1, min(int(per_page), 500))
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(int(page), pages))
    rows = conn.execute(
        f"""
        SELECT * FROM observations
         WHERE {where}
         ORDER BY site, metric, observed_at, row_no
         LIMIT ? OFFSET ?
        """,
        [*params, per_page, (page - 1) * per_page],
    ).fetchall()
    return {
        "rows": rows,
        "total": total,
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "site": site,
        "metric": metric,
    }


def parse_page(value: str | None) -> int:
    try:
        return max(1, int(value or "1"))
    except (TypeError, ValueError):
        return 1
