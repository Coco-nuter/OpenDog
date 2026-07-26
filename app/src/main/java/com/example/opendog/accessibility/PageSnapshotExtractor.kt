package com.example.opendog.accessibility

import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import com.example.opendog.event.PageSnapshot

class PageSnapshotExtractor {
    fun extract(
        event: AccessibilityEvent?,
        root: AccessibilityNodeInfo?,
        includeText: Boolean = false
    ): PageSnapshot? {
        val packageName = firstNotBlank(
            event?.packageName?.toString(),
            root?.packageName?.toString()
        ) ?: return null
        val className = firstNotBlank(
            event?.className?.toString(),
            root?.className?.toString(),
            "UnknownPage"
        ) ?: "UnknownPage"
        val title = root?.let { findFirstText(it) }.orEmpty()
        val localText = if (includeText && root != null) collectPageText(root) else ""
        return PageSnapshot(
            packageName = packageName,
            className = className,
            windowId = root?.windowId,
            title = title,
            localText = localText
        )
    }

    private fun collectPageText(root: AccessibilityNodeInfo): String {
        val values = mutableListOf<String>()
        collectNodeText(root, values)
        return values.joinToString("\n")
    }

    private fun collectNodeText(node: AccessibilityNodeInfo, values: MutableList<String>) {
        if (!node.isVisibleToUser) return

        val text = cleanText(node.text?.toString())
        val description = cleanText(node.contentDescription?.toString())
        if (text.isNotBlank()) values += text
        if (description.isNotBlank() && description != text) values += description

        for (index in 0 until node.childCount) {
            val child = node.getChild(index) ?: continue
            try {
                collectNodeText(child, values)
            } finally {
                child.recycle()
            }
        }
    }

    private fun findFirstText(node: AccessibilityNodeInfo): String? {
        if (!node.isVisibleToUser) return null

        firstNotBlank(
            cleanText(node.text?.toString()),
            cleanText(node.contentDescription?.toString())
        )?.let { return it }

        for (index in 0 until node.childCount) {
            val child = node.getChild(index) ?: continue
            try {
                findFirstText(child)?.let { return it }
            } finally {
                child.recycle()
            }
        }
        return null
    }

    private fun cleanText(value: String?): String {
        return value
            ?.replace(WHITESPACE_REGEX, " ")
            ?.trim()
            .orEmpty()
    }

    private fun firstNotBlank(vararg values: String?): String? {
        return values.firstOrNull { !it.isNullOrBlank() }?.trim()
    }

    companion object {
        private val WHITESPACE_REGEX = Regex("\\s+")
    }
}
