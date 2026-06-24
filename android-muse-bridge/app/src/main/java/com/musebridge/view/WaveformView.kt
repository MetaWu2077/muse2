package com.musebridge.view

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.util.AttributeSet
import android.view.View

/**
 * A simple EKG-style scrolling waveform view.
 */
class WaveformView @JvmOverloads constructor(
    context: Context, attrs: AttributeSet? = null, defStyleAttr: Int = 0
) : View(context, attrs, defStyleAttr) {

    private val paint = Paint().apply {
        color = Color.parseColor("#2DD4BF") // zen_accent
        style = Paint.Style.STROKE
        strokeWidth = 4f
        isAntiAlias = true
        strokeJoin = Paint.Join.ROUND
        strokeCap = Paint.Cap.ROUND
    }

    private val path = Path()
    private val dataPoints = mutableListOf<Float>()
    private val maxPoints = 100 // Covers roughly 3-5 seconds depending on update rate

    /**
     * Add a new sample to the waveform.
     * @param value A value typically between 0 and 1 representing amplitude.
     */
    fun addSample(value: Float) {
        dataPoints.add(value)
        if (dataPoints.size > maxPoints) {
            dataPoints.removeAt(0)
        }
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        if (dataPoints.isEmpty()) return

        val w = width.toFloat()
        val h = height.toFloat()
        val step = w / (maxPoints - 1)

        path.reset()
        
        // Draw baseline in middle
        val centerY = h / 2f
        
        for (i in dataPoints.indices) {
            val x = i * step
            // Map 0..1 to a vertical displacement. We want some "bounce" around center.
            // value 0 -> centerY, value 1 -> near top/bottom
            val amplitude = dataPoints[i] * (h / 2.5f)
            // Create some fake EKG oscillation if the value is > 0
            val offset = if (i % 4 == 0) amplitude else if (i % 4 == 2) -amplitude else 0f
            
            val y = centerY + offset

            if (i == 0) {
                path.moveTo(x, y)
            } else {
                path.lineTo(x, y)
            }
        }
        
        canvas.drawPath(path, paint)
    }
}
