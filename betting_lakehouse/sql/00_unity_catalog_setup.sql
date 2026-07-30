-- ===========================================================================
-- Unity Catalog setup.
--
-- Run once by an admin (or from a bundle deploy hook) before the pipeline runs.
-- Nothing here is needed for the local demo, because there is no Unity Catalog on
-- a laptop - Spark's built-in catalog gives you the same three-part naming with
-- none of the governance.
--
-- Unity Catalog is the piece that makes Databricks a platform rather than a Spark
-- cluster. Three things matter for a data engineer:
--
--   1. THREE-LEVEL NAMESPACE: catalog.schema.table. The catalog is the isolation
--      boundary between dev and prod, so the same code deploys to both by
--      changing one variable.
--   2. GRANTS ON OBJECTS, not on files. An analyst is granted SELECT on the gold
--      schema; they cannot reach the underlying storage even though the data is
--      just Parquet in a bucket. Before UC, this required a separate storage-level
--      IAM story that nobody kept in sync.
--   3. LINEAGE AND AUDIT FOR FREE. Every read and write is recorded, so
--      column-level lineage across the whole medallion stack is a query, not a
--      documentation exercise. In a regulated industry this is the reason to adopt
--      it, more than the access control.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- Storage credential and external location.
--
-- The credential holds the cloud identity; the external location maps a storage
-- path to it. Every managed table in a catalog with a MANAGED LOCATION lands
-- underneath it, so nobody has to pass storage paths around in code.
-- ---------------------------------------------------------------------------
CREATE STORAGE CREDENTIAL IF NOT EXISTS sportsbet_demo_cred
  WITH AZURE_MANAGED_IDENTITY `/subscriptions/.../accessConnectors/sportsbet-demo`
  COMMENT 'Managed identity used by the wagering lakehouse';

CREATE EXTERNAL LOCATION IF NOT EXISTS sportsbet_demo_root
  URL 'abfss://lakehouse@sportsbetdemo.dfs.core.windows.net/'
  WITH (STORAGE CREDENTIAL sportsbet_demo_cred);

-- ---------------------------------------------------------------------------
-- One catalog per environment. This is the isolation boundary that matters: a
-- dev job physically cannot write to prod tables, because it has no grant on the
-- prod catalog. Schema-name prefixes ("dev_gold") give you none of that.
-- ---------------------------------------------------------------------------
CREATE CATALOG IF NOT EXISTS sportsbet_demo
  MANAGED LOCATION 'abfss://lakehouse@sportsbetdemo.dfs.core.windows.net/prod'
  COMMENT 'Wagering lakehouse - production';

CREATE CATALOG IF NOT EXISTS sportsbet_demo_dev
  MANAGED LOCATION 'abfss://lakehouse@sportsbetdemo.dfs.core.windows.net/dev'
  COMMENT 'Wagering lakehouse - development';

USE CATALOG sportsbet_demo;

-- ---------------------------------------------------------------------------
-- One schema per medallion layer. The layer boundary is the access-control
-- boundary: analysts get gold, engineers get silver, almost nobody gets bronze.
-- ---------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS bronze
  COMMENT 'Raw landed data, unmodified. Every column is a string.';
CREATE SCHEMA IF NOT EXISTS silver
  COMMENT 'Cleansed, typed, deduplicated, conformed.';
CREATE SCHEMA IF NOT EXISTS raw_vault
  COMMENT 'Data Vault 2.0 raw vault. Insert-only system of record.';
CREATE SCHEMA IF NOT EXISTS gold
  COMMENT 'Kimball dimensional marts. Rebuilt from the vault.';
CREATE SCHEMA IF NOT EXISTS dq
  COMMENT 'Data quality results and quarantined rows.';

-- ---------------------------------------------------------------------------
-- Grants.
--
-- The service principal that runs the job owns the write side. Humans read.
-- Note that BROWSE on the catalog lets an analyst discover table names and
-- comments without being able to read the data - which is how you make a
-- warehouse explorable without over-granting.
-- ---------------------------------------------------------------------------
GRANT USE CATALOG, BROWSE ON CATALOG sportsbet_demo TO `data-analysts`;
GRANT USE SCHEMA, SELECT ON SCHEMA sportsbet_demo.gold TO `data-analysts`;
GRANT USE SCHEMA, SELECT ON SCHEMA sportsbet_demo.silver TO `data-engineers`;
GRANT USE SCHEMA, SELECT ON SCHEMA sportsbet_demo.raw_vault TO `data-engineers`;
GRANT USE SCHEMA, SELECT ON SCHEMA sportsbet_demo.dq TO `data-engineers`;

GRANT ALL PRIVILEGES ON CATALOG sportsbet_demo TO `sp-data-platform`;

-- Bronze deliberately stays closed. It holds unvalidated PII in string form, and
-- there is no legitimate reason for an analyst to query it - if they need
-- something that is only in bronze, that is a gap in silver.
GRANT USE SCHEMA, SELECT ON SCHEMA sportsbet_demo.bronze TO `data-platform-admins`;

-- ---------------------------------------------------------------------------
-- Column-level masking on PII.
--
-- A row filter or column mask is attached to the table, so it applies no matter
-- how the table is queried - notebook, SQL warehouse, BI tool, or a job. This is
-- the part people miss: masking in a view is trivially bypassed by querying the
-- underlying table, whereas a UC column mask is not.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION gold.mask_email(email STRING)
RETURN CASE
         WHEN is_account_group_member('data-platform-admins') THEN email
         WHEN email IS NULL THEN NULL
         -- Keep the domain so channel/acquisition analysis still works.
         ELSE concat('***@', split(email, '@')[1])
       END;

ALTER TABLE gold.dim_customer
  ALTER COLUMN email SET MASK gold.mask_email;

-- Self-excluded customers should not appear in marketing extracts at all. A row
-- filter enforces that centrally rather than relying on every analyst to add a
-- WHERE clause.
CREATE OR REPLACE FUNCTION gold.rg_row_filter(is_self_excluded BOOLEAN)
RETURN is_account_group_member('data-platform-admins')
       OR is_account_group_member('responsible-gambling')
       OR NOT coalesce(is_self_excluded, false);

ALTER TABLE gold.dim_customer
  SET ROW FILTER gold.rg_row_filter ON (is_self_excluded);

-- ---------------------------------------------------------------------------
-- Table comments. Worth the keystrokes: they show up in Catalog Explorer, in
-- autocomplete, and in the AI assistant's context, which is where an analyst
-- actually looks.
-- ---------------------------------------------------------------------------
COMMENT ON TABLE gold.fact_bet_leg IS
  'One row per leg of a bet. Use stake_allocated (not bet_stake_amount) when summing
   turnover here - bet_stake_amount repeats across the legs of a multi.';

COMMENT ON TABLE gold.fact_bet_settlement IS
  'One row per bet slip. The authoritative source for turnover, payouts and hold %.';

COMMENT ON TABLE gold.dim_customer IS
  'Type 2 customer dimension. Join facts on customer_sk for as-at-bet-time attributes,
   or on customer_dk with is_current for the customer as they are today.';
