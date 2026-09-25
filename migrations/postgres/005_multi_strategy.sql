BEGIN;
ALTER TABLE banxia.strategy_definition
  ADD COLUMN IF NOT EXISTS current_config jsonb,
  ADD COLUMN IF NOT EXISTS enabled boolean NOT NULL DEFAULT true,
  ADD COLUMN IF NOT EXISTS archived boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS banxia.trading_session (
  trade_date date PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS banxia.strategy_day (
  strategy_id uuid NOT NULL REFERENCES banxia.strategy_definition(strategy_id),
  trade_date date NOT NULL,
  next_plan jsonb,
  execution_plan jsonb,
  actuals jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (strategy_id, trade_date)
);
CREATE INDEX IF NOT EXISTS strategy_day_date ON banxia.strategy_day(trade_date DESC);
COMMIT;
