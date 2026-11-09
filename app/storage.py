"""Storage usage: sampling drives and S3, agent-reported samples, and the queries behind the charts."""

import logging
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import db
from .config import Config, S3Bucket, StorageConfig

try:
    from botocore.exceptions import BotoCoreError, ClientError

    AWS_ERRORS: tuple = (BotoCoreError, ClientError)
except ImportError:  # boto3 is only needed when S3 buckets are configured
    AWS_ERRORS = ()

logger = logging.getLogger(__name__)

RANGES = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "90d": 90 * 86400, "1y": 365 * 86400}
DEFAULT_RANGE = "30d"
TARGET_POINTS = 300
MIN_STEP_S = 60
# A series that has not reported for this long drops out of the "current usage" list.
CURRENT_MAX_AGE_S = 3 * 86400
DRIVE_TIMEOUT_S = 5
CLOUDWATCH_LOOKBACK_DAYS = 30
MAX_AGENT_SAMPLES = 200
# Ticks arrive a little unevenly, so an interval counts as elapsed slightly early.
DUE_SLACK_S = 60
_KIND_ORDER = {"local": 0, "repo": 1, "cloud": 2}


def _sample(source: str, kind: str, name: str, series: str, ts: float, used: int,
            total: Optional[int]) -> dict:
    return {"source": source, "kind": kind, "name": name, "series": series, "ts": ts,
            "used_bytes": int(used), "total_bytes": None if total is None else int(total)}


# ---------------------------------------------------------------- local drives

def _read_usage(path: str, out: dict) -> None:
    try:
        out["usage"] = shutil.disk_usage(path)
    except OSError as exc:
        out["error"] = exc


def sample_drives(drives: list, now: float, timeout: float = DRIVE_TIMEOUT_S) -> list:
    """Sample every drive at once; one that hangs (a dead network mount) is skipped after `timeout`."""
    pending = []
    for drive in drives:
        out: dict = {}
        thread = threading.Thread(target=_read_usage, args=(drive.path, out), daemon=True)
        thread.start()
        pending.append((drive, thread, out))
    deadline = time.monotonic() + timeout
    samples = []
    for drive, thread, out in pending:
        thread.join(max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            logger.warning("drive '%s' (%s) did not answer within %ss", drive.label, drive.path, timeout)
        elif "error" in out:
            logger.warning("drive '%s' (%s) unreadable: %s", drive.label, drive.path, out["error"])
        else:
            usage = out["usage"]
            samples.append(_sample("dashboard", "local", drive.label, "", now, usage.total - usage.free, usage.total))
    return samples


# ---------------------------------------------------------------- S3

def sample_s3_cloudwatch(cloudwatch, bucket: S3Bucket, now: float,
                         lookback_days: int = CLOUDWATCH_LOOKBACK_DAYS) -> list:
    """Daily bucket size per storage class from CloudWatch (S3 publishes it free, once a day)."""
    dimension = {"Name": "BucketName", "Value": bucket.bucket}
    storage_types = sorted({
        d["Value"]
        for page in cloudwatch.get_paginator("list_metrics").paginate(
            Namespace="AWS/S3", MetricName="BucketSizeBytes", Dimensions=[dimension])
        for metric in page["Metrics"] for d in metric["Dimensions"] if d["Name"] == "StorageType"
    })
    if not storage_types:
        return []
    queries = [
        {"Id": f"q{i}", "ReturnData": True, "MetricStat": {
            "Metric": {"Namespace": "AWS/S3", "MetricName": "BucketSizeBytes",
                       "Dimensions": [dimension, {"Name": "StorageType", "Value": storage_type}]},
            "Period": 86400, "Stat": "Average", "Unit": "Bytes"}}
        for i, storage_type in enumerate(storage_types)
    ]
    end = datetime.fromtimestamp(now, timezone.utc)
    samples = []
    for page in cloudwatch.get_paginator("get_metric_data").paginate(
        MetricDataQueries=queries, StartTime=end - timedelta(days=lookback_days), EndTime=end,
        ScanBy="TimestampAscending",
    ):
        for result in page["MetricDataResults"]:
            storage_type = storage_types[int(result["Id"][1:])]
            for stamp, value in zip(result["Timestamps"], result["Values"]):
                samples.append(_sample("dashboard", "cloud", bucket.label, storage_type, stamp.timestamp(), value, None))
    return samples


def sample_s3_list(s3, bucket: S3Bucket, now: float) -> list:
    """Scan the bucket and size each top-level prefix (the host folder in the agents' key layout)."""
    sizes: dict = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket.bucket):
        for obj in page.get("Contents", []):
            prefix = obj["Key"].split("/", 1)[0] if "/" in obj["Key"] else "(root)"
            sizes[prefix] = sizes.get(prefix, 0) + obj["Size"]
    return [_sample("dashboard", "cloud", bucket.label, prefix, now, size, None) for prefix, size in sorted(sizes.items())]


def _aws_client(service: str, region: str):
    import boto3  # imported lazily: only needed when S3 buckets are configured
    from botocore.config import Config as BotoConfig

    return boto3.client(service, region_name=region,
                        config=BotoConfig(connect_timeout=5, read_timeout=30, retries={"max_attempts": 2}))


