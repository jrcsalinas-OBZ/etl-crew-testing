-- Onboarding DDL (run by the onboarding job, never by the pipeline notebook)
CREATE TABLE IF NOT EXISTS tx.rt_b.vlr_dly_nokia (
  txn_date          DATE,
  msisdn            STRING,
  imei              STRING,
  imsi              STRING,
  vendor            STRING,
  switch_name       STRING,
  file_date         STRING,
  file_name         STRING,
  dbx_process_dttm  TIMESTAMP,
  file_id           BIGINT
)
USING DELTA
PARTITIONED BY (txn_date);
