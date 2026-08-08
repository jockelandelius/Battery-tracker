CREATE TABLE IF NOT EXISTS battery_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS battery_type_fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type_id INTEGER NOT NULL REFERENCES battery_types(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    field_key TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    UNIQUE(type_id, field_key)
);

CREATE TABLE IF NOT EXISTS batteries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type_id INTEGER NOT NULL REFERENCES battery_types(id) ON DELETE RESTRICT,
    identifier TEXT NOT NULL UNIQUE,
    brand TEXT NOT NULL,
    chemistry TEXT NOT NULL,
    voltage REAL NOT NULL,
    country TEXT,
    introduced_month TEXT NOT NULL,
    nominal_capacity_mah REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('Aktiv', 'Väntande', 'Ej aktivt')),
    custom_values TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS charges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    battery_id INTEGER NOT NULL REFERENCES batteries(id) ON DELETE CASCADE,
    charged_on TEXT NOT NULL,
    capacity_mah REAL NOT NULL CHECK(capacity_mah >= 0),
    mode TEXT NOT NULL CHECK(mode IN ('Activate', 'Charge', 'Analysis')),
    current_a REAL CHECK(current_a IS NULL OR (current_a >= 0.1 AND current_a <= 2.0)),
    comment TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_batteries_type ON batteries(type_id);
CREATE INDEX IF NOT EXISTS idx_charges_battery_date ON charges(battery_id, charged_on DESC);
