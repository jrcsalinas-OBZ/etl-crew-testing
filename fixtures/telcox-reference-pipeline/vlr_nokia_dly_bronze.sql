-- vlr_nokia_dly_bronze.sql  (FROM = last segment of target table_name)
SELECT
  to_date(file_date)        AS txn_date,
  msisdn,
  CAST(imei AS STRING)      AS imei,
  CAST(imsi AS STRING)      AS imsi,
  'NSN'                     AS vendor,
  split(file_name, '_')[0]  AS switch_name,
  file_date, file_name, now() AS dbx_process_dttm, file_id
FROM vlr_dly_nokia;
