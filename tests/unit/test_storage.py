import threading
import types
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber

from app import config, db, storage, tick
from app.config import Config, Drive, S3Bucket, StorageConfig
from app.web import api_ui, create_app

NOW = 1_800_000_000.0


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "t.sqlite"
    db.init_db(path)
    connection = db.connect(path)
    yield connection
    connection.close()


def sample(name="Root", ts=NOW - 100, used=100, total=1000, kind="local", series="", source="dashboard"):
    return {"source": source, "kind": kind, "name": name, "series": series, "ts": ts,
            "used_bytes": used, "total_bytes": total}


def cfg_with(**storage_kwargs):
    return Config(storage=StorageConfig(**storage_kwargs))


def aws_client(service):
    return boto3.client(service, region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y")


class TestConfigValidation:
    def test_labels_must_be_unique(self):
        with pytest.raises(ValueError, match="unique"):
            StorageConfig(drives=[Drive(path="/a", label="X"), Drive(path="/b", label="X")])

    def test_unknown_s3_method_is_rejected(self):
        with pytest.raises(ValueError):
            S3Bucket(bucket="b", label="B", method="magic")

    def test_defaults(self):
        bucket = S3Bucket(bucket="b", label="B")
        assert (bucket.method, bucket.region) == ("cloudwatch", None)


class TestDrives:
    def test_samples_used_and_total_bytes(self, tmp_path):
        (sample_,) = storage.sample_drives([Drive(path=str(tmp_path), label="Tmp")], NOW)
        assert sample_["kind"] == "local" and sample_["source"] == "dashboard"
        assert 0 < sample_["used_bytes"] <= sample_["total_bytes"]

    def test_unreadable_drive_is_skipped(self, tmp_path):
        drives = [Drive(path=str(tmp_path / "missing"), label="Gone"), Drive(path=str(tmp_path), label="Ok")]
        assert [s["name"] for s in storage.sample_drives(drives, NOW)] == ["Ok"]

    def test_a_hung_drive_does_not_block_the_others(self, tmp_path, monkeypatch):
        release = threading.Event()
        real = storage._read_usage

        def slow(path, out):
            if path.endswith("hung"):
                release.wait(5)
            real(path, out)

        monkeypatch.setattr(storage, "_read_usage", slow)
        drives = [Drive(path=str(tmp_path / "hung"), label="Hung"), Drive(path=str(tmp_path), label="Ok")]
        try:
            assert [s["name"] for s in storage.sample_drives(drives, NOW, timeout=0.3)] == ["Ok"]
        finally:
            release.set()


class TestS3CloudWatch:
    BUCKET = S3Bucket(bucket="photos", label="Gallery")

    def stub(self, client, types_, results):
        stubber = Stubber(client)
        stubber.add_response("list_metrics", {"Metrics": [
            {"Namespace": "AWS/S3", "MetricName": "BucketSizeBytes", "Dimensions": [
                {"Name": "BucketName", "Value": "photos"}, {"Name": "StorageType", "Value": t}]}
            for t in types_]}, {"Namespace": "AWS/S3", "MetricName": "BucketSizeBytes",
                                "Dimensions": [{"Name": "BucketName", "Value": "photos"}]})
        if results is not None:
            stubber.add_response("get_metric_data", {"MetricDataResults": results}, {
                "MetricDataQueries": ANY, "StartTime": ANY, "EndTime": ANY, "ScanBy": "TimestampAscending"})
        stubber.activate()
        return stubber

    def test_one_series_per_storage_class_with_daily_points(self):
        client = aws_client("cloudwatch")
        day1 = datetime(2026, 1, 10, tzinfo=timezone.utc)
        # Storage types are queried in sorted order, so q0 is the IA class and q1 is Standard.
        results = [
            {"Id": "q1", "Label": "a", "StatusCode": "Complete",
             "Timestamps": [day1, day1 + timedelta(days=1)], "Values": [5e11, 6e11]},
            {"Id": "q0", "Label": "b", "StatusCode": "Complete", "Timestamps": [day1], "Values": [1e9]},
        ]
        self.stub(client, ["StandardStorage", "StandardIAStorage"], results)

        samples = storage.sample_s3_cloudwatch(client, self.BUCKET, NOW)

        by_series = {}
        for s in samples:
            by_series.setdefault(s["series"], []).append(s)
        assert set(by_series) == {"StandardStorage", "StandardIAStorage"}
        assert [s["used_bytes"] for s in by_series["StandardStorage"]] == [500_000_000_000, 600_000_000_000]
        assert by_series["StandardStorage"][0]["ts"] == day1.timestamp()
        assert all(s["kind"] == "cloud" and s["name"] == "Gallery" and s["total_bytes"] is None for s in samples)

    def test_query_asks_for_daily_average_bytes_per_storage_type(self):
        client = aws_client("cloudwatch")
        stubber = Stubber(client)
        stubber.add_response("list_metrics", {"Metrics": [{"Namespace": "AWS/S3", "MetricName": "BucketSizeBytes",
                             "Dimensions": [{"Name": "BucketName", "Value": "photos"},
                                            {"Name": "StorageType", "Value": "StandardStorage"}]}]})
        seen = {}

        def capture(params, **_k):
            seen.update(params)

        client.meta.events.register("provide-client-params.cloudwatch.GetMetricData", capture)
        stubber.add_response("get_metric_data", {"MetricDataResults": []})
        stubber.activate()
        storage.sample_s3_cloudwatch(client, self.BUCKET, NOW)

        (query,) = seen["MetricDataQueries"]
        stat = query["MetricStat"]
        assert (stat["Period"], stat["Stat"], stat["Unit"]) == (86400, "Average", "Bytes")
        assert {"Name": "StorageType", "Value": "StandardStorage"} in stat["Metric"]["Dimensions"]

    def test_bucket_without_metrics_yields_nothing(self):
        client = aws_client("cloudwatch")
        self.stub(client, [], None)
        assert storage.sample_s3_cloudwatch(client, self.BUCKET, NOW) == []


class TestS3List:
    def test_sizes_are_grouped_by_top_level_prefix(self):
        client = aws_client("s3")
        stubber = Stubber(client)
        stubber.add_response("list_objects_v2", {"Contents": [
            {"Key": "host1/job/a.tar", "Size": 100}, {"Key": "host1/job/b.tar", "Size": 50},
            {"Key": "host2/x", "Size": 7}, {"Key": "readme.txt", "Size": 3}]}, {"Bucket": "photos"})
        stubber.activate()

        samples = storage.sample_s3_list(client, S3Bucket(bucket="photos", label="G", method="list"), NOW)

        assert {s["series"]: s["used_bytes"] for s in samples} == {"host1": 150, "host2": 7, "(root)": 3}


class TestS3Errors:
    def bucket_cfg(self):
        return StorageConfig(s3_buckets=[S3Bucket(bucket="a", label="A"), S3Bucket(bucket="b", label="B")])

    def test_a_failing_bucket_is_skipped_and_others_continue(self, monkeypatch):
        error = ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "ListMetrics")

        def fake(client, bucket, now):
            if bucket.bucket == "a":
                raise error
            return [sample("B", kind="cloud", series="Std")]

        monkeypatch.setattr(storage, "_aws_client", lambda service, region: object())
        monkeypatch.setattr(storage, "sample_s3_cloudwatch", fake)
        assert [s["name"] for s in storage.sample_s3(self.bucket_cfg(), NOW)] == ["B"]

    def test_missing_boto3_is_reported_not_raised(self, monkeypatch):
        def no_boto(service, region):
            raise ImportError("boto3")

        monkeypatch.setattr(storage, "_aws_client", no_boto)
        assert storage.sample_s3(self.bucket_cfg(), NOW) == []


