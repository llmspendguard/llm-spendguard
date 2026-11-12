PRAGMA foreign_keys = ON;

CREATE TABLE models (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    retired_alias_of TEXT,
    metered_id TEXT,
    price TEXT,
    price_error TEXT,
    output_ceiling TEXT,
    context_window TEXT,
    as_of TEXT,
    provider_base INTEGER,
    reasoning TEXT,
    vision INTEGER,
    capabilities TEXT,
    billing_exempt_reason TEXT,
    present_fields TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK (provenance IN ('authored','sourced','learned','breadth','lane_config'))
);

CREATE TABLE model_aliases (
    model_id TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    alias TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK (provenance IN ('authored','sourced','learned','breadth','lane_config')),
    PRIMARY KEY (model_id, ordinal),
    UNIQUE (model_id, alias)
);

CREATE TABLE model_lane_spellings (
    model_id TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    lane TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    spelling TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK (provenance IN ('authored','sourced','learned','breadth','lane_config')),
    PRIMARY KEY (model_id, lane, ordinal)
);

CREATE TABLE reasoning_equivalence (
    model_id TEXT NOT NULL,
    lane TEXT NOT NULL,
    level TEXT NOT NULL,
    cell TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK (provenance IN ('authored','sourced','learned','breadth','lane_config')),
    PRIMARY KEY (model_id, lane, level)
);

CREATE TABLE catalog_sections (
    section TEXT NOT NULL,
    entry_key TEXT NOT NULL,
    value TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK (provenance IN ('authored','sourced','learned','breadth','lane_config')),
    PRIMARY KEY (section, entry_key)
);
