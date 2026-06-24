-- Muse Cloud Server - Database Schema
-- Run this to initialize the MySQL database.

CREATE DATABASE IF NOT EXISTS muse_cloud
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE muse_cloud;

-- Sessions table: one row per streaming session
CREATE TABLE IF NOT EXISTS sessions (
    session_id CHAR(12) PRIMARY KEY,
    device_address VARCHAR(17) NOT NULL DEFAULT '',
    device_name VARCHAR(64) NOT NULL DEFAULT '',
    preset VARCHAR(16) NOT NULL DEFAULT 'p1034',
    started_at DATETIME(3) NOT NULL,
    ended_at DATETIME(3) DEFAULT NULL,
    raw_file_path VARCHAR(512) DEFAULT '',
    raw_file_size_bytes BIGINT DEFAULT 0,
    total_packets INT DEFAULT 0,
    firmware_version VARCHAR(16) DEFAULT '',
    battery_start_pct INT DEFAULT NULL,
    tags JSON DEFAULT NULL,
    INDEX idx_started_at (started_at),
    INDEX idx_device_address (device_address)
) ENGINE=InnoDB;

-- Heart rate samples: ~1 sample per second
CREATE TABLE IF NOT EXISTS heart_rate_samples (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    session_id CHAR(12) NOT NULL,
    time DATETIME(3) NOT NULL,
    bpm REAL NOT NULL,
    INDEX idx_session_time (session_id, time),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- EEG band power: computed every ~250ms (4 Hz) per channel
CREATE TABLE IF NOT EXISTS eeg_band_power (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    session_id CHAR(12) NOT NULL,
    time DATETIME(3) NOT NULL,
    channel VARCHAR(8) NOT NULL,
    delta REAL DEFAULT 0,
    theta REAL DEFAULT 0,
    alpha REAL DEFAULT 0,
    beta REAL DEFAULT 0,
    gamma REAL DEFAULT 0,
    INDEX idx_session_channel_time (session_id, channel, time),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- Session statistics table: live-updated counters
CREATE TABLE IF NOT EXISTS session_stats (
    session_id CHAR(12) PRIMARY KEY,
    eeg_packets INT DEFAULT 0,
    ppg_packets INT DEFAULT 0,
    imu_packets INT DEFAULT 0,
    decode_errors INT DEFAULT 0,
    updated_at DATETIME DEFAULT NOW(),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
) ENGINE=InnoDB;
