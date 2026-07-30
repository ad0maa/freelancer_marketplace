"""Data quality expectations.

The idea is borrowed straight from Delta Live Tables' ``@dlt.expect`` decorators
(see dlt/betting_dlt_pipeline.py for the DLT version of exactly these rules), but
written out by hand so you can see what a framework is doing for you.

Three severities, and choosing between them is a data modelling decision, not a
technical one:

``warn``   record the failure, keep the row. Use when the row is still useful.
``drop``   quarantine the row so it cannot poison the marts, keep pipeline green.
``fail``   abort the run. Reserve this for "the numbers would be wrong", e.g.
           a settlement paying out more than the bet could ever return.

Everything that gets dropped lands in ``dq.quarantine`` with the reason attached.
A quarantine table nobody reads is just a slower delete, so the run summary
prints its row count every time.
"""

from __future__ import annotations

from dataclasses import dataclass

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

from ..config import Config
from ..io_utils import append


@dataclass(frozen=True)
class Expectation:
    """A rule that must hold for a row to be considered good.

    ``condition`` is a SQL boolean expression that should evaluate to TRUE for a
    valid row. Watch out for NULLs: ``stake > 0`` is NULL (not FALSE) when stake
    is NULL, so write ``stake IS NOT NULL AND stake > 0`` when you mean it.
    """

    name: str
    condition: str
    description: str
    severity: str = "drop"


class ExpectationFailure(RuntimeError):
    """Raised when a ``fail``-severity expectation is violated."""


def apply_expectations(
    spark: SparkSession,
    df: DataFrame,
    expectations: list[Expectation],
    table: str,
    batch_id: str,
    cfg: Config,
) -> DataFrame:
    """Evaluate expectations, quarantine bad rows, and return the clean rows.

    Side effects: appends to ``dq.dq_results`` (one row per expectation per run)
    and ``dq.quarantine`` (one row per rejected source row).
    """
    if not expectations:
        return df

    payload_cols = df.columns
    flagged = df
    for exp in expectations:
        flagged = flagged.withColumn(f"_ok_{exp.name}", F.expr(exp.condition))
    flagged = flagged.persist()

    _write_results(spark, flagged, expectations, table, batch_id, cfg)

    # A NULL condition means "could not evaluate" - treat that as a failure
    # rather than silently letting the row through.
    rejecting = [e for e in expectations if e.severity in ("drop", "fail")]
    hard_failures = [e for e in expectations if e.severity == "fail"]

    if hard_failures:
        bad = flagged.where(
            " OR ".join(f"NOT coalesce(_ok_{e.name}, false)" for e in hard_failures)
        )
        count = bad.count()
        if count:
            names = ", ".join(e.name for e in hard_failures)
            flagged.unpersist()
            raise ExpectationFailure(
                f"{table}: {count} row(s) violated fail-severity expectation(s) [{names}]. "
                "The run is aborted deliberately - these rows would make the marts wrong."
            )

    if not rejecting:
        clean = df
    else:
        reject_expr = " OR ".join(f"NOT coalesce(_ok_{e.name}, false)" for e in rejecting)
        quarantine = flagged.where(reject_expr)
        _write_quarantine(spark, quarantine, rejecting, payload_cols, table, batch_id, cfg)
        clean = flagged.where(f"NOT ({reject_expr})").select(*payload_cols)

    clean = clean.persist()
    clean.count()
    flagged.unpersist()
    return clean


def _write_results(spark, flagged, expectations, table, batch_id, cfg) -> None:
    total = flagged.count()
    rows = []
    for exp in expectations:
        failed = flagged.where(f"NOT coalesce(_ok_{exp.name}, false)").count()
        rows.append(
            (
                table,
                batch_id,
                exp.name,
                exp.description,
                exp.severity,
                total,
                failed,
                round(100.0 * (total - failed) / total, 4) if total else 100.0,
            )
        )
    results = spark.createDataFrame(
        rows,
        "table_name string, batch_id string, expectation string, description string, "
        "severity string, rows_checked long, rows_failed long, pass_pct double",
    ).withColumn("checked_at", F.current_timestamp())
    append(results, cfg.table("dq", "dq_results"), cfg)


def _write_quarantine(spark, quarantine, rejecting, payload_cols, table, batch_id, cfg) -> None:
    if quarantine.isEmpty():
        return
    reasons = F.array_compact(
        F.array(
            *[
                F.when(~F.coalesce(F.col(f"_ok_{e.name}"), F.lit(False)), F.lit(e.name))
                for e in rejecting
            ]
        )
    )
    # One quarantine table for every source: the rejected row is kept verbatim as
    # JSON so no schema change is needed when a new table starts being checked.
    out = quarantine.select(
        F.lit(table).alias("table_name"),
        F.lit(batch_id).alias("batch_id"),
        reasons.alias("failed_expectations"),
        F.to_json(F.struct(*[F.col(c) for c in payload_cols])).alias("row_payload"),
        F.current_timestamp().alias("quarantined_at"),
    )
    append(out, cfg.table("dq", "quarantine"), cfg)
