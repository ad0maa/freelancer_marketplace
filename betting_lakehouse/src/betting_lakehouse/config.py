"""Configuration for the betting lakehouse.

The one job of this module is to make the same pipeline code run in two places:

* a laptop, against Spark's built-in catalog and local directories
* Databricks, against Unity Catalog and cloud object storage

Everywhere else in the codebase you write ``cfg.table("gold", "fact_bet_leg")``
and never think about it again.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml

# betting_lakehouse/src/betting_lakehouse/config.py -> betting_lakehouse/
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "conf" / "pipeline.yml"


def on_databricks() -> bool:
    """True when running on a Databricks cluster.

    Every Databricks Runtime sets DATABRICKS_RUNTIME_VERSION. This is the
    standard way to branch behaviour in code that has to work both places, and
    it is much more reliable than sniffing for dbutils.
    """
    return "DATABRICKS_RUNTIME_VERSION" in os.environ


@dataclass(frozen=True)
class Config:
    """Immutable view over conf/pipeline.yml."""

    raw: dict[str, Any]
    root: Path = PROJECT_ROOT

    # ------------------------------------------------------------------ load

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        path = Path(path) if path else DEFAULT_CONFIG_PATH
        with open(path) as fh:
            return cls(raw=yaml.safe_load(fh))

    # ------------------------------------------------------- naming: catalog

    @cached_property
    def catalog(self) -> str:
        # An env var override so one deployed artefact can build into a dev
        # catalog, a prod catalog or a per-developer sandbox without a code
        # change. The Asset Bundle sets this per target (see databricks.yml).
        override = os.environ.get("LAKEHOUSE_CATALOG")
        if override:
            return override
        key = "databricks" if on_databricks() else "local"
        return self.raw["catalog"][key]

    def schema(self, layer: str) -> str:
        """Fully qualified schema name for a medallion layer, e.g. ``gold``."""
        return f"{self.catalog}.{self.raw['schemas'][layer]}"

    def table(self, layer: str, name: str) -> str:
        """Fully qualified table name, e.g. ``spark_catalog.gold.fact_bet_leg``.

        Three-part naming is not optional on Unity Catalog and it works locally
        too, so the code never needs a local/remote branch for table names.
        """
        return f"{self.schema(layer)}.{name}"

    @property
    def layers(self) -> list[str]:
        return list(self.raw["schemas"].keys())

    # --------------------------------------------------------- naming: paths

    def _path(self, key: str) -> Path:
        configured = Path(self.raw["paths"][key])
        return configured if configured.is_absolute() else self.root / configured

    @property
    def landing_dir(self) -> Path:
        return self._path("landing")

    @property
    def warehouse_dir(self) -> Path:
        return self._path("warehouse")

    @property
    def metastore_dir(self) -> Path:
        return self._path("metastore")

    @property
    def checkpoint_dir(self) -> Path:
        return self._path("checkpoints")

    def landing_path(self, dataset: str, batch: int | None = None) -> str:
        """Landing location for a source dataset.

        Laid out as ``data/landing/<dataset>/batch=<n>/`` so that batch is a
        real Hive-style partition column. On Databricks this would be
        ``abfss://landing@.../<dataset>/batch=<n>/`` and Auto Loader would
        stream it - the shape of the path is deliberately the same.
        """
        base = self.landing_dir / dataset
        return str(base if batch is None else base / f"batch={batch}")

    # ------------------------------------------------------------ generator

    @property
    def table_format(self) -> str:
        return self.raw["table_format"]

    @property
    def generator(self) -> dict[str, Any]:
        return self.raw["generator"]

    @property
    def start_date(self) -> date:
        return date.fromisoformat(self.generator["start_date"])

    @property
    def end_date(self) -> date:
        return date.fromisoformat(self.generator["end_date"])

    # ---------------------------------------------------------------- spark

    @property
    def spark_conf(self) -> dict[str, Any]:
        return self.raw["spark"]
