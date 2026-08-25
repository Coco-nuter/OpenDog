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
        val pageContent = if (includeText && root != null) collectPageContent(root) else null
        val title = pageContent?.title ?: root?.let { findFirstText(it) }.orEmpty()
        return PageSnapshot(
            packageName = packageName,
            className = className,
            windowId = root?.windowId,
            title = title,
            localText = pageContent?.values?.distinct()?.joinToString("\n").orEmpty(),
            visibleNodeCount = pageContent?.visibleNodeCount ?: 0,
            textNodeCount = pageContent?.textNodeCount ?: 0,
            hasSurfaceContent = pageContent?.hasSurfaceContent ?: false
        )
    }

    private fun collectPageContent(root: AccessibilityNodeInfo): PageContent {
        return PageContent().also { content ->
            collectNodeContent(root, content)
        }
    }

    private fun collectNodeContent(node: AccessibilityNodeInfo, content: PageContent) {
        if (!node.isVisibleToUser) return

        content.visibleNodeCount += 1
        val className = node.className?.toString().orEmpty()
        if (className.contains("SurfaceView") || className.contains("TextureView")) {
            content.hasSurfaceContent = true
        }

        val text = cleanText(node.text?.toString())
        val description = cleanText(node.contentDescription?.toString())
        if (text.isNotBlank() || description.isNotBlank()) {
            content.textNodeCount += 1
        }
        if (text.isNotBlank()) content.values += text
        if (description.isNotBlank() && description != text) content.values += description
        if (content.title.isBlank()) {
            content.title = firstNotBlank(text, description).orEmpty()
        }

        for (index in 0 until node.childCount) {
            val child = node.getChild(index) ?: continue
            try {
                collectNodeContent(child, content)
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

private data class PageContent(
    val values: MutableList<String> = mutableListOf(),
    var title: String = "",
    var visibleNodeCount: Int = 0,
    var textNodeCount: Int = 0,
    var hasSurfaceContent: Boolean = false
)
