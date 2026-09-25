BEGIN;

CREATE TABLE IF NOT EXISTS banxia.execution_comparison (
  comparison_id uuid PRIMARY KEY,
  start_date date NOT NULL,
  end_date date NOT NULL CHECK (end_date >= start_date),
  generated_at timestamptz NOT NULL,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS execution_comparison_generated
  ON banxia.execution_comparison(generated_at DESC);

COMMIT;
