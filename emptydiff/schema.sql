-- Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
-- Created in part with AI help (Claude, Anthropic).
-- SPDX-License-Identifier: GPL-3.0-or-later

-- Schema version 1 (see DESIGN.md, section 9)

CREATE TABLE lab (
  name TEXT PRIMARY KEY,                   -- e.g. 'am26-1a'
  course TEXT NOT NULL,                    -- prefix before '-', e.g. 'am26'
  description TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',           -- optional note in the report
  due_at TEXT NOT NULL,                    -- UTC, ISO 8601
  max_attempts INTEGER NOT NULL DEFAULT 7,
  required_files TEXT NOT NULL,            -- one file per line
  show_report INTEGER NOT NULL DEFAULT 1,  -- 0 = receipt confirmation only
  testbench TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE lab_file (                    -- provided files
  lab TEXT NOT NULL REFERENCES lab(name) ON DELETE CASCADE,
  name TEXT NOT NULL,
  content BLOB NOT NULL,
  PRIMARY KEY (lab, name)
);

CREATE TABLE submission (
  id INTEGER PRIMARY KEY,
  lab TEXT NOT NULL REFERENCES lab(name),
  email TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  late INTEGER NOT NULL DEFAULT 0,
  complete INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | error
  passed INTEGER,                          -- NULL until finished
  log TEXT,
  error_log TEXT,
  UNIQUE (lab, email, attempt)
);
CREATE INDEX submission_status ON submission(status);

CREATE TABLE login_code (
  email TEXT NOT NULL,
  code_hash TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  ip TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX login_code_email ON login_code(email, created_at);
