"""Pytest fixtures: one SparkSession, one small lakehouse, shared by every test.

Two things make Spark tests bearable, and both are here:

**One session per test run.** Starting a JVM costs 5-15 seconds, so a
function-scoped SparkSession fixture turns a 20-test suite into a five-minute
suite. The session is module-level state whether you like it or not - Spark allows
one active session per JVM - so it may as well be an explicit session-scoped
fixture.

**A tiny dataset in a temp directory.** The generator is driven entirely by
config, so the tests build a 40-customer, 120-bet lakehouse in a temp warehouse
instead of pointing at the demo data. Tests that share mutable state with a
developer's working data are tests that fail for reasons nobody can reproduce.

This is also the argument for keeping the pipeline as importable functions rather
than notebooks: none of this would be possible if the transformations only existed
in cells.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from betting_lakehouse import bronze, generate_source_data, silver  # noqa: E402
from betting_lakehouse.config import DEFAULT_CONFIG_PATH, Config  # noqa: E402
from betting_lakehouse.gold import dimensions, facts  # noqa: E402
from betting_lakehouse.io_utils import ensure_schemas  # noqa: E402
from betting_lakehouse.spark import get_spark  # noqa: E402
from betting_lakehouse.vault import raw_vault  # noqa: E402

import yaml  # noqa: E402


@pytest.fixture(scope="session")
def cfg(tmp_path_factory) -> Config:
    """The demo config, shrunk and pointed at a throwaway warehouse."""
    with open(DEFAULT_CONFIG_PATH) as fh:
        raw = copy.deepcopy(yaml.safe_load(fh))

    # Small enough to be fast, large enough that the interesting cases still
    # occur: duplicates, late fixtures, orphan selections, customer changes.
    raw["generator"].update(
        {"customers": 40, "events": 12, "bets": 120, "batch2_bets": 40,
         "batch2_customer_changes": 6}
    )
    raw["spark"]["shuffle_partitions"] = 2
    raw["spark"]["log_level"] = "ERROR"

    root = tmp_path_factory.mktemp("lakehouse")
    return Config(raw=raw, root=root)


@pytest.fixture(scope="session")
def spark(cfg: Config):
    session = get_spark("betting-lakehouse-tests", cfg)
    # Created here rather than in the `lakehouse` fixture so that the unit tests -
    # which write throwaway hubs and satellites without building the whole
    # lakehouse - have somewhere to write to.
    ensure_schemas(session, cfg)
    yield session
    session.stop()


@pytest.fixture(scope="session")
def lakehouse(spark, cfg: Config) -> Config:
    """A fully built two-batch lakehouse. Most tests just read from it."""
    for batch in (1, 2):
        generate_source_data.write_batch(cfg, batch)
        bronze.ingest_batch(spark, cfg, batch)
        silver.build_silver(spark, cfg, batch)
        raw_vault.build_raw_vault(spark, cfg, batch)
    dimensions.build_all_dimensions(spark, cfg)
    facts.build_all_facts(spark, cfg)
    return cfg
