package com.example.opendog.accessibility

import com.example.opendog.event.PageSnapshot

data class AccessibilityTextAssessment(
    val sufficient: Boolean,
    val effectiveCharacterCount: Int,
    val reason: String
)

object AccessibilityTextQuality {
    fun assess(snapshot: PageSnapshot): AccessibilityTextAssessment {
        val meaningfulLines = snapshot.localText
            .lineSequence()
            .map { it.trim() }
            .filter { it.isNotBlank() }
            .distinct()
            .filterNot { line -> normalize(line) in GENERIC_LABELS }
            .toList()
        val effectiveCharacterCount = meaningfulLines
            .sumOf { line -> line.count { it.isLetterOrDigit() } }

        val reason = when {
            snapshot.visibleNodeCount == 0 -> "no_visible_nodes"
            snapshot.textNodeCount == 0 -> "no_text_nodes"
            snapshot.hasSurfaceContent && effectiveCharacterCount < MIN_EFFECTIVE_CHARACTERS ->
                "surface_without_meaningful_text"
            snapshot.textNodeCount < MIN_TEXT_NODES -> "too_few_text_nodes"
            effectiveCharacterCount < MIN_EFFECTIVE_CHARACTERS -> "too_few_characters"
            else -> "sufficient"
        }
        return AccessibilityTextAssessment(
            sufficient = reason == "sufficient",
            effectiveCharacterCount = effectiveCharacterCount,
            reason = reason
        )
    }

    private fun normalize(value: String): String {
        return value.lowercase().filter { it.isLetterOrDigit() }
    }

    private const val MIN_TEXT_NODES = 2
    private const val MIN_EFFECTIVE_CHARACTERS = 10

    private val GENERIC_LABELS = setOf(
        "返回",
        "更多",
        "确定",
        "取消",
        "关闭",
        "back",
        "more",
        "ok",
        "cancel",
        "close"
    )
}