class TestStoringAndTicks:
    def test_storing_is_idempotent_and_bumps_the_revision(self, conn):
        assert storage.store_samples(conn, [sample(), sample(name="Other")]) == 2
        rev = db.get_meta(conn, "rev")
        storage.store_samples(conn, [sample(used=999)])
        assert conn.execute("SELECT COUNT(*) FROM storage_samples").fetchone()[0] == 2
        assert conn.execute("SELECT used_bytes FROM storage_samples WHERE name = 'Root'").fetchone()[0] == 999
        assert int(db.get_meta(conn, "rev")) == int(rev) + 1

    def test_storing_nothing_does_not_touch_the_revision(self, conn):
        storage.store_samples(conn, [])
        assert db.get_meta(conn, "rev") is None

    def test_drives_are_sampled_on_their_interval_only(self, conn, tmp_path):
        cfg = cfg_with(drives=[Drive(path=str(tmp_path), label="T")], drive_interval_minutes=15)
        storage.collect_drives(conn, cfg, NOW)
        storage.collect_drives(conn, cfg, NOW + 300)
        assert conn.execute("SELECT COUNT(*) FROM storage_samples").fetchone()[0] == 1
        storage.collect_drives(conn, cfg, NOW + 15 * 60)
        assert conn.execute("SELECT COUNT(*) FROM storage_samples").fetchone()[0] == 2

    def test_s3_attempt_is_remembered_even_when_nothing_was_sampled(self, conn, monkeypatch):
        calls = []
        monkeypatch.setattr(storage, "sample_s3", lambda cfg, now: calls.append(now) or [])
        cfg = cfg_with(s3_buckets=[S3Bucket(bucket="b", label="B")], s3_interval_hours=12)
        storage.collect_s3(conn, cfg, NOW)
        storage.collect_s3(conn, cfg, NOW + 3600)
        assert calls == [NOW]

    def test_nothing_configured_means_nothing_collected(self, conn):
        storage.collect_drives(conn, Config(), NOW)
        storage.collect_s3(conn, Config(), NOW)
        assert db.get_meta(conn, "storage_drives_ts") is None

    def test_old_samples_are_purged(self, conn):
        storage.store_samples(conn, [sample(ts=NOW - 400 * 86400), sample(name="New", ts=NOW - 10)])
        storage.purge_old(conn, Config(), NOW)
        assert [r[0] for r in conn.execute("SELECT name FROM storage_samples")] == ["New"]

    def test_tick_runs_storage_tasks_and_isolates_failures(self, conn, tmp_path, monkeypatch):
        cfg = cfg_with(drives=[Drive(path=str(tmp_path), label="T")])
        monkeypatch.setattr(tick, "TASKS", [("bad", lambda *_a: 1 / 0), ("drives", storage.collect_drives)])
        tick.run_once(conn, cfg, NOW)
        assert conn.execute("SELECT COUNT(*) FROM storage_samples").fetchone()[0] == 1