def sample_s3(storage: StorageConfig, now: float) -> list:
    """Sample every configured bucket; a failing bucket is logged and skipped."""
    samples: list = []
    for bucket in storage.s3_buckets:
        region = bucket.region or storage.aws_region
        try:
            if bucket.method == "cloudwatch":
                samples += sample_s3_cloudwatch(_aws_client("cloudwatch", region), bucket, now)
            else:
                samples += sample_s3_list(_aws_client("s3", region), bucket, now)
        except ImportError:
            logger.error("boto3 is not installed; S3 sizes cannot be sampled")
            break
        except AWS_ERRORS as exc:
            logger.error("S3 bucket '%s' not sampled: %s", bucket.bucket, exc)
    return samples


# ---------------------------------------------------------------- storing and tick tasks

def store_samples(conn: sqlite3.Connection, samples: list) -> int:
    if not samples:
        return 0
    with db.transaction(conn):
        conn.executemany(
            "INSERT OR REPLACE INTO storage_samples (source, kind, name, series, ts, used_bytes, total_bytes) "
            "VALUES (:source, :kind, :name, :series, :ts, :used_bytes, :total_bytes)",
            samples,
        )
        db.bump_rev(conn)
    return len(samples)


def _due(conn: sqlite3.Connection, key: str, interval_s: float, now: float) -> bool:
    last = db.get_meta(conn, key)
    return last is None or now - float(last) >= interval_s - DUE_SLACK_S


def _mark(conn: sqlite3.Connection, key: str, now: float) -> None:
    with db.transaction(conn):
        db.set_meta(conn, key, repr(now))


def collect_drives(conn: sqlite3.Connection, cfg: Config, now: float) -> None:
    storage = cfg.storage
    if storage.drives and _due(conn, "storage_drives_ts", storage.drive_interval_minutes * 60, now):
        _mark(conn, "storage_drives_ts", now)
        store_samples(conn, sample_drives(storage.drives, now))


def collect_s3(conn: sqlite3.Connection, cfg: Config, now: float) -> None:
    storage = cfg.storage
    if storage.s3_buckets and _due(conn, "storage_s3_ts", storage.s3_interval_hours * 3600, now):
        _mark(conn, "storage_s3_ts", now)  # also on failure, so a broken bucket is not retried every tick
        store_samples(conn, sample_s3(storage, now))


def purge_old(conn: sqlite3.Connection, cfg: Config, now: float) -> None:
    with db.transaction(conn):
        conn.execute("DELETE FROM storage_samples WHERE ts < ?", (now - cfg.retention.storage_days * 86400,))


# ---------------------------------------------------------------- agent-reported samples

class AgentSample(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=100)
    kind: Literal["local", "cloud", "repo"] = "local"
    series: str = Field("", max_length=100)
    used_bytes: int = Field(ge=0)
    total_bytes: Optional[int] = Field(None, ge=0)
    timestamp: Optional[float] = None

    @field_validator("name", "series", mode="before")
    @classmethod
    def _strip(cls, value):
        return value.strip() if isinstance(value, str) else value


class AgentStoragePayload(BaseModel):
    """Body of POST /api/v2/storage."""

    model_config = ConfigDict(extra="ignore")

    samples: list[AgentSample] = Field(min_length=1, max_length=MAX_AGENT_SAMPLES)


def agent_samples(agent: str, payload: AgentStoragePayload, now: float) -> list:
    # A clock-skewed agent must not plot into the future.
    return [
        _sample(agent, s.kind, s.name, s.series, min(s.timestamp or now, now), s.used_bytes, s.total_bytes)
        for s in payload.samples
    ]


# ---------------------------------------------------------------- chart queries

def range_seconds(range_key: Optional[str]) -> tuple:
    key = range_key if range_key in RANGES else DEFAULT_RANGE
    return key, RANGES[key]


def _order(row) -> tuple:
    return (_KIND_ORDER[row["kind"]], row["source"], row["name"], row["series"])


def history(conn: sqlite3.Connection, cfg: Config, now: float, range_key: Optional[str]) -> dict:
    """Latest usage per series plus bucketed history for the requested range.

    Every series shares one time grid (bucket start times) so stacked charts line up.
    """
    key, span = range_seconds(range_key)
    step = max(MIN_STEP_S, span // TARGET_POINTS)
    start = now - span

    current = [dict(r) for r in conn.execute(
        "SELECT s.source, s.kind, s.name, s.series, s.ts, s.used_bytes, s.total_bytes FROM storage_samples s "
        "JOIN (SELECT source, kind, name, series, MAX(ts) AS ts FROM storage_samples WHERE ts >= ? "
        "GROUP BY source, kind, name, series) m USING (source, kind, name, series, ts)",
        (now - CURRENT_MAX_AGE_S,),
    )]
    current.sort(key=_order)

    grouped: dict = {}
    for row in conn.execute(
        "SELECT source, kind, name, series, CAST((ts - :start) / :step AS INTEGER) AS b, AVG(used_bytes) AS used "
        "FROM storage_samples WHERE ts >= :start AND ts <= :end GROUP BY source, kind, name, series, b ORDER BY b",
        {"start": start, "step": step, "end": now},
    ):
        grouped.setdefault((row["source"], row["kind"], row["name"], row["series"]), []).append(
            [start + row["b"] * step, round(row["used"])])
    series = [{"source": s, "kind": k, "name": n, "series": sr, "points": points}
              for (s, k, n, sr), points in grouped.items()]
    series.sort(key=_order)

    return {
        "now": now, "timezone": cfg.timezone, "range": key, "start": start, "end": now, "step_s": step,
        "configured": {"local": len(cfg.storage.drives), "cloud": len(cfg.storage.s3_buckets)},
        "current": current, "series": series,
    }
