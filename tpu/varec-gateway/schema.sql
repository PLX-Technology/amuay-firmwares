-- Esquema de la pasarela Varec. Vive en el SSD M.2, no en la eMMC.
--
-- Historico largo (años) con >30 tanques: guardar cada muestra cruda para
-- siempre no es viable ni util. Se usan TRES CAPAS con retenciones distintas,
-- y un proceso de agregacion las va consolidando:
--
--   samples_raw  -> cada muestra          -> retencion dias
--   samples_1m   -> media por minuto      -> retencion meses
--   samples_1h   -> media por hora        -> retencion años
--
-- A 30s por muestra y 30 tanques: 86.400 filas crudas/dia (~3 MB/dia).
-- Consolidado a 1h quedan 720 filas/dia (~30 KB/dia) = ~11 MB/año. Manejable.

PRAGMA journal_mode = WAL;      -- concurrencia: los lectores no bloquean al escritor
PRAGMA synchronous = NORMAL;    -- con WAL es seguro y reduce el desgaste del SSD

-- ---------------------------------------------------------------- tanques
-- El tank_id lo lleva la propia ATT en su EEPROM y viaja en cada trama:
-- la placa lleva su identidad consigo. Se cambia una ATT averiada, se le
-- escribe el tank_id por Modbus y la pasarela no se entera.
-- La MAC se guarda solo para diagnostico y para detectar duplicados.
CREATE TABLE IF NOT EXISTS tanks (
    tank_id     INTEGER PRIMARY KEY,
    name        TEXT,                  -- etiqueta legible, opcional
    mac         TEXT,                  -- ultima MAC vista con este tank_id
    scale       REAL DEFAULT 1.0,      -- pulsos del encoder -> unidad de ingenieria
    offset      REAL DEFAULT 0.0,
    unit        TEXT DEFAULT 'mm',
    first_seen  INTEGER,
    last_seen   INTEGER,
    enabled     INTEGER DEFAULT 1
);

-- Dos placas con el mismo tank_id es un error de campo que hay que GRITAR,
-- no silenciar: si la MAC cambia para un tank_id ya visto, se registra aqui.
CREATE TABLE IF NOT EXISTS id_conflicts (
    ts       INTEGER,
    tank_id  INTEGER,
    mac_old  TEXT,
    mac_new  TEXT
);

-- ---------------------------------------------------------------- muestras
CREATE TABLE IF NOT EXISTS samples_raw (
    ts        INTEGER NOT NULL,        -- epoch en segundos
    tank_id   INTEGER NOT NULL,
    count     INTEGER,                 -- posicion del encoder (crudo)
    value     REAL,                    -- count * scale + offset
    edges     INTEGER,
    errors    INTEGER,
    uptime_s  INTEGER,
    rssi_ok   INTEGER DEFAULT 1,
    PRIMARY KEY (tank_id, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS samples_1m (
    ts        INTEGER NOT NULL,        -- inicio del minuto
    tank_id   INTEGER NOT NULL,
    value_avg REAL, value_min REAL, value_max REAL,
    n         INTEGER,
    errors    INTEGER,
    PRIMARY KEY (tank_id, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS samples_1h (
    ts        INTEGER NOT NULL,        -- inicio de la hora
    tank_id   INTEGER NOT NULL,
    value_avg REAL, value_min REAL, value_max REAL,
    n         INTEGER,
    errors    INTEGER,
    PRIMARY KEY (tank_id, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_raw_ts ON samples_raw(ts);
CREATE INDEX IF NOT EXISTS idx_1m_ts  ON samples_1m(ts);
CREATE INDEX IF NOT EXISTS idx_1h_ts  ON samples_1h(ts);

-- ---------------------------------------------------------------- eventos
-- Un tanque que deja de reportar es informacion operativa, no ruido.
CREATE TABLE IF NOT EXISTS events (
    ts       INTEGER,
    tank_id  INTEGER,
    kind     TEXT,                     -- 'online' | 'offline' | 'error' | 'conflict'
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_ev_ts ON events(ts);
