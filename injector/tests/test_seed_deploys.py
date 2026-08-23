"""Deploy-ledger seeding: generation is pure, the write is injected."""

from datetime import datetime, timedelta, timezone

from injector import seed_deploys

FIXED = datetime(2026, 8, 20, 14, 9, 30, tzinfo=timezone.utc)

# Words that would give the answer away. A deploy message must hint at a
# plausible cause without naming the fault the injector is about to cause.
FAULT_WORDS = ("latency", "timeout", "memory", "bad_config", "fault", "inject", "slow")


def _generate(count=5, **kwargs):
    return seed_deploys.generate_deploys(count, now=lambda: FIXED, **kwargs)


# ── shape ────────────────────────────────────────────────────────────

def test_generates_the_requested_number_of_deploys():
    assert len(_generate(7)) == 7


def test_every_record_has_the_columns_the_table_requires():
    record = _generate(1)[0]
    assert set(record) == {
        "service", "version", "commit_sha", "author",
        "message", "changed_files", "deployed_at", "status",
    }


def test_service_is_one_of_the_victim_services():
    for record in _generate(20):
        assert record["service"] in seed_deploys.SERVICES


def test_version_looks_like_a_semver_tag():
    for record in _generate(10):
        major, minor, patch = record["version"].lstrip("v").split(".")
        assert major.isdigit() and minor.isdigit() and patch.isdigit()


def test_commit_sha_is_a_short_hex_sha():
    for record in _generate(10):
        sha = record["commit_sha"]
        assert len(sha) == 7
        int(sha, 16)


def test_changed_files_is_a_non_empty_list_of_paths():
    for record in _generate(10):
        files = record["changed_files"]
        assert files
        # Source files, but also Dockerfile / requirements.txt / compose.
        assert all(f and " " not in f for f in files)


def test_status_is_one_of_the_values_the_check_constraint_allows():
    for record in _generate(30):
        assert record["status"] in {"succeeded", "failed", "rolled_back"}


# ── timing ───────────────────────────────────────────────────────────

def test_deploys_land_in_the_past_within_the_requested_window():
    records = _generate(20, hours=6)
    for record in records:
        assert record["deployed_at"] <= FIXED
        assert record["deployed_at"] >= FIXED - timedelta(hours=6)


def test_deploys_are_returned_most_recent_first():
    stamps = [r["deployed_at"] for r in _generate(20)]
    assert stamps == sorted(stamps, reverse=True)


def test_deploy_times_are_distinct():
    """Colliding timestamps make 'which shipped last' ambiguous."""
    records = _generate(40)
    assert len({r["deployed_at"] for r in records}) == 40


def test_timestamps_are_timezone_aware_utc():
    for record in _generate(5):
        assert record["deployed_at"].utcoffset() == timedelta(0)


# ── the required property: noise ─────────────────────────────────────

def test_messages_never_name_the_fault_they_might_correlate_with():
    """Otherwise the agent reads the answer off the deploy message."""
    for record in _generate(50):
        lowered = record["message"].lower()
        for word in FAULT_WORDS:
            assert word not in lowered, record["message"]


def test_generated_deploys_cover_more_than_one_service():
    """Noise must be uncorrelated with any single service under test."""
    services = {r["service"] for r in _generate(30)}
    assert len(services) > 1


def test_versions_increase_over_time_within_a_service():
    """A ledger where v1.0.0 ships after v2.0.0 reads as corrupt."""
    records = _generate(40)
    for service in seed_deploys.SERVICES:
        rows = [r for r in records if r["service"] == service]
        ordered = sorted(rows, key=lambda r: r["deployed_at"])
        versions = [tuple(int(p) for p in r["version"].lstrip("v").split(".")) for r in ordered]
        assert versions == sorted(versions)


# ── determinism ──────────────────────────────────────────────────────

def test_the_same_seed_produces_the_same_ledger():
    assert _generate(10, seed=42) == _generate(10, seed=42)


def test_different_seeds_produce_different_ledgers():
    assert _generate(10, seed=1) != _generate(10, seed=2)


# ── the write is injected ────────────────────────────────────────────

def test_seed_writes_the_generated_records_through_the_writer():
    written = []
    records = seed_deploys.seed(
        noise=4, writer=written.append, now=lambda: FIXED, seed=7
    )

    assert len(records) == 4
    assert len(written) == 1
    assert written[0] == records


def test_clear_wipes_before_writing():
    calls = []
    seed_deploys.seed(
        noise=2,
        writer=lambda rows: calls.append(("write", len(rows))),
        clearer=lambda: calls.append(("clear", 0)),
        clear=True,
        now=lambda: FIXED,
    )
    assert calls[0][0] == "clear"
    assert calls[1][0] == "write"


def test_clear_is_not_called_unless_asked():
    calls = []
    seed_deploys.seed(
        noise=2,
        writer=lambda rows: None,
        clearer=lambda: calls.append("clear"),
        now=lambda: FIXED,
    )
    assert calls == []


def test_seeding_zero_noise_writes_nothing():
    written = []
    seed_deploys.seed(noise=0, writer=written.append, now=lambda: FIXED)
    assert written == []
