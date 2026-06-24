package com.musebridge.parser

/**
 * Muse S Athena (Gen 3) data decoder — based on amused-py protocol.
 *
 * All sensor data arrives on characteristic 273e0013 as TAG-based subpackets.
 *
 * Packet structure (from muse_athena_protocol.py parse_payload):
 *   Bytes 0-13:   14-byte header (byte 9 = TAG of first subpacket)
 *   Bytes 14+:    First subpacket data (no TAG prefix)
 *   After:        [TAG(1)][header(4)][data(N)] for each additional subpacket
 *
 * Subpacket TAGs:
 *   0x11 — EEG 4ch:  28 bytes → 4 samples × 4 channels, 14-bit LSB
 *   0x47 — ACCGYRO:   36 bytes → 3 samples × 6 axes, 16-bit LE
 *   0x34 — Optics 4ch: 30 bytes → 3 samples × 4 channels, 20-bit LSB
 *   0x35 — Optics 8ch: 40 bytes → 2 samples × 8 channels, 20-bit LSB
 */

object DataDecoder {
    private const val HEADER_SIZE = 14

    // Sensor config per TAG: name, nChannels, nSamples, dataLen
    data class SensorConfig(val name: String, val nChannels: Int, val nSamples: Int, val dataLen: Int)
    private val SENSOR_CONFIG = mapOf(
        0x11 to SensorConfig("EEG", 4, 4, 28),
        0x12 to SensorConfig("EEG", 8, 2, 28),
        0x34 to SensorConfig("OPTICS", 4, 3, 30),
        0x35 to SensorConfig("OPTICS", 8, 2, 40),
        0x36 to SensorConfig("OPTICS", 16, 1, 40),
        0x47 to SensorConfig("ACCGYRO", 6, 3, 36),
        0x88 to SensorConfig("BATTERY", 1, 1, 188),
        0x98 to SensorConfig("BATTERY", 1, 1, 20)
    )

    // Scales from amused-py. EEG is unsigned 14-bit (0-16383), midpoint ~8192 = 0 µV.
    private const val EEG_SCALE = 1450.0 / 16383.0  // 14-bit → µV
    private const val EEG_OFFSET = 8192.0 * EEG_SCALE // ~725.37 µV DC offset
    const val ACC_SCALE = 0.0000610352f
    const val GYRO_SCALE = -0.0074768f

    val EEG_CHANNELS_4 = arrayOf("TP9", "AF7", "AF8", "TP10")

    data class DecodedPacket(
        val eeg: FloatArray? = null,      // (nSamples * nChannels) flat, row-major
        val eegChannels: Int = 0,
        val eegSamples: Int = 0,
        val accel: FloatArray? = null,     // [x, y, z] averaged
        val gyro: FloatArray? = null,      // [x, y, z] averaged
        val ppg: FloatArray? = null,       // (nSamples * nChannels) flat
        val ppgChannels: Int = 0,
        val ppgSamples: Int = 0,
        val hasEeg: Boolean = false,
        val hasAccGyro: Boolean = false,
        val hasPpg: Boolean = false
    )

    /**
     * Parse a complete 273e0013 notification payload.
     * Returns a list of DecodedPacket (typically 1-3 subpackets per notification).
     */
    fun parsePayload(payload: ByteArray): List<DecodedPacket> {
        val results = mutableListOf<DecodedPacket>()
        if (payload.size < HEADER_SIZE + 1) return results

        // First subpacket: TAG at header byte 9, data starts at offset 14
        val firstTag = payload[9].toInt() and 0xFF
        var config = SENSOR_CONFIG[firstTag] ?: return results
        val dataEnd = HEADER_SIZE + config.dataLen
        if (dataEnd > payload.size) return results

        val firstData = payload.copyOfRange(HEADER_SIZE, dataEnd)
        decodeSubpacket(firstTag, firstData)?.let { results.add(it) }
        var offset = dataEnd

        // Subsequent subpackets: [TAG(1)][header(4)][data(N)]
        while (offset + 5 < payload.size) {
            val tag = payload[offset].toInt() and 0xFF
            config = SENSOR_CONFIG[tag] ?: break
            val start = offset + 5
            val end = start + config.dataLen
            if (end > payload.size) break
            decodeSubpacket(tag, payload.copyOfRange(start, end))?.let { results.add(it) }
            offset = end
        }
        return results
    }

