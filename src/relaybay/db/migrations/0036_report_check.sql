-- The `verified: <where>` marks of a report, parsed when the report is written (MCP report_add
-- and a run's turn alike): the 1-based line of the body, the `where` as written and the claim
-- line it stands on.
CREATE TABLE report_check (
    id        INTEGER PRIMARY KEY,
    report_id INTEGER NOT NULL REFERENCES report(id) ON DELETE CASCADE,
    line      INTEGER NOT NULL,
    location  TEXT NOT NULL,
    claim     TEXT NOT NULL
);
CREATE INDEX report_check_report ON report_check(report_id, line);
