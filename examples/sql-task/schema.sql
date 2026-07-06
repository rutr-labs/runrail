CREATE TABLE IF NOT EXISTS runrail_demo (id INTEGER PRIMARY KEY, created_at TEXT);
INSERT INTO runrail_demo (created_at) VALUES ('{{ ds }}');