    // Optics scale: 20-bit → normalized
    private const val OPTICS_SCALE = 1.0 / 32768.0

    private fun decodeSubpacket(tag: Int, data: ByteArray): DecodedPacket? {
        return when (tag) {
            0x11 -> decodeEeg(data, 4)
            0x12 -> decodeEeg(data, 8)
            0x47 -> decodeAccGyro(data)
            0x34 -> decodeOptics(data, 4, 3)  // 4ch × 3 samples
            0x35 -> decodeOptics(data, 8, 2)  // 8ch × 2 samples
            0x36 -> decodeOptics(data, 16, 1) // 16ch × 1 sample
            else -> null
        }
    }

    /**
     * Decode 20-bit LSB-first packed optics/PPG data.
     */
    private fun decodeOptics(data: ByteArray, nChannels: Int, nSamples: Int): DecodedPacket {
        val nValues = nSamples * nChannels
        val nBytes = nValues * 20 / 8  // 20 bits per value
        val bits = unpackBitsLsb(data, 20)
        val rawValues = extractValues(bits, nValues, 20)
        val result = FloatArray(nValues)
        for (i in 0 until nValues) {
            result[i] = (rawValues[i].toFloat() * OPTICS_SCALE).toFloat()
        }
        return DecodedPacket(
            ppg = result, ppgChannels = nChannels, ppgSamples = nSamples, hasPpg = true
        )
    }

    /**
     * Decode 14-bit LSB-first packed EEG data.
     * 4ch: 28 bytes → 4 samples × 4 channels = 16 values
     * 8ch: 28 bytes → 2 samples × 8 channels = 16 values
     */
    private fun decodeEeg(data: ByteArray, nChannels: Int): DecodedPacket {
        val nSamples = if (nChannels == 4) 4 else 2
        val nValues = nSamples * nChannels

        // Unpack bits LSB-first
        val bits = unpackBitsLsb(data, 14)
        val rawValues = extractValues(bits, nValues, 14)

        val result = FloatArray(nValues)
        for (i in 0 until nValues) {
            // Subtract DC offset (midpoint ~725 µV) to center at 0
            result[i] = (rawValues[i].toFloat() * EEG_SCALE - EEG_OFFSET).toFloat()
        }
        return DecodedPacket(
            eeg = result, eegChannels = nChannels, eegSamples = nSamples, hasEeg = true
        )
    }

    /**
     * Decode 16-bit little-endian ACC+GYRO data.
     * 36 bytes → 18 int16 values → (3, 6) matrix
     * Cols 0-2: accelerometer (g), Cols 3-5: gyroscope (deg/s)
     */
    private fun decodeAccGyro(data: ByteArray): DecodedPacket {
        // 3 samples × 6 channels, each 2 bytes LE
        val accSum = FloatArray(3)  // x, y, z
        val gyroSum = FloatArray(3)
        for (s in 0 until 3) {
            for (c in 0 until 6) {
                val idx = (s * 6 + c) * 2
                if (idx + 1 >= data.size) break
                val raw = ((data[idx + 1].toInt() shl 8) or (data[idx].toInt() and 0xFF)).toShort().toInt()
                if (c < 3) accSum[c] += raw * ACC_SCALE
                else gyroSum[c - 3] += raw * GYRO_SCALE
            }
        }
        for (i in 0..2) { accSum[i] /= 3f; gyroSum[i] /= 3f }
        return DecodedPacket(accel = accSum, gyro = gyroSum, hasAccGyro = true)
    }

    /** Flatten bytes to bit list (LSB-first within each byte) */
    private fun unpackBitsLsb(data: ByteArray, maxBits: Int): IntArray {
        val totalBits = data.size * 8
        val bits = IntArray(totalBits)
        for (i in data.indices) {
            val byte = data[i].toInt() and 0xFF
            for (b in 0 until 8) {
                bits[i * 8 + b] = (byte shr b) and 1
            }
        }
        return bits
    }

    /** Extract nValues unsigned ints of bitWidth from bit array */
    private fun extractValues(bits: IntArray, nValues: Int, bitWidth: Int): IntArray {
        val values = IntArray(nValues)
        for (i in 0 until nValues) {
            var v = 0
            val start = i * bitWidth
            for (b in 0 until bitWidth) {
                if (start + b < bits.size && bits[start + b] != 0) {
                    v = v or (1 shl b)
                }
            }
            values[i] = v
        }
        return values
    }
}