class TestHistory:
    def test_current_is_the_latest_per_series_and_ignores_stale_ones(self, conn):
        storage.store_samples(conn, [
            sample(ts=NOW - 7200, used=1), sample(ts=NOW - 60, used=2),
            sample(name="Old", ts=NOW - 5 * 86400, used=3),
            sample(name="Gallery", kind="cloud", series="Std", ts=NOW - 3600, used=9, total=None),
        ])
        data = storage.history(conn, Config(), NOW, "7d")
        assert [(c["name"], c["used_bytes"]) for c in data["current"]] == [("Root", 2), ("Gallery", 9)]

    def test_points_are_bucketed_on_a_shared_grid_and_averaged(self, conn):
        step = 86400 // storage.TARGET_POINTS
        base = NOW - 86400
        storage.store_samples(conn, [
            sample(ts=base + 10, used=100), sample(ts=base + 20, used=300),
            sample(name="Other", ts=base + 15, used=50),
        ])
        data = storage.history(conn, Config(), NOW, "24h")
        points = {s["name"]: s["points"] for s in data["series"]}
        assert points["Root"] == [[base, 200]]
        assert points["Other"] == [[base, 50]]
        assert data["step_s"] == step

    def test_range_is_validated_and_data_outside_it_is_excluded(self, conn):
        storage.store_samples(conn, [sample(ts=NOW - 40 * 86400)])
        assert storage.history(conn, Config(), NOW, "bogus")["range"] == "30d"
        assert storage.history(conn, Config(), NOW, "30d")["series"] == []
        assert len(storage.history(conn, Config(), NOW, "90d")["series"]) == 1

    def test_series_are_ordered_local_before_cloud(self, conn):
        storage.store_samples(conn, [sample(name="Z", kind="cloud", series="s", total=None), sample(name="A")])
        kinds = [s["kind"] for s in storage.history(conn, Config(), NOW, "24h")["series"]]
        assert kinds == ["local", "cloud"]

    def test_configured_counts_drive_empty_state(self, conn):
        cfg = cfg_with(drives=[Drive(path="/", label="R")], s3_buckets=[S3Bucket(bucket="b", label="B")])
        assert storage.history(conn, cfg, NOW, "24h")["configured"] == {"local": 1, "cloud": 1}


