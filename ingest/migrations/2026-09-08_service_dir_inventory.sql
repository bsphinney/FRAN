-- One row per project FOLDER on the service share.
--
-- Deliberately NOT part of delimp_submission_service_dir, which is one row per SUBMISSION: 285 of
-- its service_folder values repeat, one across eleven submissions, because a lab sends several
-- submissions whose work lands in one folder. Keying that table on the folder is impossible, and
-- storing folder facts in it would repeat run_count once per mapped submission.
--
-- run_count NULL means "could not be read", which must stay distinguishable from 0 = genuinely
-- empty. That is why count_runs() returns int | None.
CREATE TABLE IF NOT EXISTS delimp_service_dir_inventory (
    service_folder     TEXT PRIMARY KEY,
    service_folder_win TEXT,
    campus             TEXT,
    run_count          INTEGER,
    in_fran            BOOLEAN,
    scanned_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_service_dir_inv_in_fran
  ON delimp_service_dir_inventory (in_fran);
