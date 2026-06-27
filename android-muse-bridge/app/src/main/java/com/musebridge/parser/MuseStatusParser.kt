package com.musebridge.parser

import org.json.JSONObject

/**
 * Parse Muse Athena battery / status data.
 *
 * Sources (OpenMuse, muse-rs, amused-py):
 * - Control 273e0001: length-prefixed JSON fragments → reassemble → field ``bp``
 * - Sensor 0x88 / 0x98: payload bytes 0–1 = u16 LE ÷ 256 → percent
 */
object MuseStatusParser {

    private val controlAccumulator = ControlJsonAccumulator()

    /** Reset fragment buffer (call on new BLE connection). */
    fun resetControlAccumulator() {
        controlAccumulator.reset()
    }

    /**
     * Decode one control-channel notification (muse-rs ``decode_response`` +
     * ``ControlAccumulator``).
     */
    fun feedControlNotification(data: ByteArray): Float? {
        val fragment = decodeControlFragment(data)
        if (fragment.isEmpty()) return null
        val jsonText = controlAccumulator.push(fragment) ?: return null
        return parseBatteryFromJsonText(jsonText)
    }

    /** Strip length prefix: byte[0]=len, bytes[1..len]=UTF-8 fragment. */
    fun decodeControlFragment(data: ByteArray): String {
        if (data.isEmpty()) return ""
        val len = data[0].toInt() and 0xFF
        val end = (1 + len).coerceAtMost(data.size)
        if (end <= 1) return ""
        return data.copyOfRange(1, end).toString(Charsets.UTF_8)
    }

    fun parseBatteryFromJsonText(text: String): Float? {
        return try {
            val jsonStart = text.indexOf('{')
            val jsonEnd = text.lastIndexOf('}')
            if (jsonStart < 0 || jsonEnd <= jsonStart) return null
            parseBatteryFromJson(JSONObject(text.substring(jsonStart, jsonEnd + 1)))
        } catch (_: Exception) {
            null
        }
    }

    /** Legacy fallback when JSON is already complete in one buffer. */
    fun parseBatteryPercent(data: ByteArray): Float? {
        if (data.isEmpty()) return null
        return parseBatteryFromJsonText(data.toString(Charsets.UTF_8))
    }

    fun parseBatteryFromJson(json: JSONObject): Float? {
        return when {
            json.has("bp") -> json.optDouble("bp").toFloat()
            json.has("battery") -> json.optDouble("battery").toFloat()
            json.has("bl") -> json.optDouble("bl").toFloat()
            else -> null
        }?.coerceIn(0f, 100f)
    }

    /**
     * Binary SOC at [offset..offset+1], u16 LE ÷ 256 (OpenMuse ``_decode_battery_data``).
     */
    fun parseBatteryBinary(data: ByteArray, offset: Int = 0): Float? {
        if (offset + 2 > data.size) return null
        val rawSoc = (data[offset].toInt() and 0xFF) or ((data[offset + 1].toInt() and 0xFF) shl 8)
        return (rawSoc / 256.0f).coerceIn(0f, 100f)
    }

    /**
     * Scan sensor payload (273e0013) for 0x88 / 0x98 tags.
     * Supports multiple length-prefixed packets per notification (OpenMuse).
     */
    fun parseBatteryFromSensorPayload(payload: ByteArray): Float? {
        if (payload.size < 14) return null
        var offset = 0
        var parsedAnyPacket = false
        while (offset < payload.size) {
            if (offset + 14 > payload.size) break
            val pktLen = payload[offset].toInt() and 0xFF
            if (pktLen < 14 || offset + pktLen > payload.size) {
                parseBatteryInPacket(payload, offset, payload.size)?.let { return it }
                break
            }
            parsedAnyPacket = true
            parseBatteryInPacket(payload, offset, offset + pktLen)?.let { return it }
            offset += pktLen
        }
        if (!parsedAnyPacket) {
            return parseBatteryInPacket(payload, 0, payload.size)
        }
        return null
    }

    /** Walk tags inside one Athena packet (muse-rs ``parse_athena_notification``). */
    private fun parseBatteryInPacket(data: ByteArray, start: Int, end: Int): Float? {
        if (end - start < 14) return null
        val pktLen = ((data[start].toInt() and 0xFF) + start).coerceAtMost(end)
        var idx = start + 9
        while (idx + 5 <= pktLen && idx + 5 <= end) {
            when (data[idx].toInt() and 0xFF) {
                0x88 -> return parseBatteryBinary(data, idx + 5)
                0x98 -> {
                    parseBatteryBinary(data, idx + 5)?.let { return it }
                    idx += 5 + 20
                }
                0x11, 0x12 -> idx += 5 + 28
                0x34 -> idx += 5 + 30
                0x35, 0x36 -> idx += 5 + 40
                0x47 -> idx += 5 + 36
                0x53 -> idx += 5 + 24
                else -> idx++
            }
        }
        return null
    }

    /**
     * Reassemble control JSON split across BLE notifications (muse-rs ControlAccumulator).
     */
    class ControlJsonAccumulator {
        private val buffer = StringBuilder()
        private var depth = 0

        fun reset() {
            buffer.clear()
            depth = 0
        }

        fun push(fragment: String): String? {
            for (ch in fragment) {
                when (ch) {
                    '{' -> {
                        if (depth == 0) buffer.clear()
                        depth++
                        buffer.append(ch)
                    }
                    '}' -> {
                        if (depth > 0) {
                            buffer.append(ch)
                            depth--
                            if (depth == 0) {
                                val json = buffer.toString()
                                buffer.clear()
                                return json
                            }
                        }
                    }
                    else -> {
                        if (depth > 0) buffer.append(ch)
                    }
                }
            }
            return null
        }
    }
}
