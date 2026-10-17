CREATE TABLE IF NOT EXISTS trades_raw_json
(
    received_at DateTime64(3, 'UTC') DEFAULT now64(3) CODEC(Delta(8), ZSTD(1)),
    source LowCardinality(String) DEFAULT 'bks',

    ticker LowCardinality(String),
    classCode LowCardinality(String),
    event_time DateTime64(3, 'UTC') CODEC(Delta(8), ZSTD(1)),

    raw_json String CODEC(ZSTD(1))
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(received_at)
PRIMARY KEY (ticker, classCode, event_time)
ORDER BY (ticker, classCode, event_time, received_at);
