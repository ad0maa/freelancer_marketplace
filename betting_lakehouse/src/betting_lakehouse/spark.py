"""SparkSession construction.

On Databricks a SparkSession already exists before your notebook's first line
runs - you never build one, you just call ``SparkSession.builder.getOrCreate()``
and get the session the cluster made for you. Locally we have to assemble the
equivalent by hand, which is a useful thing to read once because it shows you
exactly what Databricks is configuring on your behalf:

* the Delta Lake SQL extensions and catalog
* a metastore, so tables have names instead of paths
* a warehouse location, where managed tables physically live
"""

from __future__ import annotations

from pyspark.sql import SparkSession

from .config import Config, on_databricks


def get_spark(app_name: str = "betting-lakehouse", cfg: Config | None = None) -> SparkSession:
    """Return a SparkSession configured for the current environment."""
    cfg = cfg or Config.load()
    spark = _databricks_session() if on_databricks() else _local_session(app_name, cfg)

    # Session-level settings that should hold in both environments.
    sconf = cfg.spark_conf
    spark.conf.set("spark.sql.session.timeZone", sconf["timezone"])
    spark.sparkContext.setLogLevel(sconf.get("log_level", "WARN"))
    return spark


def _databricks_session() -> SparkSession:
    """Reuse the cluster's session. Do not try to reconfigure the cluster here.

    Cluster-level Spark conf belongs in the cluster/job definition
    (see databricks.yml), not in application code - otherwise two jobs sharing
    a cluster fight over settings.
    """
    return SparkSession.builder.getOrCreate()


def _local_session(app_name: str, cfg: Config) -> SparkSession:
    from delta import configure_spark_with_delta_pip

    cfg.warehouse_dir.mkdir(parents=True, exist_ok=True)
    cfg.metastore_dir.parent.mkdir(parents=True, exist_ok=True)

    builder = (
        SparkSession.builder.appName(app_name)
        # local[*] = one JVM, one thread per core, no cluster. On Databricks this
        # is replaced by a driver plus N workers, and nothing else changes.
        .master("local[*]")
        # --- Delta Lake: these two lines are what Databricks pre-sets for you --
        .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
        .config(
            "spark.sql.catalog.spark_catalog",
            "org.apache.spark.sql.delta.catalog.DeltaCatalog",
        )
        # --- a persistent metastore, so `silver.bets` still exists tomorrow ---
        .config("spark.sql.catalogImplementation", "hive")
        .config("spark.sql.warehouse.dir", str(cfg.warehouse_dir))
        # The spark.hadoop. prefix is what forwards a property through to the
        # Hive/Hadoop conf; without it Spark logs "Ignoring non-Spark config".
        .config(
            "spark.hadoop.javax.jdo.option.ConnectionURL",
            f"jdbc:derby:;databaseName={cfg.metastore_dir};create=true",
        )
        # --- performance knobs worth knowing the names of --------------------
        # Default is 200 shuffle partitions. On 5k rows that means 200 nearly
        # empty tasks and a pipeline that spends all its time on scheduling.
        # On real volumes you leave this alone and let AQE coalesce instead.
        .config("spark.sql.shuffle.partitions", cfg.spark_conf["shuffle_partitions"])
        .config("spark.sql.adaptive.enabled", "true")
        .config("spark.sql.adaptive.coalescePartitions.enabled", "true")
        # Delta on a laptop: skip the extra listing work meant for object stores.
        .config("spark.databricks.delta.snapshotPartitions", "2")
        .config("spark.ui.showConsoleProgress", "false")
    )

    # Resolves the io.delta:delta-spark jars matching the installed pip package.
    spark = configure_spark_with_delta_pip(builder).enableHiveSupport().getOrCreate()
    _quiet_local_hive_warnings(spark)
    return spark


def _quiet_local_hive_warnings(spark: SparkSession) -> None:
    """Silence a purely local, purely cosmetic Hive warning.

    Rebuilding a Delta table makes Spark try to mirror the new schema into the
    metastore. The bundled Hive 2.3 shim cannot express some Delta types, so it
    refuses the alter, and Spark falls back to storing the schema in the table's
    properties instead - which works fine, and is what it does for every Delta
    table. The only problem is that it logs the refusal at WARN with a 120-line
    Java stack trace, once per table per rebuild, and that noise buries the actual
    pipeline output.

    None of this exists on Databricks: Unity Catalog stores Delta schemas natively.
    Suppressing it is safe here precisely because the fallback path is the normal
    path - if these loggers ever mattered, Delta writes would be failing outright,
    which the row counts and the reconciliation check would catch immediately.
    """
    noisy = (
        "org.apache.spark.sql.hive.HiveExternalCatalog",
        "org.apache.hadoop.hive.metastore.HiveAlterHandler",
        "org.apache.hadoop.hive.ql.metadata.Hive",
        "hive.log",
    )
    try:
        jvm = spark.sparkContext._jvm
        configurator = jvm.org.apache.logging.log4j.core.config.Configurator
        fatal = jvm.org.apache.logging.log4j.Level.FATAL
        for logger in noisy:
            configurator.setLevel(logger, fatal)
    except Exception:  # pragma: no cover - depends on the bundled log4j build
        # Cosmetic only. If the logging API moves, the pipeline still runs.
        pass
