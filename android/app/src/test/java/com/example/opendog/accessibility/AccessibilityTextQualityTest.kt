package com.example.opendog.accessibility

import com.example.opendog.event.PageSnapshot
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class AccessibilityTextQualityTest {
    @Test
    fun sufficientTextUsesAccessibility() {
        val result = AccessibilityTextQuality.assess(
            snapshot(
                text = "微信\n通讯录\n发现\n朋友圈动态内容",
                textNodeCount = 4,
                visibleNodeCount = 8
            )
        )

        assertTrue(result.sufficient)
    }

    @Test
    fun emptyAccessibilityTreeNeedsOcr() {
        val result = AccessibilityTextQuality.assess(snapshot())

        assertFalse(result.sufficient)
    }

    @Test
    fun genericButtonsDoNotCountAsMeaningfulText() {
        val result = AccessibilityTextQuality.assess(
            snapshot(
                text = "返回\n更多\n确定",
                textNodeCount = 3,
                visibleNodeCount = 6
            )
        )

        assertFalse(result.sufficient)
    }

    @Test
    fun surfaceWithLittleTextNeedsOcr() {
        val result = AccessibilityTextQuality.assess(
            snapshot(
                text = "菜单",
                textNodeCount = 1,
                visibleNodeCount = 3,
                hasSurfaceContent = true
            )
        )

        assertFalse(result.sufficient)
    }

    private fun snapshot(
        text: String = "",
        textNodeCount: Int = 0,
        visibleNodeCount: Int = 0,
        hasSurfaceContent: Boolean = false
    ): PageSnapshot {
        return PageSnapshot(
            packageName = "com.example.target",
            className = "TargetActivity",
            windowId = 1,
            title = "",
            localText = text,
            visibleNodeCount = visibleNodeCount,
            textNodeCount = textNodeCount,
            hasSurfaceContent = hasSurfaceContent
        )
    }
}
