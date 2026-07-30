"""Console entry points, one per pipeline layer.

These are the ``entry_point`` values referenced by the ``python_wheel_task``
definitions in databricks.yml. Packaging the pipeline as a wheel with entry points
(rather than pointing jobs at notebooks) is the more maintainable of the two
Databricks patterns:

* the code is importable, so it can be unit tested off-cluster - which is what
  tests/ does
* a task failure gives you a Python stack trace in a module, not a cell number
* the same wheel is promoted from dev to prod, so what you tested is what runs

Notebooks still earn their place for exploration and for documentation you can
run, which is what notebooks/ is for.

    lakehouse-bronze --batch 1
    lakehouse-silver --batch 1
    lakehouse-vault-hubs --batch 1
"""

from __future__ import annotations

import argparse
from collections.abc import Callable

from pyspark.sql import SparkSession

from . import bronze, silver
from .config import Config
from .gold import dimensions, facts
from .io_utils import ensure_schemas
from .spark import get_spark
from .vault import raw_vault


def _parse(description: str, needs_batch: bool = True) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--config", help="path to pipeline.yml")
    if needs_batch:
        parser.add_argument("--batch", type=int, default=1, help="landing batch to load")
    # Accepted and ignored: the bundle passes --catalog for readability, but the
    # catalog is resolved from LAKEHOUSE_CATALOG / pipeline.yml so that every
    # layer in a run cannot disagree about where it is writing.
    parser.add_argument("--catalog", help=argparse.SUPPRESS)
    parser.add_argument("--landing", help=argparse.SUPPRESS)
    return parser.parse_args()


def _run(name: str, fn: Callable[[SparkSession, Config, int], dict[str, int]], needs_batch: bool = True) -> None:
    args = _parse(name, needs_batch)
    cfg = Config.load(args.config)
    spark = get_spark(f"betting-lakehouse-{name}", cfg)
    try:
        ensure_schemas(spark, cfg)
        counts = fn(spark, cfg, getattr(args, "batch", 0)) if needs_batch else fn(spark, cfg)
        for table, rows in counts.items():
            print(f"{name}: {table} -> {rows:,} rows")
    finally:
        # On Databricks the cluster owns the session, so a task must not stop it.
        if not spark.conf.get("spark.databricks.clusterUsageTags.clusterName", None):
            spark.stop()


def bronze_ingest() -> None:
    _run("bronze", bronze.ingest_batch)


def silver_cleanse() -> None:
    _run("silver", silver.build_silver)


def vault_hubs() -> None:
    _run("vault-hubs", raw_vault.load_hubs)


def vault_links() -> None:
    _run("vault-links", raw_vault.load_links)


def vault_satellites() -> None:
    _run("vault-satellites", raw_vault.load_satellites)


def gold_dimensions() -> None:
    _run("gold-dimensions", dimensions.build_all_dimensions, needs_batch=False)


def gold_facts() -> None:
    _run("gold-facts", facts.build_all_facts, needs_batch=False)