class TestHttp:
    @pytest.fixture
    def client(self, home, monkeypatch):
        db.init_db()
        monkeypatch.setattr(api_ui, "time", types.SimpleNamespace(time=lambda: NOW))
        monkeypatch.setattr("app.web.api_ingest.time", types.SimpleNamespace(time=lambda: NOW))
        return create_app(config.load_config()).test_client()

    @pytest.fixture
    def key(self, home):
        return config.add_agent("nas1")

    def post(self, client, key, body, **kw):
        return client.post("/api/v2/storage", json=body, headers={"X-API-Key": key}, **kw)

    def test_agent_can_report_samples(self, client, key):
        resp = self.post(client, key, {"samples": [
            {"name": "vol1", "kind": "local", "used_bytes": 500, "total_bytes": 1000},
            {"name": "repo", "kind": "repo", "used_bytes": 70, "timestamp": NOW - 30}]})
        assert (resp.status_code, resp.get_json()) == (201, {"success": True, "stored": 2})
        data = client.get("/api/ui/storage?range=24h").get_json()
        assert {(c["source"], c["kind"], c["name"]) for c in data["current"]} == {
            ("nas1", "local", "vol1"), ("nas1", "repo", "repo")}

    def test_reporting_counts_as_hearing_from_the_agent(self, client, key):
        self.post(client, key, {"samples": [{"name": "v", "used_bytes": 1}]})
        conn = db.connect()
        assert conn.execute("SELECT last_heartbeat FROM agent_state WHERE name = 'nas1'").fetchone()[0] == NOW
        conn.close()

    def test_future_timestamps_are_clamped_to_now(self, client, key):
        self.post(client, key, {"samples": [{"name": "v", "used_bytes": 1, "timestamp": NOW + 99999}]})
        conn = db.connect()
        assert conn.execute("SELECT ts FROM storage_samples").fetchone()[0] == NOW
        conn.close()

    @pytest.mark.parametrize("body", [
        {}, {"samples": []}, {"samples": [{"name": "v", "used_bytes": -1}]},
        {"samples": [{"name": "v", "kind": "magic", "used_bytes": 1}]},
        {"samples": [{"name": "", "used_bytes": 1}]},
        {"samples": [{"name": "v", "used_bytes": 1}] * (storage.MAX_AGENT_SAMPLES + 1)},
    ])
    def test_invalid_reports_are_rejected(self, client, key, body):
        assert self.post(client, key, body).status_code == 400

    def test_requires_a_valid_key(self, client, key):
        assert client.post("/api/v2/storage", json={}).status_code == 401
        assert self.post(client, "wrong", {"samples": []}).status_code == 403

    def test_ui_endpoint_shape_etag_and_range(self, client, key):
        self.post(client, key, {"samples": [{"name": "v", "used_bytes": 1}]})
        first = client.get("/api/ui/storage?range=7d")
        body = first.get_json()
        assert set(body) >= {"now", "timezone", "range", "start", "end", "step_s", "configured", "current", "series"}
        assert body["range"] == "7d"
        assert client.get("/api/ui/storage?range=7d", headers={"If-None-Match": first.headers["ETag"]}).status_code == 304
        assert client.get("/api/ui/storage?range=bogus").get_json()["range"] == "30d"
