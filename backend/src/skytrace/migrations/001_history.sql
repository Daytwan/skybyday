CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS __SCHEMA__;

CREATE TABLE IF NOT EXISTS __SCHEMA__.aircraft_positions (
    source text NOT NULL CHECK (source IN ('synthetic', 'adsblol')),
    icao24 text NOT NULL CHECK (icao24 ~ '^[0-9a-f]{6}$'),
    position_observed_at timestamptz NOT NULL,
    provider_timestamp timestamptz NOT NULL,
    received_at timestamptz NOT NULL,
    latitude_deg double precision NOT NULL CHECK (latitude_deg BETWEEN -90 AND 90),
    longitude_deg double precision NOT NULL CHECK (longitude_deg BETWEEN -180 AND 180),
    geom geography(Point, 4326) NOT NULL,
    observation jsonb NOT NULL,
    attribution jsonb NOT NULL,
    PRIMARY KEY (source, icao24, position_observed_at)
);

-- The primary key already indexes source, aircraft and observation time.
CREATE INDEX IF NOT EXISTS aircraft_positions_time_idx
    ON __SCHEMA__.aircraft_positions (position_observed_at);
CREATE INDEX IF NOT EXISTS aircraft_positions_geom_gist_idx
    ON __SCHEMA__.aircraft_positions USING gist (geom);
