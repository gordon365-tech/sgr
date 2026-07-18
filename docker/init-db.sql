-- SGR Database Initialization
-- Wird beim ersten Start von Docker Compose ausgeführt.
-- TimescaleDB Extension wird hier aktiviert.
-- Tabellen werden von SQLAlchemy/Alembic erstellt.

-- TimescaleDB aktivieren
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- Performance Einstellungen für Trading Workload
ALTER SYSTEM SET shared_preload_libraries = 'timescaledb';
ALTER SYSTEM SET max_connections = 200;
ALTER SYSTEM SET shared_buffers = '256MB';
ALTER SYSTEM SET effective_cache_size = '1GB';
ALTER SYSTEM SET work_mem = '16MB';
ALTER SYSTEM SET maintenance_work_mem = '128MB';

-- Zeitzone auf UTC setzen (kritisch für Candle-Timestamps)
SET timezone = 'UTC';
ALTER DATABASE sgr SET timezone = 'UTC';

-- Hilfsfunktion: Candle-Gap-Detection auf DB-Ebene (optional, für Monitoring)
CREATE OR REPLACE FUNCTION check_candle_completeness(
    p_symbol TEXT,
    p_timeframe TEXT,
    p_from TIMESTAMPTZ,
    p_to TIMESTAMPTZ,
    p_interval INTERVAL
)
RETURNS TABLE (
    gap_start TIMESTAMPTZ,
    gap_end TIMESTAMPTZ,
    missing_bars INTEGER
) AS $$
DECLARE
    expected_ts TIMESTAMPTZ := p_from;
BEGIN
    FOR gap_start, gap_end, missing_bars IN
        WITH series AS (
            SELECT generate_series(p_from, p_to, p_interval) AS ts
        ),
        actual AS (
            SELECT timestamp FROM candles
            WHERE symbol = p_symbol
              AND timeframe = p_timeframe
              AND timestamp BETWEEN p_from AND p_to
        )
        SELECT
            s.ts AS gap_start,
            s.ts + p_interval AS gap_end,
            1 AS missing_bars
        FROM series s
        LEFT JOIN actual a ON a.timestamp = s.ts
        WHERE a.timestamp IS NULL
    LOOP
        RETURN NEXT;
    END LOOP;
END;
$$ LANGUAGE plpgsql;

SELECT 'SGR database initialized successfully.' AS status;
