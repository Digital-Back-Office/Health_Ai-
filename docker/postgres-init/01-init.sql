-- Runs once, when the data volume is first created.
-- btree_gist lets the appointments table use an exclusion constraint that
-- combines "same clinician" (=) with "overlapping time range" (&&).
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE DATABASE healthcare_test OWNER healthcare;
\connect healthcare_test
CREATE EXTENSION IF NOT EXISTS btree_gist;
