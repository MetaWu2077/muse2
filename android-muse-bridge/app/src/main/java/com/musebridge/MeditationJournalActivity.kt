package com.musebridge

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.view.View
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.lifecycleScope
import com.musebridge.databinding.ActivityMeditationJournalBinding
import com.musebridge.viewmodel.MainViewModel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

class MeditationJournalActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMeditationJournalBinding
    private lateinit var viewModel: MainViewModel

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMeditationJournalBinding.inflate(layoutInflater)
        setContentView(binding.root)

        viewModel = ViewModelProvider(
            application as MuseApp,
            ViewModelProvider.AndroidViewModelFactory.getInstance(application)
        )[MainViewModel::class.java]

        val sessionId = intent.getStringExtra(EXTRA_SESSION_ID).orEmpty()
        val durationSec = intent.getLongExtra(EXTRA_DURATION_SEC, 0L)
        val deviceName = intent.getStringExtra(EXTRA_DEVICE_NAME).orEmpty()

        val minutes = durationSec / 60
        val seconds = durationSec % 60
        val durationText = String.format("%02d:%02d", minutes, seconds)
        binding.tvSessionInfo.text = buildString {
            if (deviceName.isNotEmpty()) append("$deviceName · ")
            append("时长 $durationText")
            if (sessionId.isNotEmpty()) append("\n会话 $sessionId")
        }

        binding.btnSubmitJournal.setOnClickListener {
            submitJournal(sessionId)
        }
    }

    private fun submitJournal(sessionId: String) {
        val text = binding.etJournal.text.toString().trim()
        if (text.length < 10) {
            Toast.makeText(this, "请至少写几句描述（10 字以上）", Toast.LENGTH_SHORT).show()
            return
        }

        binding.btnSubmitJournal.isEnabled = false
        binding.progressSubmit.visibility = View.VISIBLE

        lifecycleScope.launch {
            val ok = if (sessionId.isNotEmpty()) {
                viewModel.submitSessionJournal(sessionId, text)
            } else {
                viewModel.saveOfflineJournal(text)
                true
            }

            binding.progressSubmit.visibility = View.GONE

            if (ok) {
                showThankYou()
            } else {
                binding.btnSubmitJournal.isEnabled = true
                Toast.makeText(
                    this@MeditationJournalActivity,
                    "提交失败，请检查网络后重试",
                    Toast.LENGTH_LONG
                ).show()
            }
        }
    }

    private suspend fun showThankYou() {
        binding.tvJournalTitle.visibility = View.GONE
        binding.tvJournalSubtitle.visibility = View.GONE
        binding.tvSessionInfo.visibility = View.GONE
        binding.cardJournal.visibility = View.GONE
        binding.btnSubmitJournal.visibility = View.GONE
        binding.llThankYou.visibility = View.VISIBLE
        delay(2200)
        finish()
    }

    companion object {
        private const val EXTRA_SESSION_ID = "session_id"
        private const val EXTRA_DURATION_SEC = "duration_sec"
        private const val EXTRA_DEVICE_NAME = "device_name"

        fun newIntent(
            context: Context,
            sessionId: String,
            durationSeconds: Long,
            deviceName: String
        ): Intent {
            return Intent(context, MeditationJournalActivity::class.java).apply {
                putExtra(EXTRA_SESSION_ID, sessionId)
                putExtra(EXTRA_DURATION_SEC, durationSeconds)
                putExtra(EXTRA_DEVICE_NAME, deviceName)
            }
        }
    }
}
